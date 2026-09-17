import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ai_review.context import SourceRef
from ai_review.models import ApprovalAuthority, HumanApprovalReceipt, RunState, Status
from ai_review.policy import Policy
from ai_review.runners import RunnerInterrupted, VerificationResult
from ai_review.store import RunStore, project_id
from ai_review.workflow import CodeWorkflow


def blocker(identifier, *, lineage="existing"):
    return {
        "id": identifier, "severity": "blocker", "invariant": "Code is safe",
        "location": "Feature.swift:1", "evidence": "unsafe", "required_outcome": "fix",
        "lineage": {"resolution": lineage},
    }


def review(verdict, *, findings=(), questions=(), context_requests=()):
    return {
        "verdict": verdict, "summary": "review result", "findings": list(findings),
        "questions": list(questions), "context_requests": list(context_requests),
    }


def verification(exit_code=0, stdout="build output", stderr=""):
    return VerificationResult(("verify", "task"), exit_code, stdout, stderr)


def repair_result(*identifiers):
    return {
        "summary": "repair complete",
        "resolutions": [
            {"finding_id": identifier, "outcome": "fixed", "evidence": "fixed safely"}
            for identifier in identifiers
        ],
    }


class FakeCodex:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def call_count(self):
        return len(self.calls)

    def review(self, inputs):
        self.calls.append(inputs)
        return self.responses.pop(0)


class FakeClaude:
    def __init__(self, implementations=(), repairs=()):
        self.implementations = list(implementations)
        self.repairs = list(repairs)
        self.implement_calls = []
        self.repair_calls = []

    @property
    def call_count(self):
        return len(self.implement_calls) + len(self.repair_calls)

    def implement(self, inputs):
        self.implement_calls.append(inputs)
        return self.implementations.pop(0)

    def repair(self, inputs):
        self.repair_calls.append(inputs)
        return self.repairs.pop(0)


class CodeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.plan = self.repo / "plan.md"
        self.plan.write_text("# Approved Plan\nImplement safely\n", encoding="utf-8")
        self.git("add", "plan.md")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.authority = ApprovalAuthority(self.root / "approval.key")
        self.store = RunStore(self.root / "runs", authority=self.authority)
        policy = self.policy = Policy(
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
            claude_model="opus[1m]",
            claude_fallback_model="sonnet",
            claude_max_budget_usd=5,
        )
        plan = self.store.create(RunState.new(
            "plan", str(self.plan), str(self.repo), self.base,
            verification_commands=[{"kind": "test", "argv": ["python3", "-m", "unittest", "tests.task"], "scope": "task"}],
        ))
        plan.transition("PASS", policy.max_rounds)
        plan.approve_plan(
            receipt=HumanApprovalReceipt(
                run_id=plan.run_id, plan_digest=hashlib.sha256(self.plan.read_bytes()).hexdigest(), base_oid=plan.manifest.base_oid,
                approved_at="2026-07-31T00:00:00+00:00", provider="test:held-capability", actor="test-human",
            ),
            authority=self.authority,
        )
        self.store.save(plan)
        self.run = self.store.create(RunState.new_code_from_approved_plan(plan, self.authority))
        self.diff_stats = lambda: 100

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True)

    @property
    def artifacts(self):
        return self.store.root / project_id(self.repo) / self.run.run_id

    def workflow(self, *, codex, claude, verification, **kwargs):
        return CodeWorkflow(
            self.store, codex, claude, policy=self.policy, verification_runner=verification,
            diff_stats=self.diff_stats, **kwargs,
        )

    def test_code_pass_requires_green_verification(self):
        workflow = self.workflow(
            codex=FakeCodex([review("PASS")]), claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(exit_code=65),
        )

        result = workflow.run(self.run.run_id)

        self.assertNotEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)

    def test_verification_runner_interruption_pauses_then_propagates(self):
        def interrupted(*_args, **_kwargs):
            raise RunnerInterrupted("verification timed out")

        with self.assertRaises(RunnerInterrupted):
            self.workflow(
                codex=FakeCodex([]), claude=FakeClaude([{"summary": "initial"}]), verification=interrupted,
            ).run(self.run.run_id)

        self.assertEqual(self.store.load(self.run.run_id).status, Status.INTERRUPTED)
        self.assertEqual(
            json.loads((self.artifacts / "interruption.json").read_text())["reason"],
            "RUNNER_INTERRUPTED",
        )

    def test_first_green_pass_stops(self):
        codex = FakeCodex([review("PASS"), review("CHANGES_REQUIRED", findings=[blocker("later")])])
        workflow = self.workflow(
            codex=codex, claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        )

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(codex.call_count, 1)

    def test_code_question_answer_is_immutable_then_resumes_review(self):
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["Choose behavior?"]),
            review("PASS"),
        ])
        workflow = self.workflow(
            codex=codex, claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        )
        paused = workflow.run(self.run.run_id)
        self.assertEqual(paused.status, Status.AWAITING_USER_INPUT)

        result = workflow.answer(self.run.run_id, {"Q-001": "Keep local"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        answer = json.loads(
            (self.artifacts / "question-cycles" / "0001" / "answers.json").read_text()
        )
        self.assertEqual(answer["answers"], {"Q-001": "Keep local"})
        self.assertEqual(codex.calls[-1]["user_decisions"][-1]["answer"], "Keep local")

    def test_code_answer_interruption_persists_pause_and_reraises(self):
        calls = 0

        def verify(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RunnerInterrupted("timed out after answer")
            return verification(exit_code=0)

        workflow = self.workflow(
            codex=FakeCodex([
                review("NEEDS_USER_INPUT", questions=["Choose?"]),
            ]),
            claude=FakeClaude([{"summary": "initial"}]),
            verification=verify,
        )
        self.assertEqual(
            workflow.run(self.run.run_id).status, Status.AWAITING_USER_INPUT
        )

        with self.assertRaises(RunnerInterrupted):
            workflow.answer(self.run.run_id, {"Q-001": "Local"})

        persisted = self.store.load(self.run.run_id)
        self.assertEqual(persisted.status, Status.INTERRUPTED)
        self.assertEqual(
            json.loads((self.artifacts / "interruption.json").read_text())["reason"],
            "RUNNER_INTERRUPTED",
        )

    def test_seventh_repair_is_never_started(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("F-%s" % i, lineage="existing" if i == 0 else "introduced_by_fix")])
            for i in range(7)
        ])
        claude = FakeClaude(
            implementations=[{"summary": "initial"}], repairs=[repair_result("F-%s" % i) for i in range(6)],
        )
        workflow = self.workflow(
            codex=codex, claude=claude,
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        )

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(claude.call_count, 7)

    def test_scope_growth_pauses_before_next_model_call(self):
        values = iter([100, 131])
        self.diff_stats = lambda: next(values)
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("F-1")])])
        claude = FakeClaude(implementations=[{"summary": "initial"}], repairs=[repair_result("F-1")])
        workflow = self.workflow(
            codex=codex, claude=claude,
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        )

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.pause_reason, "SCOPE_EXPANSION")
        self.assertEqual(claude.call_count, 1)

    def test_questions_pause_without_repair_or_claude_answer(self):
        claude = FakeClaude(implementations=[{"summary": "initial"}])
        result = self.workflow(
            codex=FakeCodex([review("NEEDS_USER_INPUT", questions=["Choose persistence?"])]),
            claude=claude, verification=lambda *_args, **_kwargs: verification(),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_USER_INPUT)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(claude.call_count, 1)
        self.assertTrue((self.artifacts / "user-questions.json").exists())

    def test_verification_logs_are_persisted_but_prompts_are_bounded(self):
        stdout = "\n".join("line %s" % index for index in range(250))
        codex = FakeCodex([review("PASS")])
        result = self.workflow(
            codex=codex, claude=FakeClaude(implementations=[{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(stdout=stdout),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        record = json.loads((self.artifacts / "verification" / "0001-test.json").read_text())
        self.assertEqual(record["exit_code"], 0)
        self.assertTrue(Path(record["stdout_path"]).is_file())
        evidence = codex.calls[0]["verification"]
        self.assertEqual(len(evidence[0]["relevant_output"].splitlines()), 200)
        self.assertNotIn("stdout_path", evidence[0])
        self.assertNotIn("stderr_path", evidence[0])
        # The reviewer is pointed at the frozen patch, so the artifact it will
        # open is what must be free of the run store's own verification logs.
        self.assertNotIn(b"verification/", Path(codex.calls[0]["patch_path"]).read_bytes())
        self.assertFalse((self.repo / ".ai-review").exists())

    def test_store_rejects_artifacts_inside_target_repository(self):
        unsafe = RunStore(self.repo / ".ai-review" / "runs", authority=self.authority)
        plan = RunState.new(
            "plan", str(self.plan), str(self.repo), self.base,
            verification_commands=[{"kind": "test", "argv": ["python3", "-m", "unittest", "tests.task"], "scope": "task"}],
        )

        with self.assertRaises(ValueError):
            unsafe.create(plan)

    def test_plan_mutation_after_human_approval_rejects_code_creation(self):
        plan = self.store.load(self.run.run_id)  # Code state is irrelevant; load approved Plan below.
        runs = list(self.store.root.glob("*/**/state.json"))
        approved_plan = next(
            self.store.load(path.parent.name) for path in runs
            if self.store.load(path.parent.name).kind == "plan"
        )
        self.plan.write_text("# Changed after approval\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            RunState.new_code_from_approved_plan(approved_plan, self.authority)

    def test_branch_advance_does_not_change_the_signed_base_oid(self):
        self.git("commit", "--allow-empty", "-m", "advance HEAD")
        codex = FakeCodex([review("PASS")])
        result = self.workflow(
            codex=codex, claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(codex.calls[0]["base_oid"], self.base)

    def test_a_claude_repair_is_never_replayed_even_with_a_matching_digest(self):
        """The digest cannot see gitignored files, and Claude may write them.

        `capture_diff_bytes` builds on `ls-files --others --exclude-standard`,
        so an edit to an ignored path leaves the digest unchanged.  A repair
        that died after writing one would be repeated, applying the change
        twice while the counter recorded it once.  Read-only calls have no such
        exposure, so only they are replayable.
        """
        repo = self.repo
        (repo / ".gitignore").write_text("runtime.json\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore runtime")

        class WritesIgnoredThenDies(FakeClaude):
            def repair(self, inputs):
                (repo / "runtime.json").write_text('{"written": true}\n', encoding="utf-8")
                raise KeyboardInterrupt()

        crashing = WritesIgnoredThenDies(implementations=[{"summary": "initial"}])
        with self.assertRaises(KeyboardInterrupt):
            self.workflow(
                codex=FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])]),
                claude=crashing,
                verification=lambda *_a, **_k: verification(exit_code=0),
            ).run(self.run.run_id)

        resumed = FakeClaude(repairs=[repair_result("CODE-001")])
        result = self.workflow(
            codex=FakeCodex([review("PASS")]), claude=resumed,
            verification=lambda *_a, **_k: verification(exit_code=0),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(result.pause_reason, "AMBIGUOUS_EXTERNAL_CALL")
        self.assertEqual(len(resumed.repair_calls), 0)

    def test_a_resumed_round_reuses_its_verification_instead_of_renumbering(self):
        """Regression: the two counters advanced independently across a resume.

        Verification 0001 ran, Codex was interrupted before writing its review,
        and the resume produced verification 0002 while retrying review 0001 —
        so the round was judged against one snapshot and recorded against
        another.
        """
        rounds = []

        def verify(*_args, **_kwargs):
            rounds.append(1)
            return verification(exit_code=0)

        class CrashingCodex(FakeCodex):
            def review(self, inputs):
                super().review(inputs)
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.workflow(
                codex=CrashingCodex([review("PASS")]),
                claude=FakeClaude(implementations=[{"summary": "initial"}]),
                verification=verify,
            ).run(self.run.run_id)
        self.assertEqual(len(rounds), 1)

        result = self.workflow(
            codex=FakeCodex([review("PASS")]), claude=FakeClaude(), verification=verify,
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        # One round, one snapshot: the resume reused it rather than re-running
        # the commands and filing the answer under a new number.
        self.assertEqual(
            sorted(path.name for path in (self.artifacts / "verification-rounds").iterdir()),
            ["0001.json"],
        )
        self.assertEqual(
            sorted(path.name for path in (self.artifacts / "reviews").iterdir()),
            ["0001.json"],
        )
        self.assertEqual(len(rounds), 1)

    def test_an_overloaded_repair_interrupts_and_still_refuses_to_replay(self):
        """An interruption preserves the run; it does not make a repair repeatable.

        `Status.INTERRUPTED` keeps every completed round and its evidence valid
        instead of voiding the run, which is what a terminal pause used to do.
        It does not license repeating the mutating call that was cut short: the
        patch digest cannot see a gitignored write, so the repair round still
        fails closed and a person picks it up.
        """
        from ai_review.runners import ExternalEnvironmentError

        class OverloadedOnce(FakeClaude):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.failed = False

            def repair(self, inputs):
                if not self.failed:
                    self.failed = True
                    raise ExternalEnvironmentError("EXTERNAL_OVERLOADED exit=1")
                return super().repair(inputs)

        claude = OverloadedOnce(
            implementations=[{"summary": "initial"}],
            repairs=[{
                "summary": "repaired",
                "resolutions": [{
                    "finding_id": "CODE-001", "outcome": "fixed", "evidence": "restored",
                }],
            }],
        )
        workflow = self.workflow(
            codex=FakeCodex([
                review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
            ]),
            claude=claude,
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        )

        with self.assertRaises(ExternalEnvironmentError):
            workflow.run(self.run.run_id)
        interrupted = self.store.load(self.run.run_id)

        # Interrupted, not voided: the round that finished is still on disk and
        # the failure is recorded as an environment problem, not a workflow one.
        self.assertEqual(interrupted.status, Status.INTERRUPTED)
        self.assertEqual(
            json.loads((self.artifacts / "interruption.json").read_text())["reason"],
            "RUNNER_INTERRUPTED",
        )
        self.assertTrue((self.artifacts / "reviews" / "0001.json").exists())

        resumed = workflow.run(self.run.run_id)

        # The mutating call is never repeated on the strength of a digest that
        # cannot see every file it could have written.
        self.assertEqual(resumed.status, Status.PAUSED)
        self.assertEqual(resumed.pause_reason, "AMBIGUOUS_EXTERNAL_CALL")
        self.assertEqual(len(claude.implement_calls), 1)

    def test_a_codex_intent_replays_only_while_the_worktree_is_unchanged(self):
        """Codex reads; replaying it is safe exactly while nothing has moved.

        The intent records the worktree digest it was written under.  Equal means
        the interrupted call left nothing behind and the review can be asked for
        again; different means something happened that this run cannot account
        for, and it stays ambiguous.
        """
        class CrashingCodex(FakeCodex):
            def review(self, inputs):
                super().review(inputs)
                raise KeyboardInterrupt()

        crashing = self.workflow(
            codex=CrashingCodex([review("PASS")]), claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(),
        )
        with self.assertRaises(KeyboardInterrupt):
            crashing.run(self.run.run_id)
        # The worktree is untouched, so the read-only review is asked again.
        replacement = FakeCodex([review("PASS")])
        result = self.workflow(
            codex=replacement, claude=FakeClaude(), verification=lambda *_args, **_kwargs: verification(),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(replacement.call_count, 1)

    def test_an_intent_written_before_the_digest_existed_still_fails_closed(self):
        class CrashingCodex(FakeCodex):
            def review(self, inputs):
                super().review(inputs)
                raise KeyboardInterrupt()

        crashing = self.workflow(
            codex=CrashingCodex([review("PASS")]), claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(),
        )
        with self.assertRaises(KeyboardInterrupt):
            crashing.run(self.run.run_id)
        intent = next((self.artifacts / "codex-intents").glob("*.json"))
        record = json.loads(intent.read_text(encoding="utf-8"))
        del record["patch_digest"]
        intent.write_text(json.dumps(record), encoding="utf-8")

        replacement = FakeCodex([review("PASS")])
        result = self.workflow(
            codex=replacement, claude=FakeClaude(), verification=lambda *_args, **_kwargs: verification(),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(result.pause_reason, "AMBIGUOUS_EXTERNAL_CALL")
        self.assertEqual(replacement.call_count, 0)

    def test_a_moved_worktree_keeps_an_unfinished_intent_ambiguous(self):
        class CrashingCodex(FakeCodex):
            def review(self, inputs):
                super().review(inputs)
                raise KeyboardInterrupt()

        crashing = self.workflow(
            codex=CrashingCodex([review("PASS")]), claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(),
        )
        with self.assertRaises(KeyboardInterrupt):
            crashing.run(self.run.run_id)
        # Something edited the worktree while the call was in flight.
        (self.repo / "Sneaked.py").write_text("SNEAKED = True\n", encoding="utf-8")

        replacement = FakeCodex([review("PASS")])
        result = self.workflow(
            codex=replacement, claude=FakeClaude(), verification=lambda *_args, **_kwargs: verification(),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(result.pause_reason, "AMBIGUOUS_EXTERNAL_CALL")
        self.assertEqual(replacement.call_count, 0)

    def test_verification_intent_without_result_pauses_without_rerun(self):
        calls = []
        def runner(*_args, **_kwargs):
            calls.append(1)
            return verification()
        crashing = self.workflow(
            codex=FakeCodex([review("PASS")]), claude=FakeClaude([{"summary": "initial"}]),
            verification=runner,
            fault_injector=lambda point: (_ for _ in ()).throw(KeyboardInterrupt()) if point == "after_verification_call" else None,
        )
        with self.assertRaises(KeyboardInterrupt):
            crashing.run(self.run.run_id)
        result = self.workflow(
            codex=FakeCodex([review("PASS")]), claude=FakeClaude(), verification=runner,
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(result.pause_reason, "AMBIGUOUS_EXTERNAL_CALL")
        self.assertEqual(len(calls), 1)

    def test_rejects_non_finite_custom_verification_timeout(self):
        with self.assertRaises(ValueError):
            self.workflow(
                codex=FakeCodex([]), claude=FakeClaude(), verification=lambda *_args, **_kwargs: verification(),
                verification_timeout=float("inf"),
            )

    def test_initial_implementation_runs_once_before_the_first_verification(self):
        order = []

        class OrderedClaude(FakeClaude):
            def implement(self, inputs):
                order.append("implement")
                return super().implement(inputs)

        def verify(*_args, **_kwargs):
            order.append("verify")
            return verification(exit_code=0)

        claude = OrderedClaude(implementations=[{"summary": "initial"}])
        result = self.workflow(
            codex=FakeCodex([review("PASS")]), claude=claude, verification=verify,
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(len(claude.implement_calls), 1)
        self.assertEqual(order[:2], ["implement", "verify"])

    def test_approved_plan_binding_reaches_implementation_review_and_repair(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("F-1")]), review("PASS"),
        ])
        claude = FakeClaude(
            implementations=[{"summary": "initial"}], repairs=[repair_result("F-1")],
        )
        workflow = self.workflow(
            codex=codex, claude=claude,
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        )

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        plan_text = self.plan.read_text(encoding="utf-8")
        plan_digest = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
        manifest_digest = self.store.load(self.run.run_id).manifest.digest()
        for inputs in (claude.implement_calls[0], codex.calls[0], claude.repair_calls[0]):
            self.assertEqual(inputs["approved_plan"], plan_text)
            self.assertEqual(inputs["approved_manifest_digest"], manifest_digest)
        for inputs in (claude.implement_calls[0], codex.calls[0]):
            self.assertEqual(inputs["approved_plan_digest"], plan_digest)
        self.assertEqual(claude.repair_calls[0]["finding_ids"], ["F-1"])

    def test_code_run_without_a_valid_plan_attestation_calls_no_model(self):
        foreign = RunStore(
            self.store.root, authority=ApprovalAuthority(self.root / "foreign.key")
        )
        codex, claude = FakeCodex([review("PASS")]), FakeClaude([{"summary": "initial"}])
        workflow = CodeWorkflow(
            foreign, codex, claude, policy=self.policy, diff_stats=self.diff_stats,
            verification_runner=lambda *_args, **_kwargs: verification(),
        )

        with self.assertRaises(ValueError):
            workflow.run(self.run.run_id)

        self.assertEqual(codex.call_count, 0)
        self.assertEqual(claude.call_count, 0)

    def test_every_repair_recaptures_the_complete_patch(self):
        repo = self.repo

        class WritingClaude(FakeClaude):
            def repair(self, inputs):
                (repo / "NewProduction.swift").write_text("struct New {}\n", encoding="utf-8")
                return super().repair(inputs)

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("F-1")]), review("PASS"),
        ])
        claude = WritingClaude(
            implementations=[{"summary": "initial"}], repairs=[repair_result("F-1")],
        )
        workflow = self.workflow(
            codex=codex, claude=claude,
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        )

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        initial = (self.artifacts / "patches" / "round-0000.patch").read_bytes()
        repaired = (self.artifacts / "patches" / "round-0001.patch").read_bytes()
        self.assertNotIn(b"NewProduction.swift", initial)
        self.assertIn(b"NewProduction.swift", repaired)
        # The re-review must be pointed at the recaptured patch, not the frozen one.
        self.assertEqual(
            codex.calls[-1]["patch_path"],
            str((self.artifacts / "patches" / "round-0001.patch").resolve()),
        )
        self.assertEqual(
            codex.calls[-1]["patch_sha256"], hashlib.sha256(repaired).hexdigest()
        )

    def test_resume_replays_a_journaled_repair_without_recalling_claude(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("F-1")])])
        claude = FakeClaude(
            implementations=[{"summary": "initial"}], repairs=[repair_result("F-1")],
        )
        crashing = self.workflow(
            codex=codex, claude=claude,
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
            fault_injector=lambda point: (_ for _ in ()).throw(KeyboardInterrupt())
            if point == "after_repair_action" else None,
        )
        with self.assertRaises(KeyboardInterrupt):
            crashing.run(self.run.run_id)
        self.assertEqual(self.store.load(self.run.run_id).repair_round, 1)

        resumed_codex = FakeCodex([review("PASS")])
        resumed_claude = FakeClaude()
        result = self.workflow(
            codex=resumed_codex, claude=resumed_claude,
            verification=lambda *_args, **_kwargs: verification(exit_code=0),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 1)
        self.assertEqual(resumed_claude.call_count, 0)
        self.assertEqual(len(claude.repair_calls), 1)

    def test_code_inherits_one_shot_context_for_two_cycles(self):
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text("# First\none\n", encoding="utf-8")
        second.write_text("# Second\ntwo\n", encoding="utf-8")
        workflow = self.workflow(
            codex=FakeCodex([
                review("CONTEXT_REQUEST", context_requests=["First"]),
                review("CONTEXT_REQUEST", context_requests=["Second"]),
                review("PASS"),
            ]),
            claude=FakeClaude([{"summary": "initial"}]),
            verification=lambda *_args, **_kwargs: verification(),
        )
        self.assertEqual(workflow.run(self.run.run_id).status, Status.PAUSED)
        self.assertEqual(
            workflow.provide_context(
                self.run.run_id, [SourceRef(first, "First", "first cycle")]
            ).status,
            Status.PAUSED,
        )
        self.assertEqual(
            workflow.provide_context(
                self.run.run_id, [SourceRef(second, "Second", "second cycle")]
            ).status,
            Status.AWAITING_HUMAN_CODE_REVIEW,
        )


if __name__ == "__main__":
    unittest.main()
