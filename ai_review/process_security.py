"""Canonical executable identity and sanitized subprocess boundaries."""

import contextvars
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union


GIT = Path("/usr/bin/git")
CONTROLLED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"


def controlled_env(*, include_developer_dir: bool = True) -> dict[str, str]:
    env = {
        "PATH": CONTROLLED_PATH,
        "HOME": str(Path.home()),
        "TMPDIR": os.environ.get("TMPDIR", "/private/tmp"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "core.hooksPath",
        "GIT_CONFIG_VALUE_1": "/dev/null",
    }
    if include_developer_dir and "DEVELOPER_DIR" in os.environ:
        env["DEVELOPER_DIR"] = os.environ["DEVELOPER_DIR"]
    # The claude CLI resolves its Keychain login credential by account name;
    # without USER it reports "Not logged in" even when a login exists.
    for key in ("USER", "LOGNAME"):
        if key in os.environ:
            env[key] = os.environ[key]
    # Literal local subprocess fixtures use these names; they carry only paths
    # to test-owned queue/log files and do not broaden executable lookup.
    for key in ("AI_REVIEW_FAKE_QUEUE", "AI_REVIEW_FAKE_LOG"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def executable_identity(path: Path) -> dict[str, Any]:
    requested = Path(path)
    resolved = requested.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(str(resolved), os.X_OK):
        raise ValueError("executable is not a regular executable")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "realpath": str(resolved),
        "sha256": digest.hexdigest(),
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
    }


# Commands that execute nothing but git (approve-code, status, queue,
# writeback-knowledge) may read a run whose bound claude/codex/verification
# executable has since updated: the evidence was produced while the identity
# matched, and nothing is about to run under the old binding.  Every call that
# executes something uses require_executable_identity, which ignores this.
_BOUND_EXECUTABLE_CHECKS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ai_review_bound_executable_checks", default=True,
)


def suspend_bound_executable_checks() -> contextvars.Token:
    """Stop re-hashing bound executables while a run is only being inspected."""
    return _BOUND_EXECUTABLE_CHECKS.set(False)


def restore_bound_executable_checks(token: contextvars.Token) -> None:
    _BOUND_EXECUTABLE_CHECKS.reset(token)


def bound_executable_checks_enabled() -> bool:
    return _BOUND_EXECUTABLE_CHECKS.get()


def _check_identity_shape(identity: Mapping[str, Any]) -> None:
    if not isinstance(identity, Mapping) or set(identity) != {
        "realpath", "sha256", "dev", "ino", "mode", "uid"
    }:
        raise ValueError("executable identity has an invalid shape")


def validate_executable_identity(identity: Mapping[str, Any]) -> str:
    """Validate a bound identity; skipped only while a run is being inspected."""
    if not _BOUND_EXECUTABLE_CHECKS.get():
        _check_identity_shape(identity)
        return str(identity["realpath"])
    return require_executable_identity(identity)


def require_executable_identity(identity: Mapping[str, Any]) -> str:
    """Always re-hash: the caller is about to execute this binary."""
    _check_identity_shape(identity)
    current = executable_identity(Path(str(identity["realpath"])))
    if current != dict(identity):
        raise ValueError("executable identity changed after it was bound")
    return current["realpath"]


def resolve_executable(name: str, *, path: Optional[str] = None) -> dict[str, Any]:
    located = shutil.which(name, path=path)
    if located is None:
        raise ValueError("%s executable is unavailable" % name)
    return executable_identity(Path(located))


GIT_IDENTITY = executable_identity(GIT)


def run_git(
    arguments: Sequence[str], *, cwd: Optional[Union[Path, str]] = None, **kwargs: Any
) -> subprocess.CompletedProcess:
    executable = require_executable_identity(GIT_IDENTITY)
    return subprocess.run(
        [executable, *arguments],
        cwd=None if cwd is None else str(cwd),
        env=controlled_env(),
        shell=False,
        **kwargs,
    )
