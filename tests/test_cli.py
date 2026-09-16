import hashlib
import inspect
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


class RecordingCodex:
    """A Codex boundary that records every input and refuses an extra call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def review(self, inputs):
        self.calls.append(inputs)
        if not self.responses:
            raise AssertionError("Codex was called more times than the test expected")
        return self.responses.pop(0)


class CliContractTests(unittest.TestCase):
    def setUp(self):
        from ai_review.cli import main

        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.plan = self.repo / "docs" / "plan.md"
        self.plan.parent.mkdir()
        self.plan.write_text("# Plan\n", encoding="utf-8")
        self.git("add", "docs/plan.md")
        self.git("commit", "-m", "base")
        self.runs = self.root / "runs"

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, text=True, capture_output=True)

    def cli(self, *args, store_factory=None, workflow_factory=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        options = {}
        if store_factory is not None:
            options["store_factory"] = store_factory
        if workflow_factory is not None:
            options["workflow_factory"] = workflow_factory
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.main(
                ["--runs-root", str(self.runs), *args],
                **options,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def cli_store(self):
        """Build the same store the CLI builds for --runs-root."""
        from ai_review.models import ApprovalAuthority
        from ai_review.store import RunStore

        root = self.runs.expanduser().resolve()
        return RunStore(root, authority=ApprovalAuthority(root.parent / "approval.key"))

    def init_plan(self):
        return self.cli(
            "init", "plan", "--repo", str(self.repo), "--plan", "docs/plan.md",
            "--verify", json.dumps({
                "kind": "test",
                "argv": [sys.executable, "-m", "unittest", "tests.feature"],
                "scope": "tests.feature",
            }),
        )

    def init_review(self, *extra, profile="ios"):
        self.plan.write_text("# Plan\nreview change\n", encoding="utf-8")
        return self.cli(
            "init", "review", "--repo", str(self.repo), "--base", "HEAD",
            "--brief", "Review completed retry fix", "--profile", profile,
            "--verify", "%s -m unittest tests.test_retry" % sys.executable,
            *extra,
        )

    def approved_review(self, *, profile="generic"):
        """Return one natively approved Review run id, mocking user presence."""
        code, stdout, _stderr = self.init_review(profile=profile)
        self.assertEqual(code, 0)
        run_id = json.loads(stdout)["run_id"]
        original_run = subprocess.run

        def approve(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=approve,
        ):
            approved, _output, error = self.cli("approve-review", run_id)
        self.assertEqual((approved, error), (0, ""))
        return run_id

    def review_factory(self, codex_responses, repairs, *, on_repair=None):
        """Build a DirectReviewWorkflow with deterministic model boundaries."""
        from ai_review.review_workflow import DirectReviewWorkflow
        from ai_review.runners import VerificationResult

        calls = {"codex": [], "repair": []}

        class Codex:
            def review(_self, inputs):
                calls["codex"].append(inputs)
                return codex_responses.pop(0)

        class Claude:
            def implement(_self, inputs):
                raise AssertionError("Review mode must never implement")

            def repair(_self, inputs):
                calls["repair"].append(inputs)
                if on_repair is not None:
                    on_repair(len(calls["repair"]))
                return repairs.pop(0)

        def factory(*, kind, store, state, context_packet=None):
            return DirectReviewWorkflow(
                store, Codex(), Claude(), context_packet=context_packet,
                verification_runner=lambda *_a, **_k: VerificationResult(
                    ("verify",), 0, "green", ""
                ),
            )

        return factory, calls

    def test_review_init_captures_full_patch_and_returns_scope_gate(self):
        committed = self.repo / "committed.txt"
        committed.write_text("committed\n", encoding="utf-8")
        staged = self.repo / "staged file.txt"
        staged.write_text("staged\n", encoding="utf-8")
        self.git("add", "committed.txt")
        self.git("commit", "-m", "committed review change")
        unstaged = self.repo / "unstaged.txt"
        unstaged.write_text("base\n", encoding="utf-8")
        self.git("add", "unstaged.txt")
        self.git("commit", "-m", "tracked base")
        self.git("add", "staged file.txt")
        unstaged.write_text("base\nunstaged\n", encoding="utf-8")
        untracked = self.repo / "untracked.txt"
        untracked.write_text("untracked\n", encoding="utf-8")
        base = self.git("rev-parse", "HEAD^^").stdout.strip()

        with patch("ai_review.cli._LocalCodex.review") as codex, patch(
            "ai_review.cli._LocalClaude.resolve"
        ) as claude:
            code, stdout, stderr = self.cli(
                "init", "review", "--repo", str(self.repo), "--base", base,
                "--brief", "  Review completed retry fix  ", "--profile", "ios",
                "--verify", "%s -m unittest tests.test_retry" % sys.executable,
            )

        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "AWAITING_REVIEW_APPROVAL")
        self.assertEqual(payload["next_action"], "human_review_scope")
        self.assertEqual(payload["profile"], "ios")
        self.assertEqual(payload["base_oid"], base)
        state_path = next(self.runs.rglob("state.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = state["manifest"]
        self.assertEqual(manifest["brief"], "Review completed retry fix")
        self.assertEqual(set(manifest["review_executables"]), {"codex", "claude"})
        self.assertIsNotNone(manifest["verification_commands"][0]["executable_identity"])
        patch_bytes = next(self.runs.rglob("round-0000.patch")).read_bytes()
        for name in (b"committed.txt", b"staged file.txt", b"unstaged.txt", b"untracked.txt"):
            self.assertIn(name, patch_bytes)
        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), payload["initial_patch_digest"])
        self.assertFalse(any(path.name == ".ai-review" for path in self.repo.rglob("*")))
        codex.assert_not_called()
        claude.assert_not_called()

    def test_review_approval_rebuilds_context_under_the_packet_budget(self):
        """max_context_tokens sits inside the packet checksum, so the rebuild must reuse it."""
        from ai_review import context as context_module

        note = self.root / "note.md"
        note.write_text("# Exact\nRetry constraints.\n", encoding="utf-8")
        code, stdout, stderr = self.init_review("--source", "%s#Exact" % note)
        self.assertEqual((code, stderr), (0, ""))
        run_id = json.loads(stdout)["run_id"]
        original_run = subprocess.run
        rebuild_kwargs = []

        def recording_build_packet(*args, **kwargs):
            rebuild_kwargs.append(kwargs)
            return context_module.build_packet(*args, **kwargs)

        def osascript_approval(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(
                    argv, 0, "button returned:Approve\n", "",
                )
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=osascript_approval,
        ), patch("ai_review.cli.build_packet", side_effect=recording_build_packet):
            approved, output, error = self.cli("approve-review", run_id)

        self.assertEqual((approved, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "READY")
        self.assertTrue(rebuild_kwargs, "approval did not rebuild the context packet")
        for kwargs in rebuild_kwargs:
            self.assertIn("max_tokens", kwargs)

    def test_review_init_binds_exact_context_and_rejects_empty_patch(self):
        note = self.root / "note.md"
        note.write_text("# Exact\nRetry constraints.\n", encoding="utf-8")
        code, stdout, stderr = self.init_review("--source", "%s#Exact" % note)
        self.assertEqual((code, stderr), (0, ""))
        state = json.loads(next(self.runs.rglob("state.json")).read_text(encoding="utf-8"))
        self.assertRegex(state["manifest"]["context_checksum"], r"^[0-9a-f]{64}$")

        clean_root = self.root / "clean"
        clean_root.mkdir()
        subprocess.run(["git", "init"], cwd=clean_root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=clean_root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=clean_root, check=True)
        (clean_root / "file.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=clean_root, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=clean_root, check=True, capture_output=True)
        code, output, error = self.cli(
            "init", "review", "--repo", str(clean_root), "--base", "HEAD",
            "--brief", "Review clean tree", "--profile", "generic",
            "--verify", "%s -m unittest tests.test_retry" % sys.executable,
        )
        self.assertEqual((code, output), (2, ""))
        self.assertIn("patch", error.lower())

    def test_review_init_rejects_bad_brief_and_profile(self):
        self.plan.write_text("# Plan\nchanged\n", encoding="utf-8")
        common = (
            "init", "review", "--repo", str(self.repo), "--base", "HEAD",
            "--verify", "%s -m unittest tests.test_retry" % sys.executable,
        )
        for tail in (
            ("--brief", "   ", "--profile", "generic"),
            ("--brief", "x" * 2001, "--profile", "generic"),
            ("--brief", "valid", "--profile", "android"),
        ):
            with self.subTest(tail=tail):
                code, stdout, _stderr = self.cli(*(common + tail))
                self.assertEqual((code, stdout), (2, ""))

    def test_review_init_requires_an_explicit_base(self):
        self.plan.write_text("# Plan\nchanged\n", encoding="utf-8")

        code, stdout, stderr = self.cli(
            "init", "review", "--repo", str(self.repo),
            "--brief", "Review explicit base", "--profile", "generic",
            "--verify", "%s -m unittest tests.test_retry" % sys.executable,
        )

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("--base", stderr)

    def test_review_approval_succeeds_and_changed_patch_fails_closed(self):
        code, stdout, _stderr = self.init_review()
        self.assertEqual(code, 0)
        run_id = json.loads(stdout)["run_id"]
        original_run = subprocess.run

        def osascript_approval(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                script = argv[-1]
                self.assertIn(run_id, script)
                self.assertIn("Review completed retry fix", script)
                self.assertIn('default button "Cancel"', script)
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=osascript_approval,
        ):
            approved, output, error = self.cli("approve-review", run_id)
        self.assertEqual((approved, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "READY")

        second_runs = self.root / "second-runs"
        old_runs = self.runs
        self.runs = second_runs
        try:
            code, stdout, _stderr = self.init_review()
            changed_id = json.loads(stdout)["run_id"]
            self.plan.write_text("# Plan\nchanged again\n", encoding="utf-8")
            with patch("ai_review.cli.MacOSHumanApprovalProvider.approve_review") as provider:
                rejected, rejected_out, rejected_error = self.cli("approve-review", changed_id)
            self.assertEqual((rejected, rejected_out), (2, ""))
            self.assertIn("patch", rejected_error.lower())
            provider.assert_not_called()
        finally:
            self.runs = old_runs

    def test_a_worktree_edit_during_the_approval_dialog_fails_closed(self):
        """Regression: the pre-dialog capture is stale the moment the dialog opens."""
        code, stdout, _stderr = self.init_review(profile="generic")
        self.assertEqual(code, 0)
        run_id = json.loads(stdout)["run_id"]
        original_run = subprocess.run
        sneaked = self.repo / "sneaked.txt"

        def approve_then_edit(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                # Exactly what a person leaving the dialog open allows.
                sneaked.write_text("unapproved content\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=approve_then_edit,
        ):
            approved, output, error = self.cli("approve-review", run_id)

        self.assertEqual((approved, output), (2, ""))
        self.assertIn("patch", error.lower())
        state = json.loads(next(self.runs.rglob("state.json")).read_text())
        self.assertEqual(state["status"], "AWAITING_REVIEW_APPROVAL")
        self.assertIsNone(state["approval_attestation"])

    def test_review_approval_cancel_fails_closed(self):
        code, stdout, _stderr = self.init_review()
        self.assertEqual(code, 0)
        run_id = json.loads(stdout)["run_id"]
        original_run = subprocess.run

        def cancel_osascript(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(
                    argv, 1, "", "45:52: execution error: User canceled. (-128)"
                )
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=cancel_osascript,
        ):
            approved, output, error = self.cli("approve-review", run_id)
        self.assertEqual((approved, output), (2, ""))
        self.assertIn("cancelled", error)

    def test_review_approval_rejects_moved_base_and_changed_context(self):
        self.git("branch", "review-base", "HEAD")
        self.plan.write_text("# Plan\nreview change\n", encoding="utf-8")
        note = self.root / "binding.md"
        note.write_text("# Exact\nOriginal context.\n", encoding="utf-8")
        code, stdout, _stderr = self.cli(
            "init", "review", "--repo", str(self.repo), "--base", "review-base",
            "--brief", "Review bound inputs", "--profile", "generic",
            "--verify", "%s -m unittest tests.test_retry" % sys.executable,
            "--source", "%s#Exact" % note,
        )
        self.assertEqual(code, 0)
        initialized = json.loads(stdout)
        run_id = initialized["run_id"]
        initial_base = initialized["base_oid"]
        self.git("commit", "--allow-empty", "-m", "move review base")
        self.git("branch", "-f", "review-base", "HEAD")
        with patch("ai_review.cli.MacOSHumanApprovalProvider.approve_review") as provider:
            rejected, output, error = self.cli("approve-review", run_id)
        self.assertEqual((rejected, output), (2, ""))
        self.assertIn("base", error.lower())
        provider.assert_not_called()

        self.git("branch", "-f", "review-base", initial_base)
        note.write_text("# Exact\nChanged context.\n", encoding="utf-8")
        with patch("ai_review.cli.MacOSHumanApprovalProvider.approve_review") as provider:
            rejected, output, error = self.cli("approve-review", run_id)
        self.assertEqual((rejected, output), (2, ""))
        self.assertIn("context", error.lower())
        provider.assert_not_called()

    def test_review_payload_does_not_access_plan_path(self):
        from ai_review.cli import _payload
        from ai_review.models import ReviewManifest, RunState
        from ai_review.process_security import executable_identity
        identity = executable_identity(Path(sys.executable))
        manifest = ReviewManifest(
            kind="review", repo_path=str(self.repo), base_ref="HEAD",
            base_oid=self.git("rev-parse", "HEAD").stdout.strip(), brief="review",
            brief_digest=hashlib.sha256(b"review").hexdigest(), profile="generic",
            initial_patch_digest="a" * 64,
            verification_commands=[{
                "kind": "test", "argv": [sys.executable, "-m", "unittest", "tests.test_retry"],
                "scope": "tests.test_retry", "executable_identity": identity,
            }], review_executables={"codex": identity, "claude": identity},
        )
        state = RunState.new_review(manifest)
        object.__setattr__(state, "run_id", "review-payload")
        store = Mock()
        store._run_directory.return_value = self.runs / "project" / state.run_id
        store.artifact_exists.return_value = False
        with patch.object(type(state), "plan_path", new_callable=unittest.mock.PropertyMock) as plan_path:
            payload = _payload(store, state)
        plan_path.assert_not_called()
        self.assertEqual(payload["brief_digest"], manifest.brief_digest)

    def high_risk_repair_pause(self):
        """Drive one Review run until Claude's lockfile change forces a pause."""
        run_id = self.approved_review()
        repo = self.repo

        def touch_lockfile(_round):
            (repo / "Package.resolved").write_text('{"pins":[]}\n', encoding="utf-8")

        factory, calls = self.review_factory(
            [
                {
                    "verdict": "CHANGES_REQUIRED", "summary": "needs a fix",
                    "findings": [{
                        "id": "CODE-001", "severity": "blocker", "invariant": "retry works",
                        "location": "docs/plan.md:2", "evidence": "retry drops the stream",
                        "required_outcome": "restore the stream",
                        "lineage": {"resolution": "existing"},
                    }],
                    "questions": [], "context_requests": [],
                },
                {
                    "verdict": "PASS", "summary": "resolved", "findings": [],
                    "questions": [], "context_requests": [],
                },
            ],
            [{
                "summary": "repaired",
                "resolutions": [{
                    "finding_id": "CODE-001", "outcome": "fixed",
                    "evidence": "restored the stream and pinned the dependency",
                }],
                # The Review contract requires an explicit disclosure; deterministic
                # path detection catches the lockfile regardless.
                "risk_flags": [],
            }],
            on_repair=touch_lockfile,
        )
        code, stdout, stderr = self.cli("run", run_id, workflow_factory=factory)
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "PAUSED")
        request = json.loads(next(self.runs.rglob("risk-approval-request.json")).read_text())
        self.assertEqual(request["categories"], ["dependencies"])
        return run_id, factory, calls, request

    def test_approve_risk_binds_user_presence_to_the_exact_patch_then_resumes(self):
        run_id, factory, calls, request = self.high_risk_repair_pause()
        original_run = subprocess.run
        scripts = []

        def approve(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                scripts.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=approve,
        ):
            code, stdout, stderr = self.cli("approve-risk", run_id)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "PAUSED")
        self.assertIn("dependencies", scripts[0])
        self.assertIn("Package.resolved", scripts[0])
        self.assertIn(request["patch_digest"], scripts[0])
        self.assertIn('default button "Cancel"', scripts[0])
        receipt = json.loads(next(self.runs.rglob("risk-approvals/round-0001.json")).read_text())
        self.assertEqual(receipt["receipt"]["patch_digest"], request["patch_digest"])
        self.assertRegex(receipt["signature"], r"^[0-9a-f]{64}$")

        resumed, output, error = self.cli("resume", run_id, workflow_factory=factory)

        self.assertEqual((resumed, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "AWAITING_HUMAN_CODE_REVIEW")
        self.assertEqual(len(calls["codex"]), 2)
        self.assertEqual(len(calls["repair"]), 1)

    def test_approve_risk_cancel_and_a_changed_worktree_both_fail_closed(self):
        run_id, factory, calls, _request = self.high_risk_repair_pause()
        original_run = subprocess.run

        def cancel(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(
                    argv, 1, "", "45:52: execution error: User canceled. (-128)"
                )
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=cancel,
        ):
            code, stdout, stderr = self.cli("approve-risk", run_id)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("cancelled", stderr)
        self.assertEqual(list(self.runs.rglob("risk-approvals/*.json")), [])

        (self.repo / "Sneaked.txt").write_text("sneaked\n", encoding="utf-8")
        with patch("ai_review.cli.MacOSHumanApprovalProvider.approve_risk") as provider:
            changed, output, error = self.cli("approve-risk", run_id)
        self.assertEqual((changed, output), (2, ""))
        self.assertIn("patch", error.lower())
        provider.assert_not_called()

        resumed, _out, _err = self.cli("resume", run_id, workflow_factory=factory)
        self.assertEqual(json.loads(_out)["status"], "PAUSED")
        self.assertEqual(len(calls["codex"]), 1)

    def test_approve_risk_requires_a_paused_review_with_a_pending_request(self):
        run_id = self.approved_review()

        with patch("ai_review.cli.MacOSHumanApprovalProvider.approve_risk") as provider:
            code, stdout, stderr = self.cli("approve-risk", run_id)

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("risk", stderr.lower())
        provider.assert_not_called()

    def awaiting_preflight(self):
        """Drive one iOS Review run to AWAITING_PREFLIGHT after the first Codex."""
        run_id = self.approved_review(profile="ios")
        factory, calls = self.review_factory(
            [{
                "verdict": "CHANGES_REQUIRED", "summary": "needs a fix",
                "findings": [{
                    "id": "CODE-001", "severity": "blocker", "invariant": "retry works",
                    "location": "docs/plan.md:2", "evidence": "retry drops the stream",
                    "required_outcome": "restore the stream",
                    "lineage": {"resolution": "existing"},
                }],
                "questions": [], "context_requests": [],
            }, {
                "verdict": "PASS", "summary": "resolved", "findings": [],
                "questions": [], "context_requests": [],
            }],
            [{
                "summary": "repaired",
                "resolutions": [
                    {
                        "finding_id": identifier, "outcome": "fixed",
                        "evidence": "restored the documented retry behavior",
                    }
                    for identifier in (
                        "CODE-001", "PF-SWIFTUI-SWIFTUI-001",
                        "PF-UX-UX-001", "PF-RESILIENCE-RESILIENCE-001",
                    )
                ],
                "risk_flags": [],
            }],
        )
        code, stdout, stderr = self.cli("run", run_id, workflow_factory=factory)
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "AWAITING_PREFLIGHT")
        self.assertEqual(payload["next_action"], "submit_preflight")
        request = json.loads(next(self.runs.rglob("preflight-request.json")).read_text())
        return run_id, factory, calls, request

    def preflight_file(self, patch_digest, *, findings=True, name="preflight.json"):
        specialists = []
        for specialist, category, prefix in (
            ("swiftui-reviewer", "swiftui", "SWIFTUI"),
            ("ux-critique", "ux", "UX"),
            ("resilience-auditor", "resilience", "RESILIENCE"),
        ):
            specialists.append({
                "name": specialist,
                "findings": [{
                    "id": "%s-001" % prefix, "severity": "major", "category": category,
                    "location": "docs/plan.md:2",
                    "evidence": "the %s lens observed a concrete defect" % specialist,
                    "required_outcome": "restore the expected observable behavior",
                    "risk_flags": [],
                }] if findings else [],
            })
        path = self.root / name
        path.write_text(json.dumps({
            "profile": "ios", "patch_digest": patch_digest, "specialists": specialists,
        }), encoding="utf-8")
        return path

    def test_submit_preflight_accepts_one_bound_submission_then_repairs_once(self):
        run_id, factory, calls, request = self.awaiting_preflight()
        findings = self.preflight_file(request["patch_digest"])

        code, stdout, stderr = self.cli(
            "submit-preflight", run_id, "--findings", str(findings),
            workflow_factory=factory,
        )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_CODE_REVIEW")
        self.assertEqual(len(calls["repair"]), 1)
        self.assertEqual(sorted(calls["repair"][0]["finding_ids"]), [
            "CODE-001", "PF-RESILIENCE-RESILIENCE-001",
            "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
        ])
        self.assertEqual(
            set(calls["repair"][0]["repair_lenses"]),
            {"ios-distill", "code-simplifier", "ios-polish"},
        )

        second, output, error = self.cli(
            "submit-preflight", run_id, "--findings", str(findings),
            workflow_factory=factory,
        )
        self.assertEqual((second, output), (2, ""))
        self.assertIn("preflight", error.lower())
        self.assertEqual(len(calls["repair"]), 1)

    def test_submit_preflight_rejects_a_wrong_digest_and_a_malformed_file(self):
        run_id, factory, calls, request = self.awaiting_preflight()
        wrong = self.preflight_file("b" * 64, name="wrong.json")
        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            '{"profile":"ios","profile":"ios","patch_digest":"%s","specialists":[]}'
            % request["patch_digest"], encoding="utf-8",
        )
        missing = self.root / "absent.json"

        for path in (wrong, duplicate, missing):
            with self.subTest(path=path.name):
                code, stdout, stderr = self.cli(
                    "submit-preflight", run_id, "--findings", str(path),
                    workflow_factory=factory,
                )
                self.assertEqual((code, stdout), (2, ""))
                self.assertNotEqual(stderr, "")

        self.assertEqual(len(calls["repair"]), 0)
        self.assertEqual(list(self.runs.rglob("preflight.json")), [])

    def test_submit_preflight_rejects_a_generic_run_and_an_oversized_file(self):
        from ai_review.cli import MAX_PREFLIGHT_FILE_BYTES

        generic_id = self.approved_review(profile="generic")
        findings = self.preflight_file("c" * 64, name="generic.json")

        code, stdout, stderr = self.cli(
            "submit-preflight", generic_id, "--findings", str(findings),
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertNotEqual(stderr, "")

        oversized = self.root / "huge.json"
        oversized.write_text("x" * (MAX_PREFLIGHT_FILE_BYTES + 1), encoding="utf-8")
        code, stdout, stderr = self.cli(
            "submit-preflight", generic_id, "--findings", str(oversized),
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("size", stderr.lower())

    def passed_review(self):
        """Drive one generic Review run to a terminal Codex PASS."""
        run_id = self.approved_review(profile="generic")
        factory, calls = self.review_factory(
            [{
                "verdict": "PASS", "summary": "no findings", "findings": [],
                "questions": [], "context_requests": [],
            }],
            [],
        )
        code, stdout, stderr = self.cli("run", run_id, workflow_factory=factory)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_CODE_REVIEW")
        return run_id, factory, calls

    def approve_with_presence(self, *args):
        original_run = subprocess.run
        scripts = []

        def approve(argv, *call_args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                scripts.append(argv[-1])
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *call_args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=approve,
        ):
            result = self.cli(*args)
        return result, scripts

    def test_approve_code_accepts_a_review_run_bound_to_its_terminal_pass(self):
        run_id, _factory, _calls = self.passed_review()

        (code, stdout, stderr), scripts = self.approve_with_presence("approve-code", run_id)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_CODE_REVIEW")
        candidate = json.loads(
            next(self.runs.rglob("code-review-candidate.json")).read_text()
        )
        approval = json.loads(next(self.runs.rglob("code-approval.json")).read_text())
        self.assertEqual(approval["run_id"], run_id)
        self.assertEqual(approval["candidate_digest"], hashlib.sha256(
            json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest())
        self.assertIn(approval["candidate_digest"], scripts[0])
        self.assertIsNone(candidate["knowledge_candidate_digest"])

    def test_a_worktree_change_after_pass_invalidates_review_approval(self):
        run_id, _factory, _calls = self.passed_review()
        (self.repo / "Sneaked.txt").write_text("sneaked\n", encoding="utf-8")

        with patch("ai_review.cli.MacOSHumanApprovalProvider.approve_code") as provider:
            code, stdout, stderr = self.cli("approve-code", run_id)

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("worktree", stderr.lower())
        provider.assert_not_called()

    def test_review_writeback_requires_final_human_approval_and_a_candidate(self):
        run_id, _factory, _calls = self.passed_review()
        workspace = self.root / "workspace"
        workspace.mkdir()

        code, stdout, stderr = self.cli("writeback-knowledge", run_id)
        self.assertEqual((code, stdout), (2, ""))
        self.assertNotEqual(stderr, "")

        self.approve_with_presence("approve-code", run_id)
        from ai_review.cli import _writeback_knowledge

        store = self.cli_store()
        state = store.load(run_id)
        with self.assertRaises(Exception):
            # A clean, non-learning Review has no candidate to write back.
            _writeback_knowledge(store, state, workspace_root=workspace)
        self.assertEqual(list(workspace.rglob("*.md")), [])

    def test_a_learning_review_writes_back_only_under_the_second_brain(self):
        run_id, _factory, _calls = self.passed_review()
        from ai_review.cli import _writeback_knowledge

        store = self.cli_store()
        state = store.load(run_id)
        artifacts = store._run_directory(state)
        # A human clarification cycle is one of the six approved triggers.
        store._atomic_write(artifacts / "question-cycles" / "0001" / "questions.json", {
            "cycle": 1, "review_sequence": 1,
            "questions": [{"id": "Q-001", "question": "Cap retries?"}],
        })
        answers, digest = __import__(
            "ai_review.models", fromlist=["canonical_answer_submission"]
        ).canonical_answer_submission({"Q-001": "Cap at three"})
        questions_digest = hashlib.sha256(json.dumps({
            "cycle": 1, "review_sequence": 1,
            "questions": [{"id": "Q-001", "question": "Cap retries?"}],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        store._atomic_write(artifacts / "question-cycles" / "0001" / "answers.json", {
            "cycle": 1, "questions_digest": questions_digest,
            "answers": answers, "digest": digest,
        })
        workspace = self.root / "workspace"
        workspace.mkdir()

        self.approve_with_presence("approve-code", run_id)
        reloaded = self.cli_store()
        written = _writeback_knowledge(
            reloaded, reloaded.load(run_id), workspace_root=workspace,
        )

        self.assertEqual(
            written,
            workspace.resolve() / "second-brain" / "ai-review" / ("%s.md" % run_id),
        )
        self.assertIn("# Knowledge Candidate", written.read_text(encoding="utf-8"))
        self.assertEqual(
            [path.relative_to(workspace).as_posix() for path in workspace.rglob("*.md")],
            ["second-brain/ai-review/%s.md" % run_id],
        )

    def test_changed_paths_is_nul_safe_for_spaces_and_newlines(self):
        from ai_review.git_diff import changed_paths
        spaced = self.repo / "space name.txt"
        newline = self.repo / "line\nbreak.txt"
        spaced.write_text("space\n", encoding="utf-8")
        newline.write_text("newline\n", encoding="utf-8")
        self.assertEqual(changed_paths(self.repo, "HEAD"), ("line\nbreak.txt", "space name.txt"))

    def test_plan_init_prints_one_machine_readable_status_object(self):
        code, stdout, stderr = self.init_plan()

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "READY")
        self.assertEqual(payload["kind"], "plan")
        self.assertEqual(set(payload), {
            "run_id", "kind", "status", "repair_round", "next_action",
            "questions_path", "summary_path", "knowledge_candidate_path",
            "plan_digest", "base_oid",
        })
        self.assertEqual(payload["plan_digest"], hashlib.sha256(self.plan.read_bytes()).hexdigest())

    def test_plan_init_rejects_a_plan_outside_its_repository(self):
        outside = self.root / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")

        code, stdout, stderr = self.cli("init", "plan", "--repo", str(self.repo), "--plan", str(outside))

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Plan file must be inside repository", stderr)

    def test_plan_init_rejects_more_than_three_exact_sources(self):
        sources = []
        for index in range(4):
            source = self.root / ("source-%d.md" % index)
            source.write_text("# Exact\ntext\n", encoding="utf-8")
            sources.extend(["--source", "%s#Exact" % source])

        code, stdout, stderr = self.cli("init", "plan", "--repo", str(self.repo), "--plan", "docs/plan.md", *sources)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("no more than 3", stderr)

    def test_plan_init_rejects_an_unresolvable_base_before_creating_a_run(self):
        code, stdout, stderr = self.cli(
            "init", "plan", "--repo", str(self.repo), "--plan", "docs/plan.md", "--base", "missing-base",
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("base", stderr)
        self.assertFalse(self.runs.exists())

    def test_plan_init_rejects_missing_source_section_before_creating_a_run(self):
        source = self.root / "source.md"
        source.write_text("# Present\n", encoding="utf-8")

        code, stdout, stderr = self.cli(
            "init", "plan", "--repo", str(self.repo), "--plan", "docs/plan.md",
            "--source", "%s#Missing" % source,
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("heading", stderr)
        self.assertFalse(self.runs.exists())

    def test_code_init_rejects_unapproved_plan(self):
        _code, payload, _stderr = self.init_plan()
        run_id = json.loads(payload)["run_id"]
        code, stdout, stderr = self.cli("init", "code", "--repo", str(self.repo), "--plan-run", run_id, "--base", "HEAD")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Plan is not human-approved", stderr)

    def test_status_does_not_mutate_run_bytes(self):
        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]
        state = next(self.runs.rglob("state.json"))
        before = state.read_bytes()

        code, status_stdout, stderr = self.cli("status", run_id)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(status_stdout)["run_id"], run_id)
        self.assertEqual(state.read_bytes(), before)

    def test_status_does_not_touch_run_root_or_approval_key_metadata(self):
        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]

        def snapshot(root):
            return {
                str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
                for path in sorted(root.rglob("*")) if path.is_file()
            }

        before = (snapshot(self.runs), snapshot(self.root / "approval.key") if (self.root / "approval.key").is_dir() else (self.root / "approval.key").stat())
        code, _output, stderr = self.cli("status", run_id)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        after = (snapshot(self.runs), snapshot(self.root / "approval.key") if (self.root / "approval.key").is_dir() else (self.root / "approval.key").stat())
        self.assertEqual(after, before)

    def test_answers_reject_duplicate_json_keys_before_workflow(self):
        payload = self.root / "answers.json"
        payload.write_text('{"Q-001":"first","Q-001":"second"}', encoding="utf-8")

        code, stdout, stderr = self.cli("answer", "run-1", "--answers", str(payload))

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("duplicate keys", stderr)

    def test_executable_cannot_select_a_fake_approval_provider(self):
        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]

        executable = Path(__file__).resolve().parents[1] / "bin" / "ai-review"
        completed = subprocess.run(
            [
                sys.executable, str(executable), "--runs-root", str(self.runs), "approve-plan", run_id,
                "--approval-provider", "fake",
            ],
            text=True, capture_output=True, check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("unrecognized arguments", completed.stderr)

    def test_approve_plan_binds_provider_receipt_actor_digest_and_frozen_oid(self):
        from ai_review.models import HumanApprovalReceipt

        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]
        state = next(self.runs.rglob("state.json"))
        contents = json.loads(state.read_text(encoding="utf-8"))
        contents["status"] = "AWAITING_HUMAN_PLAN_REVIEW"
        state.write_text(json.dumps(contents), encoding="utf-8")
        digest = hashlib.sha256(self.plan.read_bytes()).hexdigest()

        original_run = subprocess.run

        def osascript_approval(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=osascript_approval,
        ):
            code, output, stderr = self.cli("approve-plan", run_id)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(output)["status"], "AWAITING_HUMAN_PLAN_REVIEW")
        signed = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(signed["approval_attestation"]["actor"], "local-macos-user")
        self.assertEqual(signed["manifest"]["plan_digest"], digest)
        self.assertEqual(signed["approval_attestation"]["receipt"]["run_id"], run_id)
        receipt = HumanApprovalReceipt.from_dict(signed["approval_attestation"]["receipt"])
        self.assertEqual(receipt.provider, "macos:osascript-user-presence")
        self.assertEqual(receipt.actor, "local-macos-user")
        self.assertEqual(signed["approval_attestation"]["receipt_digest"], receipt.digest())

    def test_main_has_no_supported_fake_approval_provider_hook(self):
        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]
        state = next(self.runs.rglob("state.json"))
        contents = json.loads(state.read_text(encoding="utf-8"))
        contents["status"] = "AWAITING_HUMAN_PLAN_REVIEW"
        state.write_text(json.dumps(contents), encoding="utf-8")

        signature = inspect.signature(self.main)
        self.assertNotIn("approval_provider_factory", signature.parameters)
        self.assertNotIn("allowed_test_root", signature.parameters)

        with self.assertRaises(TypeError):
            self.main(
                ["--runs-root", str(self.runs), "approve-plan", run_id],
                approval_provider_factory=lambda: None,
            )

        self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["approval_attestation"])

    def test_plan_init_freezes_base_oid_before_head_moves_and_approval(self):
        _code, stdout, _stderr = self.init_plan()
        initial = json.loads(stdout)
        run_id = initial["run_id"]
        frozen_oid = initial["base_oid"]
        self.git("commit", "--allow-empty", "-m", "advance HEAD")
        state = next(self.runs.rglob("state.json"))
        contents = json.loads(state.read_text(encoding="utf-8"))
        contents["status"] = "AWAITING_HUMAN_PLAN_REVIEW"
        state.write_text(json.dumps(contents), encoding="utf-8")

        original_run = subprocess.run

        def osascript_approval(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=osascript_approval,
        ):
            code, _output, stderr = self.cli("approve-plan", run_id)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(state.read_text())["manifest"]["base_oid"], frozen_oid)

    def test_approve_plan_rejects_a_tampered_repository_before_calling_provider(self):
        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]
        state = next(self.runs.rglob("state.json"))
        contents = json.loads(state.read_text(encoding="utf-8"))
        contents["status"] = "AWAITING_HUMAN_PLAN_REVIEW"
        contents["manifest"]["repo_path"] = str(self.root / "not-a-repository")
        state.write_text(json.dumps(contents), encoding="utf-8")
        original_run = subprocess.run
        osascript_calls = []

        def no_osascript(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                osascript_calls.append(argv)
                raise AssertionError("approval dialog must not run for an invalid repository")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.subprocess.run", side_effect=no_osascript):
            code, output, stderr = self.cli("approve-plan", run_id)

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertEqual(osascript_calls, [])
        self.assertIn("repository", stderr)

    def test_code_init_requires_explicit_base_matching_signed_oid(self):
        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]
        state = next(self.runs.rglob("state.json"))
        contents = json.loads(state.read_text(encoding="utf-8"))
        contents["status"] = "AWAITING_HUMAN_PLAN_REVIEW"
        state.write_text(json.dumps(contents), encoding="utf-8")
        code, output, stderr = self.cli("init", "code", "--repo", str(self.repo), "--plan-run", run_id)

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("--base", stderr)

        code, output, stderr = self.cli("init", "code", "--repo", str(self.repo), "--plan-run", run_id)

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("--base", stderr)

    def test_run_uses_injected_workflow_factory_and_maps_interruptions(self):
        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]

        class InterruptedWorkflow:
            def run(self, ignored):
                raise KeyboardInterrupt()

        def factory(**kwargs):
            self.assertEqual(kwargs["kind"], "plan")
            return InterruptedWorkflow()

        code, output, stderr = self.cli("run", run_id, workflow_factory=factory)

        self.assertEqual(code, 3)
        self.assertEqual(output, "")
        self.assertIn("interrupted", stderr.lower())

    def test_runner_interruption_emits_persisted_pause_summary_and_exit_three(self):
        from ai_review.models import Status
        from ai_review.runners import RunnerInterrupted

        _code, stdout, _stderr = self.init_plan()
        run_id = json.loads(stdout)["run_id"]

        class InterruptedWorkflow:
            def __init__(self, store):
                self.store = store

            def run(self, selected):
                state = self.store.load(selected)
                object.__setattr__(state, "status", Status.PAUSED)
                self.store.save(state)
                self.store._atomic_write(
                    self.store._run_directory(state) / "pause.json",
                    {"reason": "RUNNER_INTERRUPTED", "detail": "timed out"},
                )
                raise RunnerInterrupted("timed out")

        code, output, stderr = self.cli(
            "run", run_id,
            workflow_factory=lambda **kwargs: InterruptedWorkflow(kwargs["store"]),
        )

        self.assertEqual(code, 3)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "PAUSED")
        self.assertTrue(Path(payload["summary_path"]).is_file())
        self.assertIn("interrupted", stderr.lower())

    def test_local_claude_uses_resolution_schema_for_plan_findings(self):
        from ai_review.cli import _LocalClaude

        claude = _LocalClaude(self.repo)
        claude._call = Mock(return_value={})

        claude.resolve({"findings": []})

        self.assertEqual(claude._call.call_args.args[:2], ("claude-resolution.schema.json", "claude-fix.md"))

    def test_external_argument_failure_is_classified_without_raw_stderr(self):
        from ai_review.cli import _run_external
        from ai_review.runners import RunnerError

        with patch("ai_review.cli.subprocess.run", return_value=subprocess.CompletedProcess(
            ["codex"], 2, "", "error: unexpected argument --ask token=do-not-leak",
        )):
            with self.assertRaises(RunnerError) as raised:
                _run_external(["codex", "bad"], self.repo)

        self.assertEqual(str(raised.exception), "CLI_ARG_ERROR exit=2")
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_local_codex_fails_closed_on_nonzero_exit_even_with_valid_output(self):
        from ai_review.cli import _LocalCodex
        from ai_review.runners import RunnerError

        review = {
            "verdict": "PASS", "summary": "valid structured result",
            "findings": [], "questions": [], "context_requests": [],
        }
        observed = []

        def codex_result(argv, **_kwargs):
            output = Path(argv[argv.index("-o") + 1])
            observed.append(output)
            self.assertFalse(output.exists())
            output.write_text(json.dumps(review), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 1, "", "token=never-leak")

        with patch("ai_review.cli.subprocess.run", side_effect=codex_result):
            with self.assertRaises(RunnerError) as raised:
                _LocalCodex(self.repo, model="gpt-5.6-sol").review({"plan": "# Plan\n"})

        self.assertEqual(str(raised.exception), "EXTERNAL_EXIT exit=1")
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].name == "review.json")

    def test_local_codex_rejects_nonzero_exit_without_a_fresh_valid_result(self):
        from ai_review.cli import _LocalCodex
        from ai_review.runners import RunnerError

        def no_output(argv, **_kwargs):
            output = Path(argv[argv.index("-o") + 1])
            self.assertFalse(output.exists())
            return subprocess.CompletedProcess(argv, 1, "", "token=never-leak")

        with patch("ai_review.cli.subprocess.run", side_effect=no_output):
            with self.assertRaisesRegex(RunnerError, "EXTERNAL_EXIT exit=1") as raised:
                _LocalCodex(self.repo, model="gpt-5.6-sol").review({"plan": "# Plan\n"})

        self.assertNotIn("never-leak", str(raised.exception))

    def test_local_codex_rejects_nonzero_exit_with_invalid_or_semantically_invalid_result(self):
        from ai_review.cli import _LocalCodex
        from ai_review.runners import RunnerError

        for contents in (
            '{"verdict":"PASS","verdict":"PASS"}',
            json.dumps({
                "verdict": "PASS", "summary": "blocker contradicts pass",
                "findings": [{
                    "id": "F-1", "severity": "blocker", "invariant": "safe",
                    "location": "plan.md:1", "evidence": "bad", "required_outcome": "fix",
                    "lineage": {"resolution": "fixed"},
                }],
                "questions": [], "context_requests": [],
            }),
        ):
            with self.subTest(contents=contents):
                def invalid_output(argv, **_kwargs):
                    Path(argv[argv.index("-o") + 1]).write_text(contents, encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 1, "", "invalid output")

                with patch("ai_review.cli.subprocess.run", side_effect=invalid_output):
                    with self.assertRaises(RunnerError):
                        _LocalCodex(
                            self.repo, model="gpt-5.6-sol",
                        ).review({"plan": "# Plan\n"})

    def test_authority_storage_error_is_a_bounded_invalid_input_response(self):
        def unavailable(_args):
            raise PermissionError("token=should-not-escape")

        code, stdout, stderr = self.cli("status", "run-1", store_factory=unavailable)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("should-not-escape", stderr)

    # ---- doc run kind ----------------------------------------------------

    DOC_BRIEF = "  離線編輯的 RD spec，給 PM 與 QA 讀  "

    def write_doc(self, text="# 離線編輯\n\n使用者可在離線時編輯內容。\n"):
        """Commit one document inside the repository and return its path."""
        document = self.repo / "docs" / "rd-spec.md"
        document.write_text(text, encoding="utf-8")
        self.git("add", "docs/rd-spec.md")
        self.git("commit", "-m", "rd spec")
        return document

    def init_doc(self, *extra, doc="docs/rd-spec.md", lens="requirement"):
        return self.cli(
            "init", "doc", "--repo", str(self.repo), "--doc", doc,
            "--lens", lens,
            "--lens-reason", "文件只描述使用者行為，沒有任何檔案路徑",
            "--brief", self.DOC_BRIEF,
            *extra,
        )

    def doc_state(self):
        return json.loads(
            next(self.runs.rglob("state.json")).read_text(encoding="utf-8")
        )

    def test_doc_init_rejects_a_verification_flag_outright(self):
        """A doc run has no verification; --verify must fail, not be ignored."""
        self.write_doc()

        code, stdout, stderr = self.init_doc(
            "--verify", "%s -m unittest tests.test_doc" % sys.executable,
        )

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("unrecognized arguments", stderr)
        self.assertFalse(self.runs.exists())

    def test_doc_init_requires_a_known_lens_with_a_reason_and_a_brief(self):
        self.write_doc()
        base = (
            "init", "doc", "--repo", str(self.repo), "--doc", "docs/rd-spec.md",
        )
        reason = ("--lens-reason", "只描述使用者行為")
        brief = ("--brief", "給 PM 讀的 spec")
        for tail in (
            ("--lens", "architecture") + reason + brief,   # not one of the three
            ("--lens", "") + reason + brief,               # empty is not a lens
            ("--lens", "requirement") + brief,             # missing --lens-reason
            ("--lens", "requirement") + reason,            # missing --brief
            reason + brief,                                # missing --lens
        ):
            with self.subTest(tail=tail):
                code, stdout, _stderr = self.cli(*(base + tail))
                self.assertEqual((code, stdout), (2, ""))
        self.assertFalse(self.runs.exists())

        # Positive control: the same command with all three accepted arguments
        # must succeed, so the rejections above are about the arguments and not
        # about the subcommand being unreachable.
        for lens in ("requirement", "direction", "implementation"):
            with self.subTest(lens=lens):
                code, _stdout, stderr = self.cli(
                    *(base + ("--lens", lens) + reason + brief)
                )
                self.assertEqual((code, stderr), (0, ""))

    def test_doc_init_rejects_a_document_outside_missing_or_not_a_file(self):
        self.write_doc()
        outside = self.root / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")

        code, stdout, stderr = self.init_doc(doc=str(outside))
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("inside repository", stderr)

        code, stdout, stderr = self.init_doc(doc="docs")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("regular file", stderr)

        code, stdout, stderr = self.init_doc(doc="docs/absent.md")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("regular file", stderr)

        self.assertFalse(self.runs.exists())

    def test_doc_init_binds_the_lens_and_the_exact_document_and_brief_bytes(self):
        document = self.write_doc()
        expected_doc_digest = hashlib.sha256(document.read_bytes()).hexdigest()
        expected_brief_digest = hashlib.sha256(
            self.DOC_BRIEF.strip().encode("utf-8")
        ).hexdigest()
        head = self.git("rev-parse", "HEAD").stdout.strip()

        code, stdout, stderr = self.init_doc(lens="direction")

        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["kind"], "doc")
        self.assertEqual(payload["status"], "READY")
        manifest = self.doc_state()["manifest"]
        self.assertEqual(manifest["kind"], "doc")
        self.assertEqual(manifest["lens"], "direction")
        self.assertEqual(manifest["lens_reason"], "文件只描述使用者行為，沒有任何檔案路徑")
        self.assertEqual(manifest["doc_digest"], expected_doc_digest)
        self.assertEqual(manifest["brief_digest"], expected_brief_digest)
        self.assertEqual(manifest["brief"], self.DOC_BRIEF.strip())
        self.assertEqual(manifest["doc_path"], str(document.resolve()))
        self.assertEqual(manifest["base_oid"], head)
        self.assertEqual(manifest["base_ref"], "HEAD")
        self.assertEqual(set(manifest["review_executables"]), {"codex", "claude"})
        self.assertNotIn("plan_path", manifest)
        self.assertNotIn("verification_commands", manifest)

    def test_doc_init_never_builds_a_verification_command(self):
        """A doc run runs nothing, so the verification builder must stay untouched."""
        self.write_doc()

        def tripwire(*args, **kwargs):
            raise AssertionError("a doc run must never build a verification command")

        with patch("ai_review.cli._test_commands", side_effect=tripwire):
            code, _stdout, stderr = self.init_doc()

        self.assertEqual((code, stderr), (0, ""))

    def test_doc_init_accepts_five_sources_under_the_doc_budget_and_rejects_six(self):
        """The doc budget is the policy's, not the Plan default the CLI used to inherit."""
        from ai_review.context import BUDGET_METHOD_DOC
        from ai_review.policy import load_policy

        self.write_doc()
        policy = load_policy(
            Path(__file__).resolve().parents[1] / "config" / "defaults.yaml"
        )
        max_sources, max_tokens = policy.context_limits("doc")
        self.assertEqual((max_sources, max_tokens), (5, 16000))
        self.assertNotEqual(policy.context_limits("plan"), (max_sources, max_tokens))

        selected = []
        for index in range(max_sources + 1):
            source = self.root / ("doc-source-%d.md" % index)
            source.write_text("# Exact\n約束 %d。\n" % index, encoding="utf-8")
            selected.extend(["--source", "%s#Exact" % source])

        code, stdout, stderr = self.init_doc(*selected[: 2 * max_sources])
        self.assertEqual((code, stderr), (0, ""))
        manifest = json.loads(
            next(self.runs.rglob("context-manifest.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["selected"]), max_sources)
        self.assertEqual(manifest["max_context_tokens"], max_tokens)
        self.assertEqual(manifest["budget_method"], BUDGET_METHOD_DOC)
        self.assertRegex(self.doc_state()["manifest"]["context_checksum"], r"^[0-9a-f]{64}$")

        code, stdout, stderr = self.cli(
            "init", "doc", "--repo", str(self.repo), "--doc", "docs/rd-spec.md",
            "--lens", "requirement", "--lens-reason", "行為描述", "--brief", "spec",
            *selected,
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("no more than %d" % max_sources, stderr)

    def test_local_codex_doc_mode_sends_the_prompt_for_its_lens(self):
        from ai_review.cli import _LocalCodex
        from ai_review.process_security import executable_identity

        identity = executable_identity(Path(sys.executable))
        prompts = Path(__file__).resolve().parents[1] / "prompts"
        for lens in ("requirement", "direction", "implementation"):
            with self.subTest(lens=lens):
                expected = (prompts / ("codex-doc-%s.md" % lens)).read_text(encoding="utf-8")
                sent = []

                def capture(repo, schema, output, prompt, *, model, executable):
                    sent.append(prompt)
                    return [executable]

                with patch("ai_review.cli.build_codex_argv", side_effect=capture), patch(
                    "ai_review.cli._run_codex_with_output", return_value={"verdict": "PASS"},
                ):
                    _LocalCodex(
                        self.repo, identity, mode="doc", lens=lens, model="gpt-5.6-sol",
                    ).review({"document": "# Doc\n"})

                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0].startswith(expected))
                self.assertIn("INPUT_JSON", sent[0])

        for rejected in (
            {"mode": "doc"},
            {"mode": "doc", "lens": "architecture"},
            {"mode": "plan", "lens": "requirement"},
        ):
            with self.subTest(**rejected):
                with self.assertRaises(ValueError):
                    _LocalCodex(self.repo, identity, model="gpt-5.6-sol", **rejected)

    def test_default_workflow_factory_builds_a_claude_free_doc_workflow(self):
        from ai_review.cli import _LocalCodex, _default_workflow_factory
        from ai_review.doc_workflow import DocWorkflow
        from ai_review.workflow import WorkflowError

        self.write_doc()
        code, stdout, stderr = self.init_doc(lens="implementation")
        self.assertEqual((code, stderr), (0, ""))
        store = self.cli_store()
        state = store.load(json.loads(stdout)["run_id"])

        workflow = _default_workflow_factory(kind="doc", store=store, state=state)

        self.assertIsInstance(workflow, DocWorkflow)
        self.assertIsInstance(workflow.codex, _LocalCodex)
        self.assertEqual(workflow.codex.mode, "doc")
        self.assertEqual(workflow.codex.lens, "implementation")
        with self.assertRaises(WorkflowError):
            workflow.claude.repair({})

    def test_every_codex_construction_path_pins_the_policy_model_in_its_argv(self):
        """Regression: no ``-m`` anywhere, so every kind inherited the global config.

        A change to ``~/.codex/config.toml`` -- a file this tool does not own
        -- silently broke all four kinds at once, and the operator only saw
        ``EXTERNAL_EXIT exit=1``.  This asserts on the argv that actually
        reaches the subprocess boundary, for the doc kind and for a non-doc
        kind, because that boundary is where the omission lived.
        """
        from ai_review.cli import _LocalCodex, _default_workflow_factory
        from ai_review.policy import load_policy
        from ai_review.process_security import executable_identity

        expected = load_policy(
            Path(__file__).resolve().parents[1] / "config" / "defaults.yaml"
        ).codex_model
        review = {
            "verdict": "PASS", "summary": "structurally valid result",
            "findings": [], "questions": [], "context_requests": [],
        }
        store = self.cli_store()

        self.write_doc()
        code, stdout, stderr = self.init_doc()
        self.assertEqual((code, stderr), (0, ""))
        doc_state = store.load(json.loads(stdout)["run_id"])

        code, stdout, stderr = self.init_plan()
        self.assertEqual((code, stderr), (0, ""))
        plan_state = store.load(json.loads(stdout)["run_id"])

        for kind, state, inputs in (
            ("doc", doc_state, {"document": "# Doc\n"}),
            ("plan", plan_state, {"plan": "# Plan\n"}),
        ):
            with self.subTest(kind=kind):
                workflow = _default_workflow_factory(kind=kind, store=store, state=state)
                # Keep the identity gate deterministic; the model is what is
                # under test, and it is set before this line by construction.
                workflow.codex.identity = executable_identity(Path(sys.executable))
                observed = []

                def record(argv, **_kwargs):
                    observed.append(list(argv))
                    Path(argv[argv.index("-o") + 1]).write_text(
                        json.dumps(review), encoding="utf-8"
                    )
                    return subprocess.CompletedProcess(argv, 0, "", "")

                with patch("ai_review.cli.subprocess.run", side_effect=record):
                    workflow.codex.review(inputs)

                self.assertEqual(len(observed), 1)
                argv = observed[0]
                self.assertEqual(argv.count("-m"), 1)
                self.assertEqual(argv[argv.index("-m") + 1], expected)
                self.assertEqual(argv[argv.index("-C") + 1], str(self.repo.resolve()))

        # No construction path may omit it: there is no default to inherit.
        with self.assertRaises(TypeError):
            _LocalCodex(self.repo, mode="plan")

        # The argv above proves the pin reaches the process boundary.  This
        # proves every construction site in the factory states the model for
        # itself, for all four kinds, rather than leaving it to a fall back.
        for kind, state in (
            ("doc", doc_state), ("plan", plan_state),
            ("code", plan_state), ("review", plan_state),
        ):
            with self.subTest(construction=kind):
                with patch("ai_review.cli._LocalCodex") as constructor:
                    _default_workflow_factory(kind=kind, store=store, state=state)

                self.assertEqual(constructor.call_count, 1)
                self.assertEqual(constructor.call_args.kwargs.get("model"), expected)

    def test_doc_payload_reads_doc_path_and_knows_every_doc_next_action(self):
        """Regression: _payload raised KeyError on the status and AttributeError on plan_path."""
        from ai_review.cli import _payload
        from ai_review.models import DocManifest, RunState, Status

        document = self.write_doc()
        brief = "給 PM 與 QA 讀的 spec"
        manifest = DocManifest(
            kind="doc", repo_path=str(self.repo), doc_path=str(document),
            base_ref="HEAD", base_oid=self.git("rev-parse", "HEAD").stdout.strip(),
            lens="direction", lens_reason="文件在挑做法", brief=brief,
            brief_digest=hashlib.sha256(brief.encode("utf-8")).hexdigest(),
            doc_digest=hashlib.sha256(document.read_bytes()).hexdigest(),
        )
        state = RunState.new_doc(manifest)
        object.__setattr__(state, "run_id", "doc-payload")
        store = Mock()
        store._run_directory.return_value = self.runs / "project" / "doc-payload"
        store.artifact_exists.return_value = False

        for status in (Status.READY, Status.RUNNING, Status.AWAITING_USER_INPUT,
                       Status.PAUSED, Status.AWAITING_HUMAN_DOC_REVIEW):
            with self.subTest(status=status):
                object.__setattr__(state, "status", status)
                payload = _payload(store, state)
                self.assertTrue(payload["next_action"])
                self.assertEqual(
                    payload["doc_digest"],
                    hashlib.sha256(document.read_bytes()).hexdigest(),
                )
                self.assertEqual(payload["lens"], "direction")
                self.assertEqual(payload["brief_digest"], manifest.brief_digest)
                self.assertNotIn("plan_digest", payload)

    def test_status_on_a_finished_doc_run_reports_its_document_digest(self):
        document = self.write_doc()
        code, stdout, stderr = self.init_doc()
        self.assertEqual((code, stderr), (0, ""))
        run_id = json.loads(stdout)["run_id"]
        state_path = next(self.runs.rglob("state.json"))
        contents = json.loads(state_path.read_text(encoding="utf-8"))
        contents["status"] = "AWAITING_HUMAN_DOC_REVIEW"
        state_path.write_text(json.dumps(contents), encoding="utf-8")

        code, stdout, stderr = self.cli("status", run_id)

        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "AWAITING_HUMAN_DOC_REVIEW")
        self.assertEqual(payload["next_action"], "human_doc_review")
        self.assertEqual(
            payload["doc_digest"], hashlib.sha256(document.read_bytes()).hexdigest()
        )
        self.assertEqual(payload["lens"], "requirement")
        self.assertEqual(
            payload["brief_digest"],
            hashlib.sha256(self.DOC_BRIEF.strip().encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("plan_digest", payload)

    def test_a_finished_doc_run_generates_its_report(self):
        from ai_review.doc_workflow import DocWorkflow

        self.write_doc()
        code, stdout, stderr = self.init_doc()
        self.assertEqual((code, stderr), (0, ""))
        run_id = json.loads(stdout)["run_id"]

        class PassingCodex:
            def review(_self, inputs):
                return {
                    "verdict": "PASS", "summary": "the document is clear",
                    "findings": [], "questions": [], "context_requests": [],
                }

        def factory(*, kind, store, state, context_packet=None):
            self.assertEqual(kind, "doc")
            return DocWorkflow(store, PassingCodex(), None, context_packet=context_packet)

        with patch("ai_review.cli.generate_outputs") as outputs:
            code, stdout, stderr = self.cli("run", run_id, workflow_factory=factory)

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_DOC_REVIEW")
        self.assertEqual(
            [call.args[1] for call in outputs.call_args_list], [run_id]
        )

    # ---- re-review -------------------------------------------------------

    EDITED_DOC = (
        "# 離線編輯\n\n使用者可在離線時編輯內容。\n\n"
        "## 衝突\n兩邊都改過時，以最後存檔為準。\n"
    )

    def doc_finding(self, identifier):
        return {
            "id": identifier,
            "severity": "major",
            "invariant": "every scenario states its completion condition",
            "location": "docs/rd-spec.md:3",
            "evidence": "the scenario states no observable result",
            "required_outcome": "state what proves the scenario finished",
            "lineage": {
                "resolution": "newly_discovered",
                "discovery_reason": "first pass over this document",
            },
        }

    def doc_review(self, *finding_ids):
        """One valid Codex doc review; findings make it CHANGES_REQUIRED."""
        return {
            "verdict": "CHANGES_REQUIRED" if finding_ids else "PASS",
            "summary": "document review result",
            "findings": [self.doc_finding(item) for item in finding_ids],
            "questions": [],
            "context_requests": [],
        }

    def doc_factory(self, codex, *, fault_injector=None):
        """Build the real DocWorkflow around a recorded Codex boundary."""
        from ai_review.doc_workflow import DocWorkflow

        def factory(*, kind, store, state, context_packet=None):
            self.assertEqual(kind, "doc")
            return DocWorkflow(
                store, codex, None, context_packet=context_packet,
                fault_injector=fault_injector,
            )

        return factory

    def tripwire_factory(self):
        def factory(**kwargs):
            raise AssertionError("a refused re-review must never build a workflow")

        return factory

    def run_directory(self, run_id):
        matches = [
            path.parent for path in self.runs.rglob("state.json")
            if path.parent.name == run_id
        ]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def edit_doc(self):
        """Edit the reviewed document the way a human would: in the worktree."""
        document = self.repo / "docs" / "rd-spec.md"
        document.write_text(self.EDITED_DOC, encoding="utf-8")
        return document

    def finished_doc_run(self, *, finding_ids=("DOC-001",), extra_responses=()):
        """Init one doc run and take it through a first Codex round."""
        self.write_doc()
        code, stdout, stderr = self.init_doc()
        self.assertEqual((code, stderr), (0, ""))
        run_id = json.loads(stdout)["run_id"]
        codex = RecordingCodex([self.doc_review(*finding_ids), *extra_responses])
        with patch("ai_review.cli.generate_outputs"):
            code, stdout, stderr = self.cli(
                "run", run_id, workflow_factory=self.doc_factory(codex),
            )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_DOC_REVIEW")
        self.assertEqual(len(codex.calls), 1)
        return run_id, codex

    def test_re_review_refuses_an_unchanged_document(self):
        """An unchanged document would burn a Codex session for known findings."""
        # The second response is a loaded gun: it must not fire.
        run_id, codex = self.finished_doc_run(extra_responses=(self.doc_review(),))
        directory = self.run_directory(run_id)
        before = (directory / "state.json").read_bytes()

        code, stdout, stderr = self.cli(
            "re-review", run_id, workflow_factory=self.doc_factory(codex),
        )

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("unchanged", stderr)
        self.assertEqual((directory / "state.json").read_bytes(), before)
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(
            sorted(path.name for path in (directory / "reviews").iterdir()),
            ["0001.json"],
        )
        self.assertEqual(
            sorted(path.name for path in (directory / "review-actions").iterdir()),
            ["0001.json"],
        )

        code, stdout, stderr = self.cli("status", run_id)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_DOC_REVIEW")

    def test_re_review_of_an_edited_document_runs_one_more_codex_pass(self):
        run_id, codex = self.finished_doc_run(extra_responses=(self.doc_review(),))
        directory = self.run_directory(run_id)
        document = self.edit_doc()
        edited_bytes = document.read_bytes()

        with patch("ai_review.cli.generate_outputs") as outputs:
            code, stdout, stderr = self.cli(
                "re-review", run_id, workflow_factory=self.doc_factory(codex),
            )

        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "AWAITING_HUMAN_DOC_REVIEW")
        self.assertEqual(payload["kind"], "doc")
        self.assertEqual(payload["repair_round"], 0)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(
            sorted(path.name for path in (directory / "reviews").iterdir()),
            ["0001.json", "0002.json"],
        )
        self.assertEqual(
            json.loads(
                (directory / "reviews" / "0002.json").read_text(encoding="utf-8")
            )["verdict"],
            "PASS",
        )
        # The reviewed document is the human's; nothing in the round writes it.
        self.assertEqual(document.read_bytes(), edited_bytes)
        # The terminal report still fires for the second round.
        self.assertEqual([call.args[1] for call in outputs.call_args_list], [run_id])

    def test_the_second_round_carries_the_first_rounds_findings(self):
        """Without this, a run that forgets every round looks identical outside."""
        run_id, codex = self.finished_doc_run(
            finding_ids=("DOC-001", "DOC-002"), extra_responses=(self.doc_review(),),
        )
        self.edit_doc()

        with patch("ai_review.cli.generate_outputs"):
            code, _stdout, stderr = self.cli(
                "re-review", run_id, workflow_factory=self.doc_factory(codex),
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(codex.calls[0]["unresolved_prior_findings"], [])
        self.assertEqual(
            [item["id"] for item in codex.calls[1]["unresolved_prior_findings"]],
            ["DOC-001", "DOC-002"],
        )
        # The same review, of the changed bytes.
        self.assertEqual(codex.calls[1]["document"], self.EDITED_DOC)
        self.assertEqual(codex.calls[1]["lens"], codex.calls[0]["lens"])
        self.assertEqual(codex.calls[1]["brief"], codex.calls[0]["brief"])

    def test_re_review_rebinds_the_manifest_to_the_new_document_bytes(self):
        run_id, codex = self.finished_doc_run(extra_responses=(self.doc_review(),))
        directory = self.run_directory(run_id)
        before = json.loads(
            (directory / "state.json").read_text(encoding="utf-8")
        )["manifest"]
        document = self.edit_doc()
        expected = hashlib.sha256(document.read_bytes()).hexdigest()
        self.assertNotEqual(expected, before["doc_digest"])

        with patch("ai_review.cli.generate_outputs"):
            code, stdout, stderr = self.cli(
                "re-review", run_id, workflow_factory=self.doc_factory(codex),
            )

        self.assertEqual((code, stderr), (0, ""))
        manifest = json.loads(
            (directory / "state.json").read_text(encoding="utf-8")
        )["manifest"]
        self.assertEqual(manifest["doc_digest"], expected)
        self.assertEqual(manifest, dict(before, doc_digest=expected))
        self.assertEqual(json.loads(stdout)["doc_digest"], expected)

    def test_re_review_refuses_every_other_run_kind(self):
        from ai_review.models import Status

        _code, stdout, _stderr = self.init_plan()
        plan_run = json.loads(stdout)["run_id"]
        code, stdout, stderr = self.cli(
            "re-review", plan_run, workflow_factory=self.tripwire_factory(),
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("doc run", stderr)

        review_run = self.approved_review()
        code, stdout, stderr = self.cli(
            "re-review", review_run, workflow_factory=self.tripwire_factory(),
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("doc run", stderr)

        code_run = self.code_run_from_approved_plan()
        code, stdout, stderr = self.cli(
            "re-review", code_run, workflow_factory=self.tripwire_factory(),
        )
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("doc run", stderr)

    def code_run_from_approved_plan(self):
        """Approve one Plan through the real gate and return its Code run id."""
        _code, stdout, _stderr = self.init_plan()
        plan_run = json.loads(stdout)["run_id"]
        state = self.run_directory(plan_run) / "state.json"
        contents = json.loads(state.read_text(encoding="utf-8"))
        contents["status"] = "AWAITING_HUMAN_PLAN_REVIEW"
        state.write_text(json.dumps(contents), encoding="utf-8")
        original_run = subprocess.run

        def approve(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=approve,
        ):
            code, _output, stderr = self.cli("approve-plan", plan_run)
        self.assertEqual((code, stderr), (0, ""))
        code, stdout, stderr = self.cli(
            "init", "code", "--repo", str(self.repo), "--plan-run", plan_run,
            "--base", "HEAD",
        )
        self.assertEqual((code, stderr), (0, ""))
        return json.loads(stdout)["run_id"]

    def test_re_review_refuses_a_doc_run_that_is_not_awaiting_its_reader(self):
        self.write_doc()
        code, stdout, stderr = self.init_doc()
        self.assertEqual((code, stderr), (0, ""))
        run_id = json.loads(stdout)["run_id"]
        state = self.run_directory(run_id) / "state.json"
        self.edit_doc()

        for status in ("READY", "RUNNING", "PAUSED"):
            with self.subTest(status=status):
                contents = json.loads(state.read_text(encoding="utf-8"))
                contents["status"] = status
                state.write_text(json.dumps(contents), encoding="utf-8")
                before = state.read_bytes()

                code, stdout, stderr = self.cli(
                    "re-review", run_id, workflow_factory=self.tripwire_factory(),
                )

                self.assertEqual((code, stdout), (2, ""))
                # The operator is told which status they actually have.
                self.assertIn(status, stderr)
                self.assertEqual(state.read_bytes(), before)

    def test_re_review_of_a_deleted_document_fails_cleanly(self):
        run_id, codex = self.finished_doc_run()
        directory = self.run_directory(run_id)
        before = (directory / "state.json").read_bytes()
        (self.repo / "docs" / "rd-spec.md").unlink()

        code, stdout, stderr = self.cli(
            "re-review", run_id, workflow_factory=self.tripwire_factory(),
        )

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("document", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stderr.count("\n"), 1)
        self.assertEqual((directory / "state.json").read_bytes(), before)
        self.assertEqual(len(codex.calls), 1)

    def test_re_review_takes_no_flag_that_bypasses_the_unchanged_refusal(self):
        run_id, _codex = self.finished_doc_run()

        for flag in ("--force", "--lens", "--doc"):
            with self.subTest(flag=flag):
                code, stdout, stderr = self.cli(
                    "re-review", run_id, flag, "x",
                    workflow_factory=self.tripwire_factory(),
                )
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("unrecognized arguments", stderr)

    # ---- a malformed answers file must not destroy the run ---------------
    #
    # Defect from a real dry run against Codex: `answer` validated the answers
    # inside `WorkflowError`-catching code, so a file keyed by question text
    # instead of question id paused the run (`INVALID_DOC_USER_ANSWER`).  No
    # command returns a PAUSED run to `AWAITING_USER_INPUT`, so one typo threw
    # away a completed, separately billed Codex round.  These tests assert the
    # state after the refusal, not merely the exit code.

    DOC_QUESTIONS = (
        "功能上線後，既有使用者預設是否啟用離線編輯？",
        "離線修改與雲端版本衝突時，應以哪一邊為準？",
    )

    def doc_questions_review(self, *questions):
        """One valid Codex doc review that parks the run on its questions."""
        return {
            "verdict": "NEEDS_USER_INPUT",
            "summary": "the document leaves the licence switch ambiguous",
            "findings": [],
            "questions": list(questions or self.DOC_QUESTIONS),
            "context_requests": [],
        }

    def parked_doc_run(self, *, extra_responses=()):
        """Init one doc run and park it at AWAITING_USER_INPUT, as Codex did."""
        self.write_doc()
        code, stdout, stderr = self.init_doc()
        self.assertEqual((code, stderr), (0, ""))
        run_id = json.loads(stdout)["run_id"]
        codex = RecordingCodex([self.doc_questions_review(), *extra_responses])
        code, stdout, stderr = self.cli(
            "run", run_id, workflow_factory=self.doc_factory(codex),
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_USER_INPUT")
        persisted = json.loads(
            (self.run_directory(run_id) / "user-questions.json").read_text(encoding="utf-8")
        )["questions"]
        # The artifact keys the answers file: ids, with the text beside them.
        self.assertEqual([item["id"] for item in persisted], ["Q-001", "Q-002"])
        self.assertEqual(
            [item["question"] for item in persisted], list(self.DOC_QUESTIONS),
        )
        return run_id, codex

    def answers_file(self, mapping, name="answers.json"):
        path = self.root / name
        path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def run_snapshot(self, run_id):
        """Every artifact byte of the run, so a refusal can be proved inert."""
        directory = self.run_directory(run_id)
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*")) if path.is_file()
        }

    def no_workflow_factory(self):
        def factory(**_kwargs):
            raise AssertionError(
                "a malformed answers file must be refused before any workflow"
            )

        return factory

    def assert_still_parked(self, run_id, before):
        """The run is byte-identical and still answerable."""
        directory = self.run_directory(run_id)
        self.assertEqual(self.run_snapshot(run_id), before)
        self.assertFalse((directory / "pause.json").exists())
        code, stdout, stderr = self.cli("status", run_id)
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "AWAITING_USER_INPUT")
        self.assertEqual(payload["next_action"], "answer")

    def test_answers_keyed_by_question_text_leave_the_run_awaiting_input(self):
        """The exact defect: the wording the skill shipped, refused harmlessly."""
        run_id, codex = self.parked_doc_run()
        before = self.run_snapshot(run_id)
        payload = self.answers_file(
            {question: "ANSWER-BODY-%d" % index
             for index, question in enumerate(self.DOC_QUESTIONS)}
        )

        code, stdout, stderr = self.cli(
            "answer", run_id, "--answers", payload,
            workflow_factory=self.no_workflow_factory(),
        )

        self.assertEqual((code, stdout), (2, ""))
        # An operator can act on this: the ids expected, and what was missing.
        self.assertIn("Q-001", stderr)
        self.assertIn("Q-002", stderr)
        self.assertIn("id", stderr)
        # The user's own words are never echoed back out.
        self.assertNotIn("ANSWER-BODY-0", stderr)
        self.assertNotIn("ANSWER-BODY-1", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assert_still_parked(run_id, before)
        self.assertEqual(len(codex.calls), 1)

    def test_every_malformed_answers_file_is_refused_and_changes_nothing(self):
        run_id, codex = self.parked_doc_run()
        before = self.run_snapshot(run_id)
        cases = {
            "missing one id": ({"Q-001": "只答了第一題"}, True),
            "one extra unknown id": (
                {"Q-001": "答案一", "Q-002": "答案二", "Q-009": "多的"}, True,
            ),
            "an empty-string value": ({"Q-001": "答案一", "Q-002": "   "}, True),
            # Caught one layer out by the pre-existing JSON-shape guard, which
            # also runs before the run is loaded; same exit code, same inert run.
            "a non-string value": ({"Q-001": "答案一", "Q-002": 5}, False),
        }
        for label, (mapping, names_ids) in cases.items():
            with self.subTest(case=label):
                payload = self.answers_file(mapping, "answers-%d.json" % len(label))

                code, stdout, stderr = self.cli(
                    "answer", run_id, "--answers", payload,
                    workflow_factory=self.no_workflow_factory(),
                )

                self.assertEqual((code, stdout), (2, ""))
                self.assertNotIn("Traceback", stderr)
                if names_ids:
                    self.assertIn("Q-001", stderr)
                    self.assertIn("Q-002", stderr)
                self.assert_still_parked(run_id, before)
        self.assertEqual(len(codex.calls), 1)

    def test_an_id_keyed_answers_file_still_resumes_the_run(self):
        run_id, codex = self.parked_doc_run(extra_responses=(self.doc_review(),))
        payload = self.answers_file({"Q-001": "預設啟用", "Q-002": "以最後存檔為準"})

        with patch("ai_review.cli.generate_outputs"):
            code, stdout, stderr = self.cli(
                "answer", run_id, "--answers", payload,
                workflow_factory=self.doc_factory(codex),
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_DOC_REVIEW")
        self.assertEqual(len(codex.calls), 2)
        submission = json.loads((
            self.run_directory(run_id) / "question-cycles" / "0001" / "answers.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(
            submission["answers"],
            {"Q-001": "預設啟用", "Q-002": "以最後存檔為準"},
        )
        # The answers reach the next Codex pass as decisions, keyed by id.
        self.assertIn("Q-001", codex.calls[1]["decision_log"])
        self.assertIn("預設啟用", codex.calls[1]["decision_log"])

    def test_a_second_conflicting_submission_is_still_refused(self):
        """The CLI gate must not weaken the one immutable submission per pause."""
        run_id, codex = self.parked_doc_run(extra_responses=(self.doc_review(),))
        first = self.answers_file(
            {"Q-001": "預設啟用", "Q-002": "以最後存檔為準"}, "first.json",
        )
        # A crash between the immutable submission and the state change: the
        # answers are persisted while the run is still awaiting input, which is
        # the only window in which a retry can reach `_answer_submission`.
        interrupted = self.doc_factory(codex, fault_injector=lambda point: (
            (_ for _ in ()).throw(KeyboardInterrupt())
            if point == "after_answer_submission" else None
        ))
        code, stdout, _stderr = self.cli(
            "answer", run_id, "--answers", first, workflow_factory=interrupted,
        )
        self.assertEqual((code, stdout), (3, ""))
        submission = (
            self.run_directory(run_id) / "question-cycles" / "0001" / "answers.json"
        )
        persisted = submission.read_bytes()
        before = self.run_snapshot(run_id)
        self.assert_still_parked(run_id, before)

        second = self.answers_file(
            {"Q-001": "預設關閉", "Q-002": "以最後存檔為準"}, "second.json",
        )
        code, stdout, stderr = self.cli(
            "answer", run_id, "--answers", second,
            workflow_factory=self.doc_factory(codex),
        )

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("immutable answer submission", stderr)
        self.assertEqual(submission.read_bytes(), persisted)
        self.assertEqual(len(codex.calls), 1)
        self.assert_still_parked(run_id, before)

        # Only the original answers may be retried, and they still work.
        with patch("ai_review.cli.generate_outputs"):
            code, stdout, stderr = self.cli(
                "answer", run_id, "--answers", first,
                workflow_factory=self.doc_factory(codex),
            )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_DOC_REVIEW")
        self.assertEqual(submission.read_bytes(), persisted)
        self.assertEqual(len(codex.calls), 2)


    # ---- `answer` on a run that is not awaiting input --------------------
    #
    # The sibling defect, from the same dry run.  The status check lives
    # *inside* `DocWorkflow.answer`, whose except clause pauses, so a stray
    # `answer` on an already-PAUSED run re-paused it: `pause.json` -- the one
    # record of why the run stopped -- was overwritten with
    # `INVALID_DOC_USER_ANSWER`, and the command still exited 0.  On a READY or
    # AWAITING_HUMAN_DOC_REVIEW run the same path moved a live run to PAUSED,
    # which no command returns from.  The gate belongs beside the answers-file
    # check: at the CLI, before the workflow exists.
    #
    # These tests deliberately pass the *real* workflow factory rather than a
    # tripwire: removing the gate must show the damage it prevents -- exit 0
    # and a rewritten `pause.json` -- not merely that a workflow was built.
    # `test_the_status_gate_runs_before_any_workflow_is_built` covers the
    # ordering separately.

    INVALID_VERDICT_REVIEW = {
        # Not a verdict the validator knows; kept verbatim as an audit record,
        # which is what makes the original pause recognisable in a snapshot.
        "verdict": "WELL-ACTUALLY-MAROONED",
        "summary": "codex answered with a verdict nobody declared",
        "findings": [],
        "questions": [],
        "context_requests": [],
    }
    INVALID_VERDICT_PAUSE = {
        "reason": "INVALID_CODEX_REVIEW",
        "detail": "codex review has an invalid verdict",
    }

    def pause_record(self, run_id):
        return json.loads(
            (self.run_directory(run_id) / "pause.json").read_text(encoding="utf-8")
        )

    def current_pause(self, run_id):
        """The pause record, or None -- quoted into the first failure message so
        that removing the gate shows the rewritten `pause.json`, not just an
        exit code."""
        path = self.run_directory(run_id) / "pause.json"
        return self.pause_record(run_id) if path.exists() else None

    def refuses_workflow(self, why):
        """A workflow factory that fails the test if it is ever reached."""
        def factory(**_kwargs):
            raise AssertionError(why)

        return factory

    def assert_run_untouched(self, run_id, before, status, next_action):
        """Byte-identical on disk, and still reporting the state it was in."""
        self.assertEqual(self.run_snapshot(run_id), before)
        code, stdout, stderr = self.cli("status", run_id)
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], status)
        self.assertEqual(payload["next_action"], next_action)

    def ready_doc_run(self):
        """One initialised doc run: READY, nothing asked, nothing reviewed."""
        self.write_doc()
        code, stdout, stderr = self.init_doc()
        self.assertEqual((code, stderr), (0, ""))
        return json.loads(stdout)["run_id"]

    def paused_doc_run(self):
        """A doc run killed by an invalid Codex verdict, as a real one was."""
        run_id = self.ready_doc_run()
        codex = RecordingCodex([dict(self.INVALID_VERDICT_REVIEW)])
        with patch("ai_review.cli.generate_outputs"):
            code, stdout, stderr = self.cli(
                "run", run_id, workflow_factory=self.doc_factory(codex),
            )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "PAUSED")
        self.assertEqual(self.pause_record(run_id), self.INVALID_VERDICT_PAUSE)
        return run_id, codex

    def reviewed_doc_run(self):
        """A finished doc run parked on findings, waiting for its reader."""
        run_id = self.ready_doc_run()
        codex = RecordingCodex([self.doc_review("F-1")])
        with patch("ai_review.cli.generate_outputs"):
            code, stdout, stderr = self.cli(
                "run", run_id, workflow_factory=self.doc_factory(codex),
            )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["status"], "AWAITING_HUMAN_DOC_REVIEW")
        return run_id, codex

    def refused_answer(self, run_id, codex, mapping=None, name="answers.json"):
        """Submit answers to a run that must refuse them, with the real workflow.

        `generate_outputs` is stubbed only so that it can be asserted unused: a
        command that did nothing must not rewrite the run's summary either.
        """
        payload = self.answers_file(mapping or {"Q-001": "答案一"}, name)
        with patch("ai_review.cli.generate_outputs") as outputs:
            code, stdout, stderr = self.cli(
                "answer", run_id, "--answers", payload,
                workflow_factory=self.doc_factory(codex),
            )
        self.assertEqual(
            (code, stdout), (2, ""),
            msg="pause.json is now %r" % (self.current_pause(run_id),),
        )
        self.assertNotIn("Traceback", stderr)
        outputs.assert_not_called()
        return stderr

    def test_answering_a_paused_run_is_refused_and_keeps_the_pause_reason(self):
        """The worst case: `pause.json` is the only record of why a run died."""
        run_id, codex = self.paused_doc_run()
        before = self.run_snapshot(run_id)

        # A file that would otherwise be acceptable: the run's state, not the
        # file, is what makes this wrong.
        stderr = self.refused_answer(run_id, codex)

        # The operator is told which state the run is actually in, and which
        # one `answer` needs.
        self.assertIn("PAUSED", stderr)
        self.assertIn("AWAITING_USER_INPUT", stderr)
        # The original reason survives: nothing was rewritten to blame the
        # answers file for a run Codex killed.
        self.assertEqual(self.pause_record(run_id), self.INVALID_VERDICT_PAUSE)
        self.assertNotIn(
            "INVALID_DOC_USER_ANSWER", json.dumps(self.pause_record(run_id))
        )
        self.assert_run_untouched(run_id, before, "PAUSED", "human_decision")
        # No second Codex round was spent on a run that could not advance.
        self.assertEqual(len(codex.calls), 1)

    def test_answering_a_ready_run_is_refused_and_pauses_nothing(self):
        """A run that has asked nothing yet must stay runnable, not become PAUSED."""
        run_id = self.ready_doc_run()
        codex = RecordingCodex([])
        before = self.run_snapshot(run_id)
        self.assertNotIn("pause.json", before)

        stderr = self.refused_answer(run_id, codex)

        self.assertIn("READY", stderr)
        self.assertIn("AWAITING_USER_INPUT", stderr)
        self.assertFalse((self.run_directory(run_id) / "pause.json").exists())
        self.assert_run_untouched(run_id, before, "READY", "run")
        self.assertEqual(codex.calls, [])

    def test_answering_a_reviewed_doc_run_is_refused_and_pauses_nothing(self):
        """A finished review awaiting its reader must survive a stray `answer`."""
        run_id, codex = self.reviewed_doc_run()
        before = self.run_snapshot(run_id)
        self.assertNotIn("pause.json", before)

        stderr = self.refused_answer(run_id, codex)

        self.assertIn("AWAITING_HUMAN_DOC_REVIEW", stderr)
        self.assertIn("AWAITING_USER_INPUT", stderr)
        self.assertFalse((self.run_directory(run_id) / "pause.json").exists())
        # Still re-reviewable: the human loop was not closed off by the refusal.
        self.assert_run_untouched(
            run_id, before, "AWAITING_HUMAN_DOC_REVIEW", "human_doc_review"
        )
        self.assertEqual(len(codex.calls), 1)

    def test_the_status_gate_runs_before_any_workflow_is_built(self):
        """Refused early, like the answers-file check: no workflow, no artifacts."""
        run_id, _codex = self.paused_doc_run()
        before = self.run_snapshot(run_id)
        payload = self.answers_file({"Q-001": "答案一"})

        code, stdout, stderr = self.cli(
            "answer", run_id, "--answers", payload,
            workflow_factory=self.refuses_workflow(
                "answer must be refused before a workflow can re-pause the run"
            ),
        )

        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("PAUSED", stderr)
        self.assertEqual(self.pause_record(run_id), self.INVALID_VERDICT_PAUSE)
        self.assertEqual(self.run_snapshot(run_id), before)


class UserPresenceDialogRegressionTests(unittest.TestCase):
    """Defect #4: approval dialogs must survive non-ASCII message content.

    json.dumps without ensure_ascii=False escapes non-ASCII to \\uXXXX, which
    AppleScript string literals reject as a syntax error, so the dialog never
    appeared — and stderr=DEVNULL then disguised the broken tool as a human
    Cancel. Every prior test mocked subprocess.run, so "the generated string
    is legal AppleScript" was never asserted anywhere; that missing assertion
    is exactly how the defect survived the whole suite. These tests run the
    real script-building code and only fake the osascript execution.
    """

    NON_ASCII = "核可測試：中文內容"

    def gate_calls(self):
        from ai_review.cli import MacOSHumanApprovalProvider

        provider = MacOSHumanApprovalProvider()
        verification = [{
            "kind": "test",
            "argv": ["swift", "test", "--filter", self.NON_ASCII],
            "scope": self.NON_ASCII,
        }]
        return (
            ("approve-plan", lambda: provider.approve(
                run_id="run-1", plan_digest="a" * 64, base_oid="b" * 40,
                verification_commands=verification, verification_digest="c" * 64,
            )),
            # approve_code's message only carries run_id and digest; the
            # non-ASCII run_id is a property probe, not a production shape.
            ("approve-code", lambda: provider.approve_code(
                run_id="run-%s" % self.NON_ASCII, candidate_digest="d" * 64,
            )),
            ("approve-review", lambda: provider.approve_review(
                run_id="run-1", manifest_digest="e" * 64, brief=self.NON_ASCII,
                base_oid="b" * 40, initial_patch_digest="f" * 64,
                verification_commands=verification,
            )),
            ("approve-risk", lambda: provider.approve_risk(
                run_id="run-1", manifest_digest="e" * 64, patch_digest="f" * 64,
                categories=["migration"],
                paths=["Sources/%s.swift" % self.NON_ASCII],
            )),
        )

    def captured_dialog(self, invoke):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "button returned:Approve", "")

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=fake_run,
        ):
            invoke()
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
        return argv[2], kwargs

    def test_non_ascii_dialog_text_stays_literal_in_every_gate(self):
        for name, invoke in self.gate_calls():
            with self.subTest(gate=name):
                script, _kwargs = self.captured_dialog(invoke)
                self.assertIn(self.NON_ASCII, script)
                self.assertNotIn("\\u", script)

    def test_every_gate_generates_compilable_applescript(self):
        # osacompile checks syntax without displaying anything; a \uXXXX
        # escape in the dialog text fails compilation exactly as it failed
        # osascript at approval time.
        if not Path("/usr/bin/osacompile").exists():
            self.skipTest("/usr/bin/osacompile unavailable")
        for name, invoke in self.gate_calls():
            with self.subTest(gate=name):
                script, _kwargs = self.captured_dialog(invoke)
                with tempfile.TemporaryDirectory() as raw:
                    source = Path(raw) / "dialog.applescript"
                    source.write_text(script + "\n", encoding="utf-8")
                    compiled = subprocess.run(
                        [
                            "/usr/bin/osacompile",
                            "-o", str(Path(raw) / "dialog.scpt"), str(source),
                        ],
                        capture_output=True, text=True, check=False,
                    )
                self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def test_every_gate_captures_osascript_stderr(self):
        # stderr=DEVNULL swallowed the AppleScript syntax error and let a
        # broken dialog masquerade as a human Cancel for three sessions.
        for name, invoke in self.gate_calls():
            with self.subTest(gate=name):
                _script, kwargs = self.captured_dialog(invoke)
                self.assertEqual(kwargs.get("stderr"), subprocess.PIPE)

    def test_dialog_that_never_appeared_is_not_reported_as_cancel(self):
        from ai_review.cli import CliInputError, _raise_user_presence_failure

        syntax_error = subprocess.CompletedProcess(
            ["/usr/bin/osascript"], 1, "",
            "0:12: script error: Expected \"\\\"\" but found unknown token. (-2741)",
        )
        with self.assertRaises(CliInputError) as raised:
            _raise_user_presence_failure(syntax_error)
        self.assertIn("never appeared", str(raised.exception))
        self.assertIn("-2741", str(raised.exception))

    def test_real_cancel_and_silent_failure_still_report_cancelled(self):
        from ai_review.cli import CliInputError, _raise_user_presence_failure

        for stderr in ("45:52: execution error: User canceled. (-128)", "", None):
            with self.subTest(stderr=stderr):
                completed = subprocess.CompletedProcess(
                    ["/usr/bin/osascript"], 1, "", stderr,
                )
                with self.assertRaises(CliInputError) as raised:
                    _raise_user_presence_failure(completed)
                self.assertEqual(str(raised.exception), "human approval was cancelled")
