import ast
import json
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_review.runners import (
    RunnerInterrupted,
    DEFAULT_VERIFICATION_TIMEOUT,
    build_claude_argv,
    build_codex_argv,
    run_verification,
    validate_claude_resolution,
    validate_codex_review,
    validate_plan_repair,
    validate_plan_update,
)
from ai_review.git_diff import GitDiffError, _resolve_base_ref, capture_diff


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.executable = self.root / "record.py"
        self.executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "print(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.temp.cleanup()

    def test_codex_argv_is_read_only_and_never_bypasses_safety(self):
        argv = build_codex_argv(
            self.root, self.root / "schema.json", self.root / "output.json", "review $()"
        )

        self.assertEqual(argv, [
            "codex", "-a", "never", "exec", "-C", str(self.root.resolve()), "-s", "read-only",
            "--output-schema", str((self.root / "schema.json").resolve()),
            "-o", str((self.root / "output.json").resolve()), "review $()",
        ])
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
    def test_installed_codex_help_accepts_the_builder_option_scopes_without_running_a_model(self):
        executable = shutil.which("codex")
        help_result = subprocess.run(
            [executable, "exec", "--help"], text=True, capture_output=True, check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--sandbox", help_result.stdout)
        self.assertIn("--output-schema", help_result.stdout)
        scoped_help = subprocess.run(
            [executable, "-a", "never", "exec", "--help"], text=True, capture_output=True, check=False,
        )
        self.assertEqual(scoped_help.returncode, 0, scoped_help.stderr)

    def test_claude_argv_disallows_remote_mutations_without_permission_bypass(self):
        argv = build_claude_argv('{"type":"object"}', "implement $()")

        self.assertIn("--safe-mode", argv)
        self.assertNotIn("Bash", argv[argv.index("--tools") + 1])
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[-1], "implement $()")

    def test_ai_review_never_builds_a_command_for_an_ios_specialist(self):
        """Specialists are read-only session work; the CLI must not spawn them."""
        package = Path(__file__).resolve().parent.parent / "ai_review"
        names = ("swiftui-reviewer", "ux-critique", "resilience-auditor")
        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(package.glob("*.py"))
        }

        mentions = {
            name: sorted(module for module, text in sources.items() if name in text)
            for name in names
        }

        # Only the submission parser knows the names, and it starts no process.
        for name, modules in mentions.items():
            self.assertEqual(modules, ["preflight.py"], (name, modules))

        tree = ast.parse(sources["preflight.py"])
        imported = set()
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                target = node.func
                called.add(
                    target.attr if isinstance(target, ast.Attribute)
                    else getattr(target, "id", "")
                )
        for forbidden in ("subprocess", "os", "shutil", "runners", "pty", "multiprocessing"):
            self.assertNotIn(forbidden, imported)
        for forbidden in (
            "run", "Popen", "system", "spawnv", "execv", "check_output",
            "build_claude_argv", "build_codex_argv", "run_verification",
        ):
            self.assertNotIn(forbidden, called)

    def test_review_mode_claude_argv_keeps_the_bash_free_boundary(self):
        argv = build_claude_argv('{"type":"object"}', "fix findings", mode="review")

        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep,Edit,Write")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        with self.assertRaises(ValueError):
            build_claude_argv('{"type":"object"}', "fix", mode="specialists")

    def test_timeout_is_reported_as_runner_interruption(self):
        with patch("ai_review.runners.platform.system", return_value="Linux"):
            with self.assertRaises(RunnerInterrupted):
                run_verification([str(self.executable), "arg"], cwd=self.root, timeout=0.0001)

    def test_verification_timeout_is_always_positive_and_uses_a_bounded_default(self):
        with self.assertRaises(ValueError):
            run_verification([str(self.executable)], cwd=self.root, timeout=0)
        with self.assertRaises(ValueError):
            run_verification([str(self.executable)], cwd=self.root, timeout=-1)
        with self.assertRaises(ValueError):
            run_verification([str(self.executable)], cwd=self.root, timeout=float("inf"))
        with self.assertRaises(ValueError):
            run_verification([str(self.executable)], cwd=self.root, timeout=float("nan"))
        with patch("ai_review.runners.platform.system", return_value="Linux"), patch(
            "ai_review.runners.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(["test"], 0, "", "")
            run_verification([str(self.executable)], cwd=self.root)

        self.assertGreater(DEFAULT_VERIFICATION_TIMEOUT, 0)
        self.assertEqual(run.call_args.kwargs["timeout"], DEFAULT_VERIFICATION_TIMEOUT)

    def test_verification_result_persists_argv_exit_code_stdout_and_stderr(self):
        with patch("ai_review.runners.platform.system", return_value="Linux"):
            result = run_verification([str(self.executable), "one two"], cwd=self.root)
        saved = self.root / "verification.json"
        result.persist(saved)

        record = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(record["argv"], [str(self.executable), "one two"])
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(json.loads(record["stdout"]), ["one two"])
        self.assertEqual(record["stderr"], "")

    def test_macos_verification_uses_network_and_git_write_sandbox(self):
        calls = []

        def completed(argv, **_kwargs):
            calls.append(argv)
            if argv[0] == "/usr/bin/git":
                if "--git-common-dir" in argv:
                    return subprocess.CompletedProcess(
                        argv, 0, str(self.root / "common.git") + "\n", ""
                    )
                return subprocess.CompletedProcess(argv, 0, str(self.root / ".git") + "\n", "")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with patch("ai_review.runners.platform.system", return_value="Darwin"), patch(
            "ai_review.runners._already_restricted_by_seatbelt", return_value=False
        ), patch("ai_review.runners.subprocess.run", side_effect=completed):
            result = run_verification(
                ["/usr/bin/python3", "-m", "unittest", "tests.feature"],
                cwd=self.root,
            )

        execution = calls[-1]
        self.assertEqual(execution[0], "/usr/bin/sandbox-exec")
        profile = execution[execution.index("-p") + 1]
        # Both directions of IP networking must be denied.
        self.assertIn('(deny network-outbound (remote ip "*:*"))', profile)
        self.assertIn('(deny network-inbound (local ip "*:*"))', profile)
        # 🔴 MUST NOT use `(deny network*)` — it blocks unix domain sockets
        # too, and on macOS the simulator's XCTest must talk to testmanagerd
        # over a unix socket. With it, the build succeeds but tests never
        # execute (exit 65), leaving the iOS profile's verification unusable.
        # This assertion is the regression guard for that bug, not a style
        # preference.
        self.assertNotIn("(deny network*)", profile)
        self.assertIn("(deny file-write*", profile)
        self.assertIn(".git", profile)
        self.assertIn("common.git", profile)
        self.assertEqual(result.argv[0], "/usr/bin/python3")

    def test_codex_semantics_reject_inconsistent_verdicts_and_deferred_blockers(self):
        review = {
            "verdict": "PASS", "summary": "ok", "questions": [], "context_requests": [],
            "findings": [{
                "id": "F-1", "severity": "blocker", "invariant": "safe",
                "location": "x.py:1", "evidence": "bad", "required_outcome": "fix",
                "lineage": {"resolution": "deferred"},
            }],
        }
        with self.assertRaises(ValueError):
            validate_codex_review(review)
        review["verdict"] = "NEEDS_USER_INPUT"
        review["findings"] = []
        with self.assertRaises(ValueError):
            validate_codex_review(review)

    def test_codex_review_validation_enforces_declared_schema_before_semantics(self):
        valid = {
            "verdict": "CHANGES_REQUIRED", "summary": "fix this",
            "questions": [], "context_requests": [],
            "findings": [{
                "id": "F-1", "severity": "major", "invariant": "safe",
                "location": "x.py:1", "evidence": "bad", "required_outcome": "fix",
                "lineage": {"resolution": "fixed"},
            }],
        }
        self.assertEqual(validate_codex_review(valid), valid)
        invalid_severity = dict(valid, findings=[dict(valid["findings"][0], severity="urgent")])
        with self.assertRaises(ValueError):
            validate_codex_review(invalid_severity)
        empty_lineage = dict(valid, findings=[dict(valid["findings"][0], lineage={"resolution": ""})])
        with self.assertRaises(ValueError):
            validate_codex_review(empty_lineage)
        missing_nested = dict(valid, findings=[dict(valid["findings"][0], lineage={})])
        with self.assertRaises(ValueError):
            validate_codex_review(missing_nested)
        additional_nested = dict(valid, findings=[dict(valid["findings"][0], lineage={"resolution": "fixed", "extra": "no"})])
        with self.assertRaises(ValueError):
            validate_codex_review(additional_nested)

    def test_schema_semantics_require_claude_resolutions_for_findings(self):
        resolution = {"summary": "done", "resolutions": []}
        with self.assertRaises(ValueError):
            validate_claude_resolution(resolution, required_finding_ids=["F-1"])

    def test_claude_resolution_allows_evidenced_dispute_but_rejects_empty_dispute_evidence(self):
        disputed = {
            "summary": "The finding is disputed",
            "resolutions": [{
                "finding_id": "F-1",
                "outcome": "disputed",
                "evidence": "The cited Plan section already covers the claimed risk.",
            }],
        }

        self.assertEqual(validate_claude_resolution(disputed, required_finding_ids=["F-1"]), disputed)
        disputed["resolutions"][0]["evidence"] = "no"
        with self.assertRaises(ValueError):
            validate_claude_resolution(disputed, required_finding_ids=["F-1"])

    def test_plan_update_requires_an_answer_submission_digest(self):
        content = "# Plan\nUpdated\n"
        update = {
            "summary": "updated",
            "answer_digest": "a" * 64,
            "plan": {
                "content": content,
                "previous_digest": "b" * 64,
                "new_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "changed_sections": ["Plan"],
            },
        }

        self.assertEqual(validate_plan_update(update), update)
        del update["answer_digest"]
        with self.assertRaises(ValueError):
            validate_plan_update(update)

    def _plan_repair(self, current, revised, **plan_overrides):
        plan = {
            "content": revised,
            "previous_digest": hashlib.sha256(current.encode("utf-8")).hexdigest(),
            "changed_sections": ["Plan"],
        }
        plan.update(plan_overrides)
        return {
            "summary": "repaired",
            "input_plan_digest": hashlib.sha256(current.encode("utf-8")).hexdigest(),
            "decision_log_digest": "c" * 64,
            "resolutions": [
                {"finding_id": "F-1", "outcome": "fixed", "evidence": "repaired in plan"}
            ],
            "plan": plan,
        }

    def _validate_repair(self, current, payload):
        return validate_plan_repair(
            payload,
            required_finding_ids=["F-1"],
            current_plan_digest=hashlib.sha256(current.encode("utf-8")).hexdigest(),
            decision_log_digest="c" * 64,
        )

    def test_plan_repair_derives_new_digest_without_a_model_reported_one(self):
        # Plan-mode Claude cannot hash its own output, so omitting new_digest
        # must be accepted and the digest derived from the returned bytes.
        current, revised = "# Plan\nOld\n", "# Plan\nNew\n"

        result = self._validate_repair(current, self._plan_repair(current, revised))

        self.assertEqual(
            result["plan"]["new_digest"],
            hashlib.sha256(revised.encode("utf-8")).hexdigest(),
        )

    def test_plan_repair_ignores_a_model_reported_new_digest(self):
        current, revised = "# Plan\nOld\n", "# Plan\nNew\n"
        payload = self._plan_repair(current, revised, new_digest="0" * 64)

        result = self._validate_repair(current, payload)

        self.assertEqual(
            result["plan"]["new_digest"],
            hashlib.sha256(revised.encode("utf-8")).hexdigest(),
        )

    def test_plan_repair_still_rejects_an_unchanged_plan(self):
        # Deriving the digest must not weaken the "repair must change the Plan"
        # rule, including when the model reports a plausible-looking digest.
        current = "# Plan\nOld\n"
        payload = self._plan_repair(current, current, new_digest="d" * 64)

        with self.assertRaises(ValueError):
            self._validate_repair(current, payload)

    def test_plan_repair_rejects_an_unknown_plan_field(self):
        current, revised = "# Plan\nOld\n", "# Plan\nNew\n"
        payload = self._plan_repair(current, revised, plan_path="/etc/passwd")

        with self.assertRaises(ValueError):
            self._validate_repair(current, payload)


class GitDiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.artifacts = Path(self.temp.name) / "artifacts"
        self.repo.mkdir()
        self.artifacts.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.repo / "Production.swift").write_text("let original = 1\n", encoding="utf-8")
        self.git("add", "Production.swift")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *argv):
        return subprocess.run(["git", *argv], cwd=self.repo, check=True, text=True, capture_output=True)

    def test_capture_includes_binary_tracked_and_untracked_changes_and_counts_production(self):
        (self.repo / "Production.swift").write_text("let original = 1\nlet added = 2\n", encoding="utf-8")
        (self.repo / "NewFile.swift").write_text("let new = 3\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "notes.md").write_text("+ not production\n", encoding="utf-8")
        (self.repo / "FeatureTests").mkdir()
        (self.repo / "FeatureTests" / "Tests.swift").write_text("let test = 1\n", encoding="utf-8")

        result = capture_diff(
            self.repo, self.base, self.artifacts / "round-1.patch",
            production_excludes=["docs/**", "**/*Tests/**", "**/*UITests/**"],
        )

        patch = result.patch_path.read_text(encoding="utf-8")
        self.assertIn("diff --git a/Production.swift b/Production.swift", patch)
        self.assertIn("diff --git a/NewFile.swift b/NewFile.swift", patch)
        self.assertEqual(result.production_added_lines, 2)

    def test_capture_rejects_untracked_symlink_outside_repository(self):
        outside = Path(self.temp.name) / "outside.swift"
        outside.write_text("let outside = 1\n", encoding="utf-8")
        (self.repo / "Escapes.swift").symlink_to(outside)

        with self.assertRaises(GitDiffError):
            capture_diff(self.repo, self.base, self.artifacts / "round-1.patch")

    def test_capture_does_not_include_its_previous_patch_as_an_untracked_change(self):
        (self.repo / "round-1.patch").write_text("old patch\n", encoding="utf-8")
        (self.repo / "NewFile.swift").write_text("let new = 3\n", encoding="utf-8")

        result = capture_diff(self.repo, self.base, self.artifacts / "round-1.patch")

        patch = result.patch_path.read_text(encoding="utf-8")
        self.assertIn("diff --git a/round-1.patch b/round-1.patch", patch)

    def test_capture_handles_untracked_names_that_begin_with_a_dash(self):
        (self.repo / "-injection.swift").write_text("let injected = 1\n", encoding="utf-8")

        result = capture_diff(self.repo, self.base, self.artifacts / "round-1.patch")

        self.assertIn("-injection.swift", result.patch_path.read_text(encoding="utf-8"))

    def test_capture_rejects_option_like_base_ref_before_creating_output(self):
        output = self.artifacts / "round-1.patch"

        with self.assertRaises(GitDiffError):
            capture_diff(self.repo, "--output=/dev/null", output)

        self.assertFalse(output.exists())

    def test_capture_rejects_artifact_output_inside_target_repository(self):
        with self.assertRaises(GitDiffError):
            capture_diff(self.repo, self.base, self.repo / ".ai-review" / "round.patch")

    def test_capture_resolves_symbolic_base_ref_and_uses_only_the_oid_in_diff_argv(self):
        calls = []
        original_run = subprocess.run

        def recording_run(argv, *args, **kwargs):
            calls.append(argv)
            return original_run(argv, *args, **kwargs)

        with patch("ai_review.process_security.subprocess.run", side_effect=recording_run):
            capture_diff(self.repo, "HEAD", self.artifacts / "round-1.patch")

        diff_calls = [argv for argv in calls if argv[:2] == ["/usr/bin/git", "diff"]]
        self.assertIn([
            "/usr/bin/git", "diff", "--no-textconv", "--no-ext-diff",
            "--binary", self.base, "--",
        ], diff_calls)
        self.assertIn(
            [
                "/usr/bin/git", "diff", "--no-textconv", "--no-ext-diff",
                "--numstat", "-z", "--find-renames", self.base, "--",
            ],
            diff_calls,
        )
        self.assertNotIn("HEAD", [part for argv in diff_calls for part in argv])

    def test_capture_disables_repository_textconv_and_external_diff(self):
        probe = Path(self.temp.name) / "git-diff-probe"
        driver = Path(self.temp.name) / "diff-driver.sh"
        driver.write_text(
            "#!/bin/sh\n/usr/bin/touch %s\n/bin/cat \"$1\"\n" % probe,
            encoding="utf-8",
        )
        driver.chmod(0o700)
        target = self.repo / "payload.txt"
        target.write_text("before\n", encoding="utf-8")
        (self.repo / ".gitattributes").write_text(
            "payload.txt diff=probe\n", encoding="utf-8"
        )
        self.git("config", "diff.probe.textconv", str(driver))
        self.git("config", "diff.external", str(driver))
        self.git("add", "payload.txt", ".gitattributes")
        self.git("commit", "-m", "driver base")
        base = self.git("rev-parse", "HEAD").stdout.strip()
        target.write_text("after\n", encoding="utf-8")

        output = self.artifacts / "textconv.patch"
        capture_diff(self.repo, base, output)

        self.assertFalse(probe.exists())
        self.assertIn(b"+after", output.read_bytes())

    def test_base_ref_resolution_accepts_a_unique_short_commit_prefix(self):
        self.assertEqual(_resolve_base_ref(self.repo, self.base[:12]), self.base)

    def test_base_ref_resolution_rejects_malformed_or_multiple_oid_output(self):
        with patch("ai_review.git_diff._run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, b"sha1\n", b""),
                subprocess.CompletedProcess([], 0, (b"a" * 40) + b"\n" + (b"b" * 40) + b"\n", b""),
            ]
            with self.assertRaises(GitDiffError):
                _resolve_base_ref(self.repo, "HEAD")
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
        )

        with patch("ai_review.git_diff._run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, b"sha256\n", b""),
                subprocess.CompletedProcess([], 0, b"a" * 40 + b"\n", b""),
            ]
            with self.assertRaises(GitDiffError):
                _resolve_base_ref(self.repo, "HEAD")

    def test_production_count_uses_machine_safe_paths_not_human_diff_header_splitting(self):
        path = self.repo / "Production b" / "docs" / "Ignore.swift"
        path.parent.mkdir(parents=True)
        path.write_text("let stillProduction = 1\n", encoding="utf-8")

        result = capture_diff(
            self.repo, self.base, self.artifacts / "round-1.patch",
            production_excludes=["docs/**", "**/*Tests/**", "**/*UITests/**"],
        )

        self.assertEqual(result.production_added_lines, 1)

    def test_production_count_handles_renames_binary_and_git_permitted_special_names(self):
        self.git("mv", "Production.swift", "Renamed.swift")
        special = self.repo / "Source b" / "space\tand\nnewline.swift"
        special.parent.mkdir(parents=True)
        special.write_text("let production = 1\n", encoding="utf-8")
        (self.repo / "Image.bin").write_bytes(b"\x00\x01\x02")

        result = capture_diff(
            self.repo, self.base, self.artifacts / "round-1.patch",
            production_excludes=["docs/**", "**/*Tests/**", "**/*UITests/**"],
        )

        self.assertEqual(result.production_added_lines, 1)


if __name__ == "__main__":
    unittest.main()
