import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from ai_review.models import RunState, Status, canonical_answer_submission
from ai_review.store import RunStore
from ai_review.summary import generate_outputs


def review(identifier, *, invariant="Code is safe", raw_dialogue="review result"):
    return {
        "verdict": "CHANGES_REQUIRED",
        "summary": raw_dialogue,
        "findings": [{
            "id": identifier,
            "severity": "blocker",
            "invariant": invariant,
            "location": "Feature.swift:1",
            "evidence": "structured evidence",
            "required_outcome": "fix",
            "lineage": {"resolution": "existing"},
        }],
        "questions": [],
        "context_requests": [],
    }


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.plan = self.repo / "plan.md"
        self.plan.write_text("# Plan\n", encoding="utf-8")
        self.store = RunStore(self.root / "runs")
        self.run = self.store.create(RunState.new("plan", str(self.plan), str(self.repo), "HEAD"))
        self.artifacts = self.store._run_directory(self.run)

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, relative, value):
        self.store._atomic_write(self.artifacts / relative, value)

    def write_raw_json(self, relative, contents):
        self.store.write_artifact_bytes(self.artifacts / relative, contents.encode("utf-8"))

    def output_text(self, path):
        return self.store.read_artifact_bytes(path).decode("utf-8")

    def set_state(self, *, status=Status.AWAITING_HUMAN_PLAN_REVIEW, repair_round=0):
        object.__setattr__(self.run, "status", status)
        object.__setattr__(self.run, "repair_round", repair_round)
        self.store.save(self.run)

    def test_two_round_pass_does_not_create_candidate(self):
        self.set_state(repair_round=2)

        result = generate_outputs(self.store, self.run.run_id)

        self.assertIsNone(result.knowledge_candidate)
        text = self.output_text(result.final_summary)
        self.assertIn("# Consensus Run Summary", text)

    def test_three_round_run_creates_candidate_without_raw_dialogue(self):
        self.set_state(repair_round=3)
        self.write_json("reviews/0001.json", review("F-1", raw_dialogue="private chain"))

        result = generate_outputs(self.store, self.run.run_id)

        text = self.output_text(result.knowledge_candidate)
        self.assertIn("為什麼需要這麼多輪", text)
        self.assertNotIn("private chain", text)

    def test_pause_summary_contains_only_actionable_decisions(self):
        self.set_state(status=Status.PAUSED)
        self.write_json("unresolved-findings.json", {"findings": [review("F-7")["findings"][0]]})
        self.write_json("pause.json", {"reason": "CLAUDE_NEEDS_USER_INPUT", "detail": "private detail"})

        result = generate_outputs(self.store, self.run.run_id)

        text = self.output_text(result.final_summary)
        self.assertIn("F-7", text)
        self.assertIn("需要你決定", text)
        self.assertNotIn("private detail", text)

    def test_only_the_six_approved_conditions_trigger_candidates(self):
        cases = (
            ("repeated invariant", lambda: [self.write_json("reviews/0001.json", review("F-1")), self.write_json("reviews/0002.json", review("F-2"))]),
            ("scope expansion", lambda: self.write_json("pause.json", {"reason": "SCOPE_EXPANSION", "detail": "ignore"})),
            ("human arbitration", lambda: [self.write_json("user-questions.json", user_questions("Q-001")), self.write_json("answer-submission.json", answer_submission({"Q-001": "yes"}))]),
            ("testing lesson", lambda: [self.write_json("verification/0001-test.json", verification("test", 1)), self.write_json("verification/0002-test.json", verification("test", 0))]),
            ("all six rounds", lambda: self.set_state(repair_round=6)),
        )
        for label, arrange in cases:
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                arrange()
                result = generate_outputs(self.store, self.run.run_id)
                self.assertIsNotNone(result.knowledge_candidate)

    def test_malformed_evidence_fails_closed_with_an_actionable_summary(self):
        self.set_state(repair_round=3)
        self.write_json("reviews/0001.json", {"untrusted": "shape"})

        result = generate_outputs(self.store, self.run.run_id)

        self.assertIsNone(result.knowledge_candidate)
        text = self.output_text(result.final_summary)
        self.assertIn("無法安全產生完整摘要", text)
        self.assertIn("reviews/0001.json", text)

    def test_evidence_index_uses_bounded_identifiers_not_artifact_paths_or_raw_logs(self):
        self.set_state(repair_round=3)
        self.write_json("reviews/0001.json", review("F-1", raw_dialogue="secret transcript"))
        self.write_json("verification/0001-test.json", verification("test", 0))

        result = generate_outputs(self.store, self.run.run_id)

        text = self.output_text(result.knowledge_candidate)
        self.assertIn("reviews/0001.json", text)
        self.assertIn("verification/0001-test.json", text)
        self.assertNotIn(str(self.artifacts), text)
        self.assertNotIn("secret transcript", text)

    def test_candidate_redacts_secret_like_structured_finding_values(self):
        self.set_state(repair_round=3)
        secret = "sk-1234567890abcdefghijklmnop"
        self.write_json("reviews/0001.json", review(secret))

        result = generate_outputs(self.store, self.run.run_id)

        text = self.output_text(result.knowledge_candidate)
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)

    def test_evidence_index_caps_hundreds_of_artifacts_in_stable_order(self):
        self.set_state(repair_round=3)
        for index in range(120):
            self.write_json("reviews/%04d.json" % index, review("F-%03d" % index))

        first = generate_outputs(self.store, self.run.run_id)
        first_text = self.output_text(first.knowledge_candidate)
        second = generate_outputs(self.store, self.run.run_id)
        second_text = self.output_text(second.knowledge_candidate)

        evidence = first_text.split("## 證據索引\n", 1)[1]
        entries = [line for line in evidence.splitlines() if line.startswith("- ")]
        self.assertEqual(first_text, second_text)
        self.assertLessEqual(len(entries), 50)
        self.assertIn("reviews/0000.json", evidence)
        self.assertIn("reviews/0009.json", evidence)
        self.assertNotIn("reviews/0010.json", evidence)
        self.assertIn("reviews: 110 artifact(s) omitted", evidence)

    def test_forged_answer_submission_digest_fails_closed(self):
        self.set_state(repair_round=3)
        self.write_json("user-questions.json", user_questions("Q-001"))
        forged = answer_submission({"Q-001": "yes"})
        forged["digest"] = "a" * 64
        self.write_json("answer-submission.json", forged)

        result = generate_outputs(self.store, self.run.run_id)

        self.assertIsNone(result.knowledge_candidate)
        self.assertIn("answer-submission.json", self.output_text(result.final_summary))

    def test_failed_generation_removes_a_stale_knowledge_candidate(self):
        self.set_state(repair_round=3)
        first = generate_outputs(self.store, self.run.run_id)
        self.assertIsNotNone(first.knowledge_candidate)
        self.write_json("user-questions.json", user_questions("Q-001"))
        forged = answer_submission({"Q-001": "yes"})
        forged["digest"] = "a" * 64
        self.write_json("answer-submission.json", forged)

        second = generate_outputs(self.store, self.run.run_id)

        self.assertIsNone(second.knowledge_candidate)
        self.assertFalse((self.artifacts / "knowledge-candidate.md").exists())

    def test_human_arbitration_binds_answers_to_valid_persisted_question_ids(self):
        cases = (
            ("forged id", user_questions("Q-forged"), {"Q-forged": "yes"}, "user-questions.json"),
            ("missing questions", None, {"Q-001": "yes"}, "user-questions.json"),
            ("set mismatch", user_questions("Q-001", "Q-002"), {"Q-001": "yes"}, "answer-submission.json"),
            ("duplicate ids", {"questions": [{"id": "Q-001", "question": "private"}, {"id": "Q-001", "question": "private again"}]}, {"Q-001": "yes"}, "user-questions.json"),
        )
        for label, questions, answers, identifier in cases:
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                self.set_state(repair_round=3)
                if questions is not None:
                    self.write_json("user-questions.json", questions)
                self.write_json("answer-submission.json", answer_submission(answers))

                result = generate_outputs(self.store, self.run.run_id)

                final = self.output_text(result.final_summary)
                self.assertIsNone(result.knowledge_candidate)
                self.assertIn(identifier, final)
                self.assertNotIn("private", final)

    def test_large_multibyte_values_keep_all_required_markdown_sections(self):
        self.set_state(status=Status.PAUSED, repair_round=3)
        huge = "界" * 10_000
        self.write_json("reviews/0001.json", review(huge))
        self.write_json("unresolved-findings.json", {"findings": [review(huge)["findings"][0]]})
        self.write_json("pause.json", {"reason": "CLAUDE_NEEDS_USER_INPUT", "detail": "private"})

        result = generate_outputs(self.store, self.run.run_id)

        final = self.output_text(result.final_summary)
        candidate = self.output_text(result.knowledge_candidate)
        for text, headings in (
            (final, ("# Consensus Run Summary", "## 目前狀態", "## 已解決項目", "## 未解決 Blockers", "## Claude 與 Codex 的分歧", "## Verification 與 Diff", "## 需要你決定")),
            (candidate, ("# Knowledge Candidate", "## 任務與結果", "## 輪數與關鍵轉折", "## 為什麼需要這麼多輪", "## 被否證的假設與無效修正", "## 最終 Root Cause", "## 建立或修正的不變量", "## 可以提前執行的檢查", "## 下次開工 Checklist", "## 證據索引")),
        ):
            with self.subTest(title=headings[0]):
                self.assertLessEqual(len(text.encode("utf-8")), 16 * 1024)
                self.assertEqual(text.encode("utf-8").decode("utf-8"), text)
                self.assertIn("[truncated]", text)
                for heading in headings:
                    self.assertIn(heading, text)

    def test_duplicate_json_keys_fail_closed_at_every_summary_evidence_depth(self):
        digest = canonical_answer_submission({"Q-001": "last"})[1]
        review_raw = json.dumps(review("F-1")).replace('"id": "F-1"', '"id":"F-1","id":"F-last"')
        resolution_raw = json.dumps(resolution("F-1")).replace('"finding_id": "F-1"', '"finding_id":"F-1","finding_id":"F-injected"')
        cases = (
            ("duplicate answer", [("user-questions.json", user_questions("Q-001"))], "answer-submission.json", '{"answers":{"Q-001":"first","Q-001":"last"},"digest":"%s"}' % digest),
            ("duplicate question field", [], "user-questions.json", '{"questions":[{"id":"Q-001","id":"Q-002","question":"private"}]}'),
            ("duplicate review field", [], "reviews/0001.json", review_raw),
            ("duplicate resolution field", [("reviews/0001.json", review("F-1"))], "resolutions/0001.json", resolution_raw),
        )
        for label, setup, relative, raw in cases:
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                self.set_state(repair_round=3)
                for setup_path, value in setup:
                    self.write_json(setup_path, value)
                self.write_raw_json(relative, raw)
                if label == "duplicate question field":
                    self.write_json("answer-submission.json", answer_submission({"Q-002": "yes"}))

                result = generate_outputs(self.store, self.run.run_id)

                final = self.output_text(result.final_summary)
                self.assertIsNone(result.knowledge_candidate)
                self.assertIn(relative, final)
                self.assertNotIn("F-injected", final)
                self.assertNotIn("private", final)

    def test_resolution_must_match_its_review_findings_and_sequence(self):
        cases = (
            ("extra finding", [("reviews/0001.json", review("F-1")), ("resolutions/0001.json", resolution("F-1", "F-injected"))], "resolutions/0001.json"),
            ("orphan", [("resolutions/0001.json", resolution("F-1"))], "resolutions/0001.json"),
            ("mismatched sequence", [("reviews/0001.json", review("F-1")), ("resolutions/0002.json", resolution("F-1"))], "resolutions/0002.json"),
            ("repair mismatched sequence", [("reviews/0001.json", review("F-1")), ("claude-actions/repair-0002.json", resolution("F-1"))], "claude-actions/repair-0002.json"),
        )
        for label, artifacts, identifier in cases:
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                self.set_state(repair_round=3)
                for relative, value in artifacts:
                    self.write_json(relative, value)

                result = generate_outputs(self.store, self.run.run_id)

                self.assertIsNone(result.knowledge_candidate)
                self.assertIn(identifier, self.output_text(result.final_summary))

    def test_resolution_missing_a_review_finding_fails_closed(self):
        self.set_state(repair_round=3)
        reviewed = review("F-1")
        reviewed["findings"].append(review("F-2")["findings"][0])
        self.write_json("reviews/0001.json", reviewed)
        self.write_json("resolutions/0001.json", resolution("F-1"))

        result = generate_outputs(self.store, self.run.run_id)

        self.assertIsNone(result.knowledge_candidate)
        self.assertIn("resolutions/0001.json", self.output_text(result.final_summary))


def verification(name, exit_code):
    return {
        "name": name,
        "argv": ["python3", "-m", "unittest"],
        "exit_code": exit_code,
        "duration_seconds": 1.0,
        "stdout_path": "/private/log.stdout",
        "stderr_path": "/private/log.stderr",
        "relevant_output": "full raw log must not be summarized",
    }


def answer_submission(answers):
    normalized, digest = canonical_answer_submission(answers)
    return {"answers": normalized, "digest": digest}


def resolution(*identifiers):
    return {
        "summary": "repair result",
        "resolutions": [
            {"finding_id": identifier, "outcome": "fixed", "evidence": "fixed with bounded evidence"}
            for identifier in identifiers
        ],
    }


def user_questions(*identifiers):
    return {"questions": [{"id": identifier, "question": "private question"} for identifier in identifiers]}


class ReviewSummaryTests(unittest.TestCase):
    """A Review summary is built from its own evidence, never from Plan bytes."""

    def setUp(self):
        import hashlib
        import sys

        from ai_review.git_diff import capture_diff_bytes
        from ai_review.models import (
            ApprovalAuthority, ReviewApprovalReceipt, ReviewManifest,
        )
        from ai_review.process_security import resolve_executable

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for name, value in (("user.email", "test@example.com"), ("user.name", "Test")):
            subprocess.run(["git", "-C", str(self.repo), "config", name, value], check=True)
        (self.repo / "Feature.swift").write_text("struct Feature {}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "Feature.swift"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "base"],
            check=True, capture_output=True,
        )
        self.base = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        (self.repo / "Feature.swift").write_text(
            "struct Feature {\n    let value = 1\n}\n", encoding="utf-8"
        )
        self.brief = "Review the completed retry fix."
        patch, _lines = capture_diff_bytes(self.repo, self.base)
        identity = resolve_executable(sys.executable)
        self.authority = ApprovalAuthority(self.root / "approval.key")
        self.store = RunStore(self.root / "runs", authority=self.authority)
        manifest = ReviewManifest(
            kind="review", repo_path=str(self.repo), base_ref="main", base_oid=self.base,
            brief=self.brief,
            brief_digest=hashlib.sha256(self.brief.encode("utf-8")).hexdigest(),
            profile="ios", initial_patch_digest=hashlib.sha256(patch).hexdigest(),
            verification_commands=[{
                "kind": "test", "scope": "tests.test_retry",
                "argv": [sys.executable, "-m", "unittest", "tests.test_retry"],
            }],
            review_executables={"codex": identity, "claude": identity},
        )
        self.run = self.store.create(RunState.new_review(manifest))
        self.run.approve_review(
            receipt=ReviewApprovalReceipt(
                run_id=self.run.run_id, manifest_digest=manifest.digest(),
                approved_at="2026-08-04T00:00:00+00:00",
                provider="test:held-capability", actor="test-human",
            ),
            authority=self.authority,
        )
        self.store.save(self.run)
        self.artifacts = self.store._run_directory(self.run)

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, relative, value):
        self.store._atomic_write(self.artifacts / relative, value)

    def output_text(self, path):
        return self.store.read_artifact_bytes(path).decode("utf-8")

    def set_state(self, *, status=Status.AWAITING_HUMAN_CODE_REVIEW, repair_round=0):
        object.__setattr__(self.run, "status", status)
        object.__setattr__(self.run, "repair_round", repair_round)
        self.store.save(self.run)

    def test_review_summary_names_its_own_scope_and_reads_no_plan(self):
        self.set_state(repair_round=2)
        self.write_json("reviews/0001.json", review("CODE-001"))
        self.write_json("verification/0001-test.json", verification("test", 0))
        self.write_json(
            "patch-stats/round-0000.json",
            {"round": 0, "patch_path": "patches/round-0000.patch", "production_added_lines": 3},
        )
        dependency_evidence = {
            "category": "dependencies", "path": "Package.resolved",
            "reason": "changed path matches the dependencies rule",
        }
        self.write_json("risk-assessments/round-0001.json", {
            "round": 1, "patch_digest": "a" * 64, "policy_version": "review-risk-v1",
            "categories": ["dependencies"],
            "evidence": [dependency_evidence],
            "introduced_categories": ["dependencies"],
            "introduced_evidence": [dependency_evidence],
            "path_fingerprints": {"Package.resolved": "b" * 64},
        })
        self.write_json("preflight.json", {
            "profile": "ios", "patch_digest": "a" * 64,
            "specialists": [
                {"name": name, "findings": []}
                for name in ("resilience-auditor", "swiftui-reviewer", "ux-critique")
            ],
        })

        with unittest.mock.patch.object(
            type(self.run), "plan_path", new_callable=unittest.mock.PropertyMock
        ) as plan_path:
            result = generate_outputs(self.store, self.run.run_id)
        text = self.output_text(result.final_summary)

        plan_path.assert_not_called()
        self.assertIn("# Consensus Run Summary", text)
        self.assertIn("review", text)
        self.assertIn("ios", text)
        self.assertIn(self.base[:12], text)
        self.assertIn("dependencies", text)
        self.assertIn("修復輪數：2", text)
        self.assertIn("Verification", text)
        self.assertNotIn("# Plan", text)

    def test_a_high_risk_pause_creates_a_bounded_knowledge_candidate(self):
        self.set_state(status=Status.PAUSED)
        self.write_json(
            "pause.json", {"reason": "HIGH_RISK_CHANGE", "detail": "approve-risk required"}
        )

        result = generate_outputs(self.store, self.run.run_id)

        self.assertIsNotNone(result.knowledge_candidate)
        self.assertIn("SCOPE_EXPANSION", result.candidate_triggers)
        text = self.output_text(result.knowledge_candidate)
        self.assertIn("# Knowledge Candidate", text)
        self.assertNotIn(self.brief, text)

    def test_the_summary_reports_only_risk_introduced_by_a_repair(self):
        """Baseline-inherited risk is recorded but must not be shown as a gate."""
        self.set_state(repair_round=1)
        inherited = {
            "category": "dependencies", "path": "Package.resolved",
            "reason": "changed path matches the dependencies rule",
        }
        for round_number in (0, 1):
            self.write_json("risk-assessments/round-%04d.json" % round_number, {
                "round": round_number, "patch_digest": "a" * 64,
                "policy_version": "review-risk-v1",
                "categories": ["dependencies"], "evidence": [inherited],
                "introduced_categories": [], "introduced_evidence": [],
                # Identical fingerprint across rounds: the human's baseline file
                # was never re-edited by a repair.
                "path_fingerprints": {"Package.resolved": "c" * 64},
            })

        result = generate_outputs(self.store, self.run.run_id)
        text = self.output_text(result.final_summary)

        self.assertIn("沒有偵測到高風險變更類別", text)
        self.assertNotIn("高風險類別：dependencies", text)

    def test_a_clean_generic_review_creates_no_candidate(self):
        self.set_state(repair_round=1)
        self.write_json("verification/0001-test.json", verification("test", 0))

        result = generate_outputs(self.store, self.run.run_id)

        self.assertIsNone(result.knowledge_candidate)
        self.assertEqual(result.candidate_triggers, ())


if __name__ == "__main__":
    unittest.main()
