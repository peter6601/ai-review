"""Behavioural tests for the read-only ``doc`` workflow.

A doc run is one Codex pass whose findings a human reads.  These tests hold the
properties that make that structural rather than conventional: the reviewed
document is never written, Claude is never consulted, no repair round is ever
taken, and the reviewer input stays the documented nine-key trust boundary.
"""

import contextlib
import hashlib
import inspect
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_review.context import BUDGET_METHOD_DOC, SourceRef, build_packet
from ai_review.doc_workflow import DocWorkflow
from ai_review.models import DocManifest, RunState, Status
from ai_review.policy import Policy
from ai_review.store import RunStore, project_id
from ai_review.workflow import WorkflowError


REVIEW_INPUT_KEYS = {
    "document",
    "document_path",
    "lens",
    "brief",
    "decision_log",
    "context_manifest",
    "knowledge_packet",
    "policy",
    "unresolved_prior_findings",
}


def finding(
    identifier, *, severity="major", lineage="newly_discovered",
    reason="first pass over this document",
):
    item = {
        "id": identifier,
        "severity": severity,
        "invariant": "every scenario states its completion condition",
        "location": "docs/rd-spec.md:3",
        "evidence": "the scenario states no observable result",
        "required_outcome": "state what proves the scenario finished",
        "lineage": {"resolution": lineage},
    }
    if lineage == "newly_discovered":
        item["lineage"]["discovery_reason"] = reason
    return item


def review(verdict, *, findings=(), questions=(), context_requests=()):
    return {
        "verdict": verdict,
        "summary": "document review result",
        "findings": list(findings),
        "questions": list(questions),
        "context_requests": list(context_requests),
    }


@contextlib.contextmanager
def record_build_packet():
    """Record the arguments that actually reach ``context.build_packet``.

    The bootstrap's caps are invisible from the outside once a small packet
    fits under any of them, so a literal in place of the policy read passes
    every behavioural assertion.  Binding the real signature captures the
    values whatever call style the caller uses.
    """
    from ai_review import workflow as workflow_module

    real = workflow_module.build_packet
    signature = inspect.signature(real)
    calls = []

    def spy(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        calls.append(dict(bound.arguments))
        return real(*args, **kwargs)

    workflow_module.build_packet = spy
    try:
        yield calls
    finally:
        workflow_module.build_packet = real


class FakeCodex:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def review(self, inputs):
        self.calls.append(inputs)
        if not self.responses:
            raise AssertionError("Codex was called more times than the test expected")
        return self.responses.pop(0)


class TripwireClaude:
    """Any attribute access fails the test: a doc run has no Claude at all."""

    def __init__(self):
        self.touches = []

    def __getattr__(self, name):
        self.touches.append(name)
        raise AssertionError("a doc run must never consult Claude: %s" % name)


class DocWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "docs").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.document = self.repo / "docs" / "rd-spec.md"
        self.document.write_text(
            "# 離線編輯\n\n使用者可以在離線時繼續編輯內容。\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.repo), "-c", "user.email=test@example.com",
            "-c", "user.name=Test", "commit", "-qm", "base",
        ], check=True)
        self.base_oid = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.brief = "離線編輯的 RD spec，給 PM 與 QA 讀"
        self.store = RunStore(self.root / "runs")
        self.run = self.store.create(RunState.new_doc(self.manifest()))
        self.claude = TripwireClaude()
        self.policy = Policy(
            version=1,
            max_rounds=6,
            max_context_tokens=8000,
            max_initial_sources=3,
            max_context_expansions=2,
            production_line_limit=100,
            production_growth_percent=30,
            production_excludes=["docs/**"],
            doc_max_initial_sources=5,
            doc_max_context_tokens=16000,
            codex_model="gpt-5.6-sol",
        )

    # ---- fixtures -------------------------------------------------------

    def manifest(self, **changes):
        values = {
            "kind": "doc",
            "repo_path": str(self.repo),
            "doc_path": str(self.document),
            "base_ref": "HEAD",
            "base_oid": self.base_oid,
            "lens": "requirement",
            "lens_reason": "文件只描述使用者行為，沒有任何檔案路徑",
            "brief": self.brief,
            "brief_digest": hashlib.sha256(self.brief.encode("utf-8")).hexdigest(),
            "doc_digest": hashlib.sha256(self.document.read_bytes()).hexdigest(),
            "knowledge_sources": [],
            "context_checksum": None,
            "review_executables": {},
        }
        return DocManifest(**dict(values, **changes))

    @property
    def artifacts(self):
        return self.store.root / project_id(self.repo) / self.run.run_id

    def workflow(self, codex, **kwargs):
        return DocWorkflow(self.store, codex, self.claude, policy=self.policy, **kwargs)

    def pause(self):
        return json.loads((self.artifacts / "pause.json").read_text(encoding="utf-8"))

    def assertDocumentUnchanged(self, before):
        self.assertEqual(self.document.read_bytes(), before)

    # ---- one read-only pass ---------------------------------------------

    def test_changes_required_ends_the_run_without_repair_or_claude(self):
        before = self.document.read_bytes()
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("F-001")])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(self.claude.touches, [])
        self.assertEqual(self.store.load(self.run.run_id).status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(self.store.load(self.run.run_id).repair_round, 0)
        self.assertDocumentUnchanged(before)

    def test_pass_also_ends_at_human_doc_review(self):
        before = self.document.read_bytes()
        codex = FakeCodex([review("PASS")])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(self.claude.touches, [])
        self.assertDocumentUnchanged(before)

    def test_a_finished_doc_run_is_never_reviewed_again(self):
        first = FakeCodex([review("PASS")])
        self.workflow(first).run(self.run.run_id)
        before = self.document.read_bytes()

        second = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("F-002")])])
        result = self.workflow(second).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(second.calls, [])
        self.assertDocumentUnchanged(before)

    # ---- the two ways Codex may pause a doc run --------------------------

    def test_needs_user_input_pauses_and_answers_reach_the_next_pass(self):
        before = self.document.read_bytes()
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["Which tier keeps the second licence?"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex)

        paused = workflow.run(self.run.run_id)

        self.assertEqual(paused.status, Status.AWAITING_USER_INPUT)
        self.assertEqual(paused.repair_round, 0)
        self.assertDocumentUnchanged(before)
        questions = json.loads((self.artifacts / "user-questions.json").read_text(encoding="utf-8"))
        self.assertEqual(questions["questions"][0]["id"], "Q-001")

        result = workflow.answer(self.run.run_id, {"Q-001": "兩邊都保留"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(self.claude.touches, [])
        self.assertIn("兩邊都保留", codex.calls[1]["decision_log"])
        self.assertIn("Q-001", codex.calls[1]["decision_log"])
        answers = json.loads(
            (self.artifacts / "question-cycles" / "0001" / "answers.json").read_text(encoding="utf-8")
        )
        self.assertEqual(answers["answers"], {"Q-001": "兩邊都保留"})
        self.assertDocumentUnchanged(before)

    def test_an_answered_doc_question_is_recorded_as_a_settled_decision(self):
        """A doc run has no Claude and no Plan, so nothing is ever pending one.

        Real Codex read ``pending Claude Plan update`` as evidence that an
        answered question had not yet become a document requirement, and carried
        the blocker over with the answer sitting directly above that line.  The
        log must state what is true here: the answer is the user's decision and
        the document owes conformance to it.
        """
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["Which tier keeps the second licence?"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex)
        workflow.run(self.run.run_id)

        workflow.answer(self.run.run_id, {"Q-001": "兩邊都保留"})

        decision_log = (self.artifacts / "decision-log.md").read_text(encoding="utf-8")
        self.assertNotIn("pending Claude Plan update", decision_log)
        self.assertEqual(
            decision_log,
            "## Q-001\n"
            "- Question: Which tier keeps the second licence?\n"
            "- Answer: 兩邊都保留\n"
            "- Decision impact: settled user decision, binding on the document\n",
        )

    def test_the_settled_decision_wording_is_what_reaches_the_next_round(self):
        """The artifact is only evidence; the reviewer input is the live path."""
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["Which tier keeps the second licence?"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex)
        workflow.run(self.run.run_id)

        workflow.answer(self.run.run_id, {"Q-001": "兩邊都保留"})

        delivered = codex.calls[1]["decision_log"]
        self.assertNotIn("pending Claude Plan update", delivered)
        self.assertIn(
            "- Decision impact: settled user decision, binding on the document",
            delivered,
        )
        # The question and the answer still reach the reviewer verbatim.
        self.assertIn("- Question: Which tier keeps the second licence?", delivered)
        self.assertIn("- Answer: 兩邊都保留", delivered)

    def test_context_request_expands_and_reviews_again(self):
        before = self.document.read_bytes()
        candidate = self.root / "candidate.md"
        candidate.write_text("# Candidate\nthe repository evidence\n", encoding="utf-8")
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("PASS"),
        ])

        result = self.workflow(
            codex,
            context_resolver=lambda requests: [
                SourceRef(candidate, "Candidate", requests[0], priority=1)
            ],
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(self.claude.touches, [])
        self.assertIn("the repository evidence", codex.calls[1]["knowledge_packet"])
        self.assertIsNone(result.manifest.context_checksum)
        self.assertDocumentUnchanged(before)

    def test_context_request_without_a_resolver_pauses_for_exact_sections(self):
        before = self.document.read_bytes()
        codex = FakeCodex([review("CONTEXT_REQUEST", context_requests=["Candidate"])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(self.pause()["reason"], "CONTEXT_INPUT_REQUIRED")
        self.assertEqual(result.repair_round, 0)
        self.assertDocumentUnchanged(before)

    def test_a_bootstrapped_doc_packet_gets_the_doc_caps_and_estimator(self):
        """The bootstrap resolves (5, 16000, doc estimator) from the run's kind.

        None of this is visible from the outside while a small packet fits
        under any budget, so the values are pinned where they are passed.
        """
        candidate = self.root / "candidate.md"
        candidate.write_text("# Candidate\n這是倉庫裡的證據\n", encoding="utf-8")
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("PASS"),
        ])
        workflow = self.workflow(
            codex,
            context_resolver=lambda requests: [SourceRef(candidate, "Candidate", requests[0])],
        )

        with record_build_packet() as calls:
            result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(self.policy.context_limits("doc"), (5, 16000))
        self.assertEqual(
            [(call["max_sources"], call["max_tokens"], call["budget_method"]) for call in calls],
            [(5, 16000, BUDGET_METHOD_DOC)],
        )
        self.assertEqual(workflow.context_packet.max_context_tokens, 16000)
        self.assertEqual(workflow.context_packet.budget_method, BUDGET_METHOD_DOC)

    def test_a_doc_bootstrap_may_select_five_sources(self):
        """The doc source cap is load-bearing: the plan cap of three rejects this."""
        candidates = []
        for index in range(5):
            path = self.root / ("candidate-%d.md" % index)
            path.write_text(
                "# Candidate %d\n第 %d 份倉庫證據\n" % (index, index), encoding="utf-8"
            )
            candidates.append(
                SourceRef(path, "Candidate %d" % index, "requested", priority=index)
            )
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex, context_resolver=lambda requests: candidates)

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(len(workflow.context_packet.sources), 5)

    def test_a_doc_bootstrap_resume_may_supply_five_sections(self):
        """A human bootstrapping a doc run gets the five sources policy grants.

        The cap on this path used to be a literal three, so the fifth source a
        doc run is entitled to was unreachable whenever a person, rather than a
        resolver, supplied the sections.
        """
        sections = []
        for index in range(6):
            path = self.root / ("section-%d.md" % index)
            path.write_text(
                "# S%d\n第 %d 份倉庫證據\n" % (index, index), encoding="utf-8"
            )
            sections.append(SourceRef(path, "S%d" % index, "使用者提供"))
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex)
        self.assertEqual(workflow.run(self.run.run_id).status, Status.PAUSED)

        self.assertEqual(
            self.pause()["detail"],
            "provide one to 5 exact Markdown sections with expand-context",
        )
        with self.assertRaises(WorkflowError) as caught:
            workflow.provide_context(self.run.run_id, sections)
        self.assertEqual(
            str(caught.exception), "context resume requires one to 5 exact sections"
        )

        result = workflow.provide_context(self.run.run_id, sections[:5])

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(len(workflow.context_packet.sources), 5)
        self.assertEqual(workflow.context_packet.budget_method, BUDGET_METHOD_DOC)

    def test_an_expansion_resume_stays_at_three_candidates(self):
        """With a packet in hand the cap is the candidate limit, not the kind's.

        ``expand_packet`` swaps exactly one excerpt, so this number bounds the
        alternatives a human may offer.  A doc run's five initial sources must
        not leak into it.
        """
        initial = self.root / "initial.md"
        initial.write_text("# Initial\n最初的證據\n", encoding="utf-8")
        packet = build_packet(
            [SourceRef(initial, "Initial", "initial", priority=0)],
            budget_method=BUDGET_METHOD_DOC,
        )
        candidates = []
        for index in range(4):
            path = self.root / ("candidate-%d.md" % index)
            path.write_text(
                "# C%d\n第 %d 個候選\n" % (index, index), encoding="utf-8"
            )
            candidates.append(SourceRef(path, "C%d" % index, "使用者提供", priority=1))
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex, context_packet=packet)
        self.assertEqual(workflow.run(self.run.run_id).status, Status.PAUSED)

        self.assertEqual(
            self.pause()["detail"],
            "provide one to 3 exact Markdown sections with expand-context",
        )
        with self.assertRaises(WorkflowError) as caught:
            workflow.provide_context(self.run.run_id, candidates)
        self.assertEqual(
            str(caught.exception), "context resume requires one to 3 exact sections"
        )

        result = workflow.provide_context(self.run.run_id, candidates[:3])

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(len(workflow.context_packet.sources), 1)
        self.assertEqual(workflow.context_packet.expansion_count, 1)

    # ---- findings are the deliverable ------------------------------------

    def test_findings_are_persisted_and_returned_to_the_next_round(self):
        first = FakeCodex([
            review("CHANGES_REQUIRED", findings=[finding("F-001"), finding("F-002")]),
        ])
        workflow = self.workflow(first)
        workflow.run(self.run.run_id)

        self.assertEqual(first.calls[0]["unresolved_prior_findings"], [])
        persisted = json.loads(
            (self.artifacts / "unresolved-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual([item["id"] for item in persisted["findings"]], ["F-001", "F-002"])
        seen = json.loads((self.artifacts / "seen-findings.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["ids"], ["F-001", "F-002"])

        # A re-review runs the same artifacts again after the human edits.
        self.restart(workflow)
        second = FakeCodex([review("PASS")])
        result = self.workflow(second).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(
            [item["id"] for item in second.calls[0]["unresolved_prior_findings"]],
            ["F-001", "F-002"],
        )

    def test_changes_required_without_a_finding_pauses(self):
        before = self.document.read_bytes()
        codex = FakeCodex([review("CHANGES_REQUIRED")])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(self.pause()["reason"], "CHANGES_WITHOUT_FINDING")
        self.assertEqual(result.repair_round, 0)
        self.assertDocumentUnchanged(before)

    def test_invalid_codex_output_pauses(self):
        before = self.document.read_bytes()
        codex = FakeCodex([{"verdict": "PASS", "summary": "missing the other fields"}])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(self.pause()["reason"], "INVALID_CODEX_REVIEW")
        self.assertEqual(result.repair_round, 0)
        self.assertDocumentUnchanged(before)

    def test_a_journalled_outcome_is_replayed_without_a_verdict_transition(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("F-001")])])
        workflow = self.workflow(codex)
        workflow.run(self.run.run_id)

        # A crash between the journal write and the state save leaves the run
        # RUNNING with its outcome already recorded.  Replay must finish it
        # through complete_doc_review; transition() would raise for a doc run.
        state = self.store.load(self.run.run_id)
        workflow._set_running(state)
        workflow._handle_review(
            state, self.artifacts, 1,
            review("CHANGES_REQUIRED", findings=[finding("F-001")]),
        )

        self.assertEqual(state.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(state.repair_round, 0)
        self.assertEqual(len(codex.calls), 1)

    # ---- what a round leaves behind for the next one ---------------------

    def test_a_pass_clears_the_findings_the_previous_round_left(self):
        """A PASS is the proof the human's edit landed, so the findings are done.

        ``PlanWorkflow`` empties this artifact after a repair.  A doc run's
        repair is a human editing the document, and a PASS is the only signal
        that it worked; nothing else ever clears it here.
        """
        first = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("F-001")])])
        workflow = self.workflow(first)
        workflow.run(self.run.run_id)
        self.restart(workflow)

        second = FakeCodex([review("PASS")])
        result = self.workflow(second).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        # The passing round still saw what it was settling.
        self.assertEqual(
            [item["id"] for item in second.calls[0]["unresolved_prior_findings"]],
            ["F-001"],
        )
        persisted = json.loads(
            (self.artifacts / "unresolved-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["findings"], [])

    def test_a_round_after_a_pass_is_never_told_the_settled_findings_again(self):
        """Round three must not be handed round one's already-settled findings."""
        first = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("F-001")])])
        workflow = self.workflow(first)
        workflow.run(self.run.run_id)
        self.restart(workflow)
        second = self.workflow(FakeCodex([review("PASS")]))
        second.run(self.run.run_id)
        self.restart(second)

        third = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("F-002")])])
        result = self.workflow(third).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(third.calls[0]["unresolved_prior_findings"], [])

    # ---- a question round's findings are findings too --------------------

    def test_a_question_round_records_the_findings_it_reported(self):
        """NEEDS_USER_INPUT carries findings as well as questions.

        Codex reports what it found *and* asks what only the author can
        settle.  A round that recorded nothing would hand the next round an
        empty prior set, and the next round's report would then read like the
        whole story.  The report the human reads and the artifact the next
        round reads name the same findings, because they are the same round's.
        """
        codex = FakeCodex([review(
            "NEEDS_USER_INPUT",
            findings=[finding("REQ-005"), finding("REQ-006")],
            questions=["Which tier keeps the second licence?"],
        )])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_USER_INPUT)
        persisted = json.loads(
            (self.artifacts / "unresolved-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["id"] for item in persisted["findings"]], ["REQ-005", "REQ-006"]
        )
        self.assertEqual(self.report_finding_ids(1), ["REQ-005", "REQ-006"])
        seen = json.loads((self.artifacts / "seen-findings.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["ids"], ["REQ-005", "REQ-006"])

    def test_the_round_after_an_answer_is_told_the_question_round_findings(self):
        """The regression a real dry run exposed, with the ids it actually lost.

        Round one reported REQ-005..008 and asked its questions; round two was
        handed an empty prior set and marked its own four findings newly
        discovered — correctly, given what it had been told.  A reader holding
        report -01 and report -02 then cannot tell a finding their answers
        resolved from one that was silently dropped, which is the whole point
        of the lineage contract.
        """
        first = [
            finding("REQ-005"), finding("REQ-006"),
            finding("REQ-007"), finding("REQ-008"),
        ]
        second = [
            finding("REQ-009", severity="blocker"), finding("REQ-010"),
            finding("REQ-011"), finding("REQ-012"),
        ]
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", findings=first, questions=["交易邊界是什麼？"]),
            review(
                "NEEDS_USER_INPUT", findings=second,
                questions=["fallback 候選是哪一個？"],
            ),
        ])
        workflow = self.workflow(codex)
        self.assertEqual(workflow.run(self.run.run_id).status, Status.AWAITING_USER_INPUT)

        result = workflow.answer(self.run.run_id, {"Q-001": "以後端清單為準"})

        self.assertEqual(result.status, Status.AWAITING_USER_INPUT)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(codex.calls[0]["unresolved_prior_findings"], [])
        self.assertEqual(
            [item["id"] for item in codex.calls[1]["unresolved_prior_findings"]],
            ["REQ-005", "REQ-006", "REQ-007", "REQ-008"],
        )
        # Whole findings, not bare ids: the next round needs the evidence and
        # the required outcome to judge whether the answer settled them.
        self.assertEqual(codex.calls[1]["unresolved_prior_findings"], first)
        persisted = json.loads(
            (self.artifacts / "unresolved-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["id"] for item in persisted["findings"]],
            ["REQ-009", "REQ-010", "REQ-011", "REQ-012"],
        )

    def test_a_question_round_with_no_finding_records_an_empty_list(self):
        """Finding nothing is a statement the round makes, not an absence.

        ``_unresolved_findings`` defaults a missing artifact to an empty list,
        so the next round cannot tell the two apart — but only by accident.
        The artifact mirrors the round that wrote the last report either way,
        so a later reader of the run never has to guess whether a question
        round was asked about findings at all.

        The seen-ids set is a different thing: it only ever grows, so a round
        that saw nothing leaves it untouched rather than creating it empty.
        """
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["Which tier keeps the second licence?"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex)
        workflow.run(self.run.run_id)

        persisted = json.loads(
            (self.artifacts / "unresolved-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["findings"], [])
        self.assertFalse((self.artifacts / "seen-findings.json").exists())

        result = workflow.answer(self.run.run_id, {"Q-001": "兩邊都保留"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(codex.calls[1]["unresolved_prior_findings"], [])

    def test_a_pass_after_an_answer_clears_the_question_round_findings(self):
        """The answers settled them, and the PASS is the proof."""
        findings = [finding("REQ-005"), finding("REQ-006")]
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", findings=findings, questions=["交易邊界是什麼？"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex)
        workflow.run(self.run.run_id)

        result = workflow.answer(self.run.run_id, {"Q-001": "以後端清單為準"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        # The passing round still saw what the answers were settling.
        self.assertEqual(codex.calls[1]["unresolved_prior_findings"], findings)
        persisted = json.loads(
            (self.artifacts / "unresolved-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["findings"], [])

        self.restart(workflow)
        later = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("REQ-020")])])
        reviewed = self.workflow(later).run(self.run.run_id)

        self.assertEqual(reviewed.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(later.calls[0]["unresolved_prior_findings"], [])

    def test_a_context_request_round_neither_records_nor_erases_findings(self):
        """A round that withheld judgement must not touch the lineage channel.

        A context request is Codex saying it cannot judge the document yet, and
        it is the one outcome that writes no report.  Recording its findings
        would put ids into the next round's prior set that no report ever
        showed a human, and — worse — would let a reportless round overwrite
        what the last judged round left outstanding: an empty context round
        after a question round would wipe exactly the findings this section
        exists to carry.  So the artifact stays as the last judged round left
        it.  The context round's own findings survive in ``reviews/`` as the
        audit record, and the round that finally judges the document — same
        bytes, more evidence — restates whatever still holds.
        """
        candidate = self.root / "candidate.md"
        candidate.write_text("# Candidate\nthe repository evidence\n", encoding="utf-8")
        codex = FakeCodex([
            review(
                "NEEDS_USER_INPUT", findings=[finding("REQ-005")],
                questions=["交易邊界是什麼？"],
            ),
            review(
                "CONTEXT_REQUEST", findings=[finding("REQ-900")],
                context_requests=["Candidate"],
            ),
            review("CHANGES_REQUIRED", findings=[finding("REQ-006")]),
        ])
        workflow = self.workflow(
            codex,
            context_resolver=lambda requests: [
                SourceRef(candidate, "Candidate", requests[0], priority=1)
            ],
        )
        workflow.run(self.run.run_id)

        result = workflow.answer(self.run.run_id, {"Q-001": "以後端清單為準"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(len(codex.calls), 3)
        # The context round was told what the question round left outstanding,
        self.assertEqual(
            [item["id"] for item in codex.calls[1]["unresolved_prior_findings"]], ["REQ-005"]
        )
        # and so is the round that judges after it — not the provisional
        # finding the context round happened to mention along the way.
        self.assertEqual(
            [item["id"] for item in codex.calls[2]["unresolved_prior_findings"]], ["REQ-005"]
        )
        persisted = json.loads(
            (self.artifacts / "unresolved-findings.json").read_text(encoding="utf-8")
        )
        self.assertEqual([item["id"] for item in persisted["findings"]], ["REQ-006"])
        seen = json.loads((self.artifacts / "seen-findings.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["ids"], ["REQ-005", "REQ-006"])
        # REQ-900 reached no report either: a context round takes no number.
        self.assertEqual(self.report_finding_ids(1), ["REQ-005"])
        self.assertEqual(self.report_finding_ids(2), ["REQ-006"])
        self.assertFalse(self.report(3).exists())

    # ---- the report beside the document ----------------------------------

    def report(self, number):
        """Where a report lands: beside the document, at the next free number.

        ``number`` counts reports beside this document, across every run that
        ever reviewed it — it is not the run's review sequence.
        """
        return self.document.parent / ("rd-spec-review-%02d.md" % number)

    def race_the_scan(self, number, *, symlink_to=None):
        """Create the target between the number scan and the O_EXCL open.

        The scan cannot see a writer that arrives after it looked, so this is
        the exact window ``O_EXCL`` exists to close.
        """
        from ai_review import doc_workflow as module

        real = module.next_doc_report_number

        def racing(doc_path):
            chosen = real(doc_path)
            if symlink_to is None:
                self.report(number).write_text("a racing writer got here first\n", encoding="utf-8")
            else:
                self.report(number).symlink_to(symlink_to)
            return chosen

        module.next_doc_report_number = racing
        self.addCleanup(setattr, module, "next_doc_report_number", real)

    def repository_snapshot(self):
        """Every file under the repository worktree, by bytes."""
        return {
            path: path.read_bytes()
            for path in sorted(self.repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        }

    def report_finding_ids(self, number):
        """The finding ids the report itself names, in the order it names them."""
        return re.findall(
            r"^### (\S+) — ", self.report(number).read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )

    def round_finding_ids(self, sequence):
        """The finding ids the round's own Codex artifact recorded."""
        recorded = json.loads(
            (self.artifacts / "reviews" / ("%04d.json" % sequence)).read_text(encoding="utf-8")
        )
        return [item["id"] for item in recorded["findings"]]

    def test_a_completed_round_writes_its_report_beside_the_document(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[
            finding("D1", severity="blocker"),
        ])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertTrue(self.report(1).is_file())
        self.assertEqual(self.report(1).parent, self.document.parent)
        text = self.report(1).read_text(encoding="utf-8")
        self.assertIn("# rd-spec.md review 01", text)
        self.assertIn("- run: %s" % self.run.run_id, text)
        self.assertIn("- review sequence: 1", text)
        self.assertIn(
            "- lens: requirement（文件只描述使用者行為，沒有任何檔案路徑）", text
        )
        self.assertIn("- verdict: CHANGES_REQUIRED", text)
        self.assertIn("- reviewed at: ", text)
        self.assertIn("## Blockers", text)
        self.assertIn("### D1 — every scenario states its completion condition", text)
        self.assertIn("- location: docs/rd-spec.md:3", text)
        self.assertIn("- evidence: the scenario states no observable result", text)
        self.assertIn("- required outcome: state what proves the scenario finished", text)

    def test_the_report_is_the_only_file_a_doc_run_writes_into_the_repository(self):
        before = self.repository_snapshot()
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])

        self.workflow(codex).run(self.run.run_id)

        after = self.repository_snapshot()
        self.assertEqual(sorted(after) , sorted(list(before) + [self.report(1)]))
        for path, contents in before.items():
            with self.subTest(path=path.name):
                self.assertEqual(after[path], contents)

    def test_a_second_round_of_one_run_takes_the_next_free_number(self):
        """A re-review is numbered by the shelf beside the document, not by run."""
        first = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])
        workflow = self.workflow(first)
        workflow.run(self.run.run_id)
        self.restart(workflow)

        second = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D2")])])
        self.workflow(second).run(self.run.run_id)

        self.assertTrue(self.report(2).is_file())
        text = self.report(2).read_text(encoding="utf-8")
        self.assertIn("# rd-spec.md review 02", text)
        self.assertIn("- run: %s" % self.run.run_id, text)
        self.assertIn("- review sequence: 2", text)
        self.assertEqual(self.report_finding_ids(2), self.round_finding_ids(2))

    def test_each_round_writes_its_own_report_and_keeps_the_previous_one(self):
        first = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])
        workflow = self.workflow(first)
        workflow.run(self.run.run_id)
        first_report = self.report(1).read_bytes()
        self.restart(workflow)

        second = FakeCodex([review("PASS")])
        self.workflow(second).run(self.run.run_id)

        self.assertTrue(self.report(1).is_file())
        self.assertTrue(self.report(2).is_file())
        # A previous round's report is evidence: the human puts two rounds
        # side by side to see what is left.
        self.assertEqual(self.report(1).read_bytes(), first_report)
        self.assertIn("review 02", self.report(2).read_text(encoding="utf-8"))
        self.assertIn("- verdict: PASS", self.report(2).read_text(encoding="utf-8"))

    def test_the_report_names_exactly_the_findings_the_round_recorded(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[
            finding("D1", severity="blocker"),
            finding("D2", severity="major"),
            finding("D3", severity="minor"),
            finding("D4", severity="info"),
        ])])

        self.workflow(codex).run(self.run.run_id)

        reported = self.report_finding_ids(1)
        recorded = self.round_finding_ids(1)
        # Both directions: nothing dropped from the artifact, nothing invented
        # in the report.
        self.assertEqual(sorted(reported), sorted(recorded))
        self.assertEqual(len(reported), len(recorded))
        self.assertEqual(set(recorded) - set(reported), set())
        self.assertEqual(set(reported) - set(recorded), set())

    def test_a_report_that_loses_a_race_is_a_hard_error_not_an_overwrite(self):
        """Scanning for a free number cannot close the window; O_EXCL does."""
        before = self.document.read_bytes()
        self.race_the_scan(1)
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(self.pause()["reason"], "DOC_REPORT_NOT_WRITTEN")
        self.assertEqual(
            self.report(1).read_text(encoding="utf-8"), "a racing writer got here first\n"
        )
        self.assertFalse(self.report(2).exists())
        self.assertDocumentUnchanged(before)

    def test_a_symlink_that_wins_the_race_is_never_followed(self):
        before = self.document.read_bytes()
        decoy = self.root / "decoy.md"
        decoy.write_text("# not the report\n", encoding="utf-8")
        self.race_the_scan(1, symlink_to=decoy)
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(self.pause()["reason"], "DOC_REPORT_NOT_WRITTEN")
        self.assertTrue(self.report(1).is_symlink())
        self.assertEqual(decoy.read_text(encoding="utf-8"), "# not the report\n")
        self.assertDocumentUnchanged(before)

    def test_a_second_run_over_one_document_takes_the_next_number(self):
        """Two reviews of one document sit side by side, whichever run made them."""
        first_codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])
        self.workflow(first_codex).run(self.run.run_id)
        first_run_id = self.run.run_id

        # A second run over the same document: a different lens, or the same
        # document reviewed again from scratch.  Its own review sequence is 1.
        second = self.store.create(RunState.new_doc(self.manifest()))
        second_codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D9")])])
        result = self.workflow(second_codex).run(second.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertTrue(self.report(1).is_file())
        self.assertTrue(self.report(2).is_file())
        first_text = self.report(1).read_text(encoding="utf-8")
        second_text = self.report(2).read_text(encoding="utf-8")
        # The header tells the two runs apart, and keeps the audit trail back
        # to each run's own reviews/NNNN.json.
        self.assertIn("- run: %s" % first_run_id, first_text)
        self.assertIn("- run: %s" % second.run_id, second_text)
        self.assertNotIn(second.run_id, first_text)
        self.assertNotIn(first_run_id, second_text)
        self.assertIn("- review sequence: 1", first_text)
        self.assertIn("- review sequence: 1", second_text)
        self.assertIn("### D1 — ", first_text)
        self.assertIn("### D9 — ", second_text)
        self.assertNotIn("D9", first_text)
        self.assertNotIn("D1", second_text)

    def test_a_gap_in_the_numbering_is_never_backfilled(self):
        """Filling a hole would make the order of the reports lie."""
        self.report(1).write_text("# round one\n", encoding="utf-8")
        self.report(3).write_text("# round three\n", encoding="utf-8")
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertTrue(self.report(4).is_file())
        self.assertFalse(self.report(2).exists())
        self.assertEqual(self.report(1).read_text(encoding="utf-8"), "# round one\n")
        self.assertEqual(self.report(3).read_text(encoding="utf-8"), "# round three\n")

    def test_the_scan_ignores_files_that_are_not_this_document_reports(self):
        neighbours = {
            self.document.parent / "notes.md": "# notes\n",
            self.document.parent / "rd-spec-review-notes.md": "# not numbered\n",
            self.document.parent / "other-review-05.md": "# another document\n",
            self.document.parent / "rd-spec-review-02.txt": "# not markdown\n",
        }
        for path, contents in neighbours.items():
            path.write_text(contents, encoding="utf-8")
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])

        self.workflow(codex).run(self.run.run_id)

        self.assertTrue(self.report(1).is_file())
        for path, contents in neighbours.items():
            with self.subTest(neighbour=path.name):
                self.assertEqual(path.read_text(encoding="utf-8"), contents)

    def test_a_symlink_occupying_a_number_is_counted_not_followed(self):
        decoy = self.root / "decoy.md"
        decoy.write_text("# not the report\n", encoding="utf-8")
        self.report(1).symlink_to(decoy)
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("D1")])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertTrue(self.report(2).is_file())
        self.assertEqual(decoy.read_text(encoding="utf-8"), "# not the report\n")

    def test_an_empty_severity_section_is_omitted_entirely(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[
            finding("D1", severity="major"),
        ])])

        self.workflow(codex).run(self.run.run_id)

        text = self.report(1).read_text(encoding="utf-8")
        self.assertIn("## Major", text)
        for heading in ("## Blockers", "## Minor", "## Info", "## Questions"):
            with self.subTest(heading=heading):
                self.assertNotIn(heading, text)

    def test_a_passing_round_still_writes_a_report(self):
        """A PASS report is how the human learns the round found nothing."""
        codex = FakeCodex([review("PASS")])

        self.workflow(codex).run(self.run.run_id)

        text = self.report(1).read_text(encoding="utf-8")
        self.assertIn("# rd-spec.md review 01", text)
        self.assertIn("- verdict: PASS", text)
        for heading in ("## Blockers", "## Major", "## Minor", "## Info", "## Questions"):
            with self.subTest(heading=heading):
                self.assertNotIn(heading, text)

    def test_a_needs_user_input_round_reports_its_questions(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=[
            "Which tier keeps the second licence?",
            "What happens when the first one expires?",
        ])])

        result = self.workflow(codex).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_USER_INPUT)
        text = self.report(1).read_text(encoding="utf-8")
        self.assertIn("- verdict: NEEDS_USER_INPUT", text)
        self.assertIn("## Questions", text)
        self.assertIn("- Which tier keeps the second licence?", text)
        self.assertIn("- What happens when the first one expires?", text)

    def test_a_context_request_round_takes_no_report_number(self):
        """A round that only asked for evidence leaves the numbering untouched."""
        candidate = self.root / "candidate.md"
        candidate.write_text("# Candidate\nthe repository evidence\n", encoding="utf-8")
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("CHANGES_REQUIRED", findings=[finding("D1")]),
        ])

        result = self.workflow(
            codex,
            context_resolver=lambda requests: [
                SourceRef(candidate, "Candidate", requests[0], priority=1)
            ],
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        # One report exists, and it is the first one: the context round wrote
        # none.  Its header still names the review sequence it came from.
        self.assertTrue(self.report(1).is_file())
        self.assertFalse(self.report(2).exists())
        self.assertIn("- review sequence: 2", self.report(1).read_text(encoding="utf-8"))
        self.assertEqual(self.report_finding_ids(1), self.round_finding_ids(2))

    # ---- the trust boundary ----------------------------------------------

    def test_review_input_carries_exactly_the_documented_keys(self):
        codex = FakeCodex([review("PASS")])

        self.workflow(codex).run(self.run.run_id)

        inputs = codex.calls[0]
        self.assertEqual(set(inputs), REVIEW_INPUT_KEYS)
        self.assertEqual(inputs["document"], self.document.read_text(encoding="utf-8"))
        self.assertEqual(inputs["document_path"], "docs/rd-spec.md")
        self.assertEqual(inputs["lens"], "requirement")
        self.assertEqual(inputs["brief"], self.brief)
        self.assertEqual(inputs["policy"], {"version": 1, "max_rounds": 1})
        self.assertEqual(inputs["decision_log"], "")
        self.assertEqual(inputs["context_manifest"], {})
        self.assertEqual(inputs["knowledge_packet"], "")
        self.assertEqual(inputs["unresolved_prior_findings"], [])

    def test_the_doc_context_budget_is_the_doc_budget(self):
        self.assertEqual(self.policy.context_limits("doc"), (5, 16000))
        self.assertEqual(self.policy.context_limits("plan"), (3, 8000))
        codex = FakeCodex([review("PASS")])

        self.workflow(codex).run(self.run.run_id)

        # The reviewer is told how many rounds it gets, never a Plan-sized
        # context budget: a doc run's budget is context_limits("doc").
        self.assertNotIn("max_context_tokens", codex.calls[0]["policy"])
        self.assertNotIn("max_context_expansions", codex.calls[0]["policy"])

    def test_claude_is_unusable_from_a_doc_workflow(self):
        workflow = self.workflow(FakeCodex())

        with self.assertRaises(WorkflowError):
            workflow.claude.resolve
        with self.assertRaises(WorkflowError):
            workflow.claude.update_plan
        self.assertEqual(self.claude.touches, [])

    def test_a_plan_run_cannot_be_driven_by_the_doc_workflow(self):
        plan = self.repo / "docs" / "plan.md"
        plan.write_text("# Plan\n", encoding="utf-8")
        plan_run = self.store.create(RunState.new("plan", str(plan), str(self.repo), "HEAD"))
        codex = FakeCodex([review("PASS")])

        result = self.workflow(codex).run(plan_run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(codex.calls, [])

    # ---- helpers ---------------------------------------------------------

    def restart(self, workflow):
        """Put a finished doc run back in RUNNING, as a re-review round will."""
        state = self.store.load(self.run.run_id)
        workflow._set_running(state)
        self.store.save(state)


if __name__ == "__main__":
    unittest.main()
