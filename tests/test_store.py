import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch

import yaml

from ai_review.models import (
    ApprovalAttestation,
    ApprovalAuthority,
    HumanApprovalReceipt,
    ReviewApprovalAttestation,
    ReviewApprovalReceipt,
    ReviewManifest,
    RunManifest,
    RunState,
    Status,
    Verdict,
)
from ai_review.process_security import executable_identity
from ai_review.policy import PolicyError, load_policy
from ai_review.store import (
    DEFAULT_RUNS_ROOT,
    PRODUCTION_WORKSPACE_ROOT,
    RunStore,
    default_runs_root,
    project_id,
    resolve_workspace_root,
)


def approval_receipt(plan):
    manifest = plan.manifest
    return HumanApprovalReceipt(
        run_id=plan.run_id or "in-memory-plan", plan_digest=hashlib.sha256(Path(manifest.plan_path).read_bytes()).hexdigest(),
        base_oid=manifest.base_oid, approved_at="2026-07-31T00:00:00+00:00",
        provider="test:held-capability", actor="test-human",
    )


def approve(plan, authority):
    plan.approve_plan(receipt=approval_receipt(plan), authority=authority)


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.authority = ApprovalAuthority.for_workspace(Path(self.temp.name))
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "docs").mkdir(parents=True)
        self.plan_path = self.repo / "docs" / "plan.md"
        self.plan_path.write_text("# Plan\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "docs/plan.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "base"], check=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_clarification_does_not_increment_repair_round(self):
        run = RunState.new("plan", "docs/plan.md", "abc123")

        run.transition(Verdict.NEEDS_USER_INPUT)

        self.assertEqual(run.repair_round, 0)
        self.assertEqual(run.status, Status.AWAITING_USER_INPUT)

    def test_approval_authority_uses_a_persistent_0600_32_byte_key(self):
        key_path = Path(self.temp.name) / ".ai-review" / "approval.key"
        reloaded = ApprovalAuthority.for_workspace(Path(self.temp.name))

        self.assertEqual(reloaded.key_path, key_path.resolve())
        self.assertEqual(len(key_path.read_bytes()), 32)
        self.assertEqual(S_IMODE(key_path.stat().st_mode), 0o600)
        self.assertEqual(
            reloaded.sign("test-human", "test:held-capability", "2026-07-30T00:00:00+00:00", "a" * 64, "b" * 32, "c" * 64),
            self.authority.sign("test-human", "test:held-capability", "2026-07-30T00:00:00+00:00", "a" * 64, "b" * 32, "c" * 64),
        )

    def test_sixth_changes_required_pauses(self):
        plan = RunState.new(
            "plan",
            str(self.plan_path), str(self.repo), "HEAD",
            verification_commands=[
                {
                    "kind": "test",
                    "scope": "focused unit suite",
                    "argv": ["python", "-m", "unittest", "tests.test_feature"],
                }
            ],
        )
        plan.transition(Verdict.PASS)
        approve(plan, self.authority)
        run = RunState.new_code_from_approved_plan(plan, self.authority)

        for _ in range(6):
            run.transition(Verdict.CHANGES_REQUIRED)

        self.assertEqual(run.repair_round, 6)
        self.assertEqual(run.status, Status.PAUSED)

    def test_context_request_keeps_run_running(self):
        run = RunState.new("plan", "docs/plan.md", "abc123")

        run.transition(Verdict.CONTEXT_REQUEST)

        self.assertEqual(run.status, Status.RUNNING)
        self.assertEqual(run.repair_round, 0)

    def test_human_review_requires_explicit_approval_command(self):
        run = RunState.new("plan", "docs/plan.md", "abc123")
        run.transition(Verdict.PASS)

        with self.assertRaises(ValueError):
            run.transition(Verdict.CHANGES_REQUIRED)

    def test_approve_plan_records_utc_timestamp_without_changing_review_state(self):
        run = RunState.new("plan", str(self.plan_path), str(self.repo), "HEAD")
        run.transition(Verdict.PASS)

        approve(run, self.authority)

        self.assertEqual(run.status, Status.AWAITING_HUMAN_PLAN_REVIEW)
        self.assertIsNotNone(run.human_approved_at)
        self.assertTrue(run.human_approved_at.endswith("+00:00"))

    def test_approve_plan_rejects_caller_supplied_actor_or_source(self):
        run = RunState.new("plan", "docs/plan.md", "/private/tmp/example-repo", "main")
        run.transition(Verdict.PASS)

        with self.assertRaises(TypeError):
            run.approve_plan(
                actor="codex", source="review-output", authority=self.authority
            )


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.authority = ApprovalAuthority.for_workspace(Path(self.temp.name))
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "docs").mkdir(parents=True)
        self.plan_path = self.repo / "docs" / "plan.md"
        self.plan_path.write_text("# Plan\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "docs/plan.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "base"], check=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_code_manifest_requires_a_task_specific_test_command(self):
        with self.assertRaises(ValueError):
            RunManifest(
                kind="code",
                repo_path="/private/tmp/example-repo",
                plan_path="/private/tmp/example-repo/docs/plan.md",
                base_ref="main",
                verification_commands=[
                    {
                        "kind": "test",
                        "scope": "focused unit suite",
                        "argv": ["git", "diff", "--check"],
                    }
                ],
                knowledge_sources=[],
            )

    def test_code_manifest_recognizes_common_test_runner_semantics(self):
        commands = (
            ["xcodebuild", "test"],
            ["pytest", "tests/test_feature.py"],
            ["python", "-m", "unittest", "tests.test_feature"],
            ["swift", "test"],
            ["cargo", "test"],
            ["go", "test", "./..."],
        )
        for argv in commands:
            with self.subTest(argv=argv):
                manifest = RunManifest(
                    kind="code",
                    repo_path="/private/tmp/example-repo",
                    plan_path="/private/tmp/example-repo/docs/plan.md",
                    base_ref="main",
                    verification_commands=[
                        {"kind": "test", "scope": "focused task tests", "argv": argv}
                    ],
                    knowledge_sources=[],
                )
                self.assertEqual(manifest.verification_commands[0].kind, "test")

    def test_code_run_requires_human_approved_plan(self):
        plan = RunState.new(
            "plan",
            str(self.plan_path), str(self.repo), "HEAD",
            verification_commands=[
                {
                    "kind": "test",
                    "scope": "focused unit suite",
                    "argv": ["python", "-m", "unittest", "tests.test_feature"],
                }
            ],
        )

        with self.assertRaises(ValueError):
            RunState.new_code_from_approved_plan(plan, self.authority)

    def test_code_inherits_the_exact_approved_plan_manifest(self):
        plan = RunState.new(
            "plan",
            str(self.plan_path), str(self.repo), "HEAD",
            verification_commands=[
                {
                    "kind": "test",
                    "scope": "focused unit suite",
                    "argv": ["python", "-m", "unittest", "tests.test_feature"],
                }
            ],
        )
        plan.transition(Verdict.PASS)
        approve(plan, self.authority)

        code = RunState.new_code_from_approved_plan(plan, self.authority)

        self.assertEqual(code.manifest, plan.manifest)
        self.assertEqual(code.base_ref, plan.base_ref)
        self.assertEqual(code.approval_attestation, plan.approval_attestation)
        with self.assertRaises(TypeError):
            RunState.new_code_from_approved_plan(plan, self.authority, "other")

    def test_forged_or_stale_attestation_is_rejected_on_load_and_code_creation(self):
        plan = RunState.new(
            "plan",
            str(self.plan_path), str(self.repo), "HEAD",
            verification_commands=[
                {
                    "kind": "test",
                    "scope": "focused unit suite",
                    "argv": ["python", "-m", "unittest", "tests.test_feature"],
                }
            ],
        )
        plan.transition(Verdict.PASS)
        approve(plan, self.authority)
        forged = plan.to_dict()
        forged["manifest"]["base_ref"] = "other"

        with self.assertRaises(ValueError):
            RunState.from_dict(forged, self.authority)

        object.__setattr__(
            plan,
            "approval_attestation",
            ApprovalAttestation(
                actor="test-human",
                source="test:held-capability",
                approved_at=plan.human_approved_at,
                manifest_digest="0" * 64,
                nonce="a" * 32,
                receipt=approval_receipt(plan),
                receipt_digest=approval_receipt(plan).digest(),
                signature="0" * 64,
            ),
        )
        with self.assertRaises(ValueError):
            RunState.new_code_from_approved_plan(plan, self.authority)

    def test_syntactically_valid_fake_signature_is_rejected(self):
        plan = RunState.new(
            "plan",
            str(self.plan_path), str(self.repo), "HEAD",
            verification_commands=[
                {
                    "kind": "test",
                    "scope": "focused unit suite",
                    "argv": ["python", "-m", "unittest", "tests.test_feature"],
                }
            ],
        )
        plan.transition(Verdict.PASS)
        approve(plan, self.authority)
        forged = plan.to_dict()
        forged["approval_attestation"]["signature"] = "0" * 64

        with self.assertRaises(ValueError):
            RunState.from_dict(forged, self.authority)

    def test_direct_code_construction_is_rejected(self):
        manifest = RunManifest(
            kind="plan",
            repo_path="/private/tmp/example-repo",
            plan_path="/private/tmp/example-repo/docs/plan.md",
            base_ref="main",
        )

        with self.assertRaises(ValueError):
            RunState(kind="code", manifest=manifest)

        with self.assertRaises(ValueError):
            RunState.new("code", "docs/plan.md", "abc123")

    def test_path_and_base_ref_are_derived_from_the_manifest(self):
        plan = RunState.new(
            "plan", "docs/plan.md", "/private/tmp/example-repo", "main"
        )

        self.assertEqual(plan.plan_path, plan.manifest.plan_path)
        self.assertEqual(plan.base_ref, plan.manifest.base_ref)
        with self.assertRaises(TypeError):
            replace(plan, base_ref="other")


class ReviewManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "source.md").write_text("context\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.md"], check=True)
        subprocess.run([
            "git", "-C", str(self.repo), "-c", "user.email=test@example.com",
            "-c", "user.name=Test", "commit", "-qm", "base",
        ], check=True)
        self.base_oid = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        identity = executable_identity(Path(sys.executable))
        self.values = {
            "kind": "review",
            "repo_path": str(self.repo),
            "base_ref": "HEAD",
            "base_oid": self.base_oid,
            "brief": "  Review authentication changes.  ",
            "brief_digest": hashlib.sha256(b"Review authentication changes.").hexdigest(),
            "profile": "generic",
            "initial_patch_digest": "a" * 64,
            "verification_commands": [{
                "kind": "test", "scope": "focused review tests",
                "argv": [sys.executable, "-m", "unittest", "tests.test_store.ReviewManifestTests"],
            }],
            "knowledge_sources": [str(self.repo / "source.md")],
            "context_checksum": "b" * 64,
            "review_executables": {"codex": identity, "claude": identity},
        }

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, **changes):
        return ReviewManifest(**dict(self.values, **changes))

    def receipt(self, run, **changes):
        values = {
            "run_id": run.run_id or "review-run",
            "manifest_digest": run.manifest.digest(),
            "approved_at": "2026-08-04T00:00:00+00:00",
            "provider": "test:held-capability",
            "actor": "test-human",
        }
        return ReviewApprovalReceipt(**dict(values, **changes))

    def test_review_manifest_normalizes_and_round_trips(self):
        manifest = self.manifest()

        self.assertEqual(manifest.brief, "Review authentication changes.")
        self.assertEqual(manifest.repo_path, str(self.repo.resolve()))
        self.assertEqual(manifest.knowledge_sources, (str((self.repo / "source.md").resolve()),))
        self.assertEqual(ReviewManifest.from_dict(manifest.to_dict()), manifest)
        self.assertEqual(len(manifest.digest()), 64)
        self.assertEqual(len(manifest.verification_digest()), 64)
        self.assertNotIn("plan_path", manifest.to_dict())
        self.assertNotIn("plan_digest", manifest.to_dict())

    def test_review_manifest_accepts_generic_and_ios_profiles(self):
        for profile in ("generic", "ios"):
            with self.subTest(profile=profile):
                self.assertEqual(self.manifest(profile=profile).profile, profile)

    def test_review_manifest_rejects_unknown_profile_empty_or_overlong_brief(self):
        for changes in (
            {"profile": "android"},
            {"brief": " ", "brief_digest": hashlib.sha256(b"").hexdigest()},
            {"brief": "é" * 1001, "brief_digest": hashlib.sha256(("é" * 1001).encode()).hexdigest()},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.manifest(**changes)

    def test_review_manifest_rejects_malformed_or_mismatched_digests(self):
        for changes in (
            {"brief_digest": "A" * 64},
            {"brief_digest": "0" * 64},
            {"initial_patch_digest": "bad"},
            {"context_checksum": "C" * 64},
            {"base_oid": "d" * 39},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.manifest(**changes)

    def test_review_manifest_requires_a_focused_test_and_both_reviewers(self):
        with self.assertRaises(ValueError):
            self.manifest(verification_commands=[])
        for identities in (
            {},
            {"codex": self.values["review_executables"]["codex"]},
            {"claude": self.values["review_executables"]["claude"]},
        ):
            with self.subTest(identities=identities):
                with self.assertRaises(ValueError):
                    self.manifest(review_executables=identities)

    def test_review_manifest_rejects_a_test_command_without_an_executable_identity(self):
        unavailable_test = {
            "kind": "test",
            "scope": "focused review tests",
            "argv": [
                "python-never-installed-review-test", "-m", "unittest",
                "tests.test_store.ReviewManifestTests",
            ],
        }

        with self.assertRaises(ValueError):
            self.manifest(verification_commands=[unavailable_test])

    def test_review_state_approval_binding_transition_and_round_trip(self):
        authority = ApprovalAuthority.for_workspace(Path(self.temp.name))
        run = replace(RunState.new_review(self.manifest()), run_id="review-run")

        self.assertEqual(run.status, Status.AWAITING_REVIEW_APPROVAL)
        with self.assertRaises(ValueError):
            run.approve_review(receipt=self.receipt(run, run_id="other"), authority=authority)
        run.approve_review(receipt=self.receipt(run), authority=authority)

        self.assertEqual(run.status, Status.READY)
        run.validate_review_binding("a" * 64, self.base_oid)
        loaded = RunState.from_dict(run.to_dict(), authority)
        self.assertEqual(loaded.to_dict(), run.to_dict())
        loaded.transition(Verdict.PASS)
        self.assertEqual(loaded.status, Status.AWAITING_HUMAN_CODE_REVIEW)

    def test_unpersisted_review_cannot_accept_an_approval_receipt(self):
        authority = ApprovalAuthority.for_workspace(Path(self.temp.name))
        run = RunState.new_review(self.manifest())

        with self.assertRaises(ValueError):
            run.approve_review(receipt=self.receipt(run), authority=authority)

        self.assertEqual(run.status, Status.AWAITING_REVIEW_APPROVAL)
        self.assertIsNone(run.approval_attestation)

        persisted = replace(run, run_id="review-run")
        persisted.approve_review(receipt=self.receipt(persisted), authority=authority)
        forged = persisted.to_dict()
        forged["run_id"] = None
        with self.assertRaises(ValueError):
            RunState.from_dict(forged, authority)

    def test_review_binding_detects_each_bound_field_mutation(self):
        authority = ApprovalAuthority.for_workspace(Path(self.temp.name))
        run = replace(RunState.new_review(self.manifest()), run_id="review-run")
        run.approve_review(receipt=self.receipt(run), authority=authority)

        for field_name, value in (
            ("brief", "Changed brief"),
            ("profile", "ios"),
            ("initial_patch_digest", "c" * 64),
            ("base_oid", "d" * 40),
            ("context_checksum", "e" * 64),
            ("risk_policy_version", "review-risk-v2"),
        ):
            forged = run.to_dict()
            forged["manifest"][field_name] = value
            if field_name == "brief":
                forged["manifest"]["brief_digest"] = hashlib.sha256(value.encode()).hexdigest()
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError):
                    RunState.from_dict(forged, authority)

        with self.assertRaises(ValueError):
            run.validate_review_binding("f" * 64, self.base_oid)
        with self.assertRaises(ValueError):
            run.validate_review_binding("a" * 64, "f" * 40)

    def test_review_binding_detects_valid_verification_and_reviewer_identity_mutations(self):
        authority = ApprovalAuthority.for_workspace(Path(self.temp.name))
        run = replace(RunState.new_review(self.manifest()), run_id="review-run")
        run.approve_review(receipt=self.receipt(run), authority=authority)
        alternative_identity = executable_identity(Path("/usr/bin/git"))

        forged = run.to_dict()
        forged["manifest"]["verification_commands"][0]["argv"] = [
            sys.executable, "-m", "unittest", "tests.test_store.ManifestTests",
        ]
        with self.assertRaises(ValueError):
            RunState.from_dict(forged, authority)

        for reviewer in ("codex", "claude"):
            forged = run.to_dict()
            identities = dict(forged["manifest"]["review_executables"])
            identities[reviewer] = alternative_identity
            forged["manifest"]["review_executables"] = identities
            with self.subTest(reviewer=reviewer):
                with self.assertRaises(ValueError):
                    RunState.from_dict(forged, authority)

    def test_review_attestation_rejects_receipt_and_manifest_mismatch(self):
        authority = ApprovalAuthority.for_workspace(Path(self.temp.name))
        run = replace(RunState.new_review(self.manifest()), run_id="review-run")

        with self.assertRaises(ValueError):
            ReviewApprovalAttestation.create(
                run.manifest, self.receipt(run, manifest_digest="0" * 64), authority
            )

        run.approve_review(receipt=self.receipt(run), authority=authority)
        forged = run.to_dict()
        forged["approval_attestation"]["receipt"]["actor"] = "other"
        with self.assertRaises(ValueError):
            RunState.from_dict(forged, authority)

    def test_review_receipt_rejects_noncanonical_ids_and_blank_provenance(self):
        run = replace(RunState.new_review(self.manifest()), run_id="review-run")

        for changes in ({"run_id": "."}, {"run_id": ".."}, {"provider": " "}, {"actor": "\t"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.receipt(run, **changes)

    def test_review_deserialization_fails_closed_on_plan_shapes(self):
        run = RunState.new_review(self.manifest())
        forged_manifest = run.to_dict()
        forged_manifest["manifest"]["plan_path"] = "/tmp/plan.md"
        with self.assertRaises((TypeError, ValueError)):
            RunState.from_dict(forged_manifest)

        plan = RunState.new("plan", "docs/plan.md", "abc123").to_dict()
        plan["kind"] = "review"
        with self.assertRaises(ValueError):
            RunState.from_dict(plan)

    def test_direct_code_construction_remains_prohibited(self):
        with self.assertRaises(ValueError):
            RunState(kind="code", manifest=self.manifest())

    def test_plan_state_rejects_a_review_manifest(self):
        with self.assertRaises(ValueError):
            RunState(kind="plan", manifest=self.manifest())


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = {
            "version": 1,
            "max_rounds": 6,
            "max_context_tokens": 8000,
            "max_initial_sources": 3,
            "max_context_expansions": 2,
            "production_line_limit": 100,
            "production_growth_percent": 30,
            "production_excludes": ["docs/**"],
            "doc_max_initial_sources": 5,
            "doc_max_context_tokens": 16000,
            "codex_model": "gpt-5.6-sol",
        }

    def tearDown(self):
        self.temp.cleanup()

    def write_policy(self, contents):
        path = self.root / "defaults.yaml"
        path.write_text(yaml.safe_dump(contents), encoding="utf-8")
        return path

    def test_missing_max_rounds_is_rejected(self):
        policy = dict(self.policy)
        del policy["max_rounds"]

        with self.assertRaises(PolicyError):
            load_policy(self.write_policy(policy))

    def test_unknown_policy_version_is_rejected(self):
        policy = dict(self.policy)
        policy["version"] = 2

        with self.assertRaises(PolicyError):
            load_policy(self.write_policy(policy))

    def test_unknown_top_level_key_is_rejected(self):
        policy = dict(self.policy)
        policy["verification_commands"] = []

        with self.assertRaises(PolicyError):
            load_policy(self.write_policy(policy))

    def test_doc_limits_are_loaded_from_the_shipped_defaults(self):
        policy = load_policy(Path(__file__).parents[1] / "config" / "defaults.yaml")

        self.assertEqual(policy.doc_max_initial_sources, 5)
        self.assertEqual(policy.doc_max_context_tokens, 16000)

    def test_missing_doc_limits_are_rejected(self):
        for key in ("doc_max_initial_sources", "doc_max_context_tokens"):
            with self.subTest(key=key):
                policy = dict(self.policy)
                del policy[key]

                with self.assertRaises(PolicyError):
                    load_policy(self.write_policy(policy))

    def test_doc_sources_above_five_are_rejected(self):
        policy = dict(self.policy)
        policy["doc_max_initial_sources"] = 6

        with self.assertRaises(PolicyError):
            load_policy(self.write_policy(policy))

    def test_doc_context_tokens_above_the_ceiling_are_rejected(self):
        policy = dict(self.policy)
        policy["doc_max_context_tokens"] = 24001

        with self.assertRaises(PolicyError):
            load_policy(self.write_policy(policy))

    def test_codex_model_is_loaded_from_the_shipped_defaults(self):
        """The tool states its own model requirement instead of inheriting one.

        With no such key the Codex argv carried no ``-m``, so every run of
        every kind silently took whatever ``~/.codex/config.toml`` named --
        and one unrelated edit to that file broke all four kinds at once.
        """
        policy = load_policy(Path(__file__).parents[1] / "config" / "defaults.yaml")

        self.assertEqual(policy.codex_model, "gpt-5.6-sol")

    def test_missing_codex_model_is_rejected(self):
        """Required, not optional-with-default: a default is the defect itself."""
        policy = dict(self.policy)
        del policy["codex_model"]

        with self.assertRaises(PolicyError):
            load_policy(self.write_policy(policy))

    def test_codex_model_outside_the_argv_safe_allowlist_is_rejected(self):
        """The value becomes one argv element, so it is allowlisted, not trusted.

        It never reaches a shell, but a policy file is the wrong place to
        accept arbitrary text: this module's job is refusing an unvalidated
        limit, and a model name is no exception.
        """
        for value in (
            "", " ", "\t", "\n", "gpt 5.6 sol", "gpt-5.6-sol ",
            "gpt-5.6-sol; rm -rf /", "$(id)", "`id`", "gpt-5.6-sol|tee",
            "gpt-5.6-sol\n", "gpt/5.6-sol", "gpt:5.6", "gpt@5.6",
            5, 1.5, True, None, ["gpt-5.6-sol"], {"name": "gpt-5.6-sol"},
        ):
            with self.subTest(value=value):
                policy = dict(self.policy)
                policy["codex_model"] = value

                with self.assertRaises(PolicyError):
                    load_policy(self.write_policy(policy))

    def test_a_policy_cannot_be_built_at_all_without_a_codex_model(self):
        """Strictly required: no default exists that could stand in silently.

        A hand-built ``Policy`` is how test fixtures reach the workflows, so
        this is the boundary where a tolerated default would let the model go
        unstated -- and an unstated model is the defect being closed.
        """
        from ai_review.policy import Policy

        with self.assertRaises(TypeError):
            Policy(
                version=1, max_rounds=6, max_context_tokens=8000, max_initial_sources=3,
                max_context_expansions=2, production_line_limit=100,
                production_growth_percent=30, production_excludes=["docs/**"],
                doc_max_initial_sources=5, doc_max_context_tokens=16000,
            )

    def test_shared_context_budget_keeps_its_own_ceiling(self):
        policy = dict(self.policy)
        policy["max_context_tokens"] = 16000

        with self.assertRaises(PolicyError):
            load_policy(self.write_policy(policy))

    def test_context_limits_separate_doc_from_the_shared_kinds(self):
        policy = load_policy(self.write_policy(self.policy))

        for kind in ("plan", "code", "review"):
            with self.subTest(kind=kind):
                self.assertEqual(policy.context_limits(kind), (3, 8000))
        self.assertEqual(policy.context_limits("doc"), (5, 16000))

    def test_context_limits_refuse_a_kind_that_has_no_budget(self):
        """An unknown kind is a bug, not a request for the shared pair.

        Returning ``(3, 8000)`` for a misspelled or future kind is the silently
        wrong budget this lookup exists to prevent, so it raises instead.
        """
        policy = load_policy(self.write_policy(self.policy))

        for kind in ("plan_typo", "docs", "DOC", "", None):
            with self.subTest(kind=kind):
                with self.assertRaises(PolicyError):
                    policy.context_limits(kind)


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "runs"
        self.repo = Path(self.temp.name) / "example-repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "plan.md").write_text("# Plan\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "docs/plan.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-qm", "base"], check=True)
        self.authority = ApprovalAuthority.for_workspace(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_store_round_trip_is_atomic(self):
        store = RunStore(self.root)
        created = store.create(
            RunState.new(
                "plan",
                str(self.repo / "docs" / "plan.md"),
                str(self.repo),
                "abc123",
            )
        )

        loaded = store.load(created.run_id)

        self.assertEqual(loaded.to_dict(), created.to_dict())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_store_rejects_a_repository_subdirectory(self):
        store = RunStore(self.root)
        nested = self.repo / "docs"
        nested.mkdir(exist_ok=True)
        run = RunState.new("plan", str(nested / "plan.md"), str(nested), "main")

        with self.assertRaises(ValueError):
            store.create(run)

    def test_same_named_repos_use_path_derived_project_ids(self):
        first = project_id(Path("/private/tmp/team-a/app"))
        second = project_id(Path("/private/tmp/team-b/app"))

        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 12)
        self.assertRegex(first, r"^[0-9a-f]{12}$")

    def test_store_directory_uses_git_root_path_id(self):
        store = RunStore(self.root)
        run = store.create(
            RunState.new(
                "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "main"
            )
        )

        self.assertTrue((self.root / project_id(self.repo) / run.run_id / "state.json").exists())

    def test_store_load_validates_an_approved_code_run_attestation(self):
        plan = RunState.new(
            "plan",
            str(self.repo / "docs" / "plan.md"),
            str(self.repo),
            "HEAD",
            verification_commands=[
                {
                    "kind": "test",
                    "scope": "focused store tests",
                    "argv": ["python", "-m", "unittest", "tests.test_store"],
                }
            ],
        )
        plan.transition(Verdict.PASS)
        approve(plan, self.authority)
        code = RunState.new_code_from_approved_plan(plan, self.authority)
        stored = RunStore(self.root, authority=self.authority).create(code)

        loaded = RunStore(self.root, authority=self.authority).load(stored.run_id)

        self.assertEqual(loaded.to_dict(), stored.to_dict())

    def test_default_storage_uses_the_external_application_support_root(self):
        with patch.dict(os.environ, {}, clear=True):
            root = default_runs_root()
            self.assertNotEqual(root, PRODUCTION_WORKSPACE_ROOT / ".ai-review" / "runs")
            self.assertNotIn(str(PRODUCTION_WORKSPACE_ROOT), str(root))
            self.assertIn("Library/Application Support/ai-review/runs", str(root))

    def test_ai_review_home_does_not_redirect_transient_artifacts(self):
        target_repo = Path("/private/tmp/target-repo")
        with patch.dict(os.environ, {"AI_REVIEW_HOME": str(target_repo)}):
            self.assertEqual(default_runs_root(), DEFAULT_RUNS_ROOT)

    def test_non_worktree_override_has_no_test_capability_exception(self):
        workspace = Path(self.temp.name) / "isolated-worktree"
        workspace.mkdir()
        with patch.dict(os.environ, {"AI_REVIEW_HOME": str(workspace)}):
            with self.assertRaises(ValueError):
                resolve_workspace_root()
            self.assertEqual(default_runs_root(), DEFAULT_RUNS_ROOT)

    def test_store_save_rejects_forged_persisted_state_before_writing(self):
        plan = RunState.new(
            "plan",
            str(self.repo / "docs" / "plan.md"),
            str(self.repo),
            "HEAD",
            verification_commands=[
                {
                    "kind": "test",
                    "scope": "focused store tests",
                    "argv": ["python", "-m", "unittest", "tests.test_store"],
                }
            ],
        )
        plan.transition(Verdict.PASS)
        approve(plan, self.authority)
        code = RunState.new_code_from_approved_plan(plan, self.authority)
        store = RunStore(self.root, authority=self.authority)
        stored = store.create(code)
        forged = replace(
            stored,
            approval_attestation=replace(stored.approval_attestation, signature="0" * 64),
        )

        with self.assertRaises(ValueError):
            store.save(forged)

    def test_project_id_symlink_into_target_repo_is_rejected_before_write(self):
        project = self.root / project_id(self.repo)
        project.parent.mkdir(parents=True, exist_ok=True)
        project.symlink_to(self.repo, target_is_directory=True)
        store = RunStore(self.root, authority=self.authority)
        run = RunState.new("plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD")

        with self.assertRaises(ValueError):
            store.create(run)

        self.assertFalse((self.repo / "state.json").exists())

    def test_run_directory_swap_to_symlink_is_rejected_before_save(self):
        store = RunStore(self.root, authority=self.authority)
        created = store.create(RunState.new(
            "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD"
        ))
        directory = self.root / project_id(self.repo) / created.run_id
        (directory / "state.json").unlink()
        directory.rmdir()
        directory.symlink_to(self.repo, target_is_directory=True)

        with self.assertRaises(ValueError):
            store.save(created)

        self.assertFalse((self.repo / "state.json").exists())

    def test_artifact_write_retries_short_os_writes(self):
        store = RunStore(self.root, authority=self.authority)
        created = store.create(RunState.new(
            "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD"
        ))
        target = store.root / project_id(self.repo) / created.run_id / "evidence.bin"
        payload = b"durable artifact bytes" * 32
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, data[: max(1, len(data) // 2)])

        with patch("ai_review.store.os.write", side_effect=short_write):
            store.write_artifact_bytes(target, payload)

        self.assertEqual(store.read_artifact_bytes(target), payload)

    def test_artifact_read_retries_short_os_reads_until_eof(self):
        store = RunStore(self.root, authority=self.authority)
        created = store.create(RunState.new(
            "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD"
        ))
        target = store.root / project_id(self.repo) / created.run_id / "evidence.bin"
        payload = b"complete artifact evidence" * 32
        store.write_artifact_bytes(target, payload)
        real_read = os.read

        def short_read(fd, size):
            return real_read(fd, min(size, 7))

        with patch("ai_review.store.os.read", side_effect=short_read):
            self.assertEqual(store.read_artifact_bytes(target), payload)

    def test_descriptor_traversal_rejects_a_run_symlink_swapped_during_open(self):
        store = RunStore(self.root, authority=self.authority)
        created = store.create(RunState.new(
            "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD"
        ))
        directory = store.root / project_id(self.repo) / created.run_id
        displaced = directory.with_name(directory.name + "-displaced")
        real_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == created.run_id and kwargs.get("dir_fd") is not None and not swapped:
                directory.rename(displaced)
                directory.symlink_to(self.repo, target_is_directory=True)
                swapped = True
            return real_open(path, flags, *args, **kwargs)

        with patch("ai_review.store.os.open", side_effect=swap_before_open):
            with self.assertRaises(ValueError):
                store.save(created)

        self.assertTrue(swapped)
        self.assertFalse((self.repo / "state.json").exists())

    def test_create_rejects_traversal_or_absolute_run_ids_before_any_filesystem_change(self):
        outside = self.root.parent / "outside-artifact-ids"
        outside.mkdir()
        before = sorted(path.relative_to(self.root.parent) for path in self.root.parent.rglob("*"))
        store = RunStore(self.root, authority=self.authority)

        for run_id in ("../../escaped-run", str(outside / "absolute-run"), ".", "..", "", "nested\\run", "nul\x00run"):
            with self.subTest(run_id=run_id):
                run = replace(RunState.new(
                    "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD"
                ), run_id=run_id)
                with self.assertRaises(ValueError):
                    store.create(run)
                after = sorted(path.relative_to(self.root.parent) for path in self.root.parent.rglob("*"))
                self.assertEqual(after, before)

    def test_create_accepts_a_valid_explicit_run_id(self):
        store = RunStore(self.root, authority=self.authority)
        run = replace(RunState.new(
            "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD"
        ), run_id="safe-run-001")

        created = store.create(run)

        self.assertEqual(created.run_id, "safe-run-001")
        self.assertTrue(store.artifact_exists(
            store.root / project_id(self.repo) / created.run_id / "state.json"
        ))

    def test_failed_create_cleanup_leaves_no_descriptors_or_run_artifacts(self):
        store = RunStore(self.root, authority=self.authority)
        baseline = replace(RunState.new(
            "plan", str(self.repo / "docs" / "plan.md"), str(self.repo), "HEAD"
        ), run_id="baseline-run")
        store.create(baseline)
        before_paths = sorted(path.relative_to(store.root) for path in store.root.rglob("*"))
        before_fds = len(os.listdir("/dev/fd"))

        with patch.object(store, "_atomic_write", side_effect=OSError("injected write failure")):
            for index in range(32):
                run = replace(baseline, run_id="failed-run-%02d" % index)
                with self.assertRaises(OSError):
                    store.create(run)

        self.assertEqual(
            sorted(path.relative_to(store.root) for path in store.root.rglob("*")), before_paths
        )
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)


if __name__ == "__main__":
    unittest.main()
