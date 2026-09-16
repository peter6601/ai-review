"""Behavioural tests for the read-only ``doc`` run kind.

A doc run is one Codex pass over a feature document: no Claude repair, no
approval gate, no verification commands.  These tests exercise the invariants
that make those properties structural rather than conventional.
"""

import hashlib
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ai_review.models import (
    DOC_LENSES, ApprovalAttestation, ApprovalAuthority, DocManifest,
    HumanApprovalReceipt, ReviewManifest, RunManifest, RunState, Status, Verdict,
)
from ai_review.process_security import executable_identity
from ai_review.store import RunStore


class DocRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "docs").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.doc = self.repo / "docs" / "rd-spec.md"
        self.doc.write_text(
            "# 離線編輯\n\n使用者可以在離線時繼續編輯內容。\n", encoding="utf-8"
        )
        self.plan = self.repo / "docs" / "plan.md"
        self.plan.write_text("# plan\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(self.repo), "-c", "user.email=test@example.com",
            "-c", "user.name=Test", "commit", "-qm", "base",
        ], check=True)
        self.base_oid = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.authority = ApprovalAuthority(self.root / "approval.key")
        self.brief = "離線編輯的 RD spec，給 PM 與 QA 讀"
        self.identity = executable_identity(Path(sys.executable))
        self.values = {
            "kind": "doc",
            "repo_path": str(self.repo),
            "doc_path": str(self.doc),
            "base_ref": "HEAD",
            "base_oid": self.base_oid,
            "lens": "requirement",
            "lens_reason": "文件只描述使用者行為，沒有任何檔案路徑",
            "brief": self.brief,
            "brief_digest": hashlib.sha256(self.brief.encode("utf-8")).hexdigest(),
            "doc_digest": hashlib.sha256(self.doc.read_bytes()).hexdigest(),
            "knowledge_sources": [],
            "context_checksum": None,
            "review_executables": {"codex": self.identity, "claude": self.identity},
        }

    # ---- fixtures -------------------------------------------------------

    def manifest(self, **changes):
        return DocManifest(**dict(self.values, **changes))

    def doc_run(self, **changes):
        return RunState.new_doc(self.manifest(**changes))

    def plan_attestation(self):
        """A genuine, correctly signed Plan approval from another run."""
        manifest = RunManifest(
            kind="plan", repo_path=str(self.repo), plan_path=str(self.plan),
            base_ref="HEAD", base_oid=self.base_oid,
            plan_digest=hashlib.sha256(self.plan.read_bytes()).hexdigest(),
        )
        receipt = HumanApprovalReceipt(
            run_id="plan-run", plan_digest=manifest.plan_digest, base_oid=self.base_oid,
            approved_at="2026-09-15T00:00:00+00:00", provider="test:held-capability",
            actor="test-human",
        )
        return ApprovalAttestation.create(manifest, receipt, self.authority)

    def review_manifest(self):
        brief = "Review the completed retry fix."
        return ReviewManifest(
            kind="review", repo_path=str(self.repo), base_ref="HEAD",
            base_oid=self.base_oid, brief=brief,
            brief_digest=hashlib.sha256(brief.encode("utf-8")).hexdigest(),
            profile="generic", initial_patch_digest="a" * 64,
            verification_commands=[{
                "kind": "test", "scope": "tests.test_doc_manifest",
                "argv": [sys.executable, "-m", "unittest", "tests.test_doc_manifest"],
            }],
            knowledge_sources=(), context_checksum=None,
            review_executables={"codex": self.identity, "claude": self.identity},
        )

    # ---- manifest -------------------------------------------------------

    def test_doc_manifest_normalizes_and_round_trips(self):
        manifest = self.manifest(
            brief="  %s  " % self.brief,
            knowledge_sources=[str(self.repo / "docs" / "plan.md")],
            context_checksum="b" * 64,
        )

        self.assertEqual(manifest.brief, self.brief)
        self.assertEqual(manifest.repo_path, str(self.repo.resolve()))
        self.assertEqual(manifest.doc_path, str(self.doc.resolve()))
        self.assertEqual(manifest.knowledge_sources, (str(self.plan.resolve()),))
        self.assertEqual(DocManifest.from_dict(manifest.to_dict()), manifest)
        self.assertEqual(len(manifest.digest()), 64)

    def test_doc_manifest_binds_no_verification_commands_or_plan(self):
        payload = self.manifest().to_dict()

        self.assertNotIn("verification_commands", payload)
        self.assertNotIn("plan_path", payload)
        self.assertNotIn("plan_digest", payload)
        self.assertFalse(hasattr(self.manifest(), "verification_commands"))

    def test_doc_manifest_digest_changes_with_the_lens(self):
        self.assertNotEqual(
            self.manifest(lens="requirement").digest(),
            self.manifest(lens="implementation").digest(),
        )

    def test_doc_state_round_trips_through_to_dict(self):
        state = RunState.new_doc(self.manifest())

        self.assertEqual(state.status, Status.READY)
        restored = RunState.from_dict(state.to_dict())
        self.assertEqual(restored.manifest.lens, "requirement")
        self.assertEqual(restored.kind, "doc")
        self.assertEqual(restored.manifest, state.manifest)
        self.assertIsNone(restored.approval_attestation)
        self.assertEqual(restored.repair_round, 0)

    def test_doc_lens_must_be_one_of_three(self):
        with self.assertRaises(ValueError):
            self.manifest(lens="whatever")
        for lens in DOC_LENSES:
            with self.subTest(lens=lens):
                self.assertEqual(self.manifest(lens=lens).lens, lens)
        for lens in ("", "Requirement", " requirement ", None):
            with self.subTest(lens=lens):
                with self.assertRaises(ValueError):
                    self.manifest(lens=lens)

    def test_doc_manifest_requires_a_stated_lens_reason(self):
        self.assertEqual(len(self.manifest(lens_reason="a" * 500).lens_reason), 500)
        for reason in ("", "   ", "\n\t ", "字" * 200, None, 7):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    self.manifest(lens_reason=reason)

    def test_doc_manifest_rejects_empty_or_overlong_brief(self):
        empty = hashlib.sha256(b"").hexdigest()
        overlong = "é" * 1001
        for changes in (
            {"brief": "", "brief_digest": empty},
            {"brief": "   ", "brief_digest": empty},
            {"brief": overlong,
             "brief_digest": hashlib.sha256(overlong.encode("utf-8")).hexdigest()},
            {"brief": None, "brief_digest": empty},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.manifest(**changes)

    def test_doc_brief_digest_must_match_the_normalized_brief(self):
        with self.assertRaises(ValueError):
            self.manifest(brief_digest=hashlib.sha256(b"a different brief").hexdigest())
        with self.assertRaises(ValueError):
            self.manifest(brief_digest=self.values["brief_digest"].upper())
        padded = "  %s  " % self.brief
        with self.assertRaises(ValueError):
            self.manifest(
                brief=padded,
                brief_digest=hashlib.sha256(padded.encode("utf-8")).hexdigest(),
            )

    def test_doc_manifest_rejects_malformed_digests_and_base_oid(self):
        for changes in (
            {"doc_digest": "not-a-digest"},
            {"doc_digest": "A" * 64},
            {"doc_digest": "a" * 63},
            {"context_checksum": "C" * 64},
            {"base_oid": "d" * 39},
            {"base_oid": "HEAD"},
            {"base_ref": ""},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.manifest(**changes)

    def test_doc_manifest_binds_both_reviewer_identities_or_none(self):
        self.assertEqual(self.manifest(review_executables={}).review_executables, {})
        for identities in (
            {"codex": self.identity},
            {"claude": self.identity},
            {"codex": self.identity, "claude": self.identity, "other": self.identity},
        ):
            with self.subTest(identities=identities):
                with self.assertRaises(ValueError):
                    self.manifest(review_executables=identities)
        with self.assertRaises(ValueError):
            self.manifest(review_executables={
                "codex": dict(self.identity, sha256="0" * 64), "claude": self.identity,
            })

    def test_doc_manifest_rejects_another_kind(self):
        for kind in ("plan", "code", "review", "", None):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    self.manifest(kind=kind)

    def test_doc_manifest_from_dict_rejects_missing_or_extra_fields(self):
        payload = self.manifest().to_dict()
        for value in (
            dict(payload, unexpected="x"),
            {key: item for key, item in payload.items() if key != "lens_reason"},
            "not a manifest",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    DocManifest.from_dict(value)

    # ---- state ----------------------------------------------------------

    def test_doc_state_cannot_be_built_without_the_factory_token(self):
        manifest = self.manifest()
        with self.assertRaises(ValueError):
            RunState(kind="doc", manifest=manifest)
        with self.assertRaises(ValueError):
            RunState(kind="doc", manifest=manifest, _doc_factory_token=object())
        with self.assertRaises(ValueError):
            RunState(kind="doc", manifest=manifest, _review_factory_token=object())

    def test_doc_state_requires_a_doc_manifest(self):
        for manifest in (None, self.review_manifest()):
            with self.subTest(manifest=type(manifest).__name__):
                with self.assertRaises(ValueError):
                    RunState.new_doc(manifest)

    def test_doc_state_never_carries_an_approval_attestation(self):
        attestation = self.plan_attestation()

        with self.assertRaises(ValueError):
            replace(self.doc_run(), approval_attestation=attestation)

        smuggled = self.doc_run()
        object.__setattr__(smuggled, "approval_attestation", attestation)
        with self.assertRaises(ValueError):
            smuggled.validate(self.authority)

        self.assertIsNone(self.doc_run().human_approved_at)

    def test_persisted_doc_run_cannot_reintroduce_an_approval(self):
        payload = self.doc_run().to_dict()
        attested = dict(
            payload,
            approval_attestation=self.plan_attestation().to_dict(),
            human_approved_at="2026-09-15T00:00:00+00:00",
        )

        with self.assertRaises(ValueError):
            RunState.from_dict(attested, self.authority)

        # An attestation with no approval timestamp is the shape a silent drop
        # would swallow: on disk it can only be tampering or a bug, so it has
        # to be loud rather than quietly rehydrated as an unapproved run.
        with self.assertRaises(ValueError) as caught:
            RunState.from_dict(dict(attested, human_approved_at=None), self.authority)
        self.assertIn("doc", str(caught.exception))

    def test_doc_state_with_a_repair_round_is_rejected(self):
        """A doc run has no repair loop, so any repair round is a forgery."""
        for repair_round in (1, 7):
            with self.subTest(repair_round=repair_round):
                state = replace(self.doc_run(), repair_round=repair_round)
                with self.assertRaises(ValueError):
                    state.validate(self.authority)
                with self.assertRaises(ValueError):
                    RunState.from_dict(state.to_dict(), self.authority)

    def test_clean_doc_state_validates_without_an_authority(self):
        self.doc_run().validate(None)

    def test_complete_doc_review_moves_ready_or_running_to_human_review(self):
        for status in (Status.READY, Status.RUNNING):
            with self.subTest(status=status):
                state = replace(
                    self.doc_run(), status=status,
                    updated_at="2000-01-01T00:00:00+00:00",
                )
                state.complete_doc_review()
                self.assertEqual(state.status, Status.AWAITING_HUMAN_DOC_REVIEW)
                self.assertNotEqual(state.updated_at, "2000-01-01T00:00:00+00:00")
                self.assertEqual(state.repair_round, 0)
                state.validate(self.authority)

    def test_complete_doc_review_is_refused_twice_and_for_other_kinds(self):
        completed = self.doc_run()
        completed.complete_doc_review()
        with self.assertRaises(ValueError):
            completed.complete_doc_review()

        plan = RunState.new("plan", str(self.plan), str(self.repo), "HEAD")
        review = RunState.new_review(self.review_manifest())
        for state in (plan, review):
            with self.subTest(kind=state.kind):
                with self.assertRaises(ValueError):
                    state.complete_doc_review()

    def test_completed_doc_run_cannot_transition(self):
        state = self.doc_run()
        state.complete_doc_review()

        for verdict in Verdict:
            with self.subTest(verdict=verdict):
                with self.assertRaises(ValueError):
                    state.transition(verdict)
        self.assertEqual(state.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(state.repair_round, 0)

    def test_doc_run_never_transitions_on_a_verdict(self):
        """A doc run has no repair loop, so no verdict may drive its status."""
        for status in (Status.READY, Status.RUNNING):
            for verdict in Verdict:
                with self.subTest(status=status, verdict=verdict):
                    state = replace(
                        self.doc_run(), status=status,
                        updated_at="2000-01-01T00:00:00+00:00",
                    )
                    with self.assertRaises(ValueError) as caught:
                        state.transition(verdict)
                    self.assertIn("complete_doc_review", str(caught.exception))
                    self.assertEqual(state.status, status)
                    self.assertEqual(state.repair_round, 0)
                    self.assertEqual(state.updated_at, "2000-01-01T00:00:00+00:00")

    def test_other_kinds_still_transition_on_a_verdict(self):
        plan = RunState.new("plan", str(self.plan), str(self.repo), "HEAD")
        plan.transition(Verdict.PASS)
        self.assertEqual(plan.status, Status.AWAITING_HUMAN_PLAN_REVIEW)

        review = replace(RunState.new_review(self.review_manifest()), status=Status.RUNNING)
        review.transition(Verdict.CHANGES_REQUIRED)
        self.assertEqual(review.status, Status.RUNNING)
        self.assertEqual(review.repair_round, 1)
        review.transition(Verdict.PASS)
        self.assertEqual(review.status, Status.AWAITING_HUMAN_CODE_REVIEW)

    def test_repair_round_stays_zero_through_a_doc_run(self):
        state = self.doc_run()
        self.assertEqual(state.repair_round, 0)
        state.complete_doc_review()
        self.assertEqual(state.repair_round, 0)
        restored = RunState.from_dict(state.to_dict(), self.authority)
        self.assertEqual(restored.repair_round, 0)
        self.assertEqual(restored.status, Status.AWAITING_HUMAN_DOC_REVIEW)

    # ---- rebinding one document for a second round ----------------------

    def edited_document_digest(self):
        """Edit the document on disk and hash the new bytes independently."""
        self.doc.write_text(
            "# 離線編輯\n\n使用者可以在離線時繼續編輯內容。\n\n"
            "## 衝突\n兩邊都改過時以最後存檔為準。\n",
            encoding="utf-8",
        )
        return hashlib.sha256(self.doc.read_bytes()).hexdigest()

    def test_rebind_document_moves_the_document_bytes_and_nothing_else(self):
        state = self.doc_run(
            knowledge_sources=[str(self.plan)], context_checksum="b" * 64,
        )
        state.complete_doc_review()
        object.__setattr__(state, "updated_at", "2000-01-01T00:00:00+00:00")
        before = state.manifest
        new_digest = self.edited_document_digest()
        self.assertNotEqual(new_digest, before.doc_digest)

        state.rebind_document(new_digest)

        self.assertEqual(state.status, Status.RUNNING)
        self.assertEqual(state.repair_round, 0)
        self.assertNotEqual(state.updated_at, "2000-01-01T00:00:00+00:00")
        self.assertEqual(state.manifest.doc_digest, new_digest)
        # The whole manifest, field by field: only the document's bytes move.
        self.assertEqual(
            state.manifest.to_dict(), dict(before.to_dict(), doc_digest=new_digest)
        )
        for name in (
            "lens", "lens_reason", "brief", "brief_digest", "base_ref", "base_oid",
            "knowledge_sources", "context_checksum", "repo_path", "doc_path",
            "review_executables",
        ):
            with self.subTest(field=name):
                self.assertEqual(getattr(state.manifest, name), getattr(before, name))
        state.validate(self.authority)

        # The second round still ends the only way a doc run ends.
        state.complete_doc_review()
        self.assertEqual(state.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(state.repair_round, 0)

    def test_rebind_document_is_refused_unless_the_run_awaits_its_reader(self):
        for status in (
            Status.READY, Status.RUNNING, Status.PAUSED,
            Status.AWAITING_USER_INPUT, Status.INTERRUPTED,
        ):
            with self.subTest(status=status):
                state = replace(
                    self.doc_run(), status=status,
                    updated_at="2000-01-01T00:00:00+00:00",
                )
                with self.assertRaises(ValueError):
                    state.rebind_document("c" * 64)
                self.assertEqual(state.status, status)
                self.assertEqual(state.manifest.doc_digest, self.values["doc_digest"])
                self.assertEqual(state.updated_at, "2000-01-01T00:00:00+00:00")

    def test_rebind_document_is_refused_for_every_other_run_kind(self):
        """Even parked at the doc gate, a Plan or Review run has no document."""
        plan = replace(
            RunState.new("plan", str(self.plan), str(self.repo), "HEAD"),
            status=Status.AWAITING_HUMAN_DOC_REVIEW,
        )
        review = replace(
            RunState.new_review(self.review_manifest()),
            status=Status.AWAITING_HUMAN_DOC_REVIEW,
        )
        for state in (plan, review):
            with self.subTest(kind=state.kind):
                manifest = state.manifest
                with self.assertRaises(ValueError):
                    state.rebind_document("c" * 64)
                self.assertIs(state.manifest, manifest)
                self.assertEqual(state.status, Status.AWAITING_HUMAN_DOC_REVIEW)

    def test_rebind_document_rejects_a_malformed_digest_without_touching_state(self):
        state = self.doc_run()
        state.complete_doc_review()
        object.__setattr__(state, "updated_at", "2000-01-01T00:00:00+00:00")
        manifest = state.manifest

        for value in ("c" * 63, "C" * 64, "not-a-digest", "", None):
            with self.subTest(digest=value):
                with self.assertRaises(ValueError):
                    state.rebind_document(value)
                self.assertIs(state.manifest, manifest)
                self.assertEqual(state.status, Status.AWAITING_HUMAN_DOC_REVIEW)
                self.assertEqual(state.updated_at, "2000-01-01T00:00:00+00:00")

    def test_rebound_doc_run_persists_and_reloads_with_the_new_digest(self):
        store = RunStore(self.root / "runs", authority=self.authority)
        created = store.create(self.doc_run())
        created.complete_doc_review()
        store.save(created)
        new_digest = self.edited_document_digest()

        created.rebind_document(new_digest)
        store.save(created)

        loaded = store.load(created.run_id)
        self.assertEqual(loaded.status, Status.RUNNING)
        self.assertEqual(loaded.repair_round, 0)
        self.assertEqual(loaded.manifest.doc_digest, new_digest)
        self.assertIsNone(loaded.approval_attestation)

    def test_doc_run_persists_and_reloads_through_the_run_store(self):
        store = RunStore(self.root / "runs", authority=self.authority)

        created = store.create(self.doc_run())
        created.complete_doc_review()
        store.save(created)

        loaded = store.load(created.run_id)
        self.assertEqual(loaded.kind, "doc")
        self.assertEqual(loaded.status, Status.AWAITING_HUMAN_DOC_REVIEW)
        self.assertEqual(loaded.manifest, created.manifest)
        self.assertEqual(loaded.manifest.lens_reason, self.values["lens_reason"])
        self.assertIsNone(loaded.approval_attestation)


if __name__ == "__main__":
    unittest.main()
