import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ai_review.context import SourceRef
from ai_review.models import (
    ApprovalAuthority, ReviewApprovalReceipt, ReviewManifest, RunState, Status,
)
from ai_review.policy import Policy
from ai_review.process_security import resolve_executable
from ai_review.review_workflow import DirectReviewWorkflow
from ai_review.runners import VerificationResult
from ai_review.store import RunStore, project_id


def blocker(identifier, *, lineage="existing"):
    return {
        "id": identifier, "severity": "blocker", "invariant": "Behavior is correct",
        "location": "Feature.py:3", "evidence": "retry loses the stream",
        "required_outcome": "restore the stream", "lineage": {"resolution": lineage},
    }


def review(verdict, *, findings=(), questions=(), context_requests=()):
    return {
        "verdict": verdict, "summary": "review result", "findings": list(findings),
        "questions": list(questions), "context_requests": list(context_requests),
    }


def verification(exit_code=0, stdout="focused test output", stderr=""):
    return VerificationResult(("verify", "task"), exit_code, stdout, stderr)


def repair_result(*identifiers, risk_flags=()):
    """A Review repair result, which must always declare its risk disclosure."""
    return {
        "summary": "repair complete",
        "resolutions": [
            {"finding_id": identifier, "outcome": "fixed", "evidence": "fixed safely"}
            for identifier in identifiers
        ],
        "risk_flags": list(risk_flags),
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
    """Only ``repair`` is legal in Review mode; ``implement`` must never run."""

    def __init__(self, repairs=(), *, on_repair=None):
        self.repairs = list(repairs)
        self.implement_calls = []
        self.repair_calls = []
        self.on_repair = on_repair

    @property
    def call_count(self):
        return len(self.implement_calls) + len(self.repair_calls)

    def implement(self, inputs):
        self.implement_calls.append(inputs)
        raise AssertionError("Review mode must never request an initial implementation")

    def repair(self, inputs):
        self.repair_calls.append(inputs)
        if self.on_repair is not None:
            self.on_repair(len(self.repair_calls))
        return self.repairs.pop(0)


class ReviewWorkflowTestCase(unittest.TestCase):
    profile = "generic"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "Feature.py").write_text("def retry():\n    return 0\n", encoding="utf-8")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("add", "Feature.py")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        # The reviewed diff is pre-existing local work: one tracked edit plus one
        # untracked focused test, exactly what a finished branch looks like.
        (self.repo / "Feature.py").write_text(
            "def retry():\n    return 1\n", encoding="utf-8"
        )
        (self.repo / "tests" / "test_retry.py").write_text(
            "def test_retry():\n    assert True\n", encoding="utf-8"
        )
        self.authority = ApprovalAuthority(self.root / "approval.key")
        self.store = RunStore(self.root / "runs", authority=self.authority)
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
            claude_model="opus[1m]",
            claude_fallback_model="sonnet",
            claude_max_budget_usd=5,
        )
        self.brief = "Review the completed retry fix."
        self.run = self.create_run()
        self.diff_stats = None

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True
        )

    def manifest(self, **changes):
        identity = resolve_executable(sys.executable)
        values = {
            "kind": "review",
            "repo_path": str(self.repo),
            "base_ref": "main",
            "base_oid": self.base,
            "brief": self.brief,
            "brief_digest": hashlib.sha256(self.brief.encode("utf-8")).hexdigest(),
            "profile": self.profile,
            "initial_patch_digest": self.initial_patch_digest(),
            "verification_commands": [{
                "kind": "test", "scope": "tests.test_retry",
                "argv": [sys.executable, "-m", "unittest", "tests.test_retry"],
            }],
            "knowledge_sources": (),
            "context_checksum": None,
            "review_executables": {"codex": identity, "claude": identity},
        }
        return ReviewManifest(**dict(values, **changes))

    def initial_patch_digest(self):
        from ai_review.git_diff import capture_diff_bytes

        patch, _lines = capture_diff_bytes(self.repo, self.base)
        return hashlib.sha256(patch).hexdigest()

    def create_run(self, **changes):
        run = self.store.create(RunState.new_review(self.manifest(**changes)))
        run.approve_review(
            receipt=ReviewApprovalReceipt(
                run_id=run.run_id, manifest_digest=run.manifest.digest(),
                approved_at="2026-08-04T00:00:00+00:00",
                provider="test:held-capability", actor="test-human",
            ),
            authority=self.authority,
        )
        self.store.save(run)
        return run

    @property
    def artifacts(self):
        return self.store.root / project_id(self.repo) / self.run.run_id

    def workflow(self, *, codex, claude, verification, **kwargs):
        return DirectReviewWorkflow(
            self.store, codex, claude, policy=self.policy,
            verification_runner=verification, diff_stats=self.diff_stats, **kwargs,
        )

    def green(self, *_args, **_kwargs):
        return verification(exit_code=0)


class GenericDirectReviewTests(ReviewWorkflowTestCase):
    def test_codex_reviews_first_and_claude_never_implements(self):
        order = []

        def verify(*_args, **_kwargs):
            order.append("verify")
            return verification(exit_code=0)

        class OrderedCodex(FakeCodex):
            def review(self, inputs):
                order.append("codex")
                return super().review(inputs)

        codex = OrderedCodex([review("PASS")])
        claude = FakeClaude()
        result = self.workflow(codex=codex, claude=claude, verification=verify).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(order, ["verify", "codex"])
        self.assertEqual(claude.call_count, 0)
        self.assertEqual(claude.implement_calls, [])

    def test_initial_pass_reaches_human_code_review_without_calling_claude(self):
        codex = FakeCodex([review("PASS")])
        claude = FakeClaude()

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(codex.call_count, 1)
        self.assertEqual(claude.call_count, 0)
        action = json.loads(
            (self.artifacts / "code-review-actions" / "0001.json").read_text()
        )
        self.assertEqual(action["action"], "pass")

    def test_review_input_carries_the_brief_and_full_patch_not_a_plan(self):
        codex = FakeCodex([review("PASS")])

        self.workflow(codex=codex, claude=FakeClaude(), verification=self.green).run(
            self.run.run_id
        )

        inputs = codex.calls[0]
        self.assertEqual(inputs["review_brief"], self.brief)
        self.assertEqual(inputs["profile"], self.profile)
        self.assertEqual(inputs["base_oid"], self.base)
        self.assertEqual(
            inputs["review_manifest_digest"], self.run.manifest.digest()
        )
        # Codex is pointed at the frozen patch rather than handed its bytes, so
        # the contract to test is that the pointer resolves, proves itself
        # against the digest that travels with it, and holds the whole change.
        reviewed = Path(inputs["patch_path"]).read_bytes()
        self.assertEqual(hashlib.sha256(reviewed).hexdigest(), inputs["patch_sha256"])
        self.assertEqual(len(reviewed), inputs["patch_bytes"])
        self.assertIn(b"Feature.py", reviewed)
        self.assertIn(b"tests/test_retry.py", reviewed)
        self.assertNotIn("patch", inputs)
        for forbidden in ("approved_plan", "approved_plan_digest", "approved_manifest_digest"):
            self.assertNotIn(forbidden, inputs)

    def test_findings_lead_to_one_repair_recapture_verification_and_re_review(self):
        calls = []

        def verify(*_args, **_kwargs):
            calls.append(1)
            return verification(exit_code=0)

        repo = self.repo

        def write_fix(_round):
            (repo / "Retry.py").write_text("RETRY = True\n", encoding="utf-8")

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")], on_repair=write_fix)

        result = self.workflow(codex=codex, claude=claude, verification=verify).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 1)
        self.assertEqual(len(claude.repair_calls), 1)
        self.assertEqual(codex.call_count, 2)
        self.assertEqual(len(calls), 2)
        repaired = (self.artifacts / "patches" / "round-0001.patch").read_bytes()
        self.assertIn(b"Retry.py", repaired)
        # The re-review must be pointed at the recaptured patch, not the one
        # frozen before the repair.
        self.assertEqual(
            codex.calls[1]["patch_path"],
            str((self.artifacts / "patches" / "round-0001.patch").resolve()),
        )
        self.assertIn(b"Retry.py", Path(codex.calls[1]["patch_path"]).read_bytes())

    def test_repair_input_uses_the_review_brief_and_never_a_plan(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")])

        self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        inputs = claude.repair_calls[0]
        self.assertEqual(inputs["review_brief"], self.brief)
        self.assertEqual(inputs["review_manifest_digest"], self.run.manifest.digest())
        self.assertEqual(inputs["finding_ids"], ["CODE-001"])
        self.assertNotIn("approved_plan", inputs)
        self.assertNotIn("approved_manifest_digest", inputs)

    def test_red_verification_blocks_pass_and_becomes_repairable_evidence(self):
        codex = FakeCodex([review("PASS"), review("PASS")])
        claude = FakeClaude(repairs=[repair_result()])
        results = iter([verification(exit_code=1), verification(exit_code=0)])

        result = self.workflow(
            codex=codex, claude=claude, verification=lambda *_a, **_k: next(results),
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 1)
        self.assertEqual(len(claude.repair_calls), 1)
        self.assertEqual(claude.repair_calls[0]["findings"], [])
        self.assertEqual(claude.repair_calls[0]["verification"][0]["exit_code"], 1)

    def test_questions_pause_and_the_exact_answer_digest_resumes_the_loop(self):
        codex = FakeCodex([
            review("NEEDS_USER_INPUT", questions=["Should retry be capped?"]),
            review("PASS"),
        ])
        claude = FakeClaude()
        workflow = self.workflow(codex=codex, claude=claude, verification=self.green)

        paused = workflow.run(self.run.run_id)
        self.assertEqual(paused.status, Status.AWAITING_USER_INPUT)
        self.assertEqual(claude.call_count, 0)

        result = workflow.answer(self.run.run_id, {"Q-001": "Cap at three"})

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        submission = json.loads(
            (self.artifacts / "question-cycles" / "0001" / "answers.json").read_text()
        )
        self.assertEqual(submission["answers"], {"Q-001": "Cap at three"})
        self.assertEqual(
            submission["digest"],
            hashlib.sha256(
                json.dumps({"Q-001": "Cap at three"}, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(codex.calls[-1]["user_decisions"][-1]["answer"], "Cap at three")

    def test_context_requests_obey_two_expansions_and_the_token_budget(self):
        """The initial packet bootstraps; only later packets consume expansions."""
        names = ("First", "Second", "Third", "Fourth")
        paths = {}
        for name in names:
            path = self.root / (name.lower() + ".md")
            path.write_text("# %s\ndetail\n" % name, encoding="utf-8")
            paths[name] = path
        codex = FakeCodex([
            review("CONTEXT_REQUEST", context_requests=[name]) for name in names
        ])
        workflow = self.workflow(
            codex=codex, claude=FakeClaude(), verification=self.green,
        )

        self.assertEqual(workflow.run(self.run.run_id).status, Status.PAUSED)
        reasons = []
        for name in names:
            state = workflow.provide_context(
                self.run.run_id, [SourceRef(paths[name], name, name.lower())]
            )
            self.assertEqual(state.status, Status.PAUSED)
            reasons.append(json.loads((self.artifacts / "pause.json").read_text())["reason"])

        self.assertEqual(reasons[:3], ["CONTEXT_INPUT_REQUIRED"] * 3)
        self.assertEqual(reasons[3], "MAX_CONTEXT_EXPANSIONS")
        packet = json.loads((self.artifacts / "context-reference.json").read_text())
        self.assertEqual(packet["revision"], self.policy.max_context_expansions + 1)
        manifest = json.loads((self.artifacts / "context-manifest.json").read_text())
        self.assertLessEqual(manifest["estimated_tokens"], self.policy.max_context_tokens)
        self.assertLessEqual(len(manifest["selected"]), 3)
        self.assertIs(manifest["advisory_only"], True)

    def test_six_repairs_are_allowed_and_a_seventh_never_starts(self):
        codex = FakeCodex([
            review(
                "CHANGES_REQUIRED",
                findings=[blocker(
                    "F-%d" % index,
                    lineage="existing" if index == 0 else "introduced_by_fix",
                )],
            )
            for index in range(7)
        ])
        claude = FakeClaude(repairs=[repair_result("F-%d" % index) for index in range(6)])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(result.repair_round, 6)
        self.assertEqual(len(claude.repair_calls), 6)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "MAX_REPAIR_ROUNDS",
        )
        self.assertFalse(
            (self.artifacts / "claude-intents" / "repair-0007.json").exists()
        )

    def test_an_unapproved_review_never_reaches_a_model(self):
        run = self.store.create(RunState.new_review(self.manifest()))
        codex, claude = FakeCodex([review("PASS")]), FakeClaude()

        result = self.workflow(
            codex=codex, claude=claude, verification=self.green,
        ).run(run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(codex.call_count, 0)
        self.assertEqual(claude.call_count, 0)

    def test_a_moved_base_commit_fails_closed_before_any_model_call(self):
        codex, claude = FakeCodex([review("PASS")]), FakeClaude()
        run = self.create_run(base_oid="0" * 40)

        workflow = self.workflow(codex=codex, claude=claude, verification=self.green)
        result = workflow.run(run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(codex.call_count, 0)
        self.assertEqual(claude.call_count, 0)

    def test_bindings_are_revalidated_around_every_external_model_call(self):
        events = []

        class WatchingWorkflow(DirectReviewWorkflow):
            def _validate_state(self, state):
                events.append("validate")
                return super()._validate_state(state)

        class WatchingCodex(FakeCodex):
            def review(self, inputs):
                events.append("codex")
                return super().review(inputs)

        class WatchingClaude(FakeClaude):
            def repair(self, inputs):
                events.append("claude")
                return super().repair(inputs)

        codex = WatchingCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = WatchingClaude(repairs=[repair_result("CODE-001")])
        workflow = WatchingWorkflow(
            self.store, codex, claude, policy=self.policy,
            verification_runner=self.green, diff_stats=self.diff_stats,
        )

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        for index, event in enumerate(events):
            if event in ("codex", "claude"):
                self.assertEqual(events[index - 1], "validate", events)
            if event == "claude":
                # The mutating call is also revalidated the moment it returns.
                self.assertEqual(events[index + 1], "validate", events)
        self.assertEqual(events.count("codex"), 2)
        self.assertEqual(events.count("claude"), 1)

    def test_review_state_exposes_no_plan_path_or_plan_text(self):
        state = self.store.load(self.run.run_id)
        workflow = self.workflow(
            codex=FakeCodex([]), claude=FakeClaude(), verification=self.green,
        )

        with self.assertRaises(ValueError):
            state.plan_path
        with self.assertRaises(Exception):
            workflow._plan_text(state)

    def test_a_plan_or_code_state_is_rejected_by_the_review_workflow(self):
        plan = self.store.create(RunState.new(
            "plan", str(self.repo / "Feature.py"), str(self.repo), "main",
            verification_commands=[{
                "kind": "test", "scope": "tests.test_retry",
                "argv": [sys.executable, "-m", "unittest", "tests.test_retry"],
            }],
        ))
        codex = FakeCodex([review("PASS")])

        result = self.workflow(
            codex=codex, claude=FakeClaude(), verification=self.green,
        ).run(plan.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(codex.call_count, 0)

    def test_review_mode_has_no_production_line_or_growth_gate(self):
        """Review scope is bounded by rounds, high risk, and the human gate only."""
        big = "\n".join("LINE_%d = %d" % (index, index) for index in range(400))
        (self.repo / "Feature.py").write_text(big + "\n", encoding="utf-8")
        run = self.create_run()
        self.run = run
        repo = self.repo

        def balloon(_round):
            (repo / "Generated.py").write_text(
                "\n".join("VALUE_%d = %d" % (index, index) for index in range(400)) + "\n",
                encoding="utf-8",
            )

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")], on_repair=balloon)

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        # Neither a 400-line reviewed baseline nor a 400-line repair may pause.
        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertIsNone(getattr(result, "pause_reason", None))
        self.assertEqual(len(claude.repair_calls), 1)
        self.assertEqual(codex.call_count, 2)

    def test_a_one_line_follow_up_repair_never_pauses_on_growth(self):
        """Regression: 30% of a one-line predecessor paused a one-line repair."""
        repo = self.repo
        sizes = iter((401, 402))

        def grow(_round):
            (repo / "Feature.py").write_text(
                "\n".join("L%d = %d" % (i, i) for i in range(next(sizes))) + "\n",
                encoding="utf-8",
            )

        (repo / "Feature.py").write_text(
            "\n".join("L%d = %d" % (i, i) for i in range(400)) + "\n", encoding="utf-8"
        )
        run = self.create_run()
        self.run = run
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]),
            review("CHANGES_REQUIRED", findings=[blocker("CODE-002", lineage="introduced_by_fix")]),
            review("PASS"),
        ])
        claude = FakeClaude(
            repairs=[repair_result("CODE-001"), repair_result("CODE-002")], on_repair=grow,
        )

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 2)

    def test_the_six_repair_ceiling_is_still_the_only_round_limit(self):
        codex = FakeCodex([
            review(
                "CHANGES_REQUIRED",
                findings=[blocker(
                    "F-%d" % index, lineage="existing" if index == 0 else "introduced_by_fix",
                )],
            )
            for index in range(7)
        ])
        claude = FakeClaude(repairs=[repair_result("F-%d" % index) for index in range(6)])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "MAX_REPAIR_ROUNDS",
        )
        self.assertEqual(len(claude.repair_calls), 6)

    def test_new_later_finding_without_typed_lineage_pauses_gate_moved(self):
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]),
            review("CHANGES_REQUIRED", findings=[blocker("CODE-002")]),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "REVIEW_GATE_MOVED",
        )
        self.assertEqual(len(claude.repair_calls), 1)

    def test_newly_discovered_lineage_with_reason_does_not_move_the_gate(self):
        late = blocker("CODE-002")
        late["lineage"] = {
            "resolution": "newly_discovered",
            "discovery_reason": "the first repair exposed the failing call path",
        }
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]),
            review("CHANGES_REQUIRED", findings=[late]),
            review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001"), repair_result("CODE-002")])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 2)
        self.assertEqual(len(claude.repair_calls), 2)


class HighRiskDetectionTests(ReviewWorkflowTestCase):
    """Deterministic path and diff rules decide high risk; models only add to it."""

    def detect(self, *, model_flags=()):
        from ai_review.git_diff import capture_diff_bytes
        from ai_review.review_risk import detect_high_risk

        patch, _lines = capture_diff_bytes(self.repo, self.base)
        return detect_high_risk(
            self.repo, self.base, patch, model_flags=model_flags,
        )

    def write(self, relative, contents="changed\n"):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def test_deterministic_paths_map_to_stable_categories(self):
        from ai_review.review_risk import HIGH_RISK_CATEGORIES

        cases = (
            ("dependencies", "Package.resolved"),
            ("dependencies", "Package.swift"),
            ("dependencies", "Podfile.lock"),
            ("dependencies", "App/Podfile"),
            ("dependencies", "package-lock.json"),
            ("dependencies", "Cargo.lock"),
            ("dependencies", "go.sum"),
            ("dependencies", "requirements.txt"),
            ("migration", "Sources/Migrations/0002_add_column.swift"),
            ("migration", "Sources/Store/Model.xcdatamodeld/contents"),
            ("migration", "db/schema.sql"),
            ("entitlements_signing", "App/App.entitlements"),
            ("entitlements_signing", "profiles/Team.mobileprovision"),
            ("entitlements_signing", "App/ExportOptions.plist"),
            ("ci_cd", ".github/workflows/ci.yml"),
            ("ci_cd", "fastlane/Fastfile"),
            ("ci_cd", "Jenkinsfile"),
            ("ci_cd", "deploy/Dockerfile"),
            ("persistent_format", "Sources/Wire/message.proto"),
            ("network_format", "api/openapi.yaml"),
        )
        for category, relative in cases:
            with self.subTest(path=relative):
                self.tearDown()
                self.setUp()
                self.write(relative)
                evidence = self.detect()
                self.assertIn(
                    category, {item["category"] for item in evidence},
                    "%s did not map to %s: %s" % (relative, category, evidence),
                )
                for item in evidence:
                    self.assertIn(item["category"], HIGH_RISK_CATEGORIES)
                    self.assertEqual(set(item), {"category", "path", "reason"})

    def test_signing_keys_inside_a_project_file_are_detected_by_content(self):
        self.write(
            "App.xcodeproj/project.pbxproj",
            "objects = {\n\tCODE_SIGN_IDENTITY = \"Apple Distribution\";\n};\n",
        )

        categories = {item["category"] for item in self.detect()}

        self.assertIn("entitlements_signing", categories)

    def test_new_public_swift_declarations_are_detected_by_content(self):
        self.write("Sources/Api.swift", "public struct Token {\n    public let value: Int\n}\n")

        evidence = self.detect()

        self.assertIn("public_api", {item["category"] for item in evidence})
        self.assertTrue(
            any(item["path"] == "Sources/Api.swift" for item in evidence), evidence
        )

    def test_ordinary_implementation_and_test_changes_are_not_high_risk(self):
        self.write("Sources/Feature.swift", "struct Feature {\n    let value = 1\n}\n")
        self.write("Tests/FeatureTests/FeatureTests.swift", "func testFeature() {}\n")
        self.write("README.md", "# Notes\n")

        self.assertEqual(self.detect(), ())

    def test_model_flags_add_evidence_but_never_suppress_detection(self):
        self.write("Package.resolved")

        deterministic = self.detect()
        with_flags = self.detect(model_flags=("public_api", "network_format"))
        claimed_safe = self.detect(model_flags=())

        self.assertEqual(
            {item["category"] for item in deterministic}, {"dependencies"}
        )
        self.assertEqual(
            {item["category"] for item in with_flags},
            {"dependencies", "public_api", "network_format"},
        )
        self.assertEqual(claimed_safe, deterministic)

    def test_an_unknown_model_flag_still_fails_closed_as_high_risk(self):
        self.write("Sources/Feature.swift", "struct Feature {}\n")

        evidence = self.detect(model_flags=("something-we-cannot-classify",))

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["category"], "unclassified_model_risk")
        self.assertIn("something-we-cannot-classify", evidence[0]["reason"])

    def test_evidence_is_sorted_and_bounded_in_count_and_string_size(self):
        from ai_review.review_risk import (
            MAX_RISK_EVIDENCE, MAX_RISK_PATH_BYTES, MAX_RISK_STRING_BYTES,
        )

        for index in range(60):
            self.write("modules/module%02d/Package.resolved" % index)

        evidence = self.detect(model_flags=("é" * 900,))

        self.assertEqual(len(evidence), MAX_RISK_EVIDENCE)
        self.assertEqual(
            list(evidence),
            sorted(evidence, key=lambda item: (item["category"], item["path"], item["reason"])),
        )
        for item in evidence:
            for key in ("category", "reason"):
                self.assertLessEqual(
                    len(item[key].encode("utf-8")), MAX_RISK_STRING_BYTES
                )
            self.assertLessEqual(
                len(item["path"].encode("utf-8")), MAX_RISK_PATH_BYTES
            )

    def test_a_long_path_is_recorded_exactly_so_it_stays_a_usable_key(self):
        """A truncated path would name a different file, or none at all."""
        from ai_review.review_risk import (
            MAX_RISK_STRING_BYTES, bounded_path, is_unresolved_path,
        )

        # Legal on macOS (PATH_MAX 1024) yet far beyond the string bound.
        deep = "a/" * 300 + "Package.resolved"
        self.assertGreater(len(deep), MAX_RISK_STRING_BYTES)
        self.write(deep)

        evidence = self.detect()

        paths = {item["path"] for item in evidence}
        self.assertIn(deep, paths, "the exact path must survive intact")
        self.assertFalse(any(is_unresolved_path(path) for path in paths))
        self.assertEqual(bounded_path(deep), deep)

    def test_an_unrecordable_path_is_marked_unresolved_and_never_collides(self):
        from ai_review.review_risk import bounded_path, is_unresolved_path

        first = "a" * 5000 + "/Package.resolved"
        second = "b" * 5000 + "/Package.resolved"

        self.assertTrue(is_unresolved_path(bounded_path(first)))
        self.assertNotEqual(bounded_path(first), bounded_path(second))
        self.assertFalse(is_unresolved_path(bounded_path("Package.resolved")))

    def test_detection_reads_paths_without_shell_or_newline_parsing(self):
        self.write("weird name/Package.resolved")

        categories = {item["category"] for item in self.detect()}

        self.assertIn("dependencies", categories)


class HighRiskPauseTests(ReviewWorkflowTestCase):
    def sign_risk_approval(self, run, *, patch_digest, categories, authority=None):
        from ai_review.models import RiskApprovalReceipt, sign_risk_approval

        receipt = RiskApprovalReceipt(
            run_id=run.run_id, manifest_digest=run.manifest.digest(),
            patch_digest=patch_digest, categories=tuple(categories),
            approved_at="2026-08-04T01:00:00+00:00",
            provider="test:held-capability", actor="test-human",
        )
        return sign_risk_approval(receipt, authority or self.authority)

    def paused_on_dependency_change(self):
        repo = self.repo

        def touch_lockfile(_round):
            (repo / "Package.resolved").write_text('{"pins":[]}\n', encoding="utf-8")

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")], on_repair=touch_lockfile)
        workflow = self.workflow(codex=codex, claude=claude, verification=self.green)
        return workflow, codex, claude, workflow.run(self.run.run_id)

    def test_a_high_risk_repair_pauses_with_bound_evidence_and_no_next_model_call(self):
        workflow, codex, claude, result = self.paused_on_dependency_change()

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "HIGH_RISK_CHANGE",
        )
        self.assertEqual(codex.call_count, 1)
        self.assertEqual(len(claude.repair_calls), 1)
        assessment = json.loads(
            (self.artifacts / "risk-assessments" / "round-0001.json").read_text()
        )
        self.assertEqual(
            {item["category"] for item in assessment["evidence"]}, {"dependencies"}
        )
        request = json.loads(
            (self.artifacts / "risk-approval-request.json").read_text()
        )
        current = hashlib.sha256(
            (self.artifacts / "patches" / "round-0001.patch").read_bytes()
        ).hexdigest()
        self.assertEqual(request["patch_digest"], current)
        self.assertEqual(request["manifest_digest"], self.run.manifest.digest())
        self.assertEqual(request["categories"], ["dependencies"])
        self.assertEqual(request["status"], "pending")

    def test_the_baseline_patch_alone_records_risk_without_pausing(self):
        (self.repo / "Package.resolved").write_text('{"pins":[]}\n', encoding="utf-8")
        run = self.create_run()
        self.run = run
        codex = FakeCodex([review("PASS")])

        result = self.workflow(
            codex=codex, claude=FakeClaude(), verification=self.green,
        ).run(run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        assessment = json.loads(
            (self.artifacts / "risk-assessments" / "round-0000.json").read_text()
        )
        self.assertEqual(
            {item["category"] for item in assessment["evidence"]}, {"dependencies"}
        )
        self.assertFalse((self.artifacts / "risk-approval-request.json").exists())

    def test_resume_without_an_approval_stays_paused_and_calls_no_model(self):
        workflow, _codex, _claude, _paused = self.paused_on_dependency_change()
        codex = FakeCodex([review("PASS")])
        claude = FakeClaude()

        result = self.workflow(
            codex=codex, claude=claude, verification=self.green,
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(codex.call_count, 0)
        self.assertEqual(claude.call_count, 0)

    def test_a_signed_approval_for_the_exact_patch_resumes_the_loop(self):
        workflow, _codex, _claude, _paused = self.paused_on_dependency_change()
        digest = hashlib.sha256(
            (self.artifacts / "patches" / "round-0001.patch").read_bytes()
        ).hexdigest()
        self.store.write_artifact_bytes(
            self.artifacts / "risk-approvals" / "round-0001.json",
            json.dumps(self.sign_risk_approval(
                self.run, patch_digest=digest, categories=["dependencies"],
            )).encode("utf-8"),
        )
        codex = FakeCodex([review("PASS")])

        result = self.workflow(
            codex=codex, claude=FakeClaude(), verification=self.green,
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(codex.call_count, 1)
        request = json.loads((self.artifacts / "risk-approval-request.json").read_text())
        self.assertEqual(request["status"], "approved")

    def test_an_approval_for_a_different_patch_or_key_never_resumes(self):
        workflow, _codex, _claude, _paused = self.paused_on_dependency_change()
        digest = hashlib.sha256(
            (self.artifacts / "patches" / "round-0001.patch").read_bytes()
        ).hexdigest()
        foreign = ApprovalAuthority(self.root / "foreign.key")
        for record in (
            self.sign_risk_approval(self.run, patch_digest="f" * 64, categories=["dependencies"]),
            self.sign_risk_approval(
                self.run, patch_digest=digest, categories=["dependencies"], authority=foreign,
            ),
            dict(
                self.sign_risk_approval(
                    self.run, patch_digest=digest, categories=["dependencies"],
                ),
                signature="0" * 64,
            ),
        ):
            with self.subTest(record=record["receipt"]["patch_digest"]):
                self.store.write_artifact_bytes(
                    self.artifacts / "risk-approvals" / "round-0001.json",
                    json.dumps(record).encode("utf-8"),
                )
                codex = FakeCodex([review("PASS")])

                result = self.workflow(
                    codex=codex, claude=FakeClaude(), verification=self.green,
                ).run(self.run.run_id)

                self.assertEqual(result.status, Status.PAUSED)
                self.assertEqual(codex.call_count, 0)

    def test_a_worktree_change_after_approval_invalidates_the_resume(self):
        workflow, _codex, _claude, _paused = self.paused_on_dependency_change()
        digest = hashlib.sha256(
            (self.artifacts / "patches" / "round-0001.patch").read_bytes()
        ).hexdigest()
        self.store.write_artifact_bytes(
            self.artifacts / "risk-approvals" / "round-0001.json",
            json.dumps(self.sign_risk_approval(
                self.run, patch_digest=digest, categories=["dependencies"],
            )).encode("utf-8"),
        )
        (self.repo / "Sneaked.py").write_text("SNEAKED = True\n", encoding="utf-8")
        codex = FakeCodex([review("PASS")])

        result = self.workflow(
            codex=codex, claude=FakeClaude(), verification=self.green,
        ).run(self.run.run_id)

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(codex.call_count, 0)


SPECIALISTS = ("swiftui-reviewer", "ux-critique", "resilience-auditor")

_CATEGORY_BY_SPECIALIST = {
    "swiftui-reviewer": "swiftui",
    "ux-critique": "ux",
    "resilience-auditor": "resilience",
}


def specialist_finding(name, index=1, *, risk_flags=()):
    return {
        "id": "%s-%03d" % (name.split("-")[0].upper(), index),
        "severity": "major",
        "category": _CATEGORY_BY_SPECIALIST[name],
        "location": "Sources/Feature.swift:%d" % (40 + index),
        "evidence": "the %s lens observed a concrete defect" % name,
        "required_outcome": "restore the expected observable behavior",
        "risk_flags": list(risk_flags),
    }


def preflight_envelope(*, findings_for=(), risk_flags=()):
    """The init-time envelope: specialists only, no profile or patch digest."""
    return {
        "specialists": [
            {
                "name": name,
                "findings": (
                    [specialist_finding(name, risk_flags=risk_flags)]
                    if name in findings_for else []
                ),
            }
            for name in SPECIALISTS
        ],
    }


class PreflightSubmissionTests(unittest.TestCase):
    """The submission parser is strict, bounded, and path-safe."""

    def valid_envelope(self, **kwargs):
        return preflight_envelope(**kwargs)

    def validate(self, payload):
        from ai_review.preflight import validate_preflight

        return validate_preflight(payload)

    def test_a_complete_clean_submission_is_accepted(self):
        from ai_review.preflight import REQUIRED_SPECIALISTS

        submission = self.validate(self.valid_envelope())

        self.assertEqual(set(submission), {"specialists"})
        self.assertEqual(
            [item["name"] for item in submission["specialists"]],
            list(REQUIRED_SPECIALISTS),
        )

    def test_findings_normalize_into_the_codex_finding_shape(self):
        from ai_review.preflight import normalized_findings
        from ai_review.runners import validate_codex_review

        submission = self.validate(self.valid_envelope(findings_for=SPECIALISTS))
        findings = normalized_findings(submission)

        self.assertEqual(len(findings), 3)
        self.assertEqual(
            sorted(item["id"] for item in findings),
            ["PF-RESILIENCE-RESILIENCE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001"],
        )
        for item in findings:
            self.assertEqual(set(item), {
                "id", "severity", "invariant", "location", "evidence",
                "required_outcome", "lineage",
            })
            self.assertEqual(set(item["lineage"]), {"resolution"})
        validate_codex_review({
            "verdict": "CHANGES_REQUIRED", "summary": "merged",
            "findings": list(findings), "questions": [], "context_requests": [],
        })

    def test_duplicate_json_keys_are_rejected(self):
        from ai_review.preflight import load_preflight_text

        with self.assertRaises(ValueError):
            load_preflight_text('{"specialists":[],"specialists":[]}')

    def test_a_submission_carrying_profile_or_patch_digest_is_rejected(self):
        from ai_review.preflight import PreflightError, validate_preflight

        for extra, value in (("profile", "ios"), ("patch_digest", "0" * 64)):
            with self.subTest(extra=extra):
                payload = self.valid_envelope()
                payload[extra] = value
                with self.assertRaises(PreflightError):
                    validate_preflight(payload)

    def test_a_missing_specialist_is_rejected_not_defaulted(self):
        from ai_review.preflight import PreflightError, validate_preflight

        payload = self.valid_envelope()
        payload["specialists"] = payload["specialists"][:2]
        with self.assertRaises(PreflightError):
            validate_preflight(payload)

    def test_an_empty_findings_list_is_a_clean_audit(self):
        from ai_review.preflight import normalized_findings, validate_preflight

        payload = self.valid_envelope()
        self.assertTrue(
            all(specialist["findings"] == [] for specialist in payload["specialists"])
        )

        submission = validate_preflight(payload)

        self.assertEqual(len(submission["specialists"]), 3)
        self.assertEqual(normalized_findings(submission), ())

    def test_missing_extra_or_duplicated_specialists_are_rejected(self):
        base = self.valid_envelope()
        cases = (
            {"specialists": base["specialists"][:2]},
            {"specialists": base["specialists"] + [{"name": "swiftui-reviewer", "findings": []}]},
            {"specialists": [
                {"name": "swiftui-reviewer", "findings": []},
                {"name": "swiftui-reviewer", "findings": []},
                {"name": "ux-critique", "findings": []},
            ]},
            {"specialists": [
                dict(base["specialists"][0], name="trace-analyzer"),
                base["specialists"][1], base["specialists"][2],
            ]},
        )
        for change in cases:
            with self.subTest(change=list(change)[0]):
                with self.assertRaises(ValueError):
                    self.validate(dict(base, **change))

    def test_unknown_keys_and_wrong_enums_are_rejected(self):
        base = self.valid_envelope(findings_for=("swiftui-reviewer",))
        cases = (
            lambda value: value.update({"extra": 1}),
            lambda value: value["specialists"][0].update({"extra": 1}),
            lambda value: value["specialists"][0]["findings"][0].update({"extra": 1}),
            lambda value: value["specialists"][0]["findings"][0].update({"severity": "info"}),
            lambda value: value["specialists"][0]["findings"][0].update({"category": "perf"}),
            lambda value: value.update({"profile": "generic"}),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                value = json.loads(json.dumps(base))
                mutate(value)
                with self.assertRaises(ValueError):
                    self.validate(value)

    def test_duplicate_finding_ids_are_rejected_across_specialists(self):
        base = self.valid_envelope(findings_for=SPECIALISTS)
        for specialist in base["specialists"]:
            specialist["findings"][0]["id"] = "SAME-001"

        with self.assertRaises(ValueError):
            self.validate(base)

    def test_absolute_and_traversal_locations_are_rejected(self):
        for location in (
            "/Users/example/Sources/Feature.swift:1",
            "../outside/Feature.swift:1",
            "Sources/../../Feature.swift:1",
            "Sources/Feature.swift",
            "",
        ):
            with self.subTest(location=location):
                base = self.valid_envelope(findings_for=("ux-critique",))
                base["specialists"][1]["findings"][0]["location"] = location
                with self.assertRaises(ValueError):
                    self.validate(base)

    def test_too_many_findings_or_overlong_strings_are_rejected(self):
        from ai_review.preflight import (
            MAX_PREFLIGHT_STRING_BYTES, MAX_SPECIALIST_FINDINGS,
        )

        crowded = self.valid_envelope()
        crowded["specialists"][0]["findings"] = [
            specialist_finding("swiftui-reviewer", index)
            for index in range(MAX_SPECIALIST_FINDINGS + 1)
        ]
        with self.assertRaises(ValueError):
            self.validate(crowded)

        overlong = self.valid_envelope(findings_for=("swiftui-reviewer",))
        overlong["specialists"][0]["findings"][0]["evidence"] = "é" * (
            MAX_PREFLIGHT_STRING_BYTES
        )
        with self.assertRaises(ValueError):
            self.validate(overlong)


class IosPreflightWorkflowTests(ReviewWorkflowTestCase):
    """The specialists are frozen before the run; the loop never stops for them."""

    profile = "ios"

    def freeze_preflight(self, **kwargs):
        """Write exactly what `init review --preflight` leaves behind.

        Including the manifest digest: the artifacts alone are not the contract,
        the signed digest over them is.
        """
        from ai_review.preflight import (
            merged_source_ids, normalized_findings, validate_preflight,
        )

        submission = validate_preflight(preflight_envelope(**kwargs))
        digest = hashlib.sha256(json.dumps(
            submission, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.run = self.create_run(preflight_digest=digest)
        self.write_artifact("preflight.json", submission)
        self.write_artifact("preflight-normalized.json", {
            "findings": list(normalized_findings(submission)),
            "source_ids": merged_source_ids(submission),
        })
        return submission

    def write_artifact(self, name, contents):
        path = self.artifacts / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contents), encoding="utf-8")

    def merged_review(self, sequence=1):
        return json.loads(
            (self.artifacts / "merged-reviews" / ("%04d.json" % sequence)).read_text()
        )

    def test_ios_first_decisive_review_merges_the_frozen_specialist_findings(self):
        self.freeze_preflight(findings_for=SPECIALISTS)
        codex = FakeCodex([review("PASS"), review("PASS")])
        claude = FakeClaude(repairs=[repair_result(
            "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001", "PF-RESILIENCE-RESILIENCE-001",
        )])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        # Codex passed, but three specialist findings were already on the table,
        # so the first decisive review becomes one merged CHANGES_REQUIRED.
        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 1)
        decision = self.merged_review()
        self.assertEqual(decision["verdict"], "CHANGES_REQUIRED")
        self.assertEqual(sorted(item["id"] for item in decision["findings"]), [
            "PF-RESILIENCE-RESILIENCE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
        ])

    def test_a_legacy_run_parked_at_awaiting_preflight_never_resumes(self):
        """Regression: retiring the command must not unlock the runs it stranded.

        29 stored runs sit at AWAITING_PREFLIGHT.  Removing the pause deleted
        the early-return with it, so `resume` set them RUNNING again — and the
        `awaiting_preflight` journal made the pending-review scan skip the very
        round that had findings, reaching a human gate with zero repairs.
        """
        self.freeze_preflight(findings_for=SPECIALISTS)
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])])
        workflow = self.workflow(codex=codex, claude=FakeClaude(), verification=self.green)
        # Recreate exactly what a stranded run looks like on disk.
        self.write_artifact("code-review-actions/0001.json", {
            "action": "awaiting_preflight", "review_sequence": 1,
            "patch_digest": "a" * 64,
        })
        state_path = self.artifacts / "state.json"
        persisted = json.loads(state_path.read_text())
        persisted["status"] = "AWAITING_PREFLIGHT"
        state_path.write_text(json.dumps(persisted), encoding="utf-8")

        result = workflow.run(self.run.run_id)

        self.assertEqual(result.status, Status.AWAITING_PREFLIGHT)
        self.assertEqual(codex.call_count, 0)

    def test_the_signed_preflight_is_the_source_of_the_merged_findings(self):
        """Regression: the approved digest bound nothing that was actually used.

        `preflight_digest` was written into the manifest and never read back, so
        emptying the derived artifact after approval dropped every specialist
        finding the human had signed for.
        """
        self.freeze_preflight(findings_for=SPECIALISTS)
        # Post-approval tampering with the derived artifact only.
        self.write_artifact("preflight-normalized.json", {"findings": [], "source_ids": {}})
        codex = FakeCodex([review("PASS"), review("PASS")])
        claude = FakeClaude(repairs=[repair_result(
            "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001", "PF-RESILIENCE-RESILIENCE-001",
        )])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(sorted(item["id"] for item in self.merged_review()["findings"]), [
            "PF-RESILIENCE-RESILIENCE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
        ])

    def test_a_missing_or_altered_signed_preflight_fails_closed(self):
        for corrupt in ("delete", "alter"):
            with self.subTest(corrupt=corrupt):
                self.setUp()
                self.freeze_preflight(findings_for=SPECIALISTS)
                if corrupt == "delete":
                    (self.artifacts / "preflight.json").unlink()
                else:
                    self.write_artifact("preflight.json", preflight_envelope())
                codex = FakeCodex([review("PASS")])
                claude = FakeClaude()

                result = self.workflow(
                    codex=codex, claude=claude, verification=self.green,
                ).run(self.run.run_id)

                self.assertEqual(result.status, Status.PAUSED)
                self.assertEqual(claude.call_count, 0)

    def test_losing_the_derived_artifact_changes_nothing(self):
        """It is a cache, not the contract: the signed source is re-read."""
        self.freeze_preflight(findings_for=SPECIALISTS)
        (self.artifacts / "preflight-normalized.json").unlink()
        codex = FakeCodex([review("PASS"), review("PASS")])
        claude = FakeClaude(repairs=[repair_result(
            "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001", "PF-RESILIENCE-RESILIENCE-001",
        )])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(len(self.merged_review()["findings"]), 3)

    def test_a_run_never_enters_awaiting_preflight(self):
        self.freeze_preflight(findings_for=SPECIALISTS)
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result(
            "CODE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
            "PF-RESILIENCE-RESILIENCE-001",
        )])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertFalse((self.artifacts / "preflight-request.json").exists())
        action = json.loads(
            (self.artifacts / "code-review-actions" / "0001.json").read_text()
        )
        self.assertNotEqual(action.get("action"), "awaiting_preflight")

    def test_codex_round_one_does_not_see_specialist_findings(self):
        self.freeze_preflight(findings_for=SPECIALISTS)
        codex = FakeCodex([review("PASS"), review("PASS")])
        claude = FakeClaude(repairs=[repair_result(
            "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001", "PF-RESILIENCE-RESILIENCE-001",
        )])

        self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        # Codex reviews the code on its own terms; the specialists only ever
        # reach the repair.
        first = json.dumps(codex.calls[0])
        self.assertNotIn("PF-", first)
        self.assertNotIn("preflight", first.lower())
        self.assertNotIn("specialist", first.lower())

    def test_generic_profile_never_merges_specialist_findings(self):
        generic = GenericDirectReviewTests("test_initial_pass_reaches_human_code_review_without_calling_claude")
        generic.setUp()
        try:
            result = generic.workflow(
                codex=FakeCodex([review("PASS")]), claude=FakeClaude(),
                verification=generic.green,
            ).run(generic.run.run_id)
            self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
            self.assertFalse((generic.artifacts / "merged-reviews").exists())
        finally:
            generic.tearDown()

    def test_all_clean_codex_and_specialists_reach_human_review_without_claude(self):
        self.freeze_preflight()
        codex = FakeCodex([review("PASS")])
        claude = FakeClaude()

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 0)
        self.assertEqual(claude.call_count, 0)
        self.assertEqual(codex.call_count, 1)
        self.assertEqual(self.merged_review()["verdict"], "PASS")

    def test_combined_findings_produce_exactly_one_unified_claude_repair(self):
        self.freeze_preflight(findings_for=SPECIALISTS)
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result(
            "CODE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001", "PF-RESILIENCE-RESILIENCE-001",
        )])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 1)
        self.assertEqual(len(claude.repair_calls), 1)
        inputs = claude.repair_calls[0]
        self.assertEqual(sorted(inputs["finding_ids"]), [
            "CODE-001", "PF-RESILIENCE-RESILIENCE-001",
            "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
        ])
        self.assertEqual(
            set(inputs["repair_lenses"]), {"ios-distill", "code-simplifier", "ios-polish"}
        )
        self.assertEqual(codex.call_count, 2)

    def test_specialists_do_not_rerun_after_the_unified_repair(self):
        self.freeze_preflight(findings_for=("swiftui-reviewer",))
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]),
            review("CHANGES_REQUIRED", findings=[
                blocker("CODE-002", lineage="introduced_by_fix"),
            ]),
            review("PASS"),
        ])
        claude = FakeClaude(repairs=[
            repair_result("CODE-001", "PF-SWIFTUI-SWIFTUI-001"),
            repair_result("CODE-002"),
        ])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertEqual(result.repair_round, 2)
        self.assertEqual(len(claude.repair_calls), 2)
        self.assertEqual(claude.repair_calls[1]["finding_ids"], ["CODE-002"])
        # The specialists were merged once, into the first decisive review only.
        self.assertEqual(
            [path.name for path in sorted((self.artifacts / "merged-reviews").iterdir())],
            ["0001.json"],
        )

    def test_a_replayed_round_reuses_the_merged_decision_not_the_raw_review(self):
        """A killed repair must not be replayed against the raw Codex review.

        `_pending_code_review` replays a round whose repair never advanced the
        counter, and it replays the *raw* persisted Codex review.  The
        specialist findings exist only in the merged decision, so replaying the
        raw one would silently drop them from the repair queue.
        """
        self.freeze_preflight(findings_for=SPECIALISTS)

        def die(*_args, **_kwargs):
            raise RuntimeError("the repair process was killed")

        killed = FakeClaude(repairs=[])
        killed.repair = die
        self.workflow(
            codex=FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])]),
            claude=killed, verification=self.green,
        ).run(self.run.run_id)

        # The merge landed; the repair did not.
        self.assertTrue((self.artifacts / "merged-reviews" / "0001.json").exists())
        # Put the run in the one window the intent journal does not cover: the
        # kill landed after the merge and before the repair was journalled.
        for intent in (self.artifacts / "claude-intents").glob("*.json"):
            intent.unlink()
        state_path = self.artifacts / "state.json"
        persisted = json.loads(state_path.read_text())
        self.assertEqual(persisted["repair_round"], 0)
        persisted["status"] = "RUNNING"
        persisted.pop("pause_reason", None)
        state_path.write_text(json.dumps(persisted), encoding="utf-8")

        resumed = FakeClaude(repairs=[repair_result(
            "CODE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
            "PF-RESILIENCE-RESILIENCE-001",
        )])
        self.workflow(
            codex=FakeCodex([review("PASS")]), claude=resumed, verification=self.green,
        ).run(self.run.run_id)

        self.assertEqual(sorted(resumed.repair_calls[0]["finding_ids"]), [
            "CODE-001", "PF-RESILIENCE-RESILIENCE-001",
            "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
        ])

    def test_specialist_risk_flags_reach_the_deterministic_risk_detector(self):
        self.freeze_preflight(findings_for=("ux-critique",), risk_flags=("public_api",))
        repo = self.repo

        def touch_lockfile(_round):
            (repo / "Package.resolved").write_text('{"pins":[]}\n', encoding="utf-8")

        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])])
        claude = FakeClaude(
            repairs=[repair_result("CODE-001", "PF-UX-UX-001")], on_repair=touch_lockfile,
        )

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        assessment = json.loads(
            (self.artifacts / "risk-assessments" / "round-0001.json").read_text()
        )
        self.assertEqual(
            set(assessment["categories"]), {"dependencies", "public_api"}
        )


class ReviewScopeIntegrityTests(ReviewWorkflowTestCase):
    """Regressions for unapproved content reaching a model or a human PASS gate."""

    def test_a_worktree_edit_after_scope_approval_stops_before_codex(self):
        codex, claude = FakeCodex([review("PASS")]), FakeClaude()
        # Approved, then edited before `run` — the approved patch is gone.
        (self.repo / "Sneaked.py").write_text("SNEAKED = True\n", encoding="utf-8")

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(codex.call_count, 0)
        self.assertEqual(claude.call_count, 0)
        detail = json.loads((self.artifacts / "pause.json").read_text())["detail"]
        self.assertIn("approved", detail)

    def test_the_reviewed_round_zero_patch_is_the_approved_one(self):
        codex = FakeCodex([review("PASS")])

        self.workflow(codex=codex, claude=FakeClaude(), verification=self.green).run(
            self.run.run_id
        )

        captured = (self.artifacts / "patches" / "round-0000.patch").read_bytes()
        self.assertEqual(
            hashlib.sha256(captured).hexdigest(),
            self.run.manifest.initial_patch_digest,
        )
        self.assertEqual(
            codex.calls[0]["patch_sha256"], self.run.manifest.initial_patch_digest
        )
        self.assertIn(b"Feature.py", Path(codex.calls[0]["patch_path"]).read_bytes())

    def test_a_claude_reported_risk_category_pauses_a_generic_run(self):
        """Claude's disclosure must reach the detector, not just free text."""
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])])
        disclosed = dict(
            repair_result("CODE-001"), risk_flags=["network_format"],
        )
        claude = FakeClaude(repairs=[disclosed])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "HIGH_RISK_CHANGE",
        )
        request = json.loads((self.artifacts / "risk-approval-request.json").read_text())
        self.assertEqual(request["categories"], ["network_format"])
        self.assertEqual(codex.call_count, 1)

    def test_an_unclassifiable_claude_risk_flag_is_rejected_closed(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])])
        claude = FakeClaude(repairs=[dict(repair_result("CODE-001"), risk_flags=[""])])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "INVALID_CLAUDE_RESOLUTION",
        )

    def test_an_omitted_risk_disclosure_is_rejected_not_read_as_no_risk(self):
        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])])
        silent = repair_result("CODE-001")
        silent.pop("risk_flags")
        claude = FakeClaude(repairs=[silent])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            self.run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "INVALID_CLAUDE_RESOLUTION",
        )
        self.assertEqual(codex.call_count, 1)

    def test_plan_and_code_resolutions_still_reject_risk_flags(self):
        from ai_review.runners import validate_claude_resolution

        payload = dict(repair_result("CODE-001"), risk_flags=["network_format"])

        with self.assertRaises(ValueError):
            validate_claude_resolution(payload, required_finding_ids=["CODE-001"])
        allowed = validate_claude_resolution(
            payload, required_finding_ids=["CODE-001"], allow_risk_flags=True,
        )
        self.assertEqual(allowed["risk_flags"], ["network_format"])

    def test_baseline_high_risk_paths_are_not_blamed_on_claude(self):
        """The human already bound the baseline; only new risk may gate."""
        (self.repo / "Package.resolved").write_text('{"pins":[]}\n', encoding="utf-8")
        run = self.create_run()
        self.run = run
        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")])

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertFalse((self.artifacts / "risk-approval-request.json").exists())
        assessment = json.loads(
            (self.artifacts / "risk-assessments" / "round-0001.json").read_text()
        )
        # Still recorded for audit, just not attributed to the repair.
        self.assertEqual(assessment["categories"], ["dependencies"])
        self.assertEqual(assessment["introduced_categories"], [])

    def test_claude_editing_a_baseline_high_risk_file_again_still_pauses(self):
        """A path already risky in the baseline is not a licence to edit it."""
        (self.repo / "Package.resolved").write_text(
            '{"pins":["original"]}\n', encoding="utf-8"
        )
        run = self.create_run()
        self.run = run
        repo = self.repo

        def rewrite_lockfile(_round):
            # Same path as the baseline, different content, authored by Claude.
            (repo / "Package.resolved").write_text(
                '{"pins":["claude-added-dependency"]}\n', encoding="utf-8"
            )

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(
            repairs=[repair_result("CODE-001")], on_repair=rewrite_lockfile,
        )

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(
            json.loads((self.artifacts / "pause.json").read_text())["reason"],
            "HIGH_RISK_CHANGE",
        )
        request = json.loads((self.artifacts / "risk-approval-request.json").read_text())
        self.assertEqual(request["categories"], ["dependencies"])
        self.assertEqual(
            [item["path"] for item in request["evidence"]], ["Package.resolved"]
        )
        # The second Codex review must not have run past the gate.
        self.assertEqual(codex.call_count, 1)
        self.assertIn(
            b"claude-added-dependency",
            (self.artifacts / "patches" / "round-0001.patch").read_bytes(),
        )

    def test_a_binary_high_risk_file_rewritten_by_claude_still_pauses(self):
        """Binary sections carry no `+` lines, so content must be fingerprinted."""
        profile = self.repo / "Team.mobileprovision"
        profile.write_bytes(b"\x00original signing profile\x00")
        run = self.create_run()
        self.run = run

        def rewrite_profile(_round):
            profile.write_bytes(b"\x00claude rewrote this profile\x00")

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(
            repairs=[repair_result("CODE-001")], on_repair=rewrite_profile,
        )

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        request = json.loads((self.artifacts / "risk-approval-request.json").read_text())
        self.assertEqual(request["categories"], ["entitlements_signing"])
        self.assertEqual(codex.call_count, 1)

    def test_a_tail_only_edit_to_a_large_signing_artifact_changes_its_fingerprint(self):
        """A truncated hash let a same-size tail edit look identical."""
        from ai_review.review_risk import path_content_fingerprints

        target = self.repo / "Team.mobileprovision"
        # Deliberately larger than any read cap, with the edit past the cap and
        # the byte length held constant so only the tail distinguishes them.
        size = 4 * 1024 * 1024 + 17
        target.write_bytes(b"A" * size)
        before = path_content_fingerprints(self.repo, ["Team.mobileprovision"])

        payload = bytearray(b"A" * size)
        payload[-16:] = b"CLAUDE_REWROTE!!"
        target.write_bytes(bytes(payload))
        after = path_content_fingerprints(self.repo, ["Team.mobileprovision"])

        self.assertEqual(target.stat().st_size, size)
        self.assertNotEqual(
            before["Team.mobileprovision"], after["Team.mobileprovision"],
            "a same-size tail edit must not produce an identical fingerprint",
        )

    def test_a_tail_edit_to_a_large_baseline_signing_file_still_pauses(self):
        size = 4 * 1024 * 1024 + 17
        target = self.repo / "Team.mobileprovision"
        target.write_bytes(b"A" * size)
        run = self.create_run()
        self.run = run

        def rewrite_tail(_round):
            payload = bytearray(b"A" * size)
            payload[-16:] = b"CLAUDE_REWROTE!!"
            target.write_bytes(bytes(payload))

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")], on_repair=rewrite_tail)

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        self.assertEqual(target.stat().st_size, size)
        request = json.loads((self.artifacts / "risk-approval-request.json").read_text())
        self.assertEqual(request["categories"], ["entitlements_signing"])
        self.assertEqual(codex.call_count, 1)

    def test_an_untouched_baseline_high_risk_file_still_does_not_pause(self):
        """The narrowing must not regress into pausing on inherited risk."""
        (self.repo / "Package.resolved").write_text('{"pins":[]}\n', encoding="utf-8")
        run = self.create_run()
        self.run = run
        repo = self.repo

        def edit_elsewhere(_round):
            (repo / "Feature.py").write_text("def retry():\n    return 2\n", encoding="utf-8")

        codex = FakeCodex([
            review("CHANGES_REQUIRED", findings=[blocker("CODE-001")]), review("PASS"),
        ])
        claude = FakeClaude(repairs=[repair_result("CODE-001")], on_repair=edit_elsewhere)

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        self.assertEqual(result.status, Status.AWAITING_HUMAN_CODE_REVIEW)
        self.assertFalse((self.artifacts / "risk-approval-request.json").exists())

    def test_a_new_high_risk_path_on_top_of_a_risky_baseline_still_pauses(self):
        (self.repo / "Package.resolved").write_text('{"pins":[]}\n', encoding="utf-8")
        run = self.create_run()
        self.run = run
        repo = self.repo

        def add_workflow(_round):
            target = repo / ".github" / "workflows" / "ci.yml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("on: push\n", encoding="utf-8")

        codex = FakeCodex([review("CHANGES_REQUIRED", findings=[blocker("CODE-001")])])
        claude = FakeClaude(repairs=[repair_result("CODE-001")], on_repair=add_workflow)

        result = self.workflow(codex=codex, claude=claude, verification=self.green).run(
            run.run_id
        )

        self.assertEqual(result.status, Status.PAUSED)
        request = json.loads((self.artifacts / "risk-approval-request.json").read_text())
        self.assertEqual(request["categories"], ["ci_cd"])


class IosPreflightIntegrityTests(ReviewWorkflowTestCase):
    profile = "ios"

    def test_two_lenses_reporting_one_defect_merge_and_keep_every_source_id(self):
        from ai_review.preflight import merged_source_ids, normalized_findings, validate_preflight

        shared = {
            "severity": "minor", "category": "swiftui",
            "location": "Sources/Feature.swift:42",
            "evidence": "the swiftui lens saw it", "required_outcome": "same outcome",
            "risk_flags": [],
        }
        submission = validate_preflight({
            "specialists": [
                {"name": "swiftui-reviewer", "findings": [
                    dict(shared, id="SWIFTUI-001"),
                    dict(shared, id="SWIFTUI-002", severity="blocker",
                         evidence="a second lens entry"),
                ]},
                {"name": "ux-critique", "findings": []},
                {"name": "resilience-auditor", "findings": []},
            ],
        })

        findings = normalized_findings(submission)
        mapping = merged_source_ids(submission)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["id"], "PF-SWIFTUI-SWIFTUI-001")
        # Nothing is discarded: the merged finding names the other source ID,
        # takes the highest severity, and the mapping records both.
        self.assertIn("PF-SWIFTUI-SWIFTUI-002", findings[0]["evidence"])
        self.assertEqual(findings[0]["severity"], "blocker")
        self.assertEqual(
            mapping["PF-SWIFTUI-SWIFTUI-001"],
            ["PF-SWIFTUI-SWIFTUI-001", "PF-SWIFTUI-SWIFTUI-002"],
        )
        self.assertEqual(set(findings[0]), {
            "id", "severity", "invariant", "location", "evidence",
            "required_outcome", "lineage",
        })

    def test_distinct_lenses_on_the_same_line_stay_separate_findings(self):
        from ai_review.preflight import normalized_findings, validate_preflight

        submission = validate_preflight(preflight_envelope(findings_for=SPECIALISTS))

        findings = normalized_findings(submission)

        # Different categories are different repairs even at a similar location.
        self.assertEqual(len(findings), 3)
        self.assertEqual(
            sorted(item["id"] for item in findings),
            ["PF-RESILIENCE-RESILIENCE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001"],
        )


class ReviewRunnerRoutingTests(ReviewWorkflowTestCase):
    """The run kind, not input shape, selects prompts and the workflow class."""

    def test_local_codex_uses_the_direct_review_prompt_for_review_mode(self):
        from ai_review.cli import _LocalCodex

        identity = resolve_executable(sys.executable)
        prompts = {}
        for mode, expected in (
            ("plan", "codex-plan.md"), ("code", "codex-code.md"), ("review", "codex-review.md"),
        ):
            codex = _LocalCodex(self.repo, identity, mode=mode, model=self.policy.codex_model)
            prompts[mode] = codex._PROMPTS[codex.mode]
            self.assertEqual(prompts[mode], expected)
        with self.assertRaises(ValueError):
            _LocalCodex(self.repo, identity, mode="android", model=self.policy.codex_model)

    def test_local_claude_review_mode_repairs_only_and_never_implements(self):
        from ai_review.cli import _LocalClaude
        from ai_review.runners import RunnerError, build_claude_argv

        identity = resolve_executable(sys.executable)
        claude = _LocalClaude(self.repo, mode="review", identity=identity, model="opus[1m]", fallback_model="sonnet", max_budget_usd=5)
        calls = []
        claude._call = lambda schema, prompt, inputs: calls.append((schema, prompt))

        claude.repair({"findings": []})
        claude.resolve({"findings": []})

        self.assertEqual(calls, [
            ("claude-review-resolution.schema.json", "claude-review-fix.md"),
            ("claude-review-resolution.schema.json", "claude-review-fix.md"),
        ])
        with self.assertRaises(RunnerError):
            claude.implement({"findings": []})
        argv = build_claude_argv("{}", "fix", mode="review", model="opus[1m]", fallback_model="sonnet", max_budget_usd=5)
        self.assertIn("--safe-mode", argv)
        self.assertNotIn("Bash", argv[argv.index("--tools") + 1])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_the_default_factory_routes_a_review_run_to_the_direct_workflow(self):
        from ai_review.cli import _default_workflow_factory

        state = self.store.load(self.run.run_id)

        workflow = _default_workflow_factory(
            kind=state.kind, store=self.store, state=state,
        )

        self.assertIsInstance(workflow, DirectReviewWorkflow)
        self.assertEqual(workflow.codex.mode, "review")
        self.assertEqual(workflow.claude.mode, "review")


if __name__ == "__main__":
    unittest.main()
