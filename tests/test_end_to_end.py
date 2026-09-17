"""Disposable subprocess coverage for the local Plan-to-Code workflow.

The regression this protects is a workflow that only works with in-memory
doubles or leaves project-local review state behind.  The model processes are
real child processes; their deterministic JSON responses come from fixtures.
"""

import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import yaml
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ai_review.cli import main
from ai_review.models import ApprovalAuthority
from ai_review.policy import load_policy
from ai_review.store import RunStore, project_id
from ai_review.summary import generate_outputs
from install_skills import SKILL_NAMES, InstallTarget, apply_install


def finding(identifier, *, introduced=False):
    return {
        "id": identifier,
        "severity": "blocker",
        "invariant": "Approved workflow remains internally consistent",
        "location": "docs/plan.md:1",
        "evidence": "new contradiction introduced by the prior fix" if introduced else "missing required decision",
        "required_outcome": "make the documented behavior consistent",
        "lineage": {"resolution": "introduced_by_fix" if introduced else "existing"},
    }


def review(verdict, *, findings=(), questions=()):
    return {
        "verdict": verdict,
        "summary": "deterministic fixture review",
        "findings": list(findings),
        "questions": list(questions),
        "context_requests": [],
    }


def digest(contents):
    return hashlib.sha256(contents.encode("utf-8")).hexdigest()


def resolution(identifier, summary):
    return {
        "summary": summary,
        "resolutions": [{"finding_id": identifier, "outcome": "fixed", "evidence": "literal fixture evidence"}],
    }


class _ExternalHarness:
    """Shared real-subprocess plumbing for every end-to-end scenario."""

    def install_fake_models(self, queue: dict) -> None:
        self.queue.write_text(json.dumps(queue), encoding="utf-8")
        self.log = self.root / "external-calls.jsonl"
        self.bin = self.root / "bin"
        self.bin.mkdir(exist_ok=True)
        fixtures = Path(__file__).parent / "fixtures"
        for name in ("fake_codex.py", "fake_claude.py"):
            target = self.bin / name.removeprefix("fake_").removesuffix(".py")
            shutil.copy2(fixtures / name, target)
            target.chmod(target.stat().st_mode | stat.S_IXUSR)
        self.environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "AI_REVIEW_FAKE_QUEUE": str(self.queue),
            "AI_REVIEW_FAKE_LOG": str(self.log),
        }

    def git(self, *argv):
        subprocess.run(["git", *argv], cwd=self.repo, check=True, text=True, capture_output=True)

    def cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()

        def factory(_args):
            return RunStore(self.runs, authority=self.authority)

        with patch.dict(os.environ, self.environment, clear=True), patch(
            "ai_review.runners._already_restricted_by_seatbelt", return_value=True
        ), redirect_stdout(output), redirect_stderr(errors):
            status = main(["--runs-root", str(self.runs), *argv], store_factory=factory)
        self.assertEqual(status, 0, errors.getvalue())
        self.assertEqual(errors.getvalue(), "")
        return json.loads(output.getvalue())

    def approve_with_presence(self, *argv):
        """Run one command with a mocked, Cancel-defaulted native dialog."""
        original_run = subprocess.run
        observed = []

        def dialog(call_argv, *args, **kwargs):
            if call_argv[0] == "/usr/bin/osascript":
                observed.append(call_argv)
                return subprocess.CompletedProcess(
                    call_argv, 0, "button returned:Approve\n", ""
                )
            return original_run(call_argv, *args, **kwargs)

        with patch("ai_review.cli.platform.system", return_value="Darwin"), patch(
            "ai_review.cli.subprocess.run", side_effect=dialog,
        ):
            result = self.cli(*argv)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][:2], ["/usr/bin/osascript", "-e"])
        return result, observed[0][-1]

    def external_calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def assert_safe_argv(self, argv):
        """Recognize publication commands even when wrapped or preceded by flags."""
        words = []
        for value in argv:
            try:
                words.extend(shlex.split(value))
            except ValueError:
                words.append(value)
        self.assertNotIn("--dangerously-bypass-approvals", words)
        for index, word in enumerate(words):
            if word == "gh":
                self.assertNotEqual(words[index + 1:index + 3], ["pr", "create"])
            if word != "git":
                continue
            following = words[index + 1:]
            while following and (following[0].startswith("-") or "=" in following[0]):
                if following[0] in {"-C", "--git-dir", "--work-tree"}:
                    following = following[2:]
                else:
                    following = following[1:]
            self.assertFalse(following and following[0] in {"push", "commit", "merge", "remote"})


class EndToEndWorkflowTests(_ExternalHarness, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "target-repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "e2e@example.invalid")
        self.git("config", "user.name", "E2E")
        self.plan = self.repo / "docs" / "plan.md"
        self.plan.parent.mkdir()
        self.plan_versions = (
            "# Plan\nInitial requirement.\n",
            "# Plan\nUser decision recorded.\n",
            "# Plan\nFirst contradiction resolved.\n",
            "# Plan\nSecond contradiction resolved.\n",
        )
        self.plan.write_text(self.plan_versions[0], encoding="utf-8")
        (self.repo / "task_specific_test.py").write_text(
            "import unittest\nclass Focused(unittest.TestCase):\n    def test_task_contract(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.git("add", "docs/plan.md", "task_specific_test.py")
        self.git("commit", "-qm", "base")

        self.runs = self.root / "external-artifacts" / "runs"
        self.authority = ApprovalAuthority(self.root / "workspace" / ".ai-review" / "approval.key")
        self.queue = self.root / "queue.json"
        answer_digest = hashlib.sha256(
            b'{"Q-001":"Persist for this target repository."}'
        ).hexdigest()
        decision_log = (
            "## Q-001\n"
            "- Question: Should the result persist?\n"
            "- Answer: Persist for this target repository.\n"
            "- Decision impact: pending Claude Plan update\n"
        )
        decision_digest = digest(decision_log)
        def plan_repair(identifier, summary, previous, updated):
            return {
                **resolution(identifier, summary),
                "input_plan_digest": digest(previous),
                "decision_log_digest": decision_digest,
                "plan": {
                    "content": updated,
                    "previous_digest": digest(previous),
                    "new_digest": digest(updated),
                    "changed_sections": ["Plan"],
                },
            }
        self.install_fake_models({
            "codex": [
                review("NEEDS_USER_INPUT", questions=["Should the result persist?"]),
                review("CHANGES_REQUIRED", findings=[finding("PLAN-001")]),
                review("CHANGES_REQUIRED", findings=[finding("PLAN-002", introduced=True)]),
                review("PASS"),
                review("CHANGES_REQUIRED", findings=[finding("CODE-001")]),
                review("PASS"),
            ],
            "claude": [
                {
                    "phase": "plan-update",
                    "expect": {"answer_digest": answer_digest, "plan_digest": digest(self.plan_versions[0])},
                    "output": {
                        "summary": "literal Plan update",
                        "answer_digest": answer_digest,
                        "plan": {
                            "content": self.plan_versions[1],
                            "previous_digest": digest(self.plan_versions[0]),
                            "new_digest": digest(self.plan_versions[1]),
                            "changed_sections": ["Decision"],
                        },
                    },
                },
                {
                    "phase": "plan-resolution",
                    "expect": {"finding_ids": ["PLAN-001"], "plan_digest": digest(self.plan_versions[1])},
                    "output": plan_repair(
                        "PLAN-001", "literal first Plan resolution",
                        self.plan_versions[1], self.plan_versions[2],
                    ),
                },
                {
                    "phase": "plan-resolution",
                    "expect": {"finding_ids": ["PLAN-002"], "plan_digest": digest(self.plan_versions[2])},
                    "output": plan_repair(
                        "PLAN-002", "literal introduced-contradiction resolution",
                        self.plan_versions[2], self.plan_versions[3],
                    ),
                },
                {
                    "phase": "code-initial",
                    "expect": {"finding_ids": [], "approved_plan_digest": digest(self.plan_versions[3])},
                    "write_files": {"implementation.txt": "initial implementation\n"},
                    "output": {"summary": "literal initial implementation", "resolutions": []},
                },
                {
                    "phase": "code-repair",
                    "expect": {"finding_ids": ["CODE-001"], "approved_plan_digest": digest(self.plan_versions[3])},
                    "write_files": {"implementation.txt": "fixed implementation\n"},
                    "output": resolution("CODE-001", "literal Code repair"),
                },
            ],
        })

    def tearDown(self):
        self.temp.cleanup()

    def approve_plan(self, run_id):
        result, _script = self.approve_with_presence("approve-plan", run_id)
        return result

    def test_standalone_cli_uses_home_application_support_for_transient_runs(self):
        workspace = self.root / "isolated-workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        home = self.root / "macos-home"
        executable = Path(__file__).parents[1] / "bin" / "ai-review"
        completed = subprocess.run(
            [
                sys.executable, str(executable), "init", "plan", "--repo", str(self.repo),
                "--plan", "docs/plan.md", "--verify",
                json.dumps({
                    "kind": "test",
                    "argv": [sys.executable, "-m", "unittest", "tests.feature"],
                    "scope": "tests.feature",
                }),
            ],
            cwd=workspace,
            env={
                **os.environ,
                "HOME": str(home),
                "AI_REVIEW_HOME": str(workspace),
                "PYTHONPATH": str(Path(yaml.__file__).resolve().parent.parent),
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        expected_root = home / "Library" / "Application Support" / "ai-review" / "runs"
        self.assertTrue((expected_root / project_id(self.repo) / payload["run_id"] / "state.json").is_file())
        self.assertFalse((workspace / ".ai-review" / "runs").exists())
        self.assertFalse((self.repo / ".ai-review").exists())

    def test_temporary_installer_apply_links_every_canonical_end_to_end_target(self):
        source_root = self.root / "temporary-canonical-skills"
        claude_root = self.root / "temporary-claude" / "commands"
        codex_root = self.root / "temporary-codex" / "skills"
        targets = []
        for name in SKILL_NAMES:
            source = source_root / name
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(name, encoding="utf-8")
            targets.extend((InstallTarget(source, claude_root / name), InstallTarget(source, codex_root / name)))
        apply_install(targets, self.root / "temporary-backups", [claude_root, codex_root], canonical_root=source_root)
        self.assertEqual(len(targets), 2 * len(SKILL_NAMES))
        self.assertEqual(len(targets), 4)
        self.assertIn("consensus-review", SKILL_NAMES)
        self.assertEqual([target.target.resolve() for target in targets], [target.source.resolve() for target in targets])

    def test_fake_claude_rejects_a_cross_phase_literal_payload(self):
        self.queue.write_text(json.dumps({"codex": [], "claude": [{
            "phase": "code-repair", "expect": {"finding_ids": ["CODE-001"]}, "output": resolution("CODE-001", "wrong phase"),
        }]}), encoding="utf-8")
        completed = subprocess.run(
            [str(self.bin / "claude"), "-p", "--json-schema", "{}", "prompt\n\nINPUT_JSON:\n{}"],
            cwd=self.repo, env=self.environment, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("phase", completed.stderr)

    def test_argv_safety_recognizes_wrapped_publication_commands(self):
        for argv in (
            ["env", "REVIEW=1", "git", "-C", "repo", "push"],
            ["bash", "-c", "git commit -m unsafe"],
            ["sudo", "gh", "pr", "create"],
            ["git", "remote", "set-url", "origin", "unsafe"],
        ):
            with self.subTest(argv=argv), self.assertRaises(AssertionError):
                self.assert_safe_argv(argv)

    def test_plan_to_code_uses_external_artifacts_and_real_subprocess_boundaries(self):
        test_argv = [str(Path(sys.executable).resolve()), "-m", "unittest", "task_specific_test"]
        plan = self.cli(
            "init", "plan", "--repo", str(self.repo), "--plan", "docs/plan.md",
            "--test", json.dumps(test_argv),
        )
        plan_id = plan["run_id"]
        self.assertEqual(self.cli("run", plan_id)["status"], "AWAITING_USER_INPUT")
        answers = self.root / "answers.json"
        answers.write_text('{"Q-001":"Persist for this target repository."}', encoding="utf-8")
        self.assertEqual(self.cli("answer", plan_id, "--answers", str(answers))["status"], "AWAITING_HUMAN_PLAN_REVIEW")
        self.assertEqual(self.plan.read_text(encoding="utf-8"), self.plan_versions[3])

        approved = self.approve_plan(plan_id)
        self.assertEqual(approved["status"], "AWAITING_HUMAN_PLAN_REVIEW")
        code = self.cli("init", "code", "--repo", str(self.repo), "--plan-run", plan_id, "--base", "HEAD")
        code_id = code["run_id"]
        self.assertEqual(self.cli("run", code_id)["status"], "AWAITING_HUMAN_CODE_REVIEW")

        project = project_id(self.repo)
        plan_artifacts = self.runs / project / plan_id
        code_artifacts = self.runs / project / code_id
        self.assertFalse((self.repo / ".ai-review").exists())
        self.assertFalse((self.root / "workspace" / ".ai-review" / "runs").exists())
        self.assertTrue(plan_artifacts.is_dir())
        self.assertTrue(code_artifacts.is_dir())
        self.assertEqual(json.loads((plan_artifacts / "state.json").read_text())["manifest"]["repo_path"], str(self.repo.resolve()))

        manifest = json.loads((code_artifacts / "state.json").read_text())["manifest"]
        plan_state = json.loads((plan_artifacts / "state.json").read_text())
        self.assertEqual(manifest["base_oid"], plan["base_oid"])
        verification_command = manifest["verification_commands"][0]
        self.assertEqual(
            {key: verification_command[key] for key in ("kind", "argv", "scope")},
            {"kind": "test", "argv": test_argv, "scope": "task"},
        )
        self.assertEqual(
            verification_command["executable_identity"]["realpath"], test_argv[0]
        )
        self.assertEqual(plan_state["approval_attestation"]["receipt"]["run_id"], plan_id)
        self.assertEqual(plan_state["approval_attestation"]["receipt"]["base_oid"], plan["base_oid"])
        self.assertEqual(plan_state["repair_round"], 2)
        self.assertEqual(json.loads((code_artifacts / "state.json").read_text())["repair_round"], 1)
        self.assertLess(json.loads((code_artifacts / "state.json").read_text())["repair_round"], 6)
        self.assertTrue(all(json.loads(path.read_text())["exit_code"] == 0 for path in code_artifacts.glob("verification/*.json")))
        self.assertEqual(len(list(code_artifacts.glob("verification/*.json"))), 2)
        policy = load_policy(Path(__file__).parents[1] / "config" / "defaults.yaml")
        self.assertEqual((policy.max_rounds, policy.max_context_tokens, policy.max_initial_sources, policy.max_context_expansions), (6, 8000, 3, 2))

        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([call["tool"] for call in calls], ["codex", "claude", "codex", "claude", "codex", "claude", "codex", "claude", "codex", "claude", "codex"])
        for call in calls:
            self.assert_safe_argv(call["argv"])
        codex_calls = [call for call in calls if call["tool"] == "codex"]
        self.assertTrue(all("--output-schema" in call["argv"] and "-o" in call["argv"] for call in codex_calls))
        self.assertTrue(all(Path(call["schema"]).name == "codex-review.schema.json" for call in codex_calls))
        claude_calls = [call for call in calls if call["tool"] == "claude"]
        self.assertTrue(all("--json-schema" in call["argv"] for call in claude_calls))
        self.assertTrue(all(call["schema"]["type"] == "object" for call in claude_calls))
        self.assertTrue(all("--safe-mode" in call["argv"] for call in claude_calls))
        self.assertTrue(all(
            "Bash" not in call["argv"][call["argv"].index("--tools") + 1]
            for call in claude_calls
        ))
        self.assertEqual(
            [(call["phase"], call["received"]) for call in claude_calls],
            [(call["expected"]["phase"], call["expected"]["expect"]) for call in claude_calls],
        )
        self.assertEqual(
            [(call["phase"], call["received"].get("finding_ids")) for call in claude_calls],
            [("plan-update", None), ("plan-resolution", ["PLAN-001"]), ("plan-resolution", ["PLAN-002"]), ("code-initial", []), ("code-repair", ["CODE-001"])],
        )
        self.assertEqual(
            [call["received"]["plan_digest"] for call in claude_calls[:3]],
            [digest(plan) for plan in self.plan_versions[:3]],
        )
        for record in (json.loads(path.read_text()) for path in code_artifacts.glob("verification/*.json")):
            self.assert_safe_argv(record["argv"])
        self.assertEqual(json.loads(self.queue.read_text())["codex"], [])
        self.assertEqual(json.loads(self.queue.read_text())["claude"], [])

        summary = generate_outputs(RunStore(self.runs, authority=self.authority), code_id)
        self.assertIsNotNone(summary.knowledge_candidate)
        self.assertTrue(summary.knowledge_candidate.is_file())
        self.assertIn("THREE_OR_MORE_REPAIR_ROUNDS", summary.knowledge_candidate.read_text(encoding="utf-8"))


def review_codex(profile, repair_round, output):
    return {
        "phase": "review-codex",
        "expect": {"profile": profile, "repair_round": repair_round},
        "output": output,
    }


def review_repair(
    finding_ids, *, write_files=None, summary="literal Review repair", risk_flags=(),
):
    return {
        "phase": "review-repair",
        "expect": {"finding_ids": list(finding_ids)},
        "write_files": dict(write_files or {}),
        "output": {
            "summary": summary,
            "resolutions": [
                {
                    "finding_id": identifier, "outcome": "fixed",
                    "evidence": "literal fixture repair evidence",
                }
                for identifier in finding_ids
            ],
            "risk_flags": list(risk_flags),
        },
    }


def specialist_submission():
    return {
        "specialists": [
            {
                "name": name,
                "findings": [{
                    "id": "%s-001" % prefix, "severity": "major", "category": category,
                    "location": "reviewed.txt:1",
                    "evidence": "the %s lens observed a concrete defect" % name,
                    "required_outcome": "restore the expected observable behavior",
                    "risk_flags": [],
                }],
            }
            for name, category, prefix in (
                ("swiftui-reviewer", "swiftui", "SWIFTUI"),
                ("ux-critique", "ux", "UX"),
                ("resilience-auditor", "resilience", "RESILIENCE"),
            )
        ],
    }


class DirectReviewEndToEndTests(_ExternalHarness, unittest.TestCase):
    """Prove the Review path across real child processes and native gates."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "target-repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "e2e@example.invalid")
        self.git("config", "user.name", "E2E")
        (self.repo / "reviewed.txt").write_text("baseline\n", encoding="utf-8")
        (self.repo / "task_specific_test.py").write_text(
            "import unittest\n"
            "class Focused(unittest.TestCase):\n"
            "    def test_task_contract(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.git("add", "reviewed.txt", "task_specific_test.py")
        self.git("commit", "-qm", "base")
        # The reviewed diff is pre-existing local work, exactly as a finished
        # branch or a completed bug fix would look.
        (self.repo / "reviewed.txt").write_text("baseline\nlocal change\n", encoding="utf-8")
        (self.repo / "untracked_note.txt").write_text("untracked local work\n", encoding="utf-8")
        self.runs = self.root / "external-artifacts" / "runs"
        self.authority = ApprovalAuthority(
            self.root / "workspace" / ".ai-review" / "approval.key"
        )
        self.queue = self.root / "queue.json"
        self.test_argv = [
            str(Path(sys.executable).resolve()), "-m", "unittest", "task_specific_test",
        ]

    def tearDown(self):
        self.temp.cleanup()

    def init_review(self, profile="generic"):
        arguments = []
        if profile == "ios":
            findings = self.root / "preflight.json"
            findings.write_text(json.dumps(specialist_submission()), encoding="utf-8")
            arguments = ["--preflight", str(findings)]
        return self.cli(
            "init", "review", "--repo", str(self.repo), "--base", "HEAD",
            "--brief", "Review the completed local change", "--profile", profile,
            "--verify", json.dumps({
                "kind": "test", "argv": self.test_argv, "scope": "task_specific_test",
            }),
            *arguments,
        )

    def approved_review(self, profile="generic"):
        initialized = self.init_review(profile)
        self.assertEqual(initialized["status"], "AWAITING_REVIEW_APPROVAL")
        self.assertEqual(initialized["next_action"], "human_review_scope")
        self.assertEqual(self.external_calls(), [])
        approved, script = self.approve_with_presence("approve-review", initialized["run_id"])
        self.assertEqual(approved["status"], "READY")
        self.assertIn(initialized["initial_patch_digest"], script)
        return initialized

    def artifacts(self, run_id):
        return self.runs / project_id(self.repo) / run_id

    def assert_no_project_local_state(self):
        self.assertFalse((self.repo / ".ai-review").exists())
        self.assertFalse((self.root / "workspace" / ".ai-review" / "runs").exists())

    def assert_every_call_is_a_safe_direct_process(self, calls):
        for call in calls:
            self.assert_safe_argv(call["argv"])
            words = " ".join(call["argv"])
            for forbidden in ("git push", "gh pr create", "curl ", "npm publish", "-c "):
                self.assertNotIn(forbidden, words)
            self.assertNotIn("--dangerously-skip-permissions", call["argv"])
        for call in (item for item in calls if item["tool"] == "claude"):
            self.assertIn("--safe-mode", call["argv"])
            self.assertNotIn(
                "Bash", call["argv"][call["argv"].index("--tools") + 1]
            )

    def test_generic_review_repairs_once_then_reaches_human_approval(self):
        self.install_fake_models({
            "codex": [
                review_codex("generic", 0, review(
                    "CHANGES_REQUIRED", findings=[finding("CODE-001")]
                )),
                review_codex("generic", 1, review("PASS")),
            ],
            "claude": [review_repair(
                ["CODE-001"], write_files={"reviewed.txt": "baseline\nrepaired change\n"},
            )],
        })
        initialized = self.approved_review()
        run_id = initialized["run_id"]

        result = self.cli("run", run_id)

        self.assertEqual(result["status"], "AWAITING_HUMAN_CODE_REVIEW")
        self.assertEqual(result["repair_round"], 1)
        calls = self.external_calls()
        self.assertEqual(
            [call["tool"] for call in calls], ["codex", "claude", "codex"]
        )
        self.assertEqual(calls[0]["phase"], "review-codex")
        self.assertEqual(calls[1]["phase"], "review-repair")
        self.assertIn("# Direct code review", calls[0]["argv"][-1])
        self.assertIn("# Direct review repair", calls[1]["argv"][-1])
        self.assert_every_call_is_a_safe_direct_process(calls)
        artifacts = self.artifacts(run_id)
        rounds = sorted(path.name for path in artifacts.glob("verification-rounds/*.json"))
        self.assertEqual(rounds, ["0001.json", "0002.json"])
        self.assertTrue(all(
            json.loads(path.read_text())["exit_code"] == 0
            for path in artifacts.glob("verification/*.json")
        ))
        self.assertIn(
            b"untracked_note.txt",
            (artifacts / "patches" / "round-0000.patch").read_bytes(),
        )
        self.assertEqual(json.loads(self.queue.read_text())["codex"], [])
        self.assertEqual(json.loads(self.queue.read_text())["claude"], [])
        self.assert_no_project_local_state()

        approved, script = self.approve_with_presence("approve-code", run_id)

        self.assertEqual(approved["status"], "AWAITING_HUMAN_CODE_REVIEW")
        approval = json.loads((artifacts / "code-approval.json").read_text())
        self.assertIn(approval["candidate_digest"], script)
        # One repair with green verification is not a learning event, so there is
        # nothing to write back and the attempt must fail closed.
        errors = io.StringIO()
        with patch.dict(os.environ, self.environment, clear=True), redirect_stderr(errors):
            status = main(
                ["--runs-root", str(self.runs), "writeback-knowledge", run_id],
                store_factory=lambda _args: RunStore(self.runs, authority=self.authority),
            )
        self.assertEqual(status, 2)
        self.assertNotEqual(errors.getvalue(), "")

    def test_ios_review_merges_three_specialists_into_one_repair(self):
        specialist_ids = [
            "PF-RESILIENCE-RESILIENCE-001", "PF-SWIFTUI-SWIFTUI-001", "PF-UX-UX-001",
        ]
        self.install_fake_models({
            "codex": [
                review_codex("ios", 0, review(
                    "CHANGES_REQUIRED", findings=[finding("CODE-001")]
                )),
                review_codex("ios", 1, review("PASS")),
            ],
            "claude": [review_repair(
                ["CODE-001", *specialist_ids],
                write_files={"reviewed.txt": "baseline\npolished change\n"},
            )],
        })
        initialized = self.approved_review("ios")
        run_id = initialized["run_id"]

        # The specialists were frozen at init, so the whole loop is one call.
        result = self.cli("run", run_id)

        self.assertFalse((self.artifacts(run_id) / "preflight-request.json").exists())
        merged = json.loads(
            (self.artifacts(run_id) / "merged-reviews" / "0001.json").read_text()
        )
        self.assertEqual(
            sorted(item["id"] for item in merged["findings"]),
            ["CODE-001", *specialist_ids],
        )
        self.assertEqual(result["status"], "AWAITING_HUMAN_CODE_REVIEW")
        self.assertEqual(result["repair_round"], 1)
        calls = self.external_calls()
        self.assertEqual([call["tool"] for call in calls], ["codex", "claude", "codex"])
        # Codex precedes Claude, and the mutating runner never launches a specialist.
        self.assertLess(
            [call["tool"] for call in calls].index("codex"),
            [call["tool"] for call in calls].index("claude"),
        )
        for call in calls:
            for name in ("swiftui-reviewer", "ux-critique", "resilience-auditor"):
                self.assertNotIn(name, " ".join(call["argv"][:-1]))
        self.assertEqual(len([call for call in calls if call["tool"] == "claude"]), 1)
        self.assertEqual(
            calls[1]["received"]["finding_ids"], ["CODE-001", *specialist_ids]
        )
        self.assert_every_call_is_a_safe_direct_process(calls)
        self.assertEqual(json.loads(self.queue.read_text())["claude"], [])
        self.assert_no_project_local_state()

    def test_a_high_risk_repair_pauses_until_approve_risk_binds_the_patch(self):
        self.install_fake_models({
            "codex": [
                review_codex("generic", 0, review(
                    "CHANGES_REQUIRED", findings=[finding("CODE-001")]
                )),
                review_codex("generic", 1, review("PASS")),
            ],
            "claude": [review_repair(["CODE-001"], write_files={
                "Package.resolved": '{"pins": []}\n',
            })],
        })
        initialized = self.approved_review()
        run_id = initialized["run_id"]

        paused = self.cli("run", run_id)

        self.assertEqual(paused["status"], "PAUSED")
        artifacts = self.artifacts(run_id)
        self.assertEqual(
            json.loads((artifacts / "pause.json").read_text())["reason"],
            "HIGH_RISK_CHANGE",
        )
        self.assertEqual([call["tool"] for call in self.external_calls()], ["codex", "claude"])
        request = json.loads((artifacts / "risk-approval-request.json").read_text())
        self.assertEqual(request["categories"], ["dependencies"])

        stalled = self.cli("resume", run_id)
        self.assertEqual(stalled["status"], "PAUSED")
        self.assertEqual([call["tool"] for call in self.external_calls()], ["codex", "claude"])

        _approved, script = self.approve_with_presence("approve-risk", run_id)
        self.assertIn("dependencies", script)
        self.assertIn("Package.resolved", script)
        self.assertIn(request["patch_digest"], script)

        resumed = self.cli("resume", run_id)

        self.assertEqual(resumed["status"], "AWAITING_HUMAN_CODE_REVIEW")
        self.assertEqual(
            [call["tool"] for call in self.external_calls()],
            ["codex", "claude", "codex"],
        )
        self.assert_every_call_is_a_safe_direct_process(self.external_calls())
        self.assert_no_project_local_state()

    def test_six_repairs_run_and_a_seventh_never_starts(self):
        self.install_fake_models({
            "codex": [
                review_codex("generic", index, review(
                    "CHANGES_REQUIRED",
                    findings=[finding("CODE-%03d" % (index + 1), introduced=index > 0)],
                ))
                for index in range(7)
            ],
            "claude": [
                review_repair(
                    ["CODE-%03d" % (index + 1)],
                    write_files={"reviewed.txt": "baseline\nrepair %d\n" % index},
                    summary="literal Review repair %d" % index,
                )
                for index in range(6)
            ],
        })
        initialized = self.approved_review()
        run_id = initialized["run_id"]

        result = self.cli("run", run_id)

        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["repair_round"], 6)
        artifacts = self.artifacts(run_id)
        self.assertEqual(
            json.loads((artifacts / "pause.json").read_text())["reason"],
            "MAX_REPAIR_ROUNDS",
        )
        calls = self.external_calls()
        self.assertEqual(
            [call["tool"] for call in calls],
            ["codex", "claude"] * 6 + ["codex"],
        )
        self.assertEqual(len([call for call in calls if call["tool"] == "claude"]), 6)
        self.assertFalse((artifacts / "claude-intents" / "repair-0007.json").exists())
        self.assertEqual(json.loads(self.queue.read_text())["claude"], [])
        self.assert_every_call_is_a_safe_direct_process(calls)
        self.assert_no_project_local_state()

    def test_no_model_runs_before_native_review_approval(self):
        self.install_fake_models({"codex": [], "claude": []})
        initialized = self.init_review()

        # An unapproved Review run must fail closed rather than call a model.
        result = self.cli("run", initialized["run_id"])

        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(self.external_calls(), [])
        self.assert_no_project_local_state()


if __name__ == "__main__":
    unittest.main()
