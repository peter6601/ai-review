import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from install_skills import SKILL_NAMES, InstallTarget, apply_install, check_install, main


EXPECTED_SKILL_NAMES = (
    "consensus-plan",
    "consensus-code",
    "consensus-review",
)


class InstallSkillsTests(unittest.TestCase):
    def test_canonical_skill_names_cover_both_consensus_entry_points(self):
        self.assertEqual(SKILL_NAMES, EXPECTED_SKILL_NAMES)

    def test_temporary_source_root_check_is_read_only_and_apply_links_all_six_targets(self):
        """Catch an installer that depends on a feature worktree or touches a real home."""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "canonical-skills"
            claude_root = root / "claude" / "commands"
            codex_root = root / "codex" / "skills"
            sources = []
            targets = []
            for name in EXPECTED_SKILL_NAMES:
                source = source_root / name
                source.mkdir(parents=True)
                (source / "SKILL.md").write_text(name, encoding="utf-8")
                sources.append(source)
                targets.extend((InstallTarget(source, claude_root / name), InstallTarget(source, codex_root / name)))
            self.assertEqual(len(targets), 6)
            before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            output = io.StringIO()
            with patch("install_skills.Path.home", return_value=root / "unused-home"), patch(
                "sys.argv", ["install_skills.py", "--check", "--source-root", str(source_root)],
            ), redirect_stdout(output):
                self.assertEqual(main(), 1)
            self.assertEqual(sorted(str(path.relative_to(root)) for path in root.rglob("*")), before)
            self.assertEqual(len(output.getvalue().splitlines()), 6)

            apply_install(targets, root / "backups", [claude_root, codex_root], canonical_root=source_root)
            self.assertEqual(len([target for target in targets if target.target.is_symlink()]), 6)
            self.assertEqual([target.target.resolve() for target in targets], [target.source.resolve() for target in targets])
            self.assertEqual(
                sorted(path.name for path in claude_root.iterdir()),
                sorted(EXPECTED_SKILL_NAMES),
            )
            self.assertEqual(
                sorted(path.name for path in codex_root.iterdir()),
                sorted(EXPECTED_SKILL_NAMES),
            )
            for target in targets:
                # Links stay relative so a moved workspace root keeps resolving.
                self.assertFalse(Path(target.target.readlink()).is_absolute())

    def test_check_reports_noncanonical_directory_without_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "skills" / "ios-dev"
            target = root / ".claude" / "commands" / "ios-dev"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            (target / "SKILL.md").write_text("old\n", encoding="utf-8")
            with patch("install_skills.CANONICAL_SKILLS_ROOT", root / "skills"):
                result = check_install([InstallTarget(source, target)], [target.parent])
            self.assertEqual(result.changed, [str(target)])
            self.assertTrue(target.is_dir())

    def test_apply_moves_existing_directory_then_creates_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "skills" / "ios-dev"
            target = root / ".codex" / "skills" / "ios-dev"
            backup = root / "backups"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (source / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            (target / "SKILL.md").write_text("installed\n", encoding="utf-8")
            with patch("install_skills.CANONICAL_SKILLS_ROOT", root / "skills"):
                apply_install(
                    [InstallTarget(source, target)],
                    backup,
                    [target.parent],
                )
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), source.resolve())
            self.assertEqual(
                list(backup.rglob("SKILL.md"))[0].read_text(encoding="utf-8"),
                "installed\n",
            )

    def test_apply_rejects_target_outside_allowlist(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "skills" / "ios-dev"
            source.mkdir(parents=True)
            with patch("install_skills.CANONICAL_SKILLS_ROOT", root / "skills"):
                with self.assertRaises(ValueError):
                    apply_install(
                        [InstallTarget(source, Path("/private/tmp/not-allowed"))],
                        root / "backups",
                        [root / ".claude" / "commands"],
                    )

    def test_apply_rejects_source_outside_canonical_root_without_mutation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "outside" / "ios-dev"
            target = root / ".claude" / "commands" / "ios-dev"
            backup = root / "backups"
            source.mkdir(parents=True)
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("installed\n", encoding="utf-8")

            with patch("install_skills.CANONICAL_SKILLS_ROOT", root / "skills"):
                with self.assertRaises(ValueError):
                    apply_install([InstallTarget(source, target)], backup, [target.parent])

            self.assertTrue(target.is_dir())
            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "installed\n")
            self.assertFalse(backup.exists())

    def test_apply_rejects_canonical_child_symlink_escaping_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "skills" / "ios-dev"
            outside = root / "outside" / "ios-dev"
            target = root / ".codex" / "skills" / "ios-dev"
            backup = root / "backups"
            outside.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            source.symlink_to(outside, target_is_directory=True)
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("installed\n", encoding="utf-8")

            with patch("install_skills.CANONICAL_SKILLS_ROOT", root / "skills"):
                with self.assertRaises(ValueError):
                    apply_install([InstallTarget(source, target)], backup, [target.parent])

            self.assertTrue(target.is_dir())
            self.assertEqual((target / "SKILL.md").read_text(encoding="utf-8"), "installed\n")
            self.assertFalse(backup.exists())
