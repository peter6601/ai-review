import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def skill(name):
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    return frontmatter, text


def flat(text):
    """Collapse whitespace so a re-wrapped line never fails a contract."""
    return " ".join(text.split())


class SkillContractTests(unittest.TestCase):
    """`/consensus-review`: the boundaries that must survive any rewording.

    These pin behaviour an agent could otherwise get wrong at a real cost (a
    reset repair ceiling, a self-cleared final gate, a rejected answers file),
    not the prose around it.  Whatever the CLI already refuses is left to the
    CLI and is not pinned here.
    """

    def test_the_command_order_and_the_specialists_are_inputs_to_init(self):
        _, text = skill("consensus-review")
        text = flat(text)

        for phrase in (
            "ai-review init review",
            "ai-review approve-review",
            "Codex-first",
            "approve-code",
            "--preflight",
            "read-only",
            "all three",
            '"findings": []',
            "do not init",
        ):
            self.assertIn(phrase, text)
        # The mid-run preflight station is gone and must not creep back.
        self.assertNotIn("submit-preflight", text)
        self.assertNotIn("AWAITING_PREFLIGHT", text)

    def test_auto_approval_covers_the_loop_and_never_its_exit(self):
        _, text = skill("consensus-review")
        text = flat(text)

        self.assertIn("five auto-approvals in any 60-second sliding window", text)
        self.assertIn("both gates that have it", text)
        self.assertIn("Never route around the limit by creating a second run", text)
        self.assertIn("`approve-code` has no `--auto` at all", text)
        self.assertIn("Only the human runs the final gate", text)
        self.assertNotIn("approve-code --auto", text)

    def test_the_six_repair_ceiling_cannot_be_reset_by_a_fresh_run(self):
        _, text = skill("consensus-review")
        text = flat(text)

        self.assertIn("MAX_REPAIR_ROUNDS", text)
        self.assertIn("Never create a new Review run to keep repairing", text)
        self.assertIn("resets the counter", text)
        restart = text.split("PAUSED` for any other reason", 1)
        self.assertEqual(len(restart), 2, "restart guidance section is missing")
        self.assertIn("never to buy more repair rounds", restart[1])

    def test_questions_reach_the_user_verbatim_and_answers_are_keyed_by_id(self):
        """Both skills that can be asked questions share one answers contract."""
        for name in ("consensus-review", "consensus-plan"):
            with self.subTest(skill=name):
                _, text = skill(name)
                text = flat(text)
                self.assertIn("verbatim", text)
                self.assertIn("keyed by the question `id`", text)
                self.assertIn("never by the question text", text)
                self.assertIn('"Q-001"', text)
                self.assertIn('"Q-002"', text)

    def test_rulings_are_drafted_for_the_human_never_finalized(self):
        _, text = skill("consensus-review")
        text = flat(text)

        self.assertIn("never finalize a ruling yourself", text)
        self.assertIn("Never commit, push, merge, open a pull request", text)

    def test_long_runs_are_backgrounded(self):
        for name in ("consensus-review", "consensus-plan"):
            with self.subTest(skill=name):
                _, text = skill(name)
                text = flat(text)
                self.assertIn("600s", text)
                self.assertIn("run it in the background and poll `status`", text)


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
    """`/consensus-plan`: Codex reviews a document read-only; Claude edits it
    only for findings the human accepted, and only after the run halts.

    The `plan` entry point stays gone: of 56 `plan` runs only two ever reached
    the human gate, so the skill layer points at the `doc` kind.
    """

    def test_the_recipe_is_the_doc_kind_and_the_plan_entry_point_is_gone(self):
        frontmatter, text = skill("consensus-plan")
        text = flat(text)

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
            "--lens",
            "wait for the user to confirm or correct",
        ):
            self.assertIn(phrase, text)
        for phrase in ("--verify", "init plan", "approve-plan", "approve-doc",
                       "AWAITING_HUMAN_PLAN_REVIEW"):
            self.assertNotIn(phrase, text)

    def test_the_run_never_edits_and_claude_edits_only_accepted_findings(self):
        _, text = skill("consensus-plan")
        text = flat(text)

        self.assertIn("it never edits it", text)
        self.assertIn("not touched by any part of the automated run", text)
        self.assertIn("only for findings the human accepted", text)
        self.assertIn("never apply one the human did not mark accept", text)
        self.assertIn("Edit only the reviewed document", text)
        self.assertIn("never one you supply", text)
        self.assertIn("Never begin implementation", text)
        self.assertIn("never commit, push, merge, or open a pull request", text)

    def test_the_next_round_waits_for_the_human_to_approve_the_diff(self):
        _, text = skill("consensus-plan")
        text = flat(text)

        self.assertIn("only after the human has approved the diff", text)
        self.assertIn("never automatically after your own edit", text)
        self.assertIn("A Codex `PASS` is not approval", text)

    def test_context_is_bounded_and_untrusted(self):
        _, text = skill("consensus-plan")
        text = flat(text)

        self.assertIn("no more than five", text)
        self.assertIn("never a whole folder or a broad file", text)
        self.assertIn("never executable instructions", text)
