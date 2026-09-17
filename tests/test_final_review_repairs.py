import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from ai_review.cli import CliInputError, _test_commands
from ai_review.models import ApprovalAuthority
from ai_review.runners import build_claude_argv, validate_codex_review


class FinalReviewRepairContracts(unittest.TestCase):
    def test_verification_requires_explicit_focused_test(self):
        with self.assertRaises(CliInputError):
            _test_commands([])
        commands = _test_commands([
            json.dumps({"kind": "check", "argv": ["swift", "format", "lint"], "scope": None}),
            json.dumps({"kind": "test", "argv": ["swift", "test", "--filter", "FeatureTests"], "scope": "FeatureTests"}),
        ])
        self.assertEqual([item["kind"] for item in commands], ["check", "test"])

    def test_verification_rejects_publication_shell_network_and_wrappers(self):
        dangerous = (
            ["git", "push"],
            ["gh", "pr", "create"],
            ["curl", "https://example.com"],
            ["bash", "-c", "xcodebuild test"],
            ["env", "git", "push"],
            ["python3", "-c", "import os"],
            ["npm", "test"],
        )
        focused = json.dumps({
            "kind": "test",
            "argv": ["swift", "test", "--filter", "FeatureTests"],
            "scope": "FeatureTests",
        })
        for argv in dangerous:
            with self.subTest(argv=argv), self.assertRaises(CliInputError):
                _test_commands([
                    json.dumps({"kind": "check", "argv": argv, "scope": None}),
                    focused,
                ])
        with self.assertRaises(CliInputError):
            _test_commands([
                json.dumps({
                    "kind": "check",
                    "argv": ["xcodebuild", "-scheme", "test"],
                    "scope": None,
                }),
                focused,
            ])
        for flag in (
            "-allowProvisioningUpdates", "-downloadPlatform", "-exportArchive",
            "archive", "-registerForDeveloperServices", "-notarize", "install",
        ):
            with self.subTest(flag=flag), self.assertRaises(CliInputError):
                _test_commands([
                    json.dumps({
                        "kind": "test",
                        "argv": ["xcodebuild", "test", flag],
                        "scope": "FeatureTests",
                    })
                ])
        with tempfile.TemporaryDirectory() as raw:
            fake = Path(raw) / "swift"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o700)
            with self.assertRaises(CliInputError):
                _test_commands([
                    json.dumps({
                        "kind": "test",
                        "argv": [str(fake), "test"],
                        "scope": "FeatureTests",
                    })
                ])

    def test_claude_tool_boundaries_are_mode_specific_and_bash_free(self):
        plan = build_claude_argv("{}", "prompt", mode="plan", model="opus[1m]", fallback_model="sonnet", max_budget_usd=5)
        code = build_claude_argv("{}", "prompt", mode="code", model="opus[1m]", fallback_model="sonnet", max_budget_usd=5)
        for argv in (plan, code):
            self.assertIn("--safe-mode", argv)
            self.assertIn("--tools", argv)
            tools = argv[argv.index("--tools") + 1]
            self.assertNotIn("Bash", tools)
        self.assertNotIn("Edit", plan[plan.index("--tools") + 1])
        self.assertIn("Edit", code[code.index("--tools") + 1])

    def test_pass_requires_empty_findings_questions_and_context_requests(self):
        valid = {
            "verdict": "PASS", "summary": "ok", "findings": [],
            "questions": [], "context_requests": [],
        }
        self.assertEqual(validate_codex_review(valid)["verdict"], "PASS")
        for key, value in (
            ("findings", [{
                "id": "I", "severity": "info", "invariant": "x", "location": "x",
                "evidence": "x", "required_outcome": "x", "lineage": {"resolution": "existing"},
            }]),
            ("questions", ["question"]),
            ("context_requests", ["section"]),
        ):
            payload = dict(valid)
            payload[key] = value
            with self.assertRaises(ValueError):
                validate_codex_review(payload)

    def test_approval_key_rejects_symlink_leaf(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            real.write_bytes(os.urandom(32))
            real.chmod(0o600)
            link = root / "key"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                ApprovalAuthority(link)

    def test_approval_key_rejects_symlink_in_parent_chain(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(OSError):
                ApprovalAuthority(alias / "key")

    def test_plan_manifest_binds_context_checksum(self):
        from ai_review.models import RunManifest
        manifest = RunManifest(
            kind="plan", repo_path="/tmp/repo", plan_path="/tmp/repo/plan.md",
            base_ref="HEAD", context_checksum="a" * 64,
        )
        self.assertEqual(manifest.to_dict()["context_checksum"], "a" * 64)

    def test_code_approval_gates_explicit_atomic_knowledge_writeback(self):
        from ai_review.cli import _approve_code, _writeback_knowledge
        from ai_review.models import HumanApprovalReceipt, RunState, Status
        from ai_review.store import RunStore

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            plan_path = repo / "plan.md"
            plan_path.write_text("# Plan\n", encoding="utf-8")
            subprocess.run(["git", "add", "plan.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                text=True, capture_output=True,
            ).stdout.strip()
            authority = ApprovalAuthority(root / "approval.key")
            store = RunStore(root / "runs", authority=authority)
            plan = store.create(RunState.new(
                "plan", str(plan_path), str(repo), base,
                verification_commands=[{
                    "kind": "test",
                    "argv": ["python3", "-m", "unittest", "tests.feature"],
                    "scope": "tests.feature",
                }],
            ))
            plan.transition("PASS")
            plan.approve_plan(
                receipt=HumanApprovalReceipt(
                    run_id=plan.run_id,
                    plan_digest=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                    base_oid=plan.manifest.base_oid,
                    approved_at="2026-07-31T00:00:00+00:00",
                    provider="test:user-presence", actor="human",
                ),
                authority=authority,
            )
            store.save(plan)
            code = store.create(RunState.new_code_from_approved_plan(plan, authority))
            object.__setattr__(code, "repair_round", 3)
            code.transition("PASS")
            store.save(code)
            artifacts = store._run_directory(code)
            store.write_artifact_bytes(artifacts / "patches" / "round-0000.patch", b"")
            store._atomic_write(artifacts / "reviews" / "0001.json", {
                "verdict": "PASS", "summary": "reviewed", "findings": [],
                "questions": [], "context_requests": [],
            })
            store._atomic_write(artifacts / "verification-rounds" / "0001.json", {
                "results": [{
                    "name": "test",
                    "argv": list(plan.manifest.verification_commands[0].argv),
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "stdout_path": "verification/stdout.log",
                    "stderr_path": "verification/stderr.log",
                    "relevant_output": "ok",
                }]
            })
            store._atomic_write(artifacts / "code-review-actions" / "0001.json", {
                "action": "pass",
                "review_sequence": 1,
                "verification_round": 1,
                "patch_name": "round-0000.patch",
                "patch_digest": hashlib.sha256(b"").hexdigest(),
            })
            real_run = subprocess.run

            def timeout_only_dialog(argv, *args, **kwargs):
                if argv[0] == "/usr/bin/osascript":
                    raise subprocess.TimeoutExpired(argv, 120)
                return real_run(argv, *args, **kwargs)

            with patch(
                "ai_review.cli.platform.system", return_value="Darwin"
            ), patch(
                "ai_review.cli.subprocess.run", side_effect=timeout_only_dialog,
            ):
                with self.assertRaises(CliInputError):
                    _approve_code(Namespace(run_id=code.run_id), store)
            with patch(
                "ai_review.cli.MacOSHumanApprovalProvider.approve_code",
                return_value={
                    "approved_at": "2026-07-31T01:00:00+00:00",
                    "provider": "test:user-presence", "actor": "human",
                },
            ):
                _approve_code(Namespace(run_id=code.run_id), store)

            workspace = root / "workspace"
            workspace.mkdir()
            approval_path = artifacts / "code-approval.json"
            original_approval = store.read_artifact_bytes(approval_path)
            replay = json.loads(original_approval)
            replay["run_id"] = "other-run"
            store._atomic_write(approval_path, replay)
            with self.assertRaises(CliInputError):
                _writeback_knowledge(store, code, workspace_root=workspace)
            store.write_artifact_bytes(approval_path, original_approval)
            destination = _writeback_knowledge(store, code, workspace_root=workspace)
            self.assertEqual(destination.read_bytes(), store.read_artifact_bytes(
                store._run_directory(code) / "knowledge-candidate.md"
            ))
            candidate_path = artifacts / "code-review-candidate.json"
            original_candidate = store.read_artifact_bytes(candidate_path)
            store._atomic_write(candidate_path, {"bad": "shape"})
            with self.assertRaises(CliInputError):
                _writeback_knowledge(store, code, workspace_root=workspace)
            store.write_artifact_bytes(candidate_path, original_candidate)
            (repo / "after-pass.txt").write_text("changed", encoding="utf-8")
            another = root / "another-workspace"
            another.mkdir()
            with self.assertRaises(CliInputError):
                _writeback_knowledge(
                    store, code, workspace_root=another
                )


if __name__ == "__main__":
    unittest.main()
