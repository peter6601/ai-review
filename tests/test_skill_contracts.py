import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CODE_BASELINE = ROOT / "tests" / "fixtures" / "consensus_code_no_skill_baseline.md"


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
    def test_observed_baseline_has_a_bounded_human_gate_recipe(self):
        frontmatter, text = skill("consensus-plan")

        self.assertEqual(set(frontmatter), {"name", "description"})
        self.assertEqual(frontmatter["name"], "consensus-plan")
        self.assertTrue(frontmatter["description"].startswith("Use when"))
        self.assertIn("no more than three", text)
        self.assertIn('AWAITING_HUMAN_PLAN_REVIEW', text)
        self.assertIn("Never begin implementation", text)
        self.assertIn("ask the user verbatim", text)
        self.assertIn("ai-review init plan", text)
        self.assertIn("ai-review run", text)
        self.assertIn("ai-review answer", text)
        self.assertIn("ai-review resume", text)


class ConsensusCodeSkillContractTests(unittest.TestCase):
    def test_no_skill_baseline_is_the_verbatim_observation_not_a_desired_recipe(self):
        observed = CODE_BASELINE.read_text(encoding="utf-8")

        self.assertEqual(
            observed,
            "# Observed no-skill baseline: consensus-code\n\n"
            "The no-skill agent correctly refused self-approval, seventh repair, and unauthorized commit, "
            "but proposed an unspecified \"auditable status log\" (no bounded/no-full-log rule), conversational "
            "approval rather than signed approve-plan digest contract, and base from Plan rather than requiring "
            "an explicit immutable CLI base argument.\n",
        )
        self.assertNotIn("--expected-plan-digest", observed)
        self.assertNotIn("--expected-status", observed)

    def test_observed_code_pressure_gaps_have_an_explicit_recipe(self):
        frontmatter, text = skill("consensus-code")

        self.assertEqual(set(frontmatter), {"name", "description"})
        self.assertEqual(frontmatter["name"], "consensus-code")
        self.assertTrue(frontmatter["description"].startswith("Use when"))
        self.assertIn("local approval dialog", text)
        self.assertIn("must never interact with or auto-click", text)
        self.assertNotIn("--expected-plan-digest", text)
        self.assertNotIn("--expected-status", text)
        self.assertIn("--base", text)
        self.assertIn("status summary only", text)
        self.assertIn("sixth", text)
        self.assertIn("Never commit, push, merge, or create a pull request", text)
