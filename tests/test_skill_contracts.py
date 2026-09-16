import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def skill(name):
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    return frontmatter, text


class SkillContractTests(unittest.TestCase):
    def test_consensus_review_contract(self):
        _, text = skill("consensus-review")

        for phrase in (
            "ai-review init review",
            "ai-review approve-review",
            "Codex-first",
            "sixth repair",
            "approve-code",
            "submit-preflight",
            "read-only",
            "8,000",
        ):
            self.assertIn(phrase, text)

    def test_agent_auto_approval_is_documented_with_its_limit_and_its_cost(self):
        """`--auto` weakens a human gate, so the skill must say exactly how much."""
        _, text = skill("consensus-review")

        for phrase in (
            "--auto",
            "five auto-approvals in any 60-second sliding window",
            "agent:auto-approval",
            "Never route around the limit by creating a\nsecond run.",
        ):
            self.assertIn(phrase, text)
        # The loop may run unattended; its exit may not.
        self.assertIn("`approve-code` has no `--auto` at all", text)
        self.assertIn(
            "Only the human runs the final gate, and only after they have read "
            "the diff.", text,
        )
        self.assertNotIn("approve-code --auto", text)

    def test_the_answers_file_is_keyed_by_question_id_not_by_question_text(self):
        """Defect: a `review` run hits `NEEDS_USER_INPUT` exactly like a `doc` one.

        `PlanWorkflow._validate_answers`, which `DirectReviewWorkflow` inherits,
        compares `set(answers)` against the persisted question ids, so a mapping
        of question *text* to answer can never match -- and the rejection used to pause the run, discarding a
        separately billed Codex round.  This skill said only "write one answers
        JSON object", which is the wording that destroyed a real run.
        `consensus-plan` was corrected first; this is the same contract, and
        deliberately in the same words, so the two cannot drift apart.
        """
        _, text = skill("consensus-review")
        _, plan = skill("consensus-plan")

        shared = (
            "The questions artifact (`questions_path`, reported by `status`) holds a list of\n"
            "`{\"id\": ..., \"question\": ...}` objects. Read it before writing anything. The\n"
            "answers file is a flat JSON object **keyed by the question `id`** \u2014 `Q-001`,\n"
            "`Q-002`, \u2026 \u2014 never by the question text, which is refused every time. "
            "Each value\nis that question's answer as one non-empty string, in the user's "
            "own words: one\nkey per persisted question, no extras and none left out."
        )
        self.assertIn(shared, text)
        # One contract, one phrasing, in both skills that can be asked questions.
        self.assertIn(shared, plan)
        # A concrete two-id example, so the shape is copyable rather than inferred.
        self.assertIn('"Q-001"', text)
        self.assertIn('"Q-002"', text)
        # Gone, not merely de-emphasised: the wording that produced a rejected file.
        self.assertNotIn("Write one answers JSON object", text)
        # The half that was already right: the questions reach the user untouched.
        self.assertIn("verbatim", text)
        self.assertIn("Never answer\nfor the user", text)

    def test_the_six_repair_ceiling_cannot_be_reset_by_a_fresh_run(self):
        """Regression: the generic PAUSED restart advice also covered MAX_REPAIR_ROUNDS."""
        _, text = skill("consensus-review")

        self.assertIn("MAX_REPAIR_ROUNDS", text)
        self.assertIn("Never create a new Review run to keep repairing", text)
        self.assertIn("resets the counter", text)
        # The restart path must be scoped to a broken precondition only.
        restart = text.split("PAUSED` for any other reason", 1)
        self.assertEqual(len(restart), 2, "restart guidance section is missing")
        self.assertIn("never to buy more repair rounds", restart[1])


class CanonicalSkillFrontmatterTests(unittest.TestCase):
    """The installer ships one canonical SKILL.md to both Claude and Codex.

    Codex's authoring contract is the stricter of the two, so every installed
    skill must satisfy it; anything Claude-specific belongs under `metadata`.
    """

    CODEX_ALLOWED = {"name", "description", "license", "allowed-tools", "metadata"}

    def test_every_installed_skill_uses_codex_compatible_frontmatter(self):
        import sys

        sys.path.insert(0, str(ROOT))
        from install_skills import SKILL_NAMES

        for name in SKILL_NAMES:
            with self.subTest(skill=name):
                frontmatter, _text = skill(name)
                unexpected = set(frontmatter) - self.CODEX_ALLOWED
                self.assertEqual(unexpected, set(), "%s: %s" % (name, sorted(unexpected)))
                self.assertIn("name", frontmatter)
                self.assertIn("description", frontmatter)
                self.assertEqual(frontmatter["name"], name)


class ConsensusPlanSkillContractTests(unittest.TestCase):
    """`/consensus-plan` drives read-only document review, not Plan review.

    Measured from the run store: of 56 `plan` runs only two ever reached the
    human gate, twelve of the last seventeen died at a lineage gate that exists
    only because Claude edits the Plan between rounds, and `consensus-code` --
    the thing a Plan run exists to authorise -- has never run at all.  The skill
    layer therefore points at the `doc` kind, where Codex reviews read-only and
    the findings are the deliverable.  These assertions exist to keep the old
    entry point from creeping back in.
    """

    def test_the_recipe_is_the_doc_kind_and_the_plan_entry_point_is_gone(self):
        frontmatter, text = skill("consensus-plan")

        self.assertEqual(set(frontmatter), {"name", "description"})
        self.assertEqual(frontmatter["name"], "consensus-plan")
        self.assertTrue(frontmatter["description"].startswith("Use when"))
        for phrase in (
            "ai-review init doc",
            "ai-review run",
            "ai-review status",
            "ai-review re-review",
            "ai-review expand-context",
            "ai-review answer",
            "AWAITING_HUMAN_DOC_REVIEW",
        ):
            self.assertIn(phrase, text)
        # Gone, not merely de-emphasised.  A document has nothing to run and
        # nothing downstream inherits its findings, so neither the verification
        # binding nor any approval command exists in this kind at all.
        for phrase in (
            "--verify",
            "init plan",
            "approve-plan",
            "approve-doc",
            "AWAITING_HUMAN_PLAN_REVIEW",
        ):
            self.assertNotIn(phrase, text)

    def test_the_document_is_reviewed_and_never_edited(self):
        """The one invariant that makes an unattended doc run safe."""
        _, text = skill("consensus-plan")

        self.assertIn("read-only", text)
        self.assertIn("never edits it", text)
        self.assertIn("The document's bytes are not touched by any part of this", text)
        self.assertIn("Never begin implementation", text)
        self.assertIn("never commit, push, merge, or open a pull request", text)

    def test_the_lens_is_judged_then_confirmed_once_before_anything_runs(self):
        _, text = skill("consensus-plan")

        for lens in ("`requirement`", "`direction`", "`implementation`"):
            self.assertIn(lens, text)
        self.assertIn("--lens", text)
        self.assertIn("--lens-reason", text)
        self.assertIn("wait for the user to confirm or correct", text)
        self.assertIn("One confirmation point, not two.", text)

    def test_context_is_bounded_and_questions_are_asked_verbatim(self):
        _, text = skill("consensus-plan")

        self.assertIn("no more than five", text)
        self.assertIn("16,000", text)
        self.assertIn("\"/absolute/path.md#Exact Heading\"", text)
        self.assertIn("verbatim", text)
        self.assertIn("never a whole folder or a broad file", text)
        self.assertIn("checksum-bound evidence", text)

    def test_the_answers_file_is_keyed_by_question_id_not_by_question_text(self):
        """Defect: the inherited wording produced a file `answer` always rejects.

        `PlanWorkflow._validate_answers` compares `set(answers)` against the
        persisted question ids, so a mapping of question *text* to answer can
        never match -- and the rejection used to pause the run, throwing away a
        separately billed Codex round.  An agent following this skill literally
        destroyed a real doc run, which is why the id contract is pinned here.
        """
        _, text = skill("consensus-plan")

        self.assertIn("keyed by the question `id`", text)
        self.assertIn("never by the question text", text)
        # A concrete two-id example, so the shape is copyable rather than inferred.
        self.assertIn('"Q-001"', text)
        self.assertIn('"Q-002"', text)
        # Gone, not merely de-emphasised: both halves of the wrong contract.
        self.assertNotIn("non-empty flat mapping", text)
        self.assertNotIn("question to answer", text)
        # The half that was right: the question text reaches the user untouched.
        self.assertIn("verbatim", text)
        self.assertIn("never summarize, never reword", text)

    def test_the_run_is_backgrounded_and_only_a_status_summary_is_reported(self):
        """One Codex call is bounded at 3600s; the Bash tool dies at 600s."""
        _, text = skill("consensus-plan")

        self.assertIn("3600s", text)
        self.assertIn("600s", text)
        self.assertIn("Always run it in the background and poll `status`", text)
        self.assertIn("status summary only", text)
        self.assertIn("Never paste patches, logs, prompts, or model transcripts", text)

    def test_re_review_is_the_way_back_in_and_recovery_is_state_specific(self):
        _, text = skill("consensus-plan")

        self.assertIn("unresolved findings", text)
        self.assertIn("refuses", text)
        self.assertIn("INTERRUPTED", text)
        self.assertIn("WORKFLOW_ERROR", text)
        self.assertIn("expand-context", text)

    def test_a_codex_pass_is_not_approval_and_this_kind_needs_no_claude_login(self):
        _, text = skill("consensus-plan")

        self.assertIn("A Codex `PASS` is not approval", text)
        self.assertIn("non-deterministic", text)
        self.assertIn("needs no `claude` login", text)
        self.assertIn("separately billed Codex session", text)
