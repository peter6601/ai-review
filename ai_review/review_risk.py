"""Deterministic high-risk change detection for direct code reviews.

Detection is intentionally boring: it reads Git's NUL-delimited changed-path list
and the already-captured patch text.  It never imports, evaluates, or executes
project code, and it never consults a model to decide whether a category applies.
Model-reported ``risk_flags`` can only *add* evidence.
"""

import ast
import fnmatch
import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .git_diff import changed_paths


# The seven classifiable categories, plus one fail-closed sentinel for a model
# risk report we cannot map onto them.  Uncertainty is treated as high risk.
HIGH_RISK_CATEGORIES = frozenset({
    "dependencies",
    "migration",
    "entitlements_signing",
    "ci_cd",
    "public_api",
    "persistent_format",
    "network_format",
    "unclassified_model_risk",
})

MAX_RISK_EVIDENCE = 50
MAX_RISK_STRING_BYTES = 500

# A path is a comparison key, not display text: `_evidence_beyond_baseline` and
# the content fingerprints both look a path up by this exact value. Truncating it
# would make two different files compare equal and read as unchanged, so the
# bound sits above PATH_MAX on both supported platforms (1024 on macOS, 4096 on
# Linux) and a real path can therefore never reach it.
MAX_RISK_PATH_BYTES = 4096
_OVERSIZED_PATH = "<oversized-path:%s>"


class RiskDetectionError(ValueError):
    """The captured evidence could not be inspected safely."""


_DEPENDENCY_NAMES = frozenset({
    "package.swift", "package.resolved", "podfile", "podfile.lock",
    "cartfile", "cartfile.resolved", "cartfile.private",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "requirements.txt", "requirements-dev.txt", "pipfile", "pipfile.lock",
    "poetry.lock", "pyproject.toml", "setup.py", "setup.cfg",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum",
    "gemfile", "gemfile.lock", "mise.toml", "flake.lock",
    "build.gradle", "build.gradle.kts", "gradle.lockfile", "pom.xml",
})

_DEPENDENCY_PATTERNS = ("*.podspec", "*.lock", "*.xcworkspacedata")

_MIGRATION_PATTERNS = (
    "*.sql", "*.xcdatamodel", "*.xcmappingmodel", "*.sqlite", "*.realm",
)

_MIGRATION_DIRECTORIES = frozenset({"migration", "migrations", "schema", "schemas"})

_SIGNING_PATTERNS = (
    "*.entitlements", "*.mobileprovision", "*.provisionprofile",
    "*.p12", "*.cer", "*.certsigningrequest", "exportoptions.plist",
)

_SIGNING_CONTENT = re.compile(
    r"CODE_SIGN_IDENTITY|CODE_SIGN_STYLE|CODE_SIGN_ENTITLEMENTS"
    r"|DEVELOPMENT_TEAM|PROVISIONING_PROFILE"
)

_CI_PATTERNS = (
    ".github/workflows/*", ".github/actions/*", ".gitlab-ci.yml", "jenkinsfile",
    ".circleci/*", "azure-pipelines.yml", "bitrise.yml", "codemagic.yaml",
    "fastlane/*", "*.tf", "*.tfvars", "dockerfile", "docker-compose.yml",
    "*.xcscheme", "buildkite.yml", ".buildkite/*",
)

_PERSISTENT_PATTERNS = ("*.proto", "*.avsc", "*.thrift", "*.fbs", "*.capnp")

_NETWORK_PATTERNS = (
    "*.graphql", "*.graphqls", "openapi*.yaml", "openapi*.yml", "openapi*.json",
    "swagger*.yaml", "swagger*.yml", "swagger*.json", "*.wsdl",
)

# Only declarations, never comments or string bodies: a conservative signal that
# an externally visible contract may have changed.
_PUBLIC_API_CONTENT = re.compile(
    r"^\s*(?:@\w+\s+)*(?:public|open)\s+"
    r"(?:final\s+|static\s+|class\s+|indirect\s+)*"
    r"(?:func|var|let|class|struct|enum|protocol|actor|init|subscript|typealias)\b"
)

_PUBLIC_API_SUFFIXES = (".swift",)
_PUBLIC_HEADER_SUFFIXES = (".h", ".hpp", ".hh")


def _bounded(value: str) -> str:
    """Truncate on a UTF-8 boundary so evidence can never grow unbounded."""
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_RISK_STRING_BYTES:
        return value
    return encoded[:MAX_RISK_STRING_BYTES].decode("utf-8", "ignore")


def bounded_path(path: str) -> str:
    """Return a path safe to record without ever conflating two distinct files.

    A path that fits is returned verbatim so it stays a usable filesystem key.
    One that does not is replaced by a unique digest marker rather than a prefix,
    because a truncated path would silently name a different file — or no file at
    all, which then reads as "unchanged".  Callers treat the marker as unresolved
    content and gate on it.
    """
    if len(path.encode("utf-8", "surrogateescape")) <= MAX_RISK_PATH_BYTES:
        return path
    return _OVERSIZED_PATH % hashlib.sha256(
        path.encode("utf-8", "surrogateescape")
    ).hexdigest()


def is_unresolved_path(path: str) -> bool:
    """Report whether a recorded path could not be preserved exactly."""
    return path.startswith("<oversized-path:")


def _normalized(path: str) -> str:
    # Strip only a leading "./" prefix: a character-class strip would eat the
    # leading dot of paths such as ".github/workflows/ci.yml".
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _matches(path: str, patterns: Iterable[str]) -> bool:
    lowered = _normalized(path).lower()
    name = PurePosixPath(lowered).name
    return any(
        fnmatch.fnmatchcase(lowered, pattern) or fnmatch.fnmatchcase(name, pattern)
        or fnmatch.fnmatchcase(lowered, "*/" + pattern)
        for pattern in patterns
    )


def _path_categories(path: str) -> tuple[str, ...]:
    lowered = _normalized(path).lower()
    parts = PurePosixPath(lowered).parts
    name = PurePosixPath(lowered).name
    categories = []
    if name in _DEPENDENCY_NAMES or _matches(path, _DEPENDENCY_PATTERNS):
        categories.append("dependencies")
    if (
        _matches(path, _MIGRATION_PATTERNS)
        or any(part in _MIGRATION_DIRECTORIES for part in parts[:-1])
        or any(part.endswith(".xcdatamodeld") for part in parts)
    ):
        categories.append("migration")
    if _matches(path, _SIGNING_PATTERNS):
        categories.append("entitlements_signing")
    if _matches(path, _CI_PATTERNS):
        categories.append("ci_cd")
    if _matches(path, _PERSISTENT_PATTERNS):
        categories.append("persistent_format")
    if _matches(path, _NETWORK_PATTERNS):
        categories.append("network_format")
    return tuple(categories)


def added_lines_by_path(patch: bytes) -> dict[str, list[str]]:
    """Attribute added lines to destination paths using the `+++` marker only.

    The `diff --git` header is ambiguous for paths containing ` b/`, so this
    mirrors ``git_diff.production_added_lines`` and consumes the destination
    marker, decoding Git's quoted form when present.
    """
    if not isinstance(patch, (bytes, bytearray)):
        raise RiskDetectionError("patch evidence must be bytes")
    additions: dict[str, list[str]] = {}
    current = None
    for line in bytes(patch).decode("utf-8", "replace").splitlines():
        if line.startswith("+++ "):
            marker = line[4:]
            current = None
            if marker != "/dev/null":
                if marker.startswith('"'):
                    try:
                        marker = ast.literal_eval(marker)
                    except (SyntaxError, ValueError):
                        continue
                if isinstance(marker, str) and marker.startswith("b/"):
                    current = marker[2:]
                    additions.setdefault(current, [])
            continue
        if current is None or not line.startswith("+") or line.startswith("+++"):
            continue
        additions[current].append(line[1:])
    return additions


def _content_categories(path: str, lines: Iterable[str]) -> tuple[str, ...]:
    lowered = _normalized(path).lower()
    categories = []
    if lowered.endswith("project.pbxproj") or lowered.endswith(".plist"):
        if any(_SIGNING_CONTENT.search(line) for line in lines):
            categories.append("entitlements_signing")
    if lowered.endswith(_PUBLIC_API_SUFFIXES):
        if any(_PUBLIC_API_CONTENT.search(line) for line in lines):
            categories.append("public_api")
    elif lowered.endswith(_PUBLIC_HEADER_SUFFIXES):
        if any(line.strip() for line in lines):
            categories.append("public_api")
    return tuple(categories)


def detect_high_risk(
    repo: Path,
    base_oid: str,
    patch: bytes,
    *,
    model_flags: Iterable[Any] = (),
) -> tuple[dict[str, str], ...]:
    """Return sorted, bounded high-risk evidence for the current patch.

    ``model_flags`` are additive only: they can introduce categories the path and
    content rules do not cover, and can never remove deterministic evidence.
    """
    try:
        paths = changed_paths(Path(repo), base_oid)
    except Exception as error:  # Git failure must not read as "no risk".
        raise RiskDetectionError("could not list changed paths") from error
    additions = added_lines_by_path(patch)
    evidence: set[tuple[str, str, str]] = set()
    for path in paths:
        recorded = bounded_path(path)
        for category in _path_categories(path):
            evidence.add((
                category, recorded,
                _bounded("changed path matches the %s rule" % category),
            ))
        for category in _content_categories(path, additions.get(path, ())):
            evidence.add((
                category, recorded,
                _bounded("added lines match the %s rule" % category),
            ))
    for flag in model_flags:
        text = flag.strip() if isinstance(flag, str) else ""
        if not text:
            continue
        if text in HIGH_RISK_CATEGORIES and text != "unclassified_model_risk":
            evidence.add((
                text, "", _bounded("a model reported the %s category" % text),
            ))
        else:
            evidence.add((
                "unclassified_model_risk", "",
                _bounded("a model reported an unclassifiable risk: %s" % text),
            ))
    ordered = sorted(evidence)[:MAX_RISK_EVIDENCE]
    return tuple(
        {"category": category, "path": path, "reason": reason}
        for category, path, reason in ordered
    )


def risk_categories(evidence: Iterable[Mapping[str, str]]) -> tuple[str, ...]:
    """Return the sorted unique categories present in detected evidence."""
    return tuple(sorted({item["category"] for item in evidence}))


_FINGERPRINT_CHUNK_BYTES = 1024 * 1024


def path_content_fingerprints(
    repo: Path, paths: Iterable[str]
) -> dict[str, str]:
    """Fingerprint the current worktree content of each supplied path.

    This is deliberately content-based rather than patch-based.  A unified diff
    says only *that* a path changed, and a binary section carries no ``+`` lines
    at all, so neither can distinguish "the human's baseline already touched this
    lockfile" from "Claude rewrote that same lockfile".  Unreadable paths return
    a distinct sentinel so an unknown state never compares equal to a known one.

    The whole file is streamed in fixed-size chunks: memory stays O(1) while the
    digest still covers every byte.  A read cap here would be worse than no
    fingerprint at all, because a same-size edit past the cap would produce an
    identical digest and silently certify an unreviewed change as unchanged.
    """
    fingerprints: dict[str, str] = {}
    root = Path(repo)
    for path in sorted(set(paths)):
        if not path:
            continue
        target = root / path
        try:
            if target.is_symlink():
                fingerprints[path] = "symlink:%s" % hashlib.sha256(
                    os.readlink(str(target)).encode("utf-8", "surrogateescape")
                ).hexdigest()
                continue
            metadata = target.stat()
            if not stat.S_ISREG(metadata.st_mode):
                fingerprints[path] = "not-a-regular-file"
                continue
            digest = hashlib.sha256()
            digest.update(b"%d\0" % metadata.st_size)
            read = 0
            with target.open("rb") as handle:
                while True:
                    chunk = handle.read(_FINGERPRINT_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    read += len(chunk)
            digest.update(b"\0%d" % read)
            fingerprints[path] = digest.hexdigest()
        except FileNotFoundError:
            fingerprints[path] = "absent"
        except OSError:
            # Fail closed: an unreadable path must never look unchanged.
            fingerprints[path] = "unreadable:%s" % hashlib.sha256(
                path.encode("utf-8", "surrogateescape")
            ).hexdigest()
    return fingerprints
