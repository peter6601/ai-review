"""Install approved AI skill entry points as recoverable relative symlinks."""

import argparse
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parent
CANONICAL_SKILLS_ROOT = REPOSITORY_ROOT / "skills"
SKILL_NAMES = ("consensus-plan", "consensus-review")


@dataclass(frozen=True)
class InstallTarget:
    source: Path
    target: Path


@dataclass(frozen=True)
class InstallResult:
    changed: List[str]


def validate_target(target: Path, allowed_roots: Iterable[Path]) -> None:
    """Ensure a target is directly below one of the approved skill roots."""
    resolved_parent = target.parent.resolve()
    if resolved_parent not in [root.resolve() for root in allowed_roots]:
        raise ValueError("target is outside approved skill roots: %s" % target)


def validate_source(source: Path, canonical_root: Optional[Path] = None) -> None:
    """Ensure a source resolves within the repository's canonical skills root."""
    canonical_root = Path(canonical_root or CANONICAL_SKILLS_ROOT).resolve()
    resolved_source = source.resolve()
    try:
        resolved_source.relative_to(canonical_root)
    except ValueError:
        raise ValueError("source is outside canonical skills root: %s" % source)
    if resolved_source == canonical_root or not source.is_dir():
        raise ValueError("canonical source is unavailable: %s" % source)


def check_install(
    targets: Sequence[InstallTarget], allowed_roots: Iterable[Path], *, canonical_root: Optional[Path] = None,
) -> InstallResult:
    """Report noncanonical entries without changing the filesystem."""
    changed = []
    for item in targets:
        validate_source(item.source, canonical_root)
        validate_target(item.target, allowed_roots)
        if not item.target.is_symlink() or item.target.resolve() != item.source.resolve():
            changed.append(str(item.target))
    return InstallResult(changed)


def _backup_destination(target: Path, backup_root: Path, allowed_roots: Iterable[Path]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    resolved_roots = [root.resolve() for root in allowed_roots]
    root = target.parent.resolve()
    channel = "claude" if root == resolved_roots[0] else "codex"
    destination = backup_root / timestamp / channel / target.name
    suffix = 1
    while destination.exists() or destination.is_symlink():
        destination = backup_root / timestamp / channel / (target.name + "-" + str(suffix))
        suffix += 1
    return destination


def apply_install(
    targets: Sequence[InstallTarget], backup_root: Path, allowed_roots: Iterable[Path], *, canonical_root: Optional[Path] = None,
) -> InstallResult:
    """Back up replaced entries and install canonical relative symlinks."""
    allowed_roots = list(allowed_roots)
    result = check_install(targets, allowed_roots, canonical_root=canonical_root)
    changed = set(result.changed)

    for item in targets:
        if str(item.target) not in changed:
            continue
        if item.target.exists() or item.target.is_symlink():
            destination = _backup_destination(item.target, backup_root, allowed_roots)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item.target), str(destination))

        item.target.parent.mkdir(parents=True, exist_ok=True)
        relative_source = os.path.relpath(str(item.source), str(item.target.parent))
        item.target.symlink_to(relative_source, target_is_directory=True)

    return result


def _cli_targets(home: Path, source_root: Optional[Path] = None) -> List[InstallTarget]:
    targets = []
    source_root = Path(source_root or CANONICAL_SKILLS_ROOT)
    for name in SKILL_NAMES:
        source = source_root / name
        validate_source(source, source_root)
        targets.extend(
            (
                InstallTarget(source, home / ".claude" / "commands" / name),
                InstallTarget(source, home / ".codex" / "skills" / name),
            )
        )
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report required changes")
    mode.add_argument("--apply", action="store_true", help="back up and install symlinks")
    parser.add_argument("--source-root", help="temporary canonical skills root for a read-only check")
    args = parser.parse_args()

    home = Path.home()
    allowed_roots = [home / ".claude" / "commands", home / ".codex" / "skills"]
    try:
        source_root = Path(args.source_root).expanduser().resolve() if args.source_root else CANONICAL_SKILLS_ROOT
        if args.source_root and not args.check:
            parser.error("--source-root is available only with --check")
        targets = _cli_targets(home, source_root)
        if args.apply:
            result = apply_install(targets, REPOSITORY_ROOT / ".skill-backups", allowed_roots)
            for item in targets:
                print(item.target.resolve())
        else:
            result = check_install(targets, allowed_roots, canonical_root=source_root)
            for target in result.changed:
                print(target)
    except ValueError as error:
        parser.error(str(error))

    return 1 if result.changed and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
