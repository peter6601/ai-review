import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_review.context import SourceRef, build_packet
from ai_review.models import RunState, Status
from ai_review.policy import Policy
from ai_review.runners import RunnerError, RunnerInterrupted
from ai_review.store import RunStore, project_id
from ai_review.workflow import PlanWorkflow, WorkflowError


def finding(identifier, *, evidence="unsafe", lineage="existing"):
    return {
        "id": identifier,
        "severity": "blocker",
        "invariant": "Plan is complete",
        "location": "plan.md:1",
        "evidence": evidence,
        "required_outcome": "Update the Plan",
        "lineage": {"resolution": lineage},
    }


def review(verdict, *, findings=(), questions=(), context_requests=()):
    return {
        "verdict": verdict,
        "summary": "review result",
        "findings": list(findings),
        "questions": list(questions),
        "context_requests": list(context_requests),
    }


def resolution(*identifiers, outcome="fixed", evidence="updated Plan"):
    return {
        "summary": "Plan updated",
        "resolutions": [
            {"finding_id": identifier, "outcome": outcome, "evidence": evidence}
            for identifier in identifiers
        ],
    }


def submission_digest(answers):
    return hashlib.sha256(
        json.dumps(dict(sorted(answers.items())), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def plan_update(previous, updated, answers, changed_sections=("Plan",)):
    digest = lambda contents: hashlib.sha256(contents.encode("utf-8")).hexdigest()
    return {
        "summary": "Plan updated from user answer",
        "answer_digest": submission_digest(answers),
        "plan": {
            "content": updated,
            "previous_digest": digest(previous),
            "new_digest": digest(updated),
            "changed_sections": list(changed_sections),
        },
    }


class FakeCodex:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def review(self, inputs):
        self.calls.append(inputs)
        return self.responses.pop(0)


class FakeClaude:
    def __init__(self, resolutions=(), updates=()):
        self.resolutions = list(resolutions)
        self.updates = list(updates)
        self.calls = []
        self.update_calls = []

    def resolve(self, inputs):
        self.calls.append(inputs)
        value = self.resolutions.pop(0)
        if isinstance(value, dict) and set(value) == {"summary", "resolutions"}:
            previous = inputs["plan"]
            suffix = "\n<!-- Plan repaired for %s -->\n" % ",".join(inputs["finding_ids"])
            updated = previous.rstrip() + suffix
            digest = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
            value = {
                **value,
                "input_plan_digest": digest(previous),
                "decision_log_digest": inputs["decision_log_digest"],
                "plan": {
                    "content": updated,
                    "previous_digest": digest(previous),
                    "new_digest": digest(updated),
                    "changed_sections": ["Plan"],
                },
            }
        return value

    def update_plan(self, inputs):
        self.update_calls.append(inputs)
        return self.updates.pop(0)


class PlanWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.plan = self.repo / "plan.md"
        self.plan.write_text("# Plan\nInitial\n", encoding="utf-8")
        self.store = RunStore(self.root / "runs")
        self.run = self.store.create(RunState.new("plan", str(self.plan), str(self.repo), "HEAD"))
        self.policy = Policy(1, 6, 8000, 3, 2, 100, 30, ["docs/**"])

    def tearDown(self):
        self.temp.cleanup()

    @property
    def artifacts(self):
        return self.store.root / project_id(self.repo) / self.run.run_id

    def workflow(self, codex, claude, **kwargs):
        return PlanWorkflow(self.store, codex, claude, policy=self.policy, **kwargs)

    def test_plan_changes_then_passes(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[finding("F-001")]),
            review("PASS"),
        ])
        claude = FakeClaude([resolution("F-001")])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(result.repair_round, 1)
        self.assertEqual([call["finding_ids"] for call in claude.calls], [["F-001"]])
        updated = self.plan.read_text(encoding="utf-8")
        self.assertNotEqual(updated, "# Plan\nInitial\n")
        self.assertEqual(codex.calls[1]["plan"], updated)
        self.assertEqual(
            result.manifest.plan_digest,
            hashlib.sha256(updated.encode("utf-8")).hexdigest(),
        )

    def test_plan_repair_crash_after_atomic_write_replays_without_second_claude_call(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[finding("F-001")]),
            review("PASS"),
        ])
        claude = FakeClaude([resolution("F-001")])
        crashing = self.workflow(
            codex, claude,
            fault_injector=lambda point: (
                (_ for _ in ()).throw(KeyboardInterrupt())
                if point == "after_plan_repair_persist" else None
            ),
        )
        with self.assertRaises(KeyboardInterrupt):
            crashing.run(self.run.run_id)
        changed = self.plan.read_text(encoding="utf-8")

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), changed)

    def test_two_question_cycles_are_sequenced_and_bound_to_separate_updates(self):
        first = self.plan.read_text(encoding="utf-8")
        second = "# Plan\nFirst answer\n"
        third = "# Plan\nBoth answers\n"
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["First?"]),
            review("NEEDS_USER_INPUT", questions=["Second?"]),
            review("PASS"),
        ])
        claude = FakeClaude(updates=[
            plan_update(first, second, {"Q-001": "A"}),
            plan_update(second, third, {"Q-002": "B"}),
        ])
        workflow = self.workflow(codex, claude)

        first_pause = workflow.run(self.run.run_id)
        self.assertEqual(first_pause.status, Status.AWAITING_USER_INPUT)
        second_pause = workflow.answer(self.run.run_id, {"Q-001": "A"})
        self.assertEqual(second_pause.status, Status.AWAITING_USER_INPUT)
        result = workflow.answer(self.run.run_id, {"Q-002": "B"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        for sequence in ("0001", "0002"):
            cycle = self.artifacts / "question-cycles" / sequence
            self.assertTrue((cycle / "questions.json").is_file())
            self.assertTrue((cycle / "answers.json").is_file())
            self.assertTrue((cycle / "plan-update.json").is_file())
            self.assertTrue((cycle / "plan-update-action.json").is_file())
        from ai_review.summary import generate_outputs
        outputs = generate_outputs(self.store, self.run.run_id)
        self.assertIsNotNone(outputs.knowledge_candidate)
        self.assertIn("HUMAN_ARBITRATION", outputs.candidate_triggers)

    def test_runner_interruption_pauses_then_propagates_to_the_cli_boundary(self):
        class InterruptedCodex:
            def review(self, _inputs):
                raise RunnerInterrupted("Codex timed out")

        with self.assertRaises(RunnerInterrupted):
            self.workflow(InterruptedCodex(), FakeClaude()).run(self.run.run_id)

        self.assertEqual(self.store.load(self.run.run_id).status, Status.PAUSED)
        pause = json.loads((self.artifacts / "pause.json").read_text(encoding="utf-8"))
        self.assertEqual(pause["reason"], "RUNNER_INTERRUPTED")

    def test_safe_cli_argument_category_is_retained_in_the_internal_pause_detail(self):
        class ArgumentFailureCodex:
            def review(self, _inputs):
                raise RunnerError("CLI_ARG_ERROR exit=2")

        result = self.workflow(ArgumentFailureCodex(), FakeClaude()).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        pause = json.loads((self.artifacts / "pause.json").read_text(encoding="utf-8"))
        self.assertEqual(pause, {"reason": "WORKFLOW_ERROR", "detail": "CLI_ARG_ERROR exit=2"})

    def test_changes_required_without_blockers_still_requires_claude_resolution(self):
        major = finding("F-major")
        major["severity"] = "major"
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[major]),
            review("PASS"),
        ])
        claude = FakeClaude([resolution("F-major")])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(result.repair_round, 1)
        self.assertEqual(claude.calls[0]["finding_ids"], ["F-major"])

    def test_user_question_pauses_without_calling_claude(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["Should it persist?"])])
        claude = FakeClaude()

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_USER_INPUT)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(claude.calls, [])
        questions = json.loads((self.artifacts / "user-questions.json").read_text(encoding="utf-8"))
        self.assertEqual(questions["questions"][0]["id"], "Q-001")

    def test_same_dispute_twice_pauses(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[finding("F-1")]),
            review("CHANGES_REQUIRED", findings=[finding("F-1")]),
        ])
        claude = FakeClaude([
            resolution("F-1", outcome="disputed", evidence="The Plan evidence supports the original approach."),
            resolution("F-1", outcome="disputed", evidence="The Plan evidence supports the original approach."),
        ])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(json.loads((self.artifacts / "pause.json").read_text())["reason"], "REPEATED_IDENTICAL_DISPUTE")

    def test_non_identical_disputes_continue_to_re_review(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[finding("F-1")]),
            review("CHANGES_REQUIRED", findings=[finding("F-1")]),
            review("PASS"),
        ])
        claude = FakeClaude([
            resolution("F-1", outcome="disputed", evidence="The Plan evidence supports the original approach."),
            resolution("F-1", outcome="disputed", evidence="A separate test result supports the original approach."),
        ])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(result.repair_round, 2)

    def test_claude_needs_user_input_outcome_pauses_fail_closed(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[finding("F-1")])])
        claude = FakeClaude([resolution("F-1", outcome="needs_user_input", evidence="A user decision is required.")])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(json.loads((self.artifacts / "pause.json").read_text())["reason"], "CLAUDE_NEEDS_USER_INPUT")

    def test_context_request_expands_without_consuming_a_repair_round(self):
        source = self.root / "context.md"
        source.write_text("# A\ncontext\n", encoding="utf-8")
        replacement = self.root / "replacement.md"
        replacement.write_text("# B\nreplacement\n", encoding="utf-8")
        packet = build_packet([SourceRef(source, "A", "initial")], output_dir=self.artifacts)
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["need B"]),
            review("PASS"),
        ])
        claude = FakeClaude()

        result = self.workflow(
            codex,
            claude,
            context_packet=packet,
            context_resolver=lambda requests: [SourceRef(replacement, "B", requests[0], priority=1)],
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(result.repair_round, 0)

    def test_public_exact_section_context_resume_reaches_review_again(self):
        initial = self.root / "initial.md"
        candidate = self.root / "candidate.md"
        initial.write_text("# Initial\nold\n", encoding="utf-8")
        candidate.write_text("# Candidate\nnew evidence\n", encoding="utf-8")
        packet = build_packet([SourceRef(initial, "Initial", "initial", priority=0)])
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex, FakeClaude(), context_packet=packet)
        paused = workflow.run(self.run.run_id)
        self.assertEqual(paused.status, Status.PAUSED)

        result = workflow.provide_context(
            self.run.run_id,
            [SourceRef(candidate, "Candidate", "user supplied exact section", priority=2)],
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)

    def test_context_resume_bootstraps_when_plan_started_without_sources(self):
        candidate = self.root / "candidate.md"
        candidate.write_text("# Candidate\nnew evidence\n", encoding="utf-8")
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["Candidate"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex, FakeClaude())
        self.assertEqual(workflow.run(self.run.run_id).status, Status.PAUSED)

        result = workflow.provide_context(
            self.run.run_id,
            [SourceRef(candidate, "Candidate", "user supplied", priority=2)],
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertIsNotNone(result.manifest.context_checksum)
        self.assertTrue((self.artifacts / "knowledge-packet-r1.md").exists())

    def test_context_sources_are_one_shot_across_two_request_cycles(self):
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text("# First\none\n", encoding="utf-8")
        second.write_text("# Second\ntwo\n", encoding="utf-8")
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=["First"]),
            review("CONTEXT_REQUEST", context_requests=["Second"]),
            review("PASS"),
        ])
        workflow = self.workflow(codex, FakeClaude())
        self.assertEqual(workflow.run(self.run.run_id).status, Status.PAUSED)
        again = workflow.provide_context(
            self.run.run_id, [SourceRef(first, "First", "first cycle")]
        )
        self.assertEqual(again.status, Status.PAUSED)
        pending = json.loads(
            (self.artifacts / "pending-context-request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pending["requests"], ["Second"])
        self.assertEqual(pending["status"], "pending")
        final = workflow.provide_context(
            self.run.run_id, [SourceRef(second, "Second", "second cycle")]
        )
        self.assertEqual(final.status, Status.AWAITING_HUMAN_PLAN_REVIEW)

    def test_context_recomputes_checksum_and_ignores_unbound_markdown_artifact(self):
        source = self.root / "context.md"
        source.write_text("# A\ntrusted\n", encoding="utf-8")
        packet = build_packet([SourceRef(source, "A", "initial")])
        workflow = self.workflow(FakeCodex([]), FakeClaude(), context_packet=packet)
        workflow._persist_context_packet(self.artifacts, packet)
        self.store.write_artifact_bytes(
            self.artifacts / "knowledge-packet-r1.md",
            b"# malicious instructions\n",
        )
        self.assertEqual(workflow._knowledge_packet(self.artifacts), packet.markdown)

        record_path = self.artifacts / "context-packets" / "0001.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["sources"][0]["markdown"] = "# A\ntampered\n"
        self.store._atomic_write(record_path, record)
        with self.assertRaises(WorkflowError):
            workflow._load_context_packet(self.artifacts)

    def test_context_expansion_crash_replays_without_recalling_resolver_or_consuming_another_expansion(self):
        source = self.root / "context.md"
        source.write_text("# A\ncontext\n", encoding="utf-8")
        replacement = self.root / "replacement.md"
        replacement.write_text("# B\nreplacement\n", encoding="utf-8")
        packet = build_packet([SourceRef(source, "A", "initial")], output_dir=self.artifacts)
        codex = FakeCodex([review("CONTEXT_REQUEST", context_requests=["need B"]), review("PASS")])
        resolver_calls = []
        crashing = self.workflow(
            codex,
            FakeClaude(),
            context_packet=packet,
            context_resolver=lambda requests: (resolver_calls.append(tuple(requests)) or [SourceRef(replacement, "B", requests[0], priority=1)]),
            fault_injector=lambda point: (_ for _ in ()).throw(KeyboardInterrupt()) if point == "after_context_expansion" else None,
        )
        with self.assertRaises(KeyboardInterrupt):
            crashing.run(self.run.run_id)

        result = self.workflow(codex, FakeClaude()).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(resolver_calls, [("need B",)])
        manifest = json.loads((self.artifacts / "context-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["expansion_count"], 1)

    def test_answer_records_decision_then_atomically_updates_plan_before_codex_resumes(self):
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["Should it persist?"]),
            review("PASS"),
        ])
        original = self.plan.read_text(encoding="utf-8")
        updated = "# Plan\nUpdated after user answer\n"
        claude = FakeClaude(updates=[plan_update(original, updated, {"Q-001": "Yes, for the app process."})])
        workflow = self.workflow(codex, claude)
        workflow.run(self.run.run_id)

        result = workflow.answer(self.run.run_id, {"Q-001": "Yes, for the app process."})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(len(claude.update_calls), 1)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), updated)
        self.assertEqual(claude.update_calls[0]["answer_digest"], submission_digest({"Q-001": "Yes, for the app process."}))
        decision_log = (self.artifacts / "decision-log.md").read_text(encoding="utf-8")
        self.assertIn("## Q-001", decision_log)
        self.assertIn("Decision impact: pending Claude Plan update", decision_log)

    def test_answer_with_unchanged_plan_pauses_without_re_review(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["Should it persist?"])])
        original = self.plan.read_text(encoding="utf-8")
        claude = FakeClaude(updates=[plan_update(original, original, {"Q-001": "Yes."})])
        workflow = self.workflow(codex, claude)
        workflow.run(self.run.run_id)

        result = workflow.answer(self.run.run_id, {"Q-001": "Yes."})

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(json.loads((self.artifacts / "pause.json").read_text())["reason"], "INVALID_USER_ANSWER_OR_PLAN_UPDATE")

    def test_answer_with_missing_plan_update_pauses_without_re_review(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["Should it persist?"])])
        claude = FakeClaude(updates=[{"summary": "missing plan"}])
        workflow = self.workflow(codex, claude)
        workflow.run(self.run.run_id)

        result = workflow.answer(self.run.run_id, {"Q-001": "Yes."})

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(len(codex.calls), 1)
        self.assertTrue((self.artifacts / "answer-updates" / "0001.json").exists())

    def test_answer_update_requires_matching_submission_digest_before_plan_write(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["Should it persist?"])])
        original = self.plan.read_text(encoding="utf-8")
        update = plan_update(original, "# Plan\nWould be unsafe\n", {"Q-001": "Answer A"})
        update["answer_digest"] = submission_digest({"Q-001": "Answer B"})
        claude = FakeClaude(updates=[update])
        workflow = self.workflow(codex, claude)
        workflow.run(self.run.run_id)

        result = workflow.answer(self.run.run_id, {"Q-001": "Answer A"})

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), original)
        self.assertFalse((self.artifacts / "plan-update-actions" / "0001.json").exists())

    def test_legacy_raw_update_without_submission_digest_is_not_reused(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["Should it persist?"])])
        original = self.plan.read_text(encoding="utf-8")
        workflow = self.workflow(codex, FakeClaude())
        workflow.run(self.run.run_id)
        legacy = plan_update(original, "# Plan\nLegacy\n", {"Q-001": "Answer A"})
        del legacy["answer_digest"]
        (self.artifacts / "answer-updates").mkdir()
        (self.artifacts / "answer-updates" / "0001.json").write_text(json.dumps(legacy), encoding="utf-8")

        result = workflow.answer(self.run.run_id, {"Q-001": "Answer A"})

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), original)

    def test_answer_with_digest_mismatch_pauses_without_re_review(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["Should it persist?"])])
        original = self.plan.read_text(encoding="utf-8")
        update = plan_update(original, "# Plan\nUpdated\n", {"Q-001": "Yes."})
        update["plan"]["new_digest"] = "0" * 64
        claude = FakeClaude(updates=[update])
        workflow = self.workflow(codex, claude)
        workflow.run(self.run.run_id)

        result = workflow.answer(self.run.run_id, {"Q-001": "Yes."})

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(len(codex.calls), 1)

    def test_answer_crash_rejects_different_retry_and_replays_original_without_recalling_claude(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["Should it persist?"]), review("PASS")])
        original = self.plan.read_text(encoding="utf-8")
        updated = "# Plan\nRecovered update\n"
        crashing = self.workflow(
            codex,
            FakeClaude(updates=[plan_update(original, updated, {"Q-001": "Answer A"})]),
            fault_injector=lambda point: (_ for _ in ()).throw(KeyboardInterrupt()) if point == "after_plan_persist" else None,
        )
        crashing.run(self.run.run_id)
        with self.assertRaises(KeyboardInterrupt):
            crashing.answer(self.run.run_id, {"Q-001": "Answer A"})

        tracked = [
            self.artifacts / "answer-submission.json",
            self.artifacts / "user-answers.json",
            self.artifacts / "decision-log.md",
            self.artifacts / "answer-updates" / "0001.json",
            self.plan,
        ]
        before = {path: path.read_bytes() for path in tracked}
        with self.assertRaises(WorkflowError):
            self.workflow(codex, FakeClaude()).answer(self.run.run_id, {"Q-001": "Answer B"})
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(self.store.load(self.run.run_id).status, Status.AWAITING_USER_INPUT)

        resumed_claude = FakeClaude()
        result = self.workflow(codex, resumed_claude).answer(self.run.run_id, {"Q-001": "Answer A"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(self.plan.read_text(encoding="utf-8"), updated)
        self.assertEqual(resumed_claude.update_calls, [])
        action = json.loads((self.artifacts / "plan-update-actions" / "0001.json").read_text())
        self.assertEqual(action["answers"], {"Q-001": "Answer A"})
        self.assertEqual(action["answer_digest"], json.loads((self.artifacts / "answer-submission.json").read_text())["digest"])

    def test_answer_submission_normalizes_order_and_rejects_extra_or_missing_questions(self):
        codex = FakeCodex([review("NEEDS_USER_INPUT", questions=["One?", "Two?"]), review("PASS")])
        original = self.plan.read_text(encoding="utf-8")
        updated = "# Plan\nTwo answers\n"
        claude = FakeClaude(updates=[plan_update(original, updated, {"Q-001": "one", "Q-002": "two"})])
        workflow = self.workflow(codex, claude)
        workflow.run(self.run.run_id)

        with self.assertRaises(KeyboardInterrupt):
            self.workflow(
                codex,
                claude,
                fault_injector=lambda point: (_ for _ in ()).throw(KeyboardInterrupt()) if point == "after_answer_submission" else None,
            ).answer(self.run.run_id, {"Q-002": "two", "Q-001": "one"})
        result = workflow.answer(self.run.run_id, {"Q-001": "one", "Q-002": "two"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(claude.update_calls[0]["answers"], {"Q-001": "one", "Q-002": "two"})

    def test_answer_validation_rejects_missing_or_extra_question_ids(self):
        workflow = self.workflow(FakeCodex([]), FakeClaude())
        questions = [{"id": "Q-001", "question": "One?"}, {"id": "Q-002", "question": "Two?"}]

        with self.assertRaises(WorkflowError):
            workflow._validate_answers(questions, {"Q-001": "one"})
        with self.assertRaises(WorkflowError):
            workflow._validate_answers(questions, {"Q-001": "one", "Q-002": "two", "Q-003": "extra"})

    def test_six_changes_pause_at_policy_limit(self):
        codex = FakeCodex([
            review(
                "CHANGES_REQUIRED",
                findings=[finding("F-%s" % index, lineage="existing" if index == 0 else "introduced_by_fix")],
            )
            for index in range(6)
        ])
        claude = FakeClaude([resolution("F-%s" % index) for index in range(6)])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(result.repair_round, 6)
        self.assertEqual(json.loads((self.artifacts / "pause.json").read_text())["reason"], "MAX_REPAIR_ROUNDS")

    def test_new_later_finding_without_fix_lineage_pauses_gate_moved(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[finding("F-1")]),
            review("CHANGES_REQUIRED", findings=[finding("F-2")]),
        ])
        claude = FakeClaude([resolution("F-1")])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(json.loads((self.artifacts / "pause.json").read_text())["reason"], "REVIEW_GATE_MOVED")
        self.assertEqual(len(claude.calls), 1)

    def test_newly_discovered_lineage_with_reason_does_not_move_the_gate(self):
        late = finding("F-2")
        late["lineage"] = {
            "resolution": "newly_discovered",
            "discovery_reason": "the first repair exposed the missing rollback section",
        }
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[finding("F-1")]),
            review("CHANGES_REQUIRED", findings=[late]),
            review("PASS"),
        ])
        claude = FakeClaude([resolution("F-1"), resolution("F-2")])

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(len(claude.calls), 2)

    def test_reviewer_receives_only_the_allowed_inputs_and_raw_output_is_saved_first(self):
        codex = FakeCodex([review("PASS")])
        result = self.workflow(codex, FakeClaude()).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(
            set(codex.calls[0]),
            {"plan", "decision_log", "context_manifest", "knowledge_packet", "policy", "unresolved_prior_findings"},
        )
        saved = json.loads((self.artifacts / "reviews" / "0001.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["verdict"], "PASS")

    def test_invalid_output_pauses_after_persisting_raw_payload(self):
        codex = FakeCodex([{"verdict": "PASS"}])

        result = self.workflow(codex, FakeClaude()).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(json.loads((self.artifacts / "reviews" / "0001.json").read_text()), {"verdict": "PASS"})
        self.assertEqual(json.loads((self.artifacts / "pause.json").read_text())["reason"], "INVALID_CODEX_REVIEW")

    def test_resume_applies_journaled_change_without_recalling_claude(self):
        (self.artifacts / "reviews").mkdir()
        (self.artifacts / "resolutions").mkdir()
        (self.artifacts / "review-actions").mkdir()
        (self.artifacts / "reviews" / "0001.json").write_text(
            json.dumps(review("CHANGES_REQUIRED", findings=[finding("F-1")])), encoding="utf-8"
        )
        (self.artifacts / "resolutions" / "0001.json").write_text(
            json.dumps(resolution("F-1")), encoding="utf-8"
        )
        (self.artifacts / "review-actions" / "0001.json").write_text(
            json.dumps({"action": "changes", "before_round": 0}), encoding="utf-8"
        )
        codex = FakeCodex([review("PASS")])
        claude = FakeClaude()

        result = self.workflow(codex, claude).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertEqual(result.repair_round, 1)
        self.assertEqual(claude.calls, [])


if __name__ == "__main__":
    unittest.main()
