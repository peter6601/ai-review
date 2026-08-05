"""Complete, replayable Git patch capture without a shell boundary."""

import fnmatch
import ast
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
from .process_security import run_git


class GitDiffError(RuntimeError):
    """Patch capture could not safely represent the repository changes."""


@dataclass(frozen=True)
class DiffCapture:
    patch_path: Path
    production_added_lines: int


def _run(argv: Sequence[str], *, cwd: Path, accepted: Iterable[int] = (0,)) -> subprocess.CompletedProcess:
    try:
        result = run_git(list(argv), cwd=cwd, check=False, capture_output=True)
    except OSError as error:
        raise GitDiffError("could not execute git") from error
    if result.returncode not in set(accepted):
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitDiffError("git command failed: %s" % (detail or result.returncode))
    return result


def _worktree_root(repo: Path) -> Path:
    requested = Path(repo).expanduser().resolve()
    top_level = _run(["rev-parse", "--show-toplevel"], cwd=requested).stdout
    root = Path(os.fsdecode(top_level).strip()).resolve()
    if root != requested:
        raise GitDiffError("repository path must be the exact Git worktree root")
    return root


def _resolve_base_ref(repo: Path, base_ref: str) -> str:
    """Resolve a revision to one full commit OID before it reaches `git diff`."""
    if not isinstance(base_ref, str) or not base_ref or "\0" in base_ref:
        raise GitDiffError("base_ref must be a non-empty revision without NUL")
    object_format = _run(["rev-parse", "--show-object-format"], cwd=repo).stdout
    format_name = object_format.decode("ascii", "strict").strip()
    lengths = {"sha1": 40, "sha256": 64}
    if format_name not in lengths:
        raise GitDiffError("repository object format is unsupported")
    resolved = _run(
        ["rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"],
        cwd=repo,
    ).stdout
    try:
        lines = resolved.decode("ascii", "strict").splitlines()
    except UnicodeDecodeError as error:
        raise GitDiffError("git returned a non-ASCII commit object ID") from error
    if len(lines) != 1 or not re.fullmatch(r"[0-9a-f]+", lines[0]) or len(lines[0]) != lengths[format_name]:
        raise GitDiffError("git did not resolve base_ref to exactly one full commit object ID")
    return lines[0]


def _untracked_paths(repo: Path, *, ignored: Iterable[Path] = ()) -> list[Path]:
    result = _run(["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo)
    raw_paths = [item for item in result.stdout.split(b"\0") if item]
    ignored_paths = {Path(path).resolve() for path in ignored}
    paths = []
    for raw in raw_paths:
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise GitDiffError("untracked path is not safely relative to the repository")
        candidate = repo / relative
        if candidate.resolve(strict=False) in ignored_paths:
            continue
        try:
            candidate.resolve(strict=False).relative_to(repo)
        except ValueError as error:
            raise GitDiffError("untracked path resolves outside the repository") from error
        if not candidate.is_file():
            raise GitDiffError("untracked path is not a regular file")
        paths.append(relative)
    return paths


def _is_excluded(path: str, excludes: Iterable[str]) -> bool:
    normalized = path.replace(os.sep, "/")
    parts = PurePosixPath(normalized).parts
    if parts and parts[0] == "docs":
        return any(pattern == "docs/**" for pattern in excludes)
    for part in parts[:-1]:
        if part.endswith("Tests") or part.endswith("UITests"):
            if any("Tests" in pattern or "UITests" in pattern for pattern in excludes):
                return True
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in excludes)


def production_added_lines(patch: bytes, production_excludes: Iterable[str] = ()) -> int:
    """Count textual unified-diff additions without parsing `diff --git` headers.

    This public helper consumes the destination `+++` marker, which preserves the
    entire path (including spaces and the literal text ` b/`).  Capture itself
    uses Git's NUL-delimited numstat data below, rather than this text format.
    """
    excludes = tuple(production_excludes)
    total = 0
    current_path = None
    for line in patch.decode("utf-8", "replace").splitlines():
        if line.startswith("+++ "):
            marker = line[4:]
            if marker == "/dev/null":
                current_path = None
            else:
                if marker.startswith('"'):
                    try:
                        marker = ast.literal_eval(marker)
                    except (SyntaxError, ValueError):
                        current_path = None
                        continue
                current_path = marker[2:] if isinstance(marker, str) and marker.startswith("b/") else None
            continue
        if current_path is None or _is_excluded(current_path, excludes):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            total += 1
    return total


def _numstat_entries(raw: bytes) -> Iterable[tuple[str, int]]:
    """Yield destination path and textual additions from Git's NUL-safe numstat."""
    records = raw.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise GitDiffError("git returned malformed numstat output")
        added, _deleted, path = fields
        if not path:
            if index + 1 >= len(records):
                raise GitDiffError("git returned incomplete rename numstat output")
            _old_path, path = records[index], records[index + 1]
            index += 2
        try:
            additions = int(added) if added != b"-" else 0
        except ValueError as error:
            raise GitDiffError("git returned malformed numstat count") from error
        yield os.fsdecode(path), additions


def _production_numstat_added_lines(
    repo: Path, argv: Sequence[str], production_excludes: Iterable[str], accepted: Iterable[int] = (0,)
) -> int:
    result = _run(argv, cwd=repo, accepted=accepted)
    return sum(
        additions
        for path, additions in _numstat_entries(result.stdout)
        if not _is_excluded(path, production_excludes)
    )


def _atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".round-patch-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def capture_diff(
    repo: Path,
    base_ref: str,
    output_path: Path,
    *,
    production_excludes: Iterable[str] = (),
    excluded_artifact_roots: Iterable[Path] = (),
) -> DiffCapture:
    """Save one complete binary patch for tracked and safe untracked changes.

    Git returns status 1 for `diff --no-index` when the files differ; that is the
    expected successful result for each untracked file included in the patch.
    """
    root = _worktree_root(Path(repo))
    base_oid = _resolve_base_ref(root, base_ref)
    destination = Path(output_path).expanduser().resolve()
    for artifact_root in tuple(excluded_artifact_roots) + (destination,):
        candidate = Path(artifact_root).expanduser().resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        raise GitDiffError("artifact output and exclusions must be outside the target repository")
    patch, production_lines = capture_diff_bytes(repo, base_ref, production_excludes=production_excludes)
    _atomic_write(destination, patch)
    return DiffCapture(destination.resolve(), production_lines)


def capture_diff_bytes(
    repo: Path, base_ref: str, *, production_excludes: Iterable[str] = (),
) -> tuple[bytes, int]:
    """Build a complete patch in memory; a run store persists it safely."""
    root = _worktree_root(Path(repo))
    base_oid = _resolve_base_ref(root, base_ref)
    safe_diff_options = ["--no-textconv", "--no-ext-diff"]
    tracked = _run(
        ["diff", *safe_diff_options, "--binary", base_oid, "--"], cwd=root
    ).stdout
    complete = bytearray(tracked)
    untracked = _untracked_paths(root)
    production_lines = _production_numstat_added_lines(
        root,
        ["diff", *safe_diff_options, "--numstat", "-z", "--find-renames", base_oid, "--"],
        production_excludes,
    )
    for relative in untracked:
        addition = _run(
            [
                "diff", *safe_diff_options, "--no-index", "--binary", "--",
                "/dev/null", str(relative),
            ],
            cwd=root,
            accepted=(0, 1),
        ).stdout
        complete.extend(addition)
        if complete and not complete.endswith(b"\n"):
            complete.extend(b"\n")
        production_lines += _production_numstat_added_lines(
            root,
            [
                "diff", *safe_diff_options, "--numstat", "-z", "--no-index", "--",
                "/dev/null", str(relative),
            ],
            production_excludes,
            accepted=(0, 1),
        )
    return bytes(complete), production_lines


def changed_paths(repo: Path, base_oid: str) -> tuple[str, ...]:
    """Return tracked and untracked paths without a newline parsing boundary."""
    tracked = run_git(
        ["-C", str(repo), "diff", "--name-only", "-z", base_oid, "--"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.split(b"\0")
    untracked = run_git(
        ["-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.split(b"\0")
    return tuple(sorted({
        item.decode("utf-8", "surrogateescape")
        for item in (*tracked, *untracked)
        if item
    }))


# A descriptive alias for callers that phrase the operation as a snapshot.
capture_complete_diff = capture_diff
