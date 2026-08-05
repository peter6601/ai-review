"""Immutable manifests, signed human approvals, and explicit consensus state."""

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple, Union
from .process_security import run_git
from .process_security import executable_identity, resolve_executable, validate_executable_identity


VERIFICATION_POLICY = {
    "version": 3,
    "macos_sandbox": "deny-all-network-and-worktree-git-common-writes",
    "environment_keys": ["DEVELOPER_DIR", "HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"],
    "path": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
}


class Status(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    AWAITING_USER_INPUT = "AWAITING_USER_INPUT"
    AWAITING_HUMAN_PLAN_REVIEW = "AWAITING_HUMAN_PLAN_REVIEW"
    AWAITING_HUMAN_CODE_REVIEW = "AWAITING_HUMAN_CODE_REVIEW"
    AWAITING_REVIEW_APPROVAL = "AWAITING_REVIEW_APPROVAL"
    AWAITING_PREFLIGHT = "AWAITING_PREFLIGHT"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"


class Verdict(str, Enum):
    PASS = "PASS"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    CONTEXT_REQUEST = "CONTEXT_REQUEST"


def _canonical_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _plan_digest(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError("approved Plan cannot be read") from error


def _base_oid(repo_path: str, base_ref: str) -> str:
    """Freeze a symbolic base reference to one verified commit object ID."""
    try:
        completed = run_git(
            ["-C", repo_path, "rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("approval base_ref does not resolve to a commit") from error
    oid = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", oid):
        raise ValueError("approval base_ref did not resolve to one full commit object ID")
    return oid


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_answer_submission(answers: Mapping[str, str]) -> tuple[dict[str, str], str]:
    """Return the one canonical answer mapping and digest used by Task 6.

    Human answers are normalized by trimming their values and sorting their
    question identifiers before hashing.  Keeping this contract here lets both
    the workflow that creates a submission and later local consumers verify it
    without reimplementing a subtly different serialization.
    """
    if not isinstance(answers, Mapping) or not answers:
        raise ValueError("answers must be a non-empty mapping")
    if not all(
        isinstance(identifier, str) and identifier
        and isinstance(answer, str) and answer.strip()
        for identifier, answer in answers.items()
    ):
        raise ValueError("answers must map non-empty ids to non-empty strings")
    normalized = {identifier: answers[identifier].strip() for identifier in sorted(answers)}
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return normalized, digest


def canonical_user_question_ids(questions: Any) -> tuple[str, ...]:
    """Validate persisted Task 6 questions without exposing their text."""
    if not isinstance(questions, list) or not questions:
        raise ValueError("user questions must be a non-empty list")
    identifiers = []
    for item in questions:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "question"}
            or not isinstance(item["id"], str)
            or not re.fullmatch(r"Q-[0-9]{3,}", item["id"])
            or not isinstance(item["question"], str)
            or not item["question"]
        ):
            raise ValueError("persisted user questions are invalid")
        identifiers.append(item["id"])
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("persisted user question ids must be unique")
    return tuple(sorted(identifiers))


def strict_json_loads(contents: str) -> Any:
    """Decode JSON without permitting duplicate object keys at any depth."""
    if not isinstance(contents, str):
        raise ValueError("JSON contents must be text")

    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict:
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("JSON object has duplicate keys")
            value[key] = item
        return value

    return json.loads(contents, object_pairs_hook=object_without_duplicates)


def _is_utc_timestamp(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


@dataclass(frozen=True)
class VerificationCommand:
    kind: str
    argv: Tuple[str, ...]
    scope: Optional[str] = None
    executable_identity: Optional[Mapping[str, Any]] = None

    @classmethod
    def from_value(cls, value: Any) -> "VerificationCommand":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or set(value) not in (
            {"kind", "argv", "scope"},
            {"kind", "argv", "scope", "executable_identity"},
        ):
            raise ValueError("verification command requires kind, argv, and scope")
        kind, argv, scope = value["kind"], value["argv"], value["scope"]
        if kind not in ("check", "build", "test"):
            raise ValueError("verification command kind must be check, build, or test")
        if not isinstance(argv, (list, tuple)) or not argv or not all(
            isinstance(item, str) and item for item in argv
        ):
            raise ValueError("verification command argv must be non-empty strings")
        if scope is not None and (not isinstance(scope, str) or not scope.strip()):
            raise ValueError("verification command scope must be a non-empty string")
        identity = value.get("executable_identity")
        if identity is None:
            try:
                identity = (
                    executable_identity(Path(argv[0]))
                    if "/" in argv[0] else resolve_executable(argv[0])
                )
            except (OSError, ValueError):
                # Structural inspection may describe an unavailable runner;
                # execution always fails closed without this identity.
                identity = None
        if identity is not None:
            executable = validate_executable_identity(identity)
            if "/" in argv[0] and Path(argv[0]).resolve(strict=True) != Path(executable):
                raise ValueError("verification argv does not match executable identity")
            argv = [executable, *argv[1:]]
        command = cls(
            kind=kind, argv=tuple(argv), scope=scope,
            executable_identity=None if identity is None else dict(identity),
        )
        command.validate_local_only()
        if command.kind == "test":
            if not command.scope:
                raise ValueError("test verification commands require a task-specific scope")
            if not command.has_test_semantics():
                raise ValueError("test verification command does not invoke a supported test runner")
        return command

    def validate_local_only(self) -> None:
        """Permit only known local build/test entry points, never wrappers."""
        executable = Path(self.argv[0]).name.lower()
        arguments = tuple(item.lower() for item in self.argv[1:])
        allowed = False
        if executable == "xcodebuild":
            value_options = {
                "-scheme", "-workspace", "-project", "-destination",
                "-deriveddatapath", "-resultbundlepath", "-configuration", "-sdk",
            }
            positional = []
            skip_value = False
            for item in arguments:
                if skip_value:
                    skip_value = False
                    continue
                if item in value_options:
                    skip_value = True
                    continue
                if not item.startswith("-"):
                    positional.append(item)
            actions = set(positional).intersection({
                "test", "build", "build-for-testing", "test-without-building",
                "analyze", "clean",
            })
            dangerous = (
                "archive", "export", "provision", "register", "download",
                "notar", "install",
            )
            allowed = len(actions) == 1 and not any(
                any(word in item for word in dangerous) for item in arguments
            )
        elif executable == "swift":
            allowed = bool(arguments) and arguments[0] in {"test", "build", "format"}
        elif executable == "pytest":
            allowed = self.kind == "test" and bool(arguments)
        elif executable.startswith("python"):
            allowed = (
                len(arguments) >= 3
                and arguments[0] == "-m"
                and arguments[1] in {"pytest", "unittest"}
            )
        elif executable == "cargo":
            allowed = bool(arguments) and arguments[0] in {"test", "check", "build"}
        elif executable == "go":
            allowed = bool(arguments) and arguments[0] == "test"
        if not allowed:
            raise ValueError(
                "verification executable or subcommand is outside the local-only allowlist"
            )

    def has_test_semantics(self) -> bool:
        argv = [item.lower() for item in self.argv]
        executable = Path(argv[0]).name
        if executable == "pytest":
            return len(argv) > 1
        if executable in ("xcodebuild", "swift", "cargo", "go"):
            return "test" in argv[1:]
        return (
            executable.startswith("python")
            and len(argv) >= 3
            and argv[1] == "-m"
            and argv[2] in ("pytest", "unittest")
        )

    def to_dict(self) -> dict:
        value = {"kind": self.kind, "argv": list(self.argv), "scope": self.scope}
        if self.executable_identity is not None:
            value["executable_identity"] = dict(self.executable_identity)
        return value


@dataclass(frozen=True)
class RunManifest:
    """Canonical Plan inputs, inherited without replacement by Code runs."""

    kind: str
    repo_path: str
    plan_path: str
    base_ref: str
    verification_commands: Iterable[Any] = field(default_factory=tuple)
    knowledge_sources: Iterable[str] = field(default_factory=tuple)
    plan_digest: Optional[str] = None
    base_oid: Optional[str] = None
    context_checksum: Optional[str] = None
    review_executables: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("plan", "code"):
            raise ValueError("run kind must be plan or code")
        if not isinstance(self.base_ref, str) or not self.base_ref:
            raise ValueError("base_ref must be a non-empty string")
        commands = tuple(VerificationCommand.from_value(item) for item in self.verification_commands)
        sources = tuple(_canonical_path(source) for source in self.knowledge_sources)
        if self.kind == "code" and not any(
            command.kind == "test" and command.has_test_semantics()
            for command in commands
        ):
            raise ValueError("Code runs require a task-specific test command")
        object.__setattr__(self, "repo_path", _canonical_path(self.repo_path))
        object.__setattr__(self, "plan_path", _canonical_path(self.plan_path))
        object.__setattr__(self, "verification_commands", commands)
        object.__setattr__(self, "knowledge_sources", sources)
        if self.plan_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.plan_digest):
            raise ValueError("plan_digest must be SHA-256 hex")
        if self.base_oid is not None and not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.base_oid):
            raise ValueError("base_oid must be a full commit object ID")
        if self.context_checksum is not None and not re.fullmatch(r"[0-9a-f]{64}", self.context_checksum):
            raise ValueError("context_checksum must be SHA-256 hex")
        identities = dict(self.review_executables)
        if identities and set(identities) != {"codex", "claude"}:
            raise ValueError("review executable identities must bind Codex and Claude")
        for identity in identities.values():
            validate_executable_identity(identity)
        object.__setattr__(self, "review_executables", identities)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "repo_path": self.repo_path,
            "plan_path": self.plan_path,
            "base_ref": self.base_ref,
            "verification_commands": [command.to_dict() for command in self.verification_commands],
            "knowledge_sources": list(self.knowledge_sources),
            "plan_digest": self.plan_digest,
            "base_oid": self.base_oid,
            "context_checksum": self.context_checksum,
            "review_executables": self.review_executables,
        }

    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verification_digest(self) -> str:
        payload = {
            "commands": [command.to_dict() for command in self.verification_commands],
            "policy": VERIFICATION_POLICY,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def bound_for_approval(self) -> "RunManifest":
        if not self.base_oid:
            raise ValueError("Plan base commit was not frozen before review")
        return RunManifest(
            kind=self.kind, repo_path=self.repo_path, plan_path=self.plan_path, base_ref=self.base_ref,
            verification_commands=self.verification_commands, knowledge_sources=self.knowledge_sources,
            plan_digest=_plan_digest(self.plan_path), base_oid=self.base_oid,
            context_checksum=self.context_checksum,
            review_executables=self.review_executables,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "RunManifest":
        return cls(**value)


@dataclass(frozen=True)
class ReviewManifest:
    """Canonical, independently approved inputs for a direct code review."""

    kind: str
    repo_path: str
    base_ref: str
    base_oid: str
    brief: str
    brief_digest: str
    profile: str
    initial_patch_digest: str
    verification_commands: Iterable[Any]
    knowledge_sources: Iterable[str] = field(default_factory=tuple)
    context_checksum: Optional[str] = None
    review_executables: Mapping[str, Any] = field(default_factory=dict)
    risk_policy_version: str = "review-risk-v1"

    def __post_init__(self) -> None:
        if self.kind != "review":
            raise ValueError("review manifest kind must be review")
        if not isinstance(self.base_ref, str) or not self.base_ref:
            raise ValueError("base_ref must be a non-empty string")
        if not isinstance(self.brief, str):
            raise ValueError("review brief must be text")
        normalized_brief = self.brief.strip()
        if not normalized_brief or len(normalized_brief.encode("utf-8")) > 2_000:
            raise ValueError("review brief must be non-empty and at most 2,000 UTF-8 bytes")
        expected_brief_digest = hashlib.sha256(normalized_brief.encode("utf-8")).hexdigest()
        if self.brief_digest != expected_brief_digest:
            raise ValueError("review brief digest does not match the normalized brief")
        if self.profile not in ("generic", "ios"):
            raise ValueError("review profile must be generic or ios")
        if not re.fullmatch(r"[0-9a-f]{64}", self.initial_patch_digest):
            raise ValueError("initial patch digest must be SHA-256 hex")
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.base_oid):
            raise ValueError("base_oid must be a full commit object ID")
        if self.context_checksum is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.context_checksum
        ):
            raise ValueError("context_checksum must be SHA-256 hex")
        if (
            not isinstance(self.risk_policy_version, str)
            or not self.risk_policy_version.strip()
            or self.risk_policy_version != self.risk_policy_version.strip()
            or len(self.risk_policy_version.encode("utf-8")) > 128
        ):
            raise ValueError("risk policy version must be a non-empty bounded string")

        commands = tuple(VerificationCommand.from_value(item) for item in self.verification_commands)
        if any(command.executable_identity is None for command in commands):
            raise ValueError("Review verification commands require executable identities")
        if not any(
            command.kind == "test" and command.scope and command.has_test_semantics()
            for command in commands
        ):
            raise ValueError("Review runs require a task-specific test command")
        sources = tuple(_canonical_path(source) for source in self.knowledge_sources)
        identities = dict(self.review_executables)
        if set(identities) != {"codex", "claude"}:
            raise ValueError("review executable identities must bind Codex and Claude")
        for identity in identities.values():
            validate_executable_identity(identity)

        object.__setattr__(self, "repo_path", _canonical_path(self.repo_path))
        object.__setattr__(self, "brief", normalized_brief)
        object.__setattr__(self, "verification_commands", commands)
        object.__setattr__(self, "knowledge_sources", sources)
        object.__setattr__(self, "review_executables", identities)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "repo_path": self.repo_path,
            "base_ref": self.base_ref,
            "base_oid": self.base_oid,
            "brief": self.brief,
            "brief_digest": self.brief_digest,
            "profile": self.profile,
            "initial_patch_digest": self.initial_patch_digest,
            "verification_commands": [command.to_dict() for command in self.verification_commands],
            "knowledge_sources": list(self.knowledge_sources),
            "context_checksum": self.context_checksum,
            "review_executables": self.review_executables,
            "risk_policy_version": self.risk_policy_version,
        }

    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def verification_digest(self) -> str:
        payload = {
            "commands": [command.to_dict() for command in self.verification_commands],
            "policy": VERIFICATION_POLICY,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_dict(cls, value: dict) -> "ReviewManifest":
        if not isinstance(value, dict):
            raise ValueError("review manifest must be an object")
        expected = {
            "kind", "repo_path", "base_ref", "base_oid", "brief", "brief_digest",
            "profile", "initial_patch_digest", "verification_commands", "knowledge_sources",
            "context_checksum", "review_executables", "risk_policy_version",
        }
        if set(value) != expected:
            raise ValueError("review manifest fields are invalid")
        return cls(**value)


class ApprovalAuthority:
    """Central HMAC authority whose key is never stored in run artifacts."""

    def __init__(self, key_path: Path):
        requested = Path(os.path.abspath(str(Path(key_path).expanduser())))
        # macOS exposes /var as the fixed system alias /private/var. Normalize
        # only that platform alias; every caller-controlled parent remains
        # unresolved so the dirfd/O_NOFOLLOW walk can reject it.
        if requested.parts[:2] == ("/", "var"):
            requested = Path("/private").joinpath(*requested.parts[1:])
        self.key_path = requested
        self._key = self._load_or_create_key()

    @classmethod
    def for_workspace(cls, workspace: Path) -> "ApprovalAuthority":
        return cls(Path(workspace).expanduser().resolve() / ".ai-review" / "approval.key")

    def _load_or_create_key(self) -> bytes:
        parent_fd = self._open_parent_directory()
        try:
            try:
                descriptor = os.open(
                    self.key_path.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
                created = True
            except FileExistsError:
                try:
                    descriptor = os.open(
                        self.key_path.name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_fd,
                    )
                except OSError as error:
                    raise ValueError("approval authority key must not be a symlink") from error
                created = False
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or metadata.st_nlink != 1
                ):
                    raise ValueError("approval authority key must be an owned unlinked regular file")
                if created:
                    os.fchmod(descriptor, 0o600)
                    key = os.urandom(32)
                    if os.write(descriptor, key) != len(key):
                        raise OSError("short approval key write")
                    os.fsync(descriptor)
                if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                    raise ValueError("approval authority key must have mode 0600")
                os.lseek(descriptor, 0, os.SEEK_SET)
                key = os.read(descriptor, 33)
                if len(key) != 32:
                    raise ValueError("approval authority key must be exactly 32 bytes")
                return key
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    def _open_parent_directory(self) -> int:
        """Walk/create every parent using dirfds and reject symlink traversal."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open("/", flags)
        try:
            for component in self.key_path.parent.parts[1:]:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _payload(
        actor: str, source: str, approved_at: str, manifest_digest: str, nonce: str, receipt_digest: str,
    ) -> bytes:
        return json.dumps(
            [actor, source, approved_at, manifest_digest, nonce, receipt_digest],
            separators=(",", ":"),
        ).encode("utf-8")

    def sign(
        self, actor: str, source: str, approved_at: str, manifest_digest: str, nonce: str, receipt_digest: str
    ) -> str:
        return hmac.new(
            self._key,
            self._payload(actor, source, approved_at, manifest_digest, nonce, receipt_digest),
            hashlib.sha256,
        ).hexdigest()

    def verifies(
        self, attestation: Union["ApprovalAttestation", "ReviewApprovalAttestation"]
    ) -> bool:
        if not isinstance(attestation, (ApprovalAttestation, ReviewApprovalAttestation)):
            return False
        try:
            expected = self.sign(
                attestation.actor,
                attestation.source,
                attestation.approved_at,
                attestation.manifest_digest,
                attestation.nonce,
                attestation.receipt_digest,
            )
            return hmac.compare_digest(expected, attestation.signature)
        except (AttributeError, TypeError, ValueError):
            return False


@dataclass(frozen=True)
class HumanApprovalReceipt:
    """A user-presence provider's non-forgeable approval result for one run."""

    run_id: str
    plan_digest: str
    base_oid: str
    approved_at: str
    provider: str
    actor: str
    verification_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id or "/" in self.run_id or "\\" in self.run_id:
            raise ValueError("approval receipt run id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.plan_digest):
            raise ValueError("approval receipt Plan digest is invalid")
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.base_oid):
            raise ValueError("approval receipt base OID is invalid")
        if not _is_utc_timestamp(self.approved_at):
            raise ValueError("approval receipt timestamp must be UTC")
        if not isinstance(self.provider, str) or not self.provider or len(self.provider) > 128:
            raise ValueError("approval receipt provider is invalid")
        if not isinstance(self.actor, str) or not self.actor or len(self.actor) > 128:
            raise ValueError("approval receipt actor is invalid")
        if (
            self.verification_digest is not None
            and not re.fullmatch(r"[0-9a-f]{64}", self.verification_digest)
        ):
            raise ValueError("approval receipt verification digest is invalid")

    def digest(self) -> str:
        payload = {
            "actor": self.actor, "approved_at": self.approved_at, "base_oid": self.base_oid,
            "plan_digest": self.plan_digest, "provider": self.provider, "run_id": self.run_id,
            "verification_digest": self.verification_digest,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "plan_digest": self.plan_digest, "base_oid": self.base_oid,
            "approved_at": self.approved_at, "provider": self.provider, "actor": self.actor,
            "verification_digest": self.verification_digest,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "HumanApprovalReceipt":
        return cls(**value)


@dataclass(frozen=True)
class ReviewApprovalReceipt:
    """A user-presence provider's approval of one direct-review manifest."""

    run_id: str
    manifest_digest: str
    approved_at: str
    provider: str
    actor: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, str) or not self.run_id
            or self.run_id in (".", "..") or "\x00" in self.run_id
            or "/" in self.run_id or "\\" in self.run_id or len(self.run_id) > 128
        ):
            raise ValueError("review approval receipt run id is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_digest):
            raise ValueError("review approval receipt manifest digest is invalid")
        if not _is_utc_timestamp(self.approved_at):
            raise ValueError("review approval receipt timestamp must be UTC")
        if (
            not isinstance(self.provider, str) or not self.provider.strip()
            or self.provider != self.provider.strip() or len(self.provider) > 128
        ):
            raise ValueError("review approval receipt provider is invalid")
        if (
            not isinstance(self.actor, str) or not self.actor.strip()
            or self.actor != self.actor.strip() or len(self.actor) > 128
        ):
            raise ValueError("review approval receipt actor is invalid")

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "manifest_digest": self.manifest_digest,
            "approved_at": self.approved_at,
            "provider": self.provider,
            "actor": self.actor,
        }

    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: dict) -> "ReviewApprovalReceipt":
        if not isinstance(value, dict) or set(value) != {
            "run_id", "manifest_digest", "approved_at", "provider", "actor",
        }:
            raise ValueError("review approval receipt fields are invalid")
        return cls(**value)


@dataclass(frozen=True)
class ApprovalAttestation:
    actor: str
    source: str
    approved_at: str
    manifest_digest: str
    nonce: str
    receipt: HumanApprovalReceipt
    receipt_digest: str
    signature: str

    @classmethod
    def create(
        cls, manifest: RunManifest, receipt: HumanApprovalReceipt, authority: ApprovalAuthority
    ) -> "ApprovalAttestation":
        if not manifest.plan_digest or not manifest.base_oid:
            raise ValueError("approval requires a bound Plan digest and frozen base")
        if receipt.plan_digest != manifest.plan_digest or receipt.base_oid != manifest.base_oid:
            raise ValueError("approval receipt does not match the reviewed Plan")
        if (
            receipt.verification_digest is not None
            and receipt.verification_digest != manifest.verification_digest()
        ):
            raise ValueError("approval receipt does not match verification commands")
        approved_at = receipt.approved_at
        digest = manifest.digest()
        nonce = secrets.token_hex(16)
        receipt_digest = receipt.digest()
        return cls(
            actor=receipt.actor,
            source=receipt.provider,
            approved_at=approved_at,
            manifest_digest=digest,
            nonce=nonce,
            receipt=receipt,
            receipt_digest=receipt_digest,
            signature=authority.sign(receipt.actor, receipt.provider, approved_at, digest, nonce, receipt_digest),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.actor, str) or not self.actor:
            raise ValueError("approval actor must come from a user-presence provider")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("approval source must come from a user-presence provider")
        if not _is_utc_timestamp(self.approved_at):
            raise ValueError("approval timestamp must be UTC")
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_digest):
            raise ValueError("approval manifest digest must be SHA-256 hex")
        if not re.fullmatch(r"[0-9a-f]{32}", self.nonce):
            raise ValueError("approval nonce must be 16 random bytes of hex")
        if not re.fullmatch(r"[0-9a-f]{64}", self.receipt_digest):
            raise ValueError("approval receipt digest must be SHA-256 hex")
        if self.receipt.digest() != self.receipt_digest:
            raise ValueError("approval receipt digest does not match receipt")
        if (
            self.receipt.actor != self.actor or self.receipt.provider != self.source
            or self.receipt.approved_at != self.approved_at
        ):
            raise ValueError("approval receipt provenance does not match attestation")
        if not re.fullmatch(r"[0-9a-f]{64}", self.signature):
            raise ValueError("approval signature must be HMAC-SHA256 hex")

    def to_dict(self) -> dict:
        return {
            "actor": self.actor,
            "source": self.source,
            "approved_at": self.approved_at,
            "manifest_digest": self.manifest_digest,
            "nonce": self.nonce,
            "receipt": self.receipt.to_dict(),
            "receipt_digest": self.receipt_digest,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ApprovalAttestation":
        receipt = value.get("receipt")
        if not isinstance(receipt, dict):
            raise ValueError("approval receipt is invalid")
        return cls(**dict(value, receipt=HumanApprovalReceipt.from_dict(receipt)))


@dataclass(frozen=True)
class ReviewApprovalAttestation:
    actor: str
    source: str
    approved_at: str
    manifest_digest: str
    nonce: str
    receipt: ReviewApprovalReceipt
    receipt_digest: str
    signature: str

    @classmethod
    def create(
        cls,
        manifest: ReviewManifest,
        receipt: ReviewApprovalReceipt,
        authority: ApprovalAuthority,
    ) -> "ReviewApprovalAttestation":
        if not isinstance(manifest, ReviewManifest) or manifest.kind != "review":
            raise ValueError("review approval requires a Review manifest")
        manifest_digest = manifest.digest()
        if receipt.manifest_digest != manifest_digest:
            raise ValueError("review approval receipt does not match the reviewed manifest")
        nonce = secrets.token_hex(16)
        receipt_digest = receipt.digest()
        return cls(
            actor=receipt.actor,
            source=receipt.provider,
            approved_at=receipt.approved_at,
            manifest_digest=manifest_digest,
            nonce=nonce,
            receipt=receipt,
            receipt_digest=receipt_digest,
            signature=authority.sign(
                receipt.actor, receipt.provider, receipt.approved_at,
                manifest_digest, nonce, receipt_digest,
            ),
        )

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ReviewApprovalReceipt):
            raise ValueError("review approval receipt is invalid")
        if not isinstance(self.actor, str) or not self.actor:
            raise ValueError("review approval actor must come from a user-presence provider")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("review approval source must come from a user-presence provider")
        if not _is_utc_timestamp(self.approved_at):
            raise ValueError("review approval timestamp must be UTC")
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_digest):
            raise ValueError("review approval manifest digest must be SHA-256 hex")
        if not re.fullmatch(r"[0-9a-f]{32}", self.nonce):
            raise ValueError("review approval nonce must be 16 random bytes of hex")
        if not re.fullmatch(r"[0-9a-f]{64}", self.receipt_digest):
            raise ValueError("review approval receipt digest must be SHA-256 hex")
        if self.receipt.digest() != self.receipt_digest:
            raise ValueError("review approval receipt digest does not match receipt")
        if self.receipt.manifest_digest != self.manifest_digest:
            raise ValueError("review approval receipt manifest does not match attestation")
        if (
            self.receipt.actor != self.actor
            or self.receipt.provider != self.source
            or self.receipt.approved_at != self.approved_at
        ):
            raise ValueError("review approval receipt provenance does not match attestation")
        if not re.fullmatch(r"[0-9a-f]{64}", self.signature):
            raise ValueError("review approval signature must be HMAC-SHA256 hex")

    def to_dict(self) -> dict:
        return {
            "actor": self.actor,
            "source": self.source,
            "approved_at": self.approved_at,
            "manifest_digest": self.manifest_digest,
            "nonce": self.nonce,
            "receipt": self.receipt.to_dict(),
            "receipt_digest": self.receipt_digest,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ReviewApprovalAttestation":
        expected = {
            "actor", "source", "approved_at", "manifest_digest", "nonce",
            "receipt", "receipt_digest", "signature",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("review approval attestation fields are invalid")
        receipt = value["receipt"]
        return cls(**dict(value, receipt=ReviewApprovalReceipt.from_dict(receipt)))


@dataclass(frozen=True)
class RiskApprovalReceipt:
    """A user-presence approval of one high-risk Review patch, by digest.

    This is a per-round artifact rather than run state: it authorizes exactly one
    scope growth and becomes worthless the moment the worktree changes again.
    """

    run_id: str
    manifest_digest: str
    patch_digest: str
    categories: Tuple[str, ...]
    approved_at: str
    provider: str
    actor: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.run_id, str) or not self.run_id
            or self.run_id in (".", "..") or "\x00" in self.run_id
            or "/" in self.run_id or "\\" in self.run_id or len(self.run_id) > 128
        ):
            raise ValueError("risk approval receipt run id is invalid")
        for name in ("manifest_digest", "patch_digest"):
            if not re.fullmatch(r"[0-9a-f]{64}", getattr(self, name)):
                raise ValueError("risk approval receipt %s is invalid" % name)
        if not _is_utc_timestamp(self.approved_at):
            raise ValueError("risk approval receipt timestamp must be UTC")
        for name in ("provider", "actor"):
            value = getattr(self, name)
            if (
                not isinstance(value, str) or not value.strip()
                or value != value.strip() or len(value) > 128
            ):
                raise ValueError("risk approval receipt %s is invalid" % name)
        categories = tuple(self.categories)
        if not categories or not all(
            isinstance(item, str) and item.strip() and len(item) <= 64 for item in categories
        ):
            raise ValueError("risk approval receipt requires bounded categories")
        if list(categories) != sorted(set(categories)):
            raise ValueError("risk approval receipt categories must be sorted and unique")
        object.__setattr__(self, "categories", categories)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "manifest_digest": self.manifest_digest,
            "patch_digest": self.patch_digest,
            "categories": list(self.categories),
            "approved_at": self.approved_at,
            "provider": self.provider,
            "actor": self.actor,
        }

    def digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Any) -> "RiskApprovalReceipt":
        expected = {
            "run_id", "manifest_digest", "patch_digest", "categories",
            "approved_at", "provider", "actor",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("risk approval receipt fields are invalid")
        return cls(**dict(value, categories=tuple(value["categories"])))


def sign_risk_approval(
    receipt: RiskApprovalReceipt, authority: ApprovalAuthority
) -> dict:
    """Return the persistable, HMAC-signed record for one risk approval."""
    if not isinstance(receipt, RiskApprovalReceipt):
        raise ValueError("risk approval requires a risk approval receipt")
    nonce = secrets.token_hex(16)
    receipt_digest = receipt.digest()
    return {
        "receipt": receipt.to_dict(),
        "receipt_digest": receipt_digest,
        "nonce": nonce,
        "signature": authority.sign(
            receipt.actor, receipt.provider, receipt.approved_at,
            receipt.manifest_digest, nonce, receipt_digest,
        ),
    }


def verify_risk_approval(
    record: Any,
    authority: ApprovalAuthority,
    *,
    run_id: str,
    manifest_digest: str,
    patch_digest: str,
) -> RiskApprovalReceipt:
    """Accept a risk approval only for this run, manifest, and exact patch."""
    if not isinstance(record, dict) or set(record) != {
        "receipt", "receipt_digest", "nonce", "signature",
    }:
        raise ValueError("risk approval record is invalid")
    receipt = RiskApprovalReceipt.from_dict(record["receipt"])
    if not re.fullmatch(r"[0-9a-f]{32}", record["nonce"] if isinstance(record["nonce"], str) else ""):
        raise ValueError("risk approval nonce must be 16 random bytes of hex")
    if not isinstance(record["receipt_digest"], str) or not hmac.compare_digest(
        receipt.digest(), record["receipt_digest"]
    ):
        raise ValueError("risk approval receipt digest does not match receipt")
    if not isinstance(record["signature"], str) or not hmac.compare_digest(
        authority.sign(
            receipt.actor, receipt.provider, receipt.approved_at,
            receipt.manifest_digest, record["nonce"], record["receipt_digest"],
        ),
        record["signature"],
    ):
        raise ValueError("risk approval signature is invalid")
    if receipt.run_id != run_id:
        raise ValueError("risk approval does not match this run")
    if not hmac.compare_digest(receipt.manifest_digest, manifest_digest):
        raise ValueError("risk approval does not match the approved Review manifest")
    if not hmac.compare_digest(receipt.patch_digest, patch_digest):
        raise ValueError("risk approval does not match the current patch")
    return receipt


_CODE_FACTORY_TOKEN = object()
_REVIEW_FACTORY_TOKEN = object()


@dataclass(frozen=True)
class RunState:
    kind: str
    manifest: Optional[Union[RunManifest, ReviewManifest]] = None
    run_id: Optional[str] = None
    status: Status = Status.READY
    repair_round: int = 0
    approval_attestation: Optional[Union[ApprovalAttestation, ReviewApprovalAttestation]] = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    _code_factory_token: Any = field(default=None, repr=False, compare=False)
    _review_factory_token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in ("plan", "code", "review"):
            raise ValueError("run kind must be plan, code, or review")
        if self.kind == "plan" and self.manifest is not None and (
            not isinstance(self.manifest, RunManifest) or self.manifest.kind != "plan"
        ):
            raise ValueError("Plan state requires a Plan manifest")
        if self.kind == "code":
            if self._code_factory_token is not _CODE_FACTORY_TOKEN:
                raise ValueError("Code state must be created from an approved Plan")
            if self.manifest is None or self.manifest.kind != "plan":
                raise ValueError("Code state must inherit an approved Plan manifest")
        if self.kind == "review":
            if self._review_factory_token is not _REVIEW_FACTORY_TOKEN:
                raise ValueError("Review state must be created with new_review")
            if not isinstance(self.manifest, ReviewManifest):
                raise ValueError("Review state requires a Review manifest")
            if self.approval_attestation is not None and not isinstance(
                self.approval_attestation, ReviewApprovalAttestation
            ):
                raise ValueError("Review state cannot carry a Plan approval attestation")

    @property
    def plan_path(self) -> str:
        if not isinstance(self.manifest, RunManifest):
            raise ValueError("run state has no Plan manifest")
        return self.manifest.plan_path

    @property
    def base_ref(self) -> str:
        if self.manifest is None:
            raise ValueError("run state has no manifest")
        return self.manifest.base_ref

    @property
    def human_approved_at(self) -> Optional[str]:
        return self.approval_attestation.approved_at if self.approval_attestation else None

    @classmethod
    def new(
        cls,
        kind: str,
        plan_path: str,
        repo_path_or_base_ref: str,
        base_ref: Optional[str] = None,
        verification_commands: Iterable[Any] = (),
        knowledge_sources: Iterable[str] = (),
        base_oid: Optional[str] = None,
        context_checksum: Optional[str] = None,
        review_executables: Mapping[str, Any] = {},
    ) -> "RunState":
        if kind == "code":
            raise ValueError("Code runs must be created from an approved Plan")
        if kind != "plan":
            raise ValueError("run kind must be plan or code; use new_review for Review runs")
        if base_ref is None:
            return cls(kind="plan")
        if base_oid is None:
            try:
                base_oid = _base_oid(repo_path_or_base_ref, base_ref)
            except ValueError:
                # Legacy non-persisted state objects remain usable for tests,
                # but cannot later cross the approval boundary.
                base_oid = None
        manifest = RunManifest(
            kind="plan",
            repo_path=repo_path_or_base_ref,
            plan_path=plan_path,
            base_ref=base_ref,
            verification_commands=verification_commands,
            knowledge_sources=knowledge_sources,
            base_oid=base_oid,
            context_checksum=context_checksum,
            review_executables=review_executables,
        )
        return cls(kind="plan", manifest=manifest)

    @classmethod
    def new_review(cls, manifest: ReviewManifest) -> "RunState":
        if not isinstance(manifest, ReviewManifest):
            raise ValueError("Review initialization requires a Review manifest")
        return cls(
            kind="review",
            manifest=manifest,
            status=Status.AWAITING_REVIEW_APPROVAL,
            _review_factory_token=_REVIEW_FACTORY_TOKEN,
        )

    @classmethod
    def new_code_from_approved_plan(
        cls, plan_run: "RunState", authority: ApprovalAuthority
    ) -> "RunState":
        plan_run._validate_approval_for_code(authority)
        return cls(
            kind="code",
            manifest=plan_run.manifest,
            approval_attestation=plan_run.approval_attestation,
            _code_factory_token=_CODE_FACTORY_TOKEN,
        )

    def _validate_approval_for_code(self, authority: ApprovalAuthority) -> None:
        if self.kind != "plan":
            raise ValueError("Code initialization requires a Plan run")
        if self.status != Status.AWAITING_HUMAN_PLAN_REVIEW:
            raise ValueError("Plan is not awaiting human review")
        if self.manifest is None or self.manifest.kind != "plan":
            raise ValueError("Plan run has no approved Plan manifest")
        if not self.manifest.plan_digest or not self.manifest.base_oid:
            raise ValueError("legacy approval lacks a signed Plan digest and base commit")
        if _plan_digest(self.manifest.plan_path) != self.manifest.plan_digest:
            raise ValueError("approved Plan bytes have changed")
        if _base_oid(self.manifest.repo_path, self.manifest.base_oid) != self.manifest.base_oid:
            raise ValueError("approved base commit is unavailable")
        if self.approval_attestation is None:
            raise ValueError("Plan is not human-approved")
        if self.approval_attestation.manifest_digest != self.manifest.digest():
            raise ValueError("Plan approval does not match the current manifest")
        if not authority.verifies(self.approval_attestation):
            raise ValueError("Plan approval signature is invalid")
        if not any(
            command.kind == "test" and command.has_test_semantics()
            for command in self.manifest.verification_commands
        ):
            raise ValueError("Code runs require a task-specific test command")

    def transition(self, verdict: Verdict, max_rounds: int = 6) -> None:
        if not isinstance(verdict, Verdict):
            verdict = Verdict(verdict)
        if self.status in (
            Status.AWAITING_HUMAN_PLAN_REVIEW,
            Status.AWAITING_HUMAN_CODE_REVIEW,
            Status.AWAITING_REVIEW_APPROVAL,
        ):
            raise ValueError("human review requires an explicit approval command")
        if self.status in (Status.PAUSED, Status.INTERRUPTED):
            raise ValueError("paused or interrupted runs cannot transition")
        if max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if verdict == Verdict.PASS:
            status = Status.AWAITING_HUMAN_PLAN_REVIEW if self.kind == "plan" else Status.AWAITING_HUMAN_CODE_REVIEW
            object.__setattr__(self, "status", status)
        elif verdict == Verdict.CHANGES_REQUIRED:
            repair_round = self.repair_round + 1
            object.__setattr__(self, "repair_round", repair_round)
            object.__setattr__(self, "status", Status.PAUSED if repair_round >= max_rounds else Status.RUNNING)
        elif verdict == Verdict.NEEDS_USER_INPUT:
            object.__setattr__(self, "status", Status.AWAITING_USER_INPUT)
        else:
            object.__setattr__(self, "status", Status.RUNNING)
        object.__setattr__(self, "updated_at", _utc_now())

    def approve_plan(self, *, receipt: HumanApprovalReceipt, authority: ApprovalAuthority) -> None:
        if self.kind != "plan" or self.status != Status.AWAITING_HUMAN_PLAN_REVIEW:
            raise ValueError("only a Plan awaiting human review can be approved")
        if self.manifest is None:
            raise ValueError("Plan approval requires a manifest")
        if self.approval_attestation is None:
            bound = self.manifest.bound_for_approval()
            if self.run_id is not None and receipt.run_id != self.run_id:
                raise ValueError("approval receipt does not match this run")
            object.__setattr__(self, "manifest", bound)
            attestation = ApprovalAttestation.create(bound, receipt, authority)
            object.__setattr__(self, "approval_attestation", attestation)
            object.__setattr__(self, "updated_at", attestation.approved_at)

    def approve_review(
        self, *, receipt: ReviewApprovalReceipt, authority: ApprovalAuthority
    ) -> None:
        if self.kind != "review" or self.status != Status.AWAITING_REVIEW_APPROVAL:
            raise ValueError("only a Review awaiting approval can be approved")
        if not isinstance(self.manifest, ReviewManifest):
            raise ValueError("Review approval requires a Review manifest")
        if self.approval_attestation is not None:
            raise ValueError("Review is already approved")
        if self.run_id is None or receipt.run_id != self.run_id:
            raise ValueError("review approval receipt does not match this run")
        if receipt.manifest_digest != self.manifest.digest():
            raise ValueError("review approval receipt does not match this manifest")
        attestation = ReviewApprovalAttestation.create(self.manifest, receipt, authority)
        object.__setattr__(self, "approval_attestation", attestation)
        object.__setattr__(self, "status", Status.READY)
        object.__setattr__(self, "updated_at", attestation.approved_at)

    def validate(self, authority: Optional[ApprovalAuthority], *, verify_attestation: bool = True) -> None:
        if type(self.repair_round) is not int or self.repair_round < 0:
            raise ValueError("repair_round must be a non-negative integer")
        if self.kind in ("plan", "code") and self.approval_attestation is not None:
            if not isinstance(self.approval_attestation, ApprovalAttestation):
                raise ValueError("Plan and Code states require a Plan approval attestation")
            if self.manifest is None:
                raise ValueError("approval attestation requires a manifest")
            if self.approval_attestation.manifest_digest != self.manifest.digest():
                raise ValueError("approval attestation has a stale or forged manifest digest")
            receipt = self.approval_attestation.receipt
            if receipt.plan_digest != self.manifest.plan_digest or receipt.base_oid != self.manifest.base_oid:
                raise ValueError("approval receipt does not match the signed Plan binding")
            if self.kind == "plan" and self.run_id is not None and receipt.run_id != self.run_id:
                raise ValueError("approval receipt does not match the persisted run")
            if verify_attestation and (authority is None or not authority.verifies(self.approval_attestation)):
                raise ValueError("approval attestation signature is invalid")
            if self.kind == "plan" and self.status != Status.AWAITING_HUMAN_PLAN_REVIEW:
                raise ValueError("Plan approval attestation requires human Plan review state")
        if self.kind == "review":
            if not isinstance(self.manifest, ReviewManifest):
                raise ValueError("Review state requires a Review manifest")
            if self.approval_attestation is None:
                if self.status != Status.AWAITING_REVIEW_APPROVAL:
                    raise ValueError("unapproved Review must await Review approval")
            else:
                if not isinstance(self.approval_attestation, ReviewApprovalAttestation):
                    raise ValueError("Review state requires a Review approval attestation")
                if self.status == Status.AWAITING_REVIEW_APPROVAL:
                    raise ValueError("approved Review cannot await Review approval")
                if self.approval_attestation.manifest_digest != self.manifest.digest():
                    raise ValueError("review approval has a stale or forged manifest digest")
                receipt = self.approval_attestation.receipt
                if receipt.manifest_digest != self.manifest.digest():
                    raise ValueError("review receipt does not match the signed manifest")
                if self.run_id is None or receipt.run_id != self.run_id:
                    raise ValueError("review approval receipt does not match the persisted run")
                if verify_attestation and (
                    authority is None or not authority.verifies(self.approval_attestation)
                ):
                    raise ValueError("review approval attestation signature is invalid")
        if self.kind == "code":
            if self.approval_attestation is None:
                raise ValueError("Code state requires a human Plan approval")
            if self.manifest is None or self.manifest.kind != "plan":
                raise ValueError("Code state must inherit an approved Plan manifest")
            if not self.manifest.plan_digest or not self.manifest.base_oid:
                raise ValueError("Code state requires a signed Plan digest and base commit")
            if not any(
                command.kind == "test" and command.has_test_semantics()
                for command in self.manifest.verification_commands
            ):
                raise ValueError("Code runs require a task-specific test command")

    def validate_code_binding(self) -> None:
        """Recheck immutable human-approved bytes and base before model use."""
        if self.kind != "code" or self.manifest is None:
            raise ValueError("Code binding requires a Code state")
        if _plan_digest(self.manifest.plan_path) != self.manifest.plan_digest:
            raise ValueError("approved Plan bytes have changed")
        if _base_oid(self.manifest.repo_path, self.manifest.base_oid) != self.manifest.base_oid:
            raise ValueError("approved base commit is unavailable")

    def validate_review_binding(
        self, current_patch_digest: str, current_base_oid: str
    ) -> None:
        """Compare recaptured repository state with the approved Review inputs."""
        if self.kind != "review" or not isinstance(self.manifest, ReviewManifest):
            raise ValueError("Review binding requires a Review state")
        if not isinstance(self.approval_attestation, ReviewApprovalAttestation):
            raise ValueError("Review binding requires an approved Review manifest")
        if self.approval_attestation.manifest_digest != self.manifest.digest():
            raise ValueError("Review approval does not match the current manifest")
        if current_patch_digest != self.manifest.initial_patch_digest:
            raise ValueError("current patch does not match the approved Review patch")
        if current_base_oid != self.manifest.base_oid:
            raise ValueError("current base does not match the approved Review base")

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "status": self.status.value,
            "repair_round": self.repair_round,
            "approval_attestation": self.approval_attestation.to_dict() if self.approval_attestation else None,
            "human_approved_at": self.human_approved_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(
        cls, value: dict, authority: Optional[ApprovalAuthority] = None, *, verify_attestation: bool = True,
    ) -> "RunState":
        manifest_value = value.get("manifest")
        attestation_value = value.get("approval_attestation")
        kind = value["kind"]
        if kind == "review":
            manifest = ReviewManifest.from_dict(manifest_value) if manifest_value else None
            attestation = (
                ReviewApprovalAttestation.from_dict(attestation_value)
                if attestation_value else None
            )
        else:
            manifest = RunManifest.from_dict(manifest_value) if manifest_value else None
            attestation = (
                ApprovalAttestation.from_dict(attestation_value) if attestation_value else None
            )
        state = cls(
            run_id=value.get("run_id"),
            kind=kind,
            manifest=manifest,
            status=Status(value["status"]),
            repair_round=value["repair_round"],
            approval_attestation=attestation,
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            _code_factory_token=_CODE_FACTORY_TOKEN if kind == "code" else None,
            _review_factory_token=_REVIEW_FACTORY_TOKEN if kind == "review" else None,
        )
        if value.get("human_approved_at") != state.human_approved_at:
            raise ValueError("human approval timestamp does not match its attestation")
        state.validate(authority, verify_attestation=verify_attestation)
        return state
