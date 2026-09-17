"""Safe, structured process boundaries for local consensus reviews."""

import hashlib
import ctypes
import errno
import json
import math
import numbers
import os
import platform
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from .process_security import controlled_env, run_git


class RunnerInterrupted(RuntimeError):
    """A bounded external process did not finish before its deadline."""


class ExternalEnvironmentError(RunnerInterrupted):
    """An external call failed for a reason outside this run's control.

    Overloaded, out of budget at the org level, not logged in, or unable to
    start: none of these say anything about the work in progress, so the run
    is interrupted and resumable rather than void.  It subclasses
    ``RunnerInterrupted`` so every existing catch site treats it the same way
    a timeout is treated.

    Deliberately *not* in this class: a model that returned data failing its
    schema, and a call stopped by this tool's own spend ceiling.  Both are
    statements about the work, and both stay terminal.
    """


class RunnerError(RuntimeError):
    """A runner could not be started or returned an invalid structured result."""


# A real suite, not a smoke test: a cold simulator boot plus an incremental
# xcodebuild test run passes 300s routinely, and a timeout there is reported as
# INTERRUPTED rather than as a verification failure.
DEFAULT_VERIFICATION_TIMEOUT = 1800.0


def _already_restricted_by_seatbelt(git_directories: Iterable[Path]) -> bool:
    """Recognize an outer macOS sandbox that already denies network and Git writes."""
    try:
        check = ctypes.CDLL(
            "/usr/lib/system/libsystem_sandbox.dylib"
        ).sandbox_check
        check.restype = ctypes.c_int
        if (
            check(os.getpid(), b"network-outbound", 0) != 1
            or check(os.getpid(), b"network-inbound", 0) != 1
        ):
            return False
        for directory in git_directories:
            target = directory / "HEAD" if directory.is_dir() else directory
            try:
                descriptor = os.open(str(target), os.O_WRONLY)
            except OSError as error:
                if error.errno not in (errno.EPERM, errno.EACCES, errno.EROFS):
                    return False
            else:
                os.close(descriptor)
                return False
        return True
    except (AttributeError, OSError):
        return False


def _argv(values: Iterable[str]) -> list[str]:
    argv = list(values)
    if not argv or not all(isinstance(value, str) and value for value in argv):
        raise ValueError("argv must contain non-empty strings")
    return argv


def build_codex_argv(
    repo: Path, schema: Path, output: Path, prompt: str, *, model: str,
    executable: str = "codex",
) -> list[str]:
    """Build a read-only Codex request; prompt text remains one argv item.

    ``model`` is deliberately required and never defaulted.  Without ``-m``
    the model comes from ``~/.codex/config.toml`` -- a file this tool does not
    control -- so an edit there silently broke every run of every kind, and
    the operator saw only ``EXTERNAL_EXIT exit=1``.  A default here would
    recreate exactly that: a caller who forgets would inherit the global
    config again instead of failing loudly.  The position is the one the
    installed CLI parses: ``codex -a never exec -m <model> -C <repo> ...``.
    """
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    return _argv([
        executable, "-a", "never", "exec", "-m", model,
        "-C", str(Path(repo).resolve()), "-s", "read-only",
        "--output-schema", str(Path(schema).resolve()),
        "-o", str(Path(output).resolve()), prompt,
    ])


def build_claude_argv(
    schema_json: str, prompt: str, *, model: str, fallback_model: str,
    max_budget_usd: int, mode: str = "code", executable: str = "claude",
) -> list[str]:
    """Build a Claude request with an enforceable, Bash-free tool boundary.

    ``model``, ``fallback_model`` and ``max_budget_usd`` are required and never
    defaulted, for the reason ``build_codex_argv`` spells out: without ``--model``
    the repair inherits ``~/.claude/settings.json``, so an unrelated edit there
    silently changes what every repair runs on.  That inheritance is how one
    repair ran 43 minutes at a 1M context and then failed five schema
    validations in a row.  ``--fallback-model`` keeps an overloaded primary from
    killing the run, and ``--max-budget-usd`` is the ceiling that a smaller
    context used to provide implicitly.
    """
    if not isinstance(schema_json, str) or not schema_json:
        raise ValueError("schema_json must be a non-empty string")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a non-empty string")
    if mode not in ("plan", "code", "review"):
        raise ValueError("Claude mode must be plan, code, or review")
    for name, value in (("model", model), ("fallback model", fallback_model)):
        if not isinstance(value, str) or not value:
            raise ValueError("Claude %s must be a non-empty string" % name)
    if type(max_budget_usd) is not int or max_budget_usd < 1:
        raise ValueError("Claude budget must be a positive whole number of dollars")
    # Review repairs edit existing code, so they need exactly the Code-mode
    # boundary: file tools without Bash, and no permission bypass.
    tools = "Read,Glob,Grep" if mode == "plan" else "Read,Glob,Grep,Edit,Write"
    permission = "dontAsk" if mode == "plan" else "acceptEdits"
    return _argv([
        executable, "-p", "--safe-mode", "--permission-mode", permission,
        "--model", model, "--fallback-model", fallback_model,
        "--max-budget-usd", str(max_budget_usd),
        "--tools", tools, "--json-schema", schema_json, prompt,
    ])


@dataclass(frozen=True)
class VerificationResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    def persist(self, path: Path) -> None:
        """Atomically persist the complete evidence needed to reproduce a check."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".verification-", dir=str(target.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def run_verification(argv: Sequence[str], *, cwd: Path, timeout: Optional[float] = None) -> VerificationResult:
    """Run one verification argv directly, never through a shell."""
    safe_argv = _argv(argv)
    effective_timeout = DEFAULT_VERIFICATION_TIMEOUT if timeout is None else timeout
    if (
        isinstance(effective_timeout, bool)
        or not isinstance(effective_timeout, numbers.Real)
        or not math.isfinite(effective_timeout)
        or effective_timeout <= 0
    ):
        raise ValueError("verification timeout must be a positive number")
    execution_argv = list(safe_argv)
    environment = controlled_env()
    if platform.system() == "Darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        try:
            metadata = sandbox.lstat()
        except OSError as error:
            raise RunnerError("required macOS verification sandbox is unavailable") from error
        if sandbox.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RunnerError("required macOS verification sandbox is untrusted")
        repo = Path(cwd).resolve()
        denied = {repo / ".git"}
        try:
            git_dir = run_git(
                ["-C", str(repo), "rev-parse", "--absolute-git-dir"],
                check=True, text=True, capture_output=True, timeout=10,
            ).stdout.strip()
            common_dir = run_git(
                ["-C", str(repo), "rev-parse", "--git-common-dir"],
                check=True, text=True, capture_output=True, timeout=10,
            ).stdout.strip()
            denied.add(Path(git_dir).resolve())
            common = Path(common_dir)
            if not common.is_absolute():
                common = repo / common
            denied.add(common.resolve())
        except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise RunnerError("cannot discover every Git metadata root") from error
        clauses = "".join(
            '(deny file-write* (subpath %s))' % json.dumps(str(path))
            for path in sorted(denied)
        )
        # 拒絕 IP 網路的**兩個方向**，但**不擋 unix domain socket**。
        #
        # 原本是 `(deny network*)`，而 Seatbelt 的 `network*` 連 unix domain socket 一起擋 ⇒
        # macOS 上任何 `xcodebuild test` 都跑不起來：模擬器的 XCTest 必須經
        # `/private/var/tmp/com.apple.launchd.*/com.apple.testmanagerd.unix-domain.socket`
        # 與 testmanagerd 通訊，被擋時 build 成功但測試從未執行、只回
        # 「Failed to establish communication with the test runner … Operation not permitted」
        # （exit 65）。⇒ 整個 iOS profile 的 verification 形同無法使用。
        #
        # unix socket 是本機 IPC、不會離開這台機器，所以「拒絕網路外連」的意圖仍然保住。
        # 唯一殘留的間接路徑是「經本機 daemon 代理出去」——但在 `(allow default)` 之下
        # spawn process 與 mach-lookup 本來就是更寬的通道，這個 profile 的實際強度不變。
        #
        # `network-bind` 刻意不擋：兩個方向都已拒絕，單純 bind 不構成外流路徑，
        # 而加上它會擋掉測試執行器自己的本機 socket 設置（實測會退回同一個失敗）。
        profile = (
            '(version 1)(allow default)'
            '(deny network-outbound (remote ip "*:*"))'
            '(deny network-inbound (local ip "*:*"))'
            '%s' % clauses
        )
        if not _already_restricted_by_seatbelt(denied):
            execution_argv = [str(sandbox), "-p", profile, *safe_argv]
    try:
        completed = subprocess.run(
            execution_argv,
            cwd=str(Path(cwd).resolve()),
            shell=False,
            text=True,
            capture_output=True,
            timeout=effective_timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise RunnerInterrupted("verification timed out") from error
    except OSError as error:
        raise RunnerError("could not start verification") from error
    return VerificationResult(tuple(safe_argv), completed.returncode, completed.stdout, completed.stderr)


def _object(value: Any, name: str, *, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("%s has an invalid object shape" % name)
    return value


def _strings(values: Any, name: str) -> list[str]:
    if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
        raise ValueError("%s must be a list of non-empty strings" % name)
    return values


def validate_codex_review(payload: Any) -> Mapping[str, Any]:
    """Validate schema shape plus verdict/finding relationships not expressible in JSON Schema."""
    review = _object(payload, "codex review", fields={"verdict", "summary", "findings", "questions", "context_requests"})
    verdict = review["verdict"]
    if verdict not in {"PASS", "CHANGES_REQUIRED", "NEEDS_USER_INPUT", "CONTEXT_REQUEST"}:
        raise ValueError("codex review has an invalid verdict")
    if not isinstance(review["summary"], str) or not review["summary"]:
        raise ValueError("codex review summary must be non-empty")
    _strings(review["questions"], "questions")
    _strings(review["context_requests"], "context_requests")
    if not isinstance(review["findings"], list):
        raise ValueError("findings must be a list")
    blocker = False
    for finding in review["findings"]:
        item = _object(finding, "finding", fields={"id", "severity", "invariant", "location", "evidence", "required_outcome", "lineage"})
        for field in ("id", "severity", "invariant", "location", "evidence", "required_outcome"):
            if not isinstance(item[field], str) or not item[field]:
                raise ValueError("finding %s must be non-empty" % field)
        if item["severity"] not in {"blocker", "major", "minor", "info"}:
            raise ValueError("finding severity must be a declared enum value")
        lineage = item["lineage"]
        # A structured-output schema must mark every property required, so an
        # optional field can only be expressed as a nullable one.  Codex
        # therefore always sends `discovery_reason`, and sends it as null when
        # there is none: absent and null have to mean the same thing here, or
        # the rule below would accept a newly discovered finding that explains
        # nothing.
        if isinstance(lineage, dict) and lineage.get("discovery_reason") is None:
            lineage.pop("discovery_reason", None)
        if (
            not isinstance(lineage, dict)
            or "resolution" not in lineage
            or not set(lineage) <= {"resolution", "discovery_reason"}
            or not isinstance(lineage["resolution"], str)
        ):
            raise ValueError("finding lineage must contain resolution and at most discovery_reason")
        if lineage["resolution"] not in {"existing", "introduced_by_fix", "newly_discovered", "deferred"}:
            raise ValueError("finding lineage resolution must be a declared enum value")
        if "discovery_reason" in lineage and (
            not isinstance(lineage["discovery_reason"], str) or not lineage["discovery_reason"].strip()
        ):
            raise ValueError("finding lineage discovery_reason must be non-empty")
        if lineage["resolution"] == "newly_discovered" and "discovery_reason" not in lineage:
            raise ValueError("newly_discovered findings require a discovery_reason")
        blocker = blocker or item["severity"] == "blocker"
        if item["severity"] == "blocker" and lineage["resolution"] == "deferred":
            raise ValueError("blocker findings cannot be deferred")
    if verdict == "PASS" and (
        review["findings"] or review["questions"] or review["context_requests"]
    ):
        raise ValueError("PASS requires empty findings, questions, and context requests")
    if verdict == "NEEDS_USER_INPUT" and not review["questions"]:
        raise ValueError("NEEDS_USER_INPUT requires questions")
    if verdict == "CONTEXT_REQUEST" and not review["context_requests"]:
        raise ValueError("CONTEXT_REQUEST requires context requests")
    return review


def validate_claude_resolution(
    payload: Any, *, required_finding_ids: Iterable[str] = (),
    allow_risk_flags: bool = False,
) -> Mapping[str, Any]:
    """Require a concrete non-deferred Claude outcome for every requested finding.

    ``allow_risk_flags`` is opt-in and used only by direct Review runs, so Plan
    and Code validation keeps its exact field set unchanged.  When enabled, a
    bounded ``risk_flags`` list may accompany the resolution and is normalized
    into the returned mapping for deterministic high-risk detection.
    """
    if allow_risk_flags and isinstance(payload, dict) and "risk_flags" in payload:
        flags = payload["risk_flags"]
        if (
            not isinstance(flags, list)
            or len(flags) > 16
            or not all(
                isinstance(item, str) and item.strip() and len(item.encode("utf-8")) <= 200
                for item in flags
            )
        ):
            raise ValueError("claude resolution risk flags must be a bounded string list")
        normalized_flags = tuple(sorted({item.strip() for item in flags}))
        rest = {key: value for key, value in payload.items() if key != "risk_flags"}
        return dict(
            validate_claude_resolution(rest, required_finding_ids=required_finding_ids),
            risk_flags=list(normalized_flags),
        )
    resolution = _object(payload, "claude resolution", fields={"summary", "resolutions"})
    if not isinstance(resolution["summary"], str) or not resolution["summary"]:
        raise ValueError("claude resolution summary must be non-empty")
    if not isinstance(resolution["resolutions"], list):
        raise ValueError("resolutions must be a list")
    seen = set()
    for item in resolution["resolutions"]:
        entry = _object(item, "resolution", fields={"finding_id", "outcome", "evidence"})
        if not all(isinstance(entry[field], str) and entry[field] for field in entry):
            raise ValueError("resolution fields must be non-empty")
        if entry["outcome"] not in {"fixed", "not_reproduced", "needs_user_input", "disputed"}:
            raise ValueError("resolution has an invalid outcome")
        if entry["outcome"] == "disputed" and len(entry["evidence"].strip()) < 16:
            raise ValueError("disputed resolutions require meaningful evidence")
        seen.add(entry["finding_id"])
    missing = set(required_finding_ids) - seen
    if missing:
        raise ValueError("missing resolutions for findings: %s" % sorted(missing))
    return resolution


def validate_plan_update(payload: Any) -> Mapping[str, Any]:
    """Validate a self-contained Plan revision before the workflow persists it.

    The response carries content rather than a model-selected path, keeping the
    only write target under the frozen run manifest's Plan path.
    """
    update = _object(payload, "Claude Plan update", fields={"summary", "answer_digest", "plan"})
    if not isinstance(update["summary"], str) or not update["summary"].strip():
        raise ValueError("Claude Plan update summary must be non-empty")
    if (
        not isinstance(update["answer_digest"], str)
        or len(update["answer_digest"]) != 64
        or any(character not in "0123456789abcdef" for character in update["answer_digest"])
    ):
        raise ValueError("Claude Plan update answer digest must be SHA-256 hex")
    plan = _object(
        update["plan"],
        "Claude Plan update plan",
        fields={"content", "previous_digest", "new_digest", "changed_sections"},
    )
    if not isinstance(plan["content"], str) or not plan["content"]:
        raise ValueError("Claude Plan update content must be non-empty")
    if not all(
        isinstance(plan[field], str) and len(plan[field]) == 64
        and all(character in "0123456789abcdef" for character in plan[field])
        for field in ("previous_digest", "new_digest")
    ):
        raise ValueError("Claude Plan update digests must be SHA-256 hex")
    if not isinstance(plan["changed_sections"], list) or not plan["changed_sections"] or not all(
        isinstance(section, str) and section.strip() for section in plan["changed_sections"]
    ):
        raise ValueError("Claude Plan update requires changed sections")
    if hashlib.sha256(plan["content"].encode("utf-8")).hexdigest() != plan["new_digest"]:
        raise ValueError("Claude Plan update new digest does not match content")
    return update


def validate_plan_repair(
    payload: Any, *, required_finding_ids: Iterable[str],
    current_plan_digest: str, decision_log_digest: str,
) -> Mapping[str, Any]:
    """Validate a finding resolution that necessarily revises exact Plan bytes."""
    repair = _object(
        payload, "Claude Plan repair",
        fields={
            "summary", "input_plan_digest", "decision_log_digest",
            "resolutions", "plan",
        },
    )
    resolution = validate_claude_resolution(
        {"summary": repair["summary"], "resolutions": repair["resolutions"]},
        required_finding_ids=required_finding_ids,
    )
    if repair["input_plan_digest"] != current_plan_digest:
        raise ValueError("Claude Plan repair input digest is stale")
    if repair["decision_log_digest"] != decision_log_digest:
        raise ValueError("Claude Plan repair decision log digest is stale")
    plan = repair["plan"]
    # Plan-mode Claude holds only Read/Glob/Grep and may not invoke a shell, so it
    # cannot hash its own output; a model-reported new_digest would be checked
    # against bytes this process already owns and is therefore no independent
    # witness. Derive it here and ignore any reported value.
    if not isinstance(plan, dict) or not {
        "content", "previous_digest", "changed_sections"
    } <= set(plan) <= {"content", "previous_digest", "new_digest", "changed_sections"}:
        raise ValueError("Claude Plan repair plan has an invalid shape")
    if plan["previous_digest"] != current_plan_digest:
        raise ValueError("Claude Plan repair previous digest is stale")
    if not isinstance(plan["content"], str) or not plan["content"]:
        raise ValueError("Claude Plan repair must contain a changed digest-bound Plan")
    derived_digest = hashlib.sha256(plan["content"].encode("utf-8")).hexdigest()
    if derived_digest == current_plan_digest:
        raise ValueError("Claude Plan repair must contain a changed digest-bound Plan")
    if (
        not isinstance(plan["changed_sections"], list)
        or not plan["changed_sections"]
        or not all(isinstance(item, str) and item.strip() for item in plan["changed_sections"])
    ):
        raise ValueError("Claude Plan repair requires changed sections")
    return dict(
        repair,
        resolutions=resolution["resolutions"],
        plan=dict(plan, new_digest=derived_digest),
    )


# Explicit aliases keep callers focused on domain intent without relaxing safety.
run_command = run_verification
