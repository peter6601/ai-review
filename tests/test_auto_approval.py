import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


class RateLimiterTests(unittest.TestCase):
    """The window is the whole safety story once a person stops clicking."""

    def setUp(self):
        from ai_review import auto_approval

        self.auto_approval = auto_approval
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temp.name) / "auto-approvals.json"

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, *, at, run_id="20260830T000000Z-abcdef01", command="approve-review"):
        return self.auto_approval.reserve(
            self.ledger, run_id=run_id, command=command, now=at,
        )

    def test_five_approvals_fit_in_one_window_and_the_sixth_does_not(self):
        for index in range(self.auto_approval.MAX_AUTO_APPROVALS):
            self.reserve(at=1000.0 + index)
        with self.assertRaises(self.auto_approval.AutoApprovalRateLimited) as raised:
            self.reserve(at=1005.0)
        self.assertEqual(raised.exception.window_count, 5)
        # The oldest slot was taken at t=1000, so it frees 60s later.
        self.assertAlmostEqual(raised.exception.retry_after, 55.0, places=3)

    def test_a_slot_frees_once_the_window_slides_past_it(self):
        for index in range(5):
            self.reserve(at=1000.0 + index)
        with self.assertRaises(self.auto_approval.AutoApprovalRateLimited):
            self.reserve(at=1059.9)
        self.reserve(at=1060.1)  # t=1000 has now aged out.
        with self.assertRaises(self.auto_approval.AutoApprovalRateLimited):
            self.reserve(at=1060.2)

    def test_the_ledger_keeps_only_the_live_window(self):
        for index in range(5):
            self.reserve(at=1000.0 + index)
        self.reserve(at=2000.0)
        contents = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(contents["version"], 1)
        self.assertEqual([entry["at"] for entry in contents["entries"]], [2000.0])

    def test_a_backwards_clock_cannot_buy_extra_approvals(self):
        """Entries dated in the future still count, so the limiter fails closed."""
        for index in range(5):
            self.reserve(at=5000.0 + index)
        with self.assertRaises(self.auto_approval.AutoApprovalRateLimited):
            self.reserve(at=1000.0)

    def test_a_malformed_ledger_is_treated_as_empty(self):
        self.ledger.write_text("{ not json", encoding="utf-8")
        record = self.reserve(at=1000.0)
        self.assertEqual(record["command"], "approve-review")

    def test_only_the_two_in_loop_review_gates_may_reserve(self):
        for command in ("approve-plan", "approve-code"):
            with self.assertRaises(ValueError):
                self.reserve(at=1000.0, command=command)

    def test_the_ledger_sits_beside_the_approval_key_not_inside_the_run_store(self):
        root = Path("/tmp/store/runs")
        self.assertEqual(
            self.auto_approval.ledger_path(root), Path("/tmp/store/auto-approvals.json")
        )


class AutoApprovalCliTests(unittest.TestCase):
    """`--auto` must clear a Review gate headlessly, and only a Review gate."""

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
        self.source = self.repo / "docs" / "plan.md"
        self.source.parent.mkdir()
        self.source.write_text("# Plan\n", encoding="utf-8")
        self.git("add", "docs/plan.md")
        self.git("commit", "-m", "base")
        self.runs = self.root / "runs"

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, text=True, capture_output=True
        )

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.main(["--runs-root", str(self.runs), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def cli_store(self):
        from ai_review.models import ApprovalAuthority
        from ai_review.store import RunStore

        root = self.runs.expanduser().resolve()
        return RunStore(root, authority=ApprovalAuthority(root.parent / "approval.key"))

    def init_review(self, profile="generic"):
        self.source.write_text("# Plan\nreview change\n", encoding="utf-8")
        code, stdout, error = self.cli(
            "init", "review", "--repo", str(self.repo), "--base", "HEAD",
            "--brief", "Review the retry fix", "--profile", profile,
            "--verify", "%s -m unittest tests.test_retry" % sys.executable,
        )
        self.assertEqual((code, error), (0, ""))
        return json.loads(stdout)["run_id"]

    def ledger(self):
        from ai_review.auto_approval import ledger_path

        return ledger_path(self.runs.expanduser().resolve())

    def test_auto_approval_clears_the_scope_gate_without_user_presence(self):
        from ai_review.auto_approval import AUTO_APPROVAL_ACTOR, AUTO_APPROVAL_PROVIDER
        from ai_review.models import Status

        run_id = self.init_review()
        # No osascript patch anywhere: a dialog would fail this test outright.
        code, _stdout, error = self.cli("approve-review", "--auto", run_id)
        self.assertEqual((code, error), (0, ""))
        state = self.cli_store().load(run_id)
        self.assertEqual(state.status, Status.READY)
        self.assertEqual(state.approval_attestation.source, AUTO_APPROVAL_PROVIDER)
        self.assertEqual(state.approval_attestation.actor, AUTO_APPROVAL_ACTOR)

    def test_an_auto_approved_receipt_never_claims_a_human_pressed_anything(self):
        run_id = self.init_review()
        self.cli("approve-review", "--auto", run_id)
        state = self.cli_store().load(run_id)
        self.assertNotIn("osascript", state.approval_attestation.source)
        self.assertNotIn("local-macos-user", state.approval_attestation.actor)

    def test_the_sixth_auto_approval_in_a_window_is_refused_and_changes_nothing(self):
        from ai_review.auto_approval import reserve
        from ai_review.cli import EXIT_RATE_LIMITED
        from ai_review.models import Status

        run_id = self.init_review()
        import time

        now = time.time()
        for index in range(5):
            reserve(
                self.ledger(), run_id="filler-%d" % index,
                command="approve-review", now=now - index,
            )
        code, _stdout, error = self.cli("approve-review", "--auto", run_id)
        self.assertEqual(code, EXIT_RATE_LIMITED)
        self.assertIn("rate limit", error)
        self.assertIn("limit 5 per 60s", error)
        state = self.cli_store().load(run_id)
        self.assertEqual(state.status, Status.AWAITING_REVIEW_APPROVAL)
        self.assertIsNone(state.approval_attestation)

    def test_a_human_approval_never_spends_a_rate_limit_slot(self):
        from unittest.mock import patch

        run_id = self.init_review()
        original_run = subprocess.run

        def approve(argv, *args, **kwargs):
            if argv[0] == "/usr/bin/osascript":
                return subprocess.CompletedProcess(argv, 0, "button returned:Approve\n", "")
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=approve,
        ):
            code, _stdout, error = self.cli("approve-review", run_id)
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(self.ledger().exists())

    def test_a_refused_gate_does_not_spend_a_slot(self):
        """A slot is only ever spent alongside a signed receipt."""
        from ai_review.cli import EXIT_INVALID

        run_id = self.init_review()
        self.assertEqual(self.cli("approve-review", "--auto", run_id)[0], 0)
        # The run is READY now, so a second approval is rejected before the gate.
        code, _stdout, _error = self.cli("approve-review", "--auto", run_id)
        self.assertEqual(code, EXIT_INVALID)
        contents = json.loads(self.ledger().read_text(encoding="utf-8"))
        self.assertEqual(len(contents["entries"]), 1)

    def test_the_terminal_gate_has_no_auto_flag_at_all(self):
        """The loop may run unattended; a person still reads the final diff."""
        from ai_review.cli import EXIT_INVALID

        run_id = self.init_review()
        code, _stdout, error = self.cli("approve-code", "--auto", run_id)
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("--auto", error)
        self.assertFalse(self.ledger().exists())

    def test_the_agent_provider_cannot_approve_code_even_by_direct_call(self):
        from ai_review.auto_approval import AgentApprovalProvider

        self.assertFalse(hasattr(AgentApprovalProvider(), "approve_code"))


if __name__ == "__main__":
    unittest.main()
