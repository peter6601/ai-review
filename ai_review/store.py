"""Atomic, central persistence for consensus runs."""

import hashlib
import json
import os
import errno
import re
import secrets
import subprocess
import stat
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ApprovalAuthority, RunState, strict_json_loads
from .process_security import run_git


def _canonical_workspace_root() -> Path:
    """The sole workspace allowed to own central review authority.

    Configure it with the AI_REVIEW_WORKSPACE environment variable; the
    default keeps everything under the user's home directory. The value is
    deliberately left unresolved so downstream canonical-path checks can
    still reject a symlinked root.
    """
    configured = os.environ.get("AI_REVIEW_WORKSPACE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ai-review" / "workspace"


PRODUCTION_WORKSPACE_ROOT = _canonical_workspace_root()
DEFAULT_RUNS_ROOT = Path.home() / "Library" / "Application Support" / "ai-review" / "runs"
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_PROJECT_ID_PATTERN = re.compile(r"[0-9a-f]{12}\Z")


def _validate_component(value: object, label: str, *, pattern: Optional[re.Pattern] = None) -> str:
    """Reject identifiers that could change a dirfd-relative operation's scope."""
    if not isinstance(value, str) or not value or value in (".", "..") or "\x00" in value:
        raise ValueError("%s must be a non-empty path component" % label)
    separators = {"/", "\\", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    if any(separator in value for separator in separators) or os.path.normpath(value) != value:
        raise ValueError("%s must be a normalized single path component" % label)
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError("%s has an invalid format" % label)
    return value


def _validate_run_id(value: object) -> str:
    return _validate_component(value, "run id")


def _validate_project_id(value: object) -> str:
    return _validate_component(value, "project id", pattern=_PROJECT_ID_PATTERN)


def _current_worktree_root() -> Optional[Path]:
    try:
        completed = run_git(
            ["rev-parse", "--show-toplevel"],
            cwd=str(Path.cwd()),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(completed.stdout.strip()).resolve()


def _approved_override(path: Path) -> bool:
    worktree = _current_worktree_root()
    return worktree is not None and path == worktree


def resolve_workspace_root() -> Path:
    """Resolve the sole workspace allowed to own central review authority."""
    override = os.environ.get("AI_REVIEW_HOME")
    if not override:
        return PRODUCTION_WORKSPACE_ROOT
    workspace = Path(override).expanduser().resolve()
    if _approved_override(workspace):
        return workspace
    raise ValueError("AI_REVIEW_HOME must be the isolated worktree")


def default_runs_root() -> Path:
    """Return the external transient run root without changing approval ownership."""
    return DEFAULT_RUNS_ROOT


def project_id(repo_path: Path) -> str:
    """Stable project namespace from the resolved Git root path supplied by caller."""
    resolved = str(Path(repo_path).expanduser().resolve())
    return _validate_project_id(hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12])


def git_worktree_root(repo_path: Path) -> Path:
    """Resolve and verify that *repo_path* itself is a Git worktree root."""
    requested = Path(repo_path).expanduser().resolve()
    if not requested.is_dir():
        raise ValueError("repository path is not a directory: %s" % repo_path)
    try:
        completed = run_git(
            ["-C", str(requested), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("repository path is not a Git worktree: %s" % repo_path) from error
    root = Path(completed.stdout.strip()).resolve()
    if root != requested:
        raise ValueError("repository path must be exactly the Git worktree root")
    return root


class RunStore:
    """Persist states under ``<central runs>/<project id>/<run id>/state.json``."""

    def __init__(
        self,
        root: Optional[Path] = None,
        authority: Optional[ApprovalAuthority] = None,
    ):
        if root is None:
            workspace = resolve_workspace_root()
            self.root = default_runs_root()
            self.authority = authority or ApprovalAuthority.for_workspace(workspace)
        else:
            # Keep the spelling supplied by the authority owner.  Resolving it
            # here would silently accept a symlink before descriptor authority
            # is established below.
            candidate = Path(root).expanduser()
            self.root = candidate if candidate.is_absolute() else candidate.absolute()
            self.authority = authority
        self._root_identity: Optional[tuple[int, int]] = None
        self._directory_identities: dict[tuple[str, ...], tuple[int, int]] = {}

    @staticmethod
    def _new_run_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return "%s-%s" % (timestamp, secrets.token_hex(4))

    def _artifact_parts(self, path: Path) -> tuple[str, ...]:
        """Validate only lexical containment; filesystem authority is dirfd-only."""
        candidate = Path(path)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("artifact path escapes the run root") from error
        parts = relative.parts
        if not parts or any(part in ("", ".", "..") or "/" in part for part in parts):
            raise ValueError("artifact path is not a safe relative path")
        if len(parts) < 3:
            raise ValueError("artifact path must be inside a project run")
        _validate_project_id(parts[0])
        _validate_run_id(parts[1])
        return parts

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return (info.st_dev, info.st_ino)

    def _pin_directory(self, parts: tuple[str, ...], info: os.stat_result) -> None:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("artifact path component is not a directory")
        identity = self._identity(info)
        previous = self._directory_identities.get(parts)
        if previous is None:
            self._directory_identities[parts] = identity
        elif previous != identity:
            raise ValueError("artifact directory identity changed")

    def _open_root(self, *, create: bool) -> int:
        """Bootstrap once, then pin a real root directory by descriptor identity."""
        if create:
            # This is the only pathname creation.  All run artifacts below this
            # root are created through mkdirat/openat from the returned fd.
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.root), self._directory_flags())
        except OSError as error:
            raise ValueError("artifact root cannot be opened safely") from error
        try:
            info = os.fstat(fd)
            self._pin_directory((), info)
            identity = self._identity(info)
            if self._root_identity is None:
                self._root_identity = identity
            elif self._root_identity != identity:
                raise ValueError("artifact root identity changed")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_directory(self, root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
        """Traverse each directory with O_NOFOLLOW and validate its fstat identity."""
        # The caller retains its descriptor; traversal owns only this duplicate.
        fd = os.dup(root_fd)
        traversed: tuple[str, ...] = ()
        try:
            for component in parts:
                traversed += (component,)
                try:
                    child = os.open(component, self._directory_flags(), dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        # A concurrent replacement is not trusted until its
                        # descriptor is opened with no-follow and fstat below.
                        pass
                    child = os.open(component, self._directory_flags(), dir_fd=fd)
                except OSError as error:
                    if error.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ValueError("artifact path contains an unsafe directory") from error
                    raise
                info = os.fstat(child)
                self._pin_directory(traversed, info)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_artifact_parent(self, path: Path, *, create: bool) -> tuple[int, str]:
        """Open every component with no-follow semantics; never retain a pathname."""
        parts = self._artifact_parts(path)
        if not parts:
            raise ValueError("artifact path must name a file")
        root_fd = self._open_root(create=create)
        try:
            return self._open_directory(root_fd, parts[:-1], create=create), parts[-1]
        finally:
            os.close(root_fd)

    def _open_artifact_directory(self, path: Path, *, create: bool) -> int:
        root_fd = self._open_root(create=create)
        try:
            return self._open_directory(root_fd, self._artifact_parts(path), create=create)
        finally:
            os.close(root_fd)

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("artifact write made no progress")
            view = view[written:]

    @staticmethod
    def _read_all(fd: int, size: int) -> bytes:
        if size < 0 or size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds maximum size")
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, min(64 * 1024, MAX_ARTIFACT_BYTES - total + 1))
            except InterruptedError:
                continue
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact exceeds maximum size")
            chunks.append(chunk)

    def write_artifact_bytes(self, path: Path, data: bytes, *, create_parents: bool = True) -> None:
        parent, leaf = self._open_artifact_parent(path, create=create_parents)
        temporary = ".%s-%s.tmp" % (leaf, secrets.token_hex(8))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
            try:
                self._write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(parent)

    def read_artifact_bytes(self, path: Path) -> bytes:
        parent, leaf = self._open_artifact_parent(path, create=False)
        try:
            fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("artifact leaf must be a regular file")
                return self._read_all(fd, info.st_size)
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def remove_artifact(self, path: Path) -> None:
        """Atomically make one descriptor-authorized artifact leaf absent."""
        try:
            parent, leaf = self._open_artifact_parent(path, create=False)
        except FileNotFoundError:
            return
        try:
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("artifact leaf must be a regular file")
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def _atomic_write(self, path: Path, contents: dict, *, create_parents: bool = True) -> None:
        self.write_artifact_bytes(
            path,
            (json.dumps(contents, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            create_parents=create_parents,
        )

    def _run_directory(self, state: RunState) -> Path:
        if state.manifest is None:
            raise ValueError("run state requires a manifest for central storage")
        root = git_worktree_root(Path(state.manifest.repo_path))
        self._assert_artifact_root_outside_repo(root)
        run_id = _validate_run_id(state.run_id)
        project = _validate_project_id(project_id(root))
        directory = self.root / project / run_id
        self._artifact_parts(directory / "state.json")
        return directory

    def _assert_artifact_root_outside_repo(self, repo: Path) -> None:
        try:
            self.root.resolve().relative_to(repo.resolve())
        except ValueError:
            return
        raise ValueError("review artifacts must be stored outside the target repository")

    def create(self, state: RunState) -> RunState:
        if state.run_id is not None:
            _validate_run_id(state.run_id)
        state.validate(self.authority)
        if state.manifest is None:
            raise ValueError("run state requires a manifest for central storage")
        root = git_worktree_root(Path(state.manifest.repo_path))
        self._assert_artifact_root_outside_repo(root)
        run_id = _validate_run_id(state.run_id or self._new_run_id())
        project = _validate_project_id(project_id(root))
        directory = self.root / project / run_id
        project_parts = (project,)
        root_fd = self._open_root(create=True)
        try:
            project_fd = self._open_directory(root_fd, project_parts, create=True)
            try:
                try:
                    os.mkdir(run_id, mode=0o700, dir_fd=project_fd)
                except FileExistsError as error:
                    raise FileExistsError("run directory already exists: %s" % directory) from error
                run_fd = os.open(run_id, self._directory_flags(), dir_fd=project_fd)
                try:
                    self._pin_directory(project_parts + (run_id,), os.fstat(run_fd))
                    os.fsync(project_fd)
                finally:
                    os.close(run_fd)
            finally:
                os.close(project_fd)
        finally:
            os.close(root_fd)
        state = replace(state, run_id=run_id)
        try:
            self._atomic_write(directory / "state.json", state.to_dict())
        except BaseException:
            # Remove only by the already-validated parent descriptor.
            root_fd: Optional[int] = None
            try:
                root_fd = self._open_root(create=False)
                project_fd = self._open_directory(root_fd, project_parts, create=False)
                try:
                    os.rmdir(run_id, dir_fd=project_fd)
                    os.fsync(project_fd)
                finally:
                    os.close(project_fd)
            except OSError:
                pass
            finally:
                if root_fd is not None:
                    os.close(root_fd)
            raise
        return state

    def save(self, state: RunState) -> None:
        state.validate(self.authority)
        directory = self._run_directory(state)
        self._atomic_write(directory / "state.json", state.to_dict(), create_parents=False)

    def load(self, run_id: str, *, verify_authority: bool = True) -> RunState:
        run_id = _validate_run_id(run_id)
        try:
            root_fd = self._open_root(create=False)
        except ValueError as error:
            raise FileNotFoundError("run does not exist: %s" % run_id) from error
        matches: list[Path] = []
        try:
            for project in os.listdir(root_fd):
                if project in (".", ".."):
                    continue
                _validate_project_id(project)
                try:
                    project_fd = self._open_directory(root_fd, (project,), create=False)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                try:
                    try:
                        run_fd = self._open_directory(project_fd, (run_id,), create=False)
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    try:
                        info = os.stat("state.json", dir_fd=run_fd, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise ValueError("artifact leaf must not be a symlink")
                        if stat.S_ISREG(info.st_mode):
                            matches.append(self.root / project / run_id / "state.json")
                    except FileNotFoundError:
                        pass
                    finally:
                        os.close(run_fd)
                finally:
                    os.close(project_fd)
        finally:
            os.close(root_fd)
        if not matches:
            raise FileNotFoundError("run does not exist: %s" % run_id)
        if len(matches) != 1:
            raise ValueError("run id is ambiguous: %s" % run_id)
        try:
            return RunState.from_dict(
                strict_json_loads(self.read_artifact_bytes(matches[0]).decode("utf-8")), self.authority,
                verify_attestation=verify_authority,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("persisted run state is invalid") from error

    def load_status(self, run_id: str) -> RunState:
        """Read a structurally valid public status without opening approval authority."""
        return self.load(run_id, verify_authority=False)

    def artifact_exists(self, path: Path) -> bool:
        """Descriptor-backed regular-file existence check for workflow recovery."""
        try:
            parent, leaf = self._open_artifact_parent(path, create=False)
        except FileNotFoundError:
            return False
        try:
            try:
                info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("artifact leaf must not be a symlink")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("artifact leaf must be a regular file")
            return True
        finally:
            os.close(parent)

    def list_artifacts(self, directory: Path, suffix: str) -> list[Path]:
        """List regular artifact leaves through a pinned no-follow directory fd."""
        if not isinstance(suffix, str):
            raise ValueError("artifact suffix must be a string")
        try:
            fd = self._open_artifact_directory(directory, create=False)
        except FileNotFoundError:
            return []
        try:
            values = []
            for name in os.listdir(fd):
                if not name.endswith(suffix):
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("artifact listing contains a symlink")
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("artifact listing contains a non-regular file")
                values.append(directory / name)
            return sorted(values)
        finally:
            os.close(fd)

    def list_artifact_directories(self, directory: Path) -> list[Path]:
        """List immediate real directories through the same no-follow authority."""
        try:
            fd = self._open_artifact_directory(directory, create=False)
        except FileNotFoundError:
            return []
        try:
            values = []
            for name in os.listdir(fd):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("artifact directory listing contains a symlink")
                if stat.S_ISDIR(info.st_mode):
                    values.append(directory / name)
            return sorted(values)
        finally:
            os.close(fd)

    def read_optional_artifact_bytes(self, path: Path) -> Optional[bytes]:
        try:
            return self.read_artifact_bytes(path)
        except FileNotFoundError:
            return None
