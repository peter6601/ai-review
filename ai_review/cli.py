"""Strict local command line boundary for the consensus workflows.

The CLI owns parsing, human confirmation material, and concise machine output.
Workflow state, signatures, and model execution remain in their dedicated
modules so callers and tests cannot bypass the real authority boundary.
"""

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NoReturn, Optional

from .context import BUDGET_METHOD_DOC, SourceRef, build_packet
from .git_diff import capture_diff_bytes
from .models import (
    DOC_LENSES, ApprovalAuthority, DocManifest, HumanApprovalReceipt,
    ReviewApprovalAttestation, ReviewApprovalReceipt, ReviewManifest,
    RiskApprovalReceipt, RunState, Status, VerificationCommand,
    canonical_user_question_ids, sign_risk_approval, strict_json_loads,
    verify_risk_approval,
)
from .runners import RunnerError, RunnerInterrupted, build_claude_argv, build_codex_argv, validate_codex_review
from .store import PRODUCTION_WORKSPACE_ROOT, RunStore, git_worktree_root
from .summary import generate_outputs
from .process_security import (
    controlled_env, executable_identity, resolve_executable, run_git,
    validate_executable_identity,
)
from .auto_approval import (
    AgentApprovalProvider,
    AutoApprovalRateLimited,
    MAX_AUTO_APPROVALS,
    WINDOW_SECONDS,
    ledger_path,
    reserve as reserve_auto_approval,
)
from .doc_workflow import DocWorkflow
from .policy import Policy, load_policy
from .preflight import PreflightError, load_preflight_text
from .review_workflow import DirectReviewWorkflow
from .workflow import CodeWorkflow, PlanWorkflow


EXIT_INVALID = 2
EXIT_INTERRUPTED = 3
EXIT_RATE_LIMITED = 4
# Plan repair must echo the whole revised Plan in one structured result, so the
# bound scales with Plan size rather than with review latency; 300s truncated a
# 62KB Plan repair mid-flight.
#
# 2026-08-10, raised 1800 -> 3600 from measurement, not guesswork. A Review
# repair on a 48-file / ~10k-line patch consumed the entire 1800s budget twice:
# once with 29 preflight findings and again with only 8, so the bound is driven
# by patch size, not by how much work was requested. The repair runs in safe
# mode with a Bash-free tool set, so it cannot grep -- it must Read whole files,
# and a single 2000-line ViewModel plus its spec and design doc already eat most
# of half an hour before the first edit. Both runs ended PAUSED /
# RUNNER_INTERRUPTED with the worktree half-written, which is the expensive
# failure mode this bound is supposed to prevent.
#
# Deliberately not asserted in tests: this is a tuning knob, not an invariant,
# and pinning the exact number would just make a legitimate future adjustment
# show up as a red test.
EXTERNAL_CALL_TIMEOUT = 3600
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SECRET = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]+-----|(?i:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s]+)")
_ROOT = Path(__file__).resolve().parent.parent


def _raise_user_presence_failure(completed: "subprocess.CompletedProcess[str]") -> NoReturn:
    """Distinguish a real Cancel click from the dialog never appearing at all.

    osascript exits non-zero for both, so reporting every failure as "cancelled"
    hides tool bugs as user intent. A real cancel carries AppleScript error
    "(-128)"; anything else (syntax error, no GUI session) is our problem, not
    the human's.
    """
    detail = (completed.stderr or "").strip()
    if detail and "(-128)" not in detail:
        raise CliInputError(
            "local user-presence approval could not be displayed "
            "(the dialog never appeared): %s" % detail
        )
    raise CliInputError("human approval was cancelled")


class CliInputError(ValueError):
    """An input is invalid before a workflow may run."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliInputError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="ai-review", add_help=True)
    parser.add_argument("--runs-root", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    kinds = init.add_subparsers(dest="kind", required=True)
    plan = kinds.add_parser("plan")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--plan", required=True)
    plan.add_argument("--base", default="HEAD")
    plan.add_argument("--source", action="append", default=[])
    plan.add_argument("--test", action="append", default=[])
    plan.add_argument("--verify", action="append", default=[])
    code = kinds.add_parser("code")
    code.add_argument("--repo", required=True)
    code.add_argument("--plan-run", required=True)
    code.add_argument("--base", required=True)
    review = kinds.add_parser("review")
    review.add_argument("--repo", required=True)
    review.add_argument("--base", required=True)
    review.add_argument("--brief", required=True)
    review.add_argument("--profile", choices=("generic", "ios"), required=True)
    review.add_argument("--source", action="append", default=[])
    review.add_argument("--verify", action="append", default=[])
    # A doc run is one read-only pass over a document: it runs no command, so
    # there is deliberately no --verify here.  Passing one must be an argparse
    # error rather than a flag the caller believes was honoured.
    doc = kinds.add_parser("doc")
    doc.add_argument("--repo", required=True)
    doc.add_argument("--doc", required=True)
    doc.add_argument("--base", default="HEAD")
    doc.add_argument("--lens", choices=list(DOC_LENSES), required=True)
    doc.add_argument("--lens-reason", required=True)
    doc.add_argument("--brief", required=True)
    doc.add_argument("--source", action="append", default=[])

    for name in ("run", "resume"):
        item = commands.add_parser(name)
        item.add_argument("run_id")
    # A second round over a document the human has edited.  It deliberately
    # takes no flags: nothing but the document's bytes may change between
    # rounds, and there is no --force, because a fresh opinion on unchanged
    # bytes is `init doc`, which leaves its own audit trail.
    re_review = commands.add_parser("re-review")
    re_review.add_argument("run_id")
    answer = commands.add_parser("answer")
    answer.add_argument("run_id")
    answer.add_argument("--answers", required=True)
    approval = commands.add_parser("approve-plan")
    approval.add_argument("run_id")
    code_approval = commands.add_parser("approve-code")
    code_approval.add_argument("run_id")
    review_approval = commands.add_parser("approve-review")
    review_approval.add_argument("run_id")
    risk_approval = commands.add_parser("approve-risk")
    risk_approval.add_argument("run_id")
    # Review's two in-loop gates, and only those, may be cleared by the calling
    # agent instead of by a person.  approve-code is deliberately absent: the
    # terminal gate is where a person reads the diff, whether the loop ended in
    # a Codex PASS or ran out of repair rounds.  The flag is explicit at every
    # call site so the choice is visible in the transcript, never inherited
    # from the environment or from configuration.
    for gate in (review_approval, risk_approval):
        gate.add_argument(
            "--auto", action="store_true",
            help="approve as the agent, under the auto-approval rate limit",
        )
    preflight = commands.add_parser("submit-preflight")
    preflight.add_argument("run_id")
    preflight.add_argument("--findings", required=True)
    writeback = commands.add_parser("writeback-knowledge")
    writeback.add_argument("run_id")
    status = commands.add_parser("status")
    status.add_argument("run_id")
    expansion = commands.add_parser("expand-context")
    expansion.add_argument("run_id")
    expansion.add_argument("--source", action="append", required=True)
    return parser


def _safe_error(error: BaseException) -> str:
    text = _SECRET.sub("[REDACTED]", str(error)).replace("\n", " ").replace("\r", " ").strip()
    return (text or "invalid request")[:400]


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _resolve_repo(raw: str) -> Path:
    return git_worktree_root(Path(raw))


def _resolve_repository_file(repo: Path, raw: str, *, label: str) -> Path:
    """Resolve one readable regular file that lives inside the repository.

    Plan and doc runs both bind a single file by content, so they share one
    rule; only the noun in the message differs, so a caller is told which input
    was rejected.
    """
    candidate = Path(raw).expanduser()
    resolved = (repo / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not resolved.is_file():
        raise CliInputError("%s must be a readable regular file" % label)
    try:
        resolved.relative_to(repo)
    except ValueError as error:
        raise CliInputError("%s must be inside repository" % label) from error
    return resolved


def _resolve_plan(repo: Path, raw: str) -> Path:
    return _resolve_repository_file(repo, raw, label="Plan file")


def _resolve_document(repo: Path, raw: str) -> Path:
    return _resolve_repository_file(repo, raw, label="document")


def _central_policy() -> Policy:
    """Load the same central policy the workflows load, from the same file.

    Both the doc caps and the pinned Codex model come from here, so there is
    exactly one way for the CLI to reach the policy file.
    """
    return load_policy(_ROOT / "config" / "defaults.yaml")


def _doc_source_refs(values: Iterable[str], *, max_sources: int) -> tuple[SourceRef, ...]:
    """Apply the doc source cap from policy, then the shared per-source rule."""
    raw = tuple(values)
    if len(raw) > max_sources:
        raise CliInputError(
            "initial context may select no more than %d exact source sections" % max_sources
        )
    return _validated_source_refs(raw)


def _source_refs(values: Iterable[str]) -> tuple[SourceRef, ...]:
    raw = tuple(values)
    if len(raw) > 3:
        raise CliInputError("initial context may select no more than 3 exact source sections")
    return _validated_source_refs(raw)


def _validated_source_refs(raw: tuple) -> tuple[SourceRef, ...]:
    refs = []
    for value in raw:
        path_text, separator, section = value.rpartition("#")
        path = Path(path_text).expanduser()
        if not separator or not path_text or not section or not path.is_absolute():
            raise CliInputError("--source must be an absolute Markdown path followed by one exact #Heading")
        source = path.resolve()
        if not source.is_file():
            raise CliInputError("context source must be a readable regular file")
        refs.append(SourceRef(source, section, "user-selected exact section", priority=1))
    return tuple(refs)


def _trusted_verification_executable(raw: str, repo: Optional[Path]) -> str:
    candidate = Path(raw).expanduser()
    if "/" in raw or "\\" in raw:
        candidate = candidate if candidate.is_absolute() else (repo or Path.cwd()) / candidate
    else:
        located = shutil.which(raw)
        if located is None:
            raise CliInputError("verification executable is unavailable")
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise CliInputError("verification executable cannot be resolved") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not os.access(str(resolved), os.X_OK)
        or metadata.st_mode & 0o022
    ):
        raise CliInputError("verification executable is not a trusted regular executable")
    trusted_roots = tuple(Path(item) for item in (
        "/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin", "/Applications",
    ))
    if not any(resolved == root or root in resolved.parents for root in trusted_roots):
        raise CliInputError("verification executable is outside trusted system roots")
    return str(resolved)


def _test_commands(
    values: Iterable[str], *, repo: Optional[Path] = None
) -> list[dict[str, Any]]:
    # Keep an explicit argv boundary: no shell fragment ever reaches a runner.
    commands = []
    for value in values:
        item = strict_json_loads(value)
        if isinstance(item, list):
            item = {"kind": "test", "argv": item, "scope": "task"}
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "argv", "scope"}
            or item["kind"] not in {"check", "build", "test"}
            or not isinstance(item["argv"], list)
            or not item["argv"]
            or not all(isinstance(part, str) and part for part in item["argv"])
        ):
            raise CliInputError("verification must be a typed JSON object with kind, argv, and scope")
        try:
            item["argv"][0] = _trusted_verification_executable(item["argv"][0], repo)
            item["executable_identity"] = executable_identity(Path(item["argv"][0]))
            commands.append(VerificationCommand.from_value(item).to_dict())
        except ValueError as error:
            raise CliInputError(str(error)) from error
    if not any(item["kind"] == "test" and item["scope"] for item in commands):
        raise CliInputError("an explicit task-focused test verification is required")
    return commands


def _review_commands(values: Iterable[str], *, repo: Path) -> list[dict[str, Any]]:
    """Accept typed JSON or the documented shell-like spelling as safe argv."""
    normalized = []
    for value in values:
        try:
            parsed = strict_json_loads(value)
        except ValueError:
            try:
                argv = shlex.split(value)
            except ValueError as error:
                raise CliInputError("verification command quoting is invalid") from error
            if not argv:
                raise CliInputError("verification command must not be empty")
            parsed = {
                "kind": "test",
                "argv": argv,
                "scope": argv[-1],
            }
        normalized.append(json.dumps(parsed, ensure_ascii=False))
    return _test_commands(normalized, repo=repo)


def _store_from_args(args: argparse.Namespace) -> RunStore:
    if args.runs_root:
        root = Path(args.runs_root).expanduser().resolve()
        return RunStore(root, authority=ApprovalAuthority(root.parent / "approval.key"))
    return RunStore()


def _status_store_from_args(args: argparse.Namespace) -> RunStore:
    """Open only existing artifacts; status must not create authority state."""
    if args.runs_root:
        return RunStore(Path(args.runs_root).expanduser().resolve())
    from .store import default_runs_root
    return RunStore(default_runs_root())


def _artifact_path(store: RunStore, state: RunState, relative: str) -> Optional[str]:
    path = store._run_directory(state) / relative
    return str(path) if store.artifact_exists(path) else None


def _payload(store: RunStore, state: RunState) -> dict[str, Any]:
    status = state.status.value
    next_actions = {
        Status.READY.value: "run",
        Status.RUNNING.value: "resume",
        Status.AWAITING_USER_INPUT.value: "answer",
        Status.AWAITING_HUMAN_PLAN_REVIEW.value: "human_plan_review",
        Status.AWAITING_HUMAN_CODE_REVIEW.value: "human_code_review",
        Status.AWAITING_HUMAN_DOC_REVIEW.value: "human_doc_review",
        Status.AWAITING_REVIEW_APPROVAL.value: "human_review_scope",
        Status.AWAITING_PREFLIGHT.value: "submit_preflight",
        Status.PAUSED.value: "human_decision",
        Status.INTERRUPTED.value: "resume",
    }
    payload = {
        "run_id": state.run_id,
        "kind": state.kind,
        "status": status,
        "repair_round": state.repair_round,
        "next_action": next_actions[status],
        "questions_path": _artifact_path(store, state, "user-questions.json"),
        "summary_path": _artifact_path(store, state, "final-summary.md"),
        "knowledge_candidate_path": _artifact_path(store, state, "knowledge-candidate.md"),
        "base_oid": state.manifest.base_oid,
    }
    if isinstance(state.manifest, ReviewManifest):
        payload.update({
            "brief_digest": state.manifest.brief_digest,
            "initial_patch_digest": state.manifest.initial_patch_digest,
            "profile": state.manifest.profile,
        })
    elif isinstance(state.manifest, DocManifest):
        # The doc equivalent of the plan-digest receipt: a person can bind the
        # findings they are about to read to the exact document bytes on disk,
        # under the lens and purpose the review was run with.  A doc run has no
        # Plan, so it never reports a plan_digest.
        payload.update({
            "doc_digest": hashlib.sha256(
                Path(state.manifest.doc_path).read_bytes()
            ).hexdigest(),
            "lens": state.manifest.lens,
            "brief_digest": state.manifest.brief_digest,
        })
    else:
        # This is the human-review receipt: it lets a person bind approval to
        # the exact bytes they reviewed without exposing any model transcript.
        payload["plan_digest"] = hashlib.sha256(
            Path(state.manifest.plan_path).read_bytes()
        ).hexdigest()
    return payload


def _read_json_object(path_text: str) -> Mapping[str, str]:
    path = Path(path_text).expanduser().resolve()
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise CliInputError("answers JSON has duplicate keys or invalid syntax") from error
    except OSError as error:
        raise CliInputError("answers file cannot be read") from error
    if not isinstance(value, dict) or not value or not all(
        isinstance(key, str) and isinstance(answer, str) for key, answer in value.items()
    ):
        raise CliInputError("answers must be a non-empty JSON object of strings")
    return value


# How much of an unexpected key to quote back. A key that is really a whole
# question runs to a paragraph, and the reader only needs to recognise it.
_ANSWER_KEY_PREVIEW = 32
_ANSWER_KEYS_SHOWN = 3


def _key_preview(keys: list[str]) -> str:
    """Name the unexpected keys without pasting whole questions back at a reader."""
    shown = [
        key if len(key) <= _ANSWER_KEY_PREVIEW else key[:_ANSWER_KEY_PREVIEW] + "..."
        for key in keys[:_ANSWER_KEYS_SHOWN]
    ]
    if len(keys) > _ANSWER_KEYS_SHOWN:
        shown.append("and %d more" % (len(keys) - _ANSWER_KEYS_SHOWN))
    return ", ".join('"%s"' % item for item in shown)


def _persisted_question_ids(store: RunStore, state: RunState) -> Optional[tuple[str, ...]]:
    """Read the ids this pause is waiting on, writing nothing.

    Deliberately not ``PlanWorkflow._active_question_cycle``: that helper
    migrates a legacy first-cycle run into the sequenced layout, which is a
    write, and a rejected input must leave the run byte-identical. ``None``
    means the questions could not be read at all, which is a broken run for the
    workflow to refuse rather than an operator's typo to correct.
    """
    directory = store._run_directory(state)
    cycles = [
        int(path.name)
        for path in store.list_artifact_directories(directory / "question-cycles")
        if path.name.isdigit()
    ]
    path = (
        directory / "question-cycles" / ("%04d" % max(cycles)) / "questions.json"
        if cycles else directory / "user-questions.json"
    )
    try:
        raw = store.read_optional_artifact_bytes(path)
        if raw is None:
            return None
        contents = strict_json_loads(raw.decode("utf-8"))
        return canonical_user_question_ids(
            contents.get("questions") if isinstance(contents, dict) else None
        )
    except (ValueError, OSError, UnicodeDecodeError):
        return None


def _answerable(state: RunState) -> None:
    """Refuse ``answer`` on a run that is not parked on questions.

    Each workflow's own ``status != AWAITING_USER_INPUT`` check stays where it
    is and remains the authority; but it raises *inside* ``answer()``, whose
    except clause pauses.  So an ``answer`` aimed at an already-``PAUSED`` run
    re-paused it: ``pause.json`` is the only record of why a run stopped, and a
    run killed by ``INVALID_CODEX_REVIEW`` came back reading
    ``INVALID_DOC_USER_ANSWER`` -- the real cause gone.  Worse, the command
    exited 0, so nothing told the operator they had just erased it.  A live
    ``READY`` or ``AWAITING_HUMAN_DOC_REVIEW`` run was destroyed the same way:
    moved to ``PAUSED``, which no command returns from.

    The same check one layer out is an input error (exit 2) that touches
    nothing.  It is checked before ``_checked_answers`` because the answers
    file is beside the point on a run that cannot be answered at all.
    """
    if state.status != Status.AWAITING_USER_INPUT:
        raise CliInputError(
            "answer requires a run parked at AWAITING_USER_INPUT, not %s"
            % state.status.value
        )


def _checked_answers(
    store: RunStore, state: RunState, answers: Mapping[str, str],
) -> Mapping[str, str]:
    """Refuse a malformed answers file before a workflow can pause the run.

    ``_validate_answers`` stays where it is as the authority boundary, but it
    runs *inside* ``answer()``, whose except clause pauses the run -- and no
    command returns a ``PAUSED`` run to ``AWAITING_USER_INPUT``. One typo in a
    JSON file therefore discarded a completed, separately billed Codex round;
    the skill's own wording produced exactly such a file every time. This is the
    same shape check one layer out, where a bad file is an input error and the
    run is never touched.

    Every kind that can ask questions is gated, not just ``doc``: ``plan`` and
    ``review`` pause the same unrecoverable way, on rounds that cost more.
    """
    if state.status != Status.AWAITING_USER_INPUT:
        # Not this gate's business; the workflow owns the state check.
        return answers
    expected = _persisted_question_ids(store, state)
    if expected is None:
        return answers
    known = set(expected)
    supplied = set(answers)
    missing = sorted(known - supplied)
    unexpected = sorted(supplied - known)
    blank = sorted(key for key in supplied & known if not answers[key].strip())
    if not (missing or unexpected or blank):
        return answers
    parts = [
        "answers must be a JSON object keyed by question id, not by question "
        "text: expected %s" % ", ".join(expected)
    ]
    if missing:
        parts.append("missing %s" % ", ".join(missing))
    if unexpected:
        parts.append("unexpected %s" % _key_preview(unexpected))
    if blank:
        parts.append("empty answer for %s" % ", ".join(blank))
    # No answer value is ever quoted: they are the user's words, and long.
    raise CliInputError("; ".join(parts))


def _oid(repo: Path, ref: str) -> str:
    try:
        completed = run_git(
            ["-C", str(repo), "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CliInputError("base must resolve to one commit") from error
    oid = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", oid):
        raise CliInputError("base must resolve to one full commit object ID")
    return oid


class _LocalCodex:
    # The run kind selects the prompt: a Review input also carries a patch, so
    # structural detection alone cannot distinguish Code from Review.
    _PROMPTS = {"plan": "codex-plan.md", "code": "codex-code.md", "review": "codex-review.md"}

    def __init__(
        self, repo: Path, identity: Optional[Mapping[str, Any]] = None, *, model: str,
        mode: str = "code", lens: Optional[str] = None,
    ):
        self.repo = repo
        if mode not in self._PROMPTS and mode != "doc":
            raise ValueError("Codex mode must be plan, code, review, or doc")
        # A doc review's standard is the lens, so the lens picks the prompt.
        # Requiring it here keeps a doc run from silently being judged by some
        # default standard, and refusing it elsewhere keeps the other three
        # modes exactly as they were.
        if mode == "doc":
            if lens not in DOC_LENSES:
                raise ValueError("a doc review requires one of the three document lenses")
        elif lens is not None:
            raise ValueError("only a doc review is selected by lens")
        self.mode = mode
        self.lens = lens
        # Required, with no fall back: the model is this tool's own
        # requirement, and every caller states it from the central policy.  A
        # default here -- even one resolved from that same policy -- would
        # leave a path that a forgetful caller can take without saying so.
        self.model = model
        self.identity = dict(identity or resolve_executable("codex", path=os.environ.get("PATH")))

    @property
    def prompt_name(self) -> str:
        if self.mode == "doc":
            return "codex-doc-%s.md" % self.lens
        return self._PROMPTS[self.mode]

    def review(self, inputs: Mapping[str, Any]) -> Any:
        prompt = (_ROOT / "prompts" / self.prompt_name).read_text(encoding="utf-8")
        prompt += "\n\nINPUT_JSON:\n" + json.dumps(inputs, ensure_ascii=False, sort_keys=True)
        schema = _ROOT / "schemas" / "codex-review.schema.json"
        with tempfile.TemporaryDirectory(prefix="ai-review-codex-") as raw:
            output = Path(raw) / "review.json"
            if output.exists() or output.is_symlink():
                raise RunnerError("Codex output path was not fresh")
            executable = validate_executable_identity(self.identity)
            argv = build_codex_argv(
                self.repo, schema, output, prompt,
                model=self.model, executable=executable,
            )
            return _run_codex_with_output(argv, self.repo, output, Path(raw))


class _LocalClaude:
    def __init__(
        self, repo: Path, *, mode: str = "code",
        identity: Optional[Mapping[str, Any]] = None,
    ):
        self.repo = repo
        if mode not in ("plan", "code", "review"):
            raise ValueError("Claude mode must be plan, code, or review")
        self.mode = mode
        self.identity = dict(identity or resolve_executable("claude", path=os.environ.get("PATH")))

    @property
    def _fix_prompt(self) -> str:
        return "claude-review-fix.md" if self.mode == "review" else "claude-fix.md"

    @property
    def _resolution_schema(self) -> str:
        # The Review contract adds a structured risk disclosure that feeds
        # deterministic high-risk detection; Plan and Code keep their schemas.
        return (
            "claude-review-resolution.schema.json"
            if self.mode == "review" else "claude-resolution.schema.json"
        )

    def resolve(self, inputs: Mapping[str, Any]) -> Any:
        schema = (
            "claude-plan-repair.schema.json"
            if self.mode == "plan" else self._resolution_schema
        )
        return self._call(schema, self._fix_prompt, inputs)

    def update_plan(self, inputs: Mapping[str, Any]) -> Any:
        return self._call("claude-plan-update.schema.json", "claude-implement.md", inputs)

    def implement(self, inputs: Mapping[str, Any]) -> Any:
        if self.mode == "review":
            raise RunnerError("Review mode never requests an initial implementation")
        return self._call("claude-resolution.schema.json", "claude-implement.md", inputs)

    def repair(self, inputs: Mapping[str, Any]) -> Any:
        return self._call(self._resolution_schema, self._fix_prompt, inputs)

    def _call(self, schema_name: str, prompt_name: str, inputs: Mapping[str, Any]) -> Any:
        schema = (_ROOT / "schemas" / schema_name).read_text(encoding="utf-8")
        prompt = (_ROOT / "prompts" / prompt_name).read_text(encoding="utf-8")
        prompt += "\n\nINPUT_JSON:\n" + json.dumps(inputs, ensure_ascii=False, sort_keys=True)
        mode = "plan" if schema_name == "claude-plan-update.schema.json" else self.mode
        executable = validate_executable_identity(self.identity)
        stdout = _run_external(
            build_claude_argv(schema, prompt, mode=mode, executable=executable), self.repo
        )
        try:
            return strict_json_loads(stdout)
        except ValueError as error:
            raise RunnerError("Claude did not produce one valid JSON result") from error


def _run_external(argv: list[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), check=False, text=True, capture_output=True,
            timeout=EXTERNAL_CALL_TIMEOUT, env=controlled_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise RunnerInterrupted("external review timed out") from error
    except OSError as error:
        raise RunnerError("external review could not start") from error
    if completed.returncode:
        raise RunnerError(_external_failure_detail(completed.returncode, completed.stderr))
    return completed.stdout


def _run_codex_with_output(argv: list[str], cwd: Path, output: Path, temporary_root: Path) -> Any:
    """Accept only a fresh valid result from a successful Codex process."""
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), check=False, text=True, capture_output=True,
            timeout=EXTERNAL_CALL_TIMEOUT, env=controlled_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise RunnerInterrupted("external review timed out") from error
    except OSError as error:
        raise RunnerError("external review could not start") from error
    if completed.returncode:
        raise RunnerError(_external_failure_detail(completed.returncode, completed.stderr))
    try:
        result = _read_fresh_codex_output(output, temporary_root)
    except (OSError, ValueError) as error:
        raise RunnerError("Codex did not produce one valid JSON result") from error
    return result


def _read_fresh_codex_output(output: Path, temporary_root: Path) -> Any:
    """Read only a regular, temp-root-contained, semantically valid result."""
    root = Path(temporary_root).resolve()
    candidate = Path(output)
    resolved = candidate.resolve(strict=True)
    if resolved.parent != root or candidate.is_symlink():
        raise OSError("Codex output is outside its fresh temporary directory")
    metadata = candidate.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("Codex output is not a regular file")
    return validate_codex_review(strict_json_loads(candidate.read_text(encoding="utf-8")))


def _external_failure_detail(exit_code: int, stderr: str) -> str:
    """Keep a useful local failure category without retaining model/CLI output."""
    normalized = stderr.lower() if isinstance(stderr, str) else ""
    argument_markers = (
        "unexpected argument", "unrecognized argument", "unknown option", "unknown argument",
        "invalid value", "requires a value", "found argument",
    )
    category = "CLI_ARG_ERROR" if any(marker in normalized for marker in argument_markers) else "EXTERNAL_EXIT"
    return "%s exit=%d" % (category, exit_code)


def _default_workflow_factory(*, kind: str, store: RunStore, state: RunState, context_packet: Any = None) -> Any:
    repo = Path(state.manifest.repo_path)
    identities = state.manifest.review_executables
    # Read once, here, and hand it to every kind: the pinned model is a
    # property of the tool, so no run of any kind may be judged by whatever
    # the machine's global Codex config happens to name.
    model = _central_policy().codex_model
    if kind == "doc":
        # No Claude is built at all: DocWorkflow replaces whatever it is given
        # with a guard that raises, so there is nothing here to pass it.
        return DocWorkflow(
            store,
            _LocalCodex(
                repo, identities.get("codex"), mode="doc",
                lens=state.manifest.lens, model=model,
            ),
            None,
            context_packet=context_packet,
        )
    codex = _LocalCodex(repo, identities.get("codex"), mode=kind, model=model)
    claude = _LocalClaude(repo, mode=kind, identity=identities.get("claude"))
    if kind == "plan":
        return PlanWorkflow(store, codex, claude, context_packet=context_packet)
    if kind == "review":
        return DirectReviewWorkflow(store, codex, claude, context_packet=context_packet)
    return CodeWorkflow(store, codex, claude, context_packet=context_packet)


def _workflow(store: RunStore, state: RunState, factory: Callable[..., Any], context_packet: Any = None) -> Any:
    return factory(kind=state.kind, store=store, state=state, context_packet=context_packet)


class MacOSHumanApprovalProvider:
    """A Cancel-default native dialog, invokable without a shell boundary."""

    def approve(
        self, *, run_id: str, plan_digest: str, base_oid: str,
        verification_commands: list[dict[str, Any]], verification_digest: str,
    ) -> HumanApprovalReceipt:
        if platform.system() != "Darwin":
            raise CliInputError("independent local user-presence approval is unavailable")
        rendered = json.dumps(
            verification_commands, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        message = "Approve Code work for Plan run %s?\\n\\nPlan digest:\\n%s\\n\\nFrozen base OID:\\n%s\\n\\nVerification digest:\\n%s\\n\\nExact verification argv:\\n%s" % (
            run_id, plan_digest, base_oid, verification_digest, rendered,
        )
        script = "display dialog %s buttons {\"Cancel\", \"Approve\"} default button \"Cancel\" cancel button \"Cancel\" with icon caution" % json.dumps(message, ensure_ascii=False)
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-e", script], check=False, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CliInputError("independent local user-presence approval is unavailable") from error
        if completed.returncode != 0 or "Approve" not in completed.stdout:
            _raise_user_presence_failure(completed)
        return HumanApprovalReceipt(
            run_id=run_id, plan_digest=plan_digest, base_oid=base_oid, approved_at=_utc_now(),
            provider="macos:osascript-user-presence", actor="local-macos-user",
            verification_digest=verification_digest,
        )

    def approve_code(self, *, run_id: str, candidate_digest: str) -> dict[str, str]:
        if platform.system() != "Darwin":
            raise CliInputError("independent local user-presence approval is unavailable")
        message = "Approve local Code candidate for run %s?\\n\\nCandidate digest:\\n%s" % (
            run_id, candidate_digest,
        )
        script = "display dialog %s buttons {\"Cancel\", \"Approve\"} default button \"Cancel\" cancel button \"Cancel\" with icon caution" % json.dumps(message, ensure_ascii=False)
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-e", script], check=False, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CliInputError("independent local user-presence approval is unavailable") from error
        if completed.returncode != 0 or "Approve" not in completed.stdout:
            _raise_user_presence_failure(completed)
        return {
            "approved_at": _utc_now(), "provider": "macos:osascript-user-presence",
            "actor": "local-macos-user",
        }

    def approve_review(
        self, *, run_id: str, manifest_digest: str, brief: str,
        base_oid: str, initial_patch_digest: str,
        verification_commands: list[dict[str, Any]],
    ) -> ReviewApprovalReceipt:
        if platform.system() != "Darwin":
            raise CliInputError("independent local user-presence approval is unavailable")
        rendered = json.dumps(
            [command["argv"] for command in verification_commands],
            ensure_ascii=False, separators=(",", ":"),
        )
        message = (
            "Approve direct Review scope for run %s?\\n\\nBrief:\\n%s"
            "\\n\\nFrozen base OID:\\n%s\\n\\nInitial patch digest:\\n%s"
            "\\n\\nExact verification argv:\\n%s"
        ) % (run_id, brief.strip(), base_oid, initial_patch_digest, rendered)
        script = (
            "display dialog %s buttons {\"Cancel\", \"Approve\"} "
            "default button \"Cancel\" cancel button \"Cancel\" with icon caution"
        ) % json.dumps(message, ensure_ascii=False)
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-e", script], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CliInputError("independent local user-presence approval is unavailable") from error
        if completed.returncode != 0 or "Approve" not in completed.stdout:
            _raise_user_presence_failure(completed)
        return ReviewApprovalReceipt(
            run_id=run_id, manifest_digest=manifest_digest, approved_at=_utc_now(),
            provider="macos:osascript-user-presence", actor="local-macos-user",
        )

    def approve_risk(
        self, *, run_id: str, manifest_digest: str, patch_digest: str,
        categories: list[str], paths: list[str],
    ) -> RiskApprovalReceipt:
        if platform.system() != "Darwin":
            raise CliInputError("independent local user-presence approval is unavailable")
        message = (
            "Approve high-risk Review scope growth for run %s?"
            "\\n\\nCategories:\\n%s\\n\\nPaths:\\n%s\\n\\nCurrent patch digest:\\n%s"
        ) % (run_id, ", ".join(categories), "\\n".join(paths) or "(none)", patch_digest)
        script = (
            "display dialog %s buttons {\"Cancel\", \"Approve\"} "
            "default button \"Cancel\" cancel button \"Cancel\" with icon caution"
        ) % json.dumps(message, ensure_ascii=False)
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-e", script], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CliInputError("independent local user-presence approval is unavailable") from error
        if completed.returncode != 0 or "Approve" not in completed.stdout:
            _raise_user_presence_failure(completed)
        return RiskApprovalReceipt(
            run_id=run_id, manifest_digest=manifest_digest, patch_digest=patch_digest,
            categories=tuple(sorted(set(categories))), approved_at=_utc_now(),
            provider="macos:osascript-user-presence", actor="local-macos-user",
        )


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _review_gate_provider(
    args: argparse.Namespace, store: RunStore, *, run_id: str, command: str
) -> Any:
    """Choose the human dialog, or spend one slot of the agent's rate limit.

    The slot is consumed at the moment of approval, after every binding has
    already been validated, so a rejected request never costs the caller a
    slot and a granted slot always corresponds to a signed receipt.
    """
    if not getattr(args, "auto", False):
        return MacOSHumanApprovalProvider()
    reserve_auto_approval(ledger_path(store.root), run_id=run_id, command=command)
    return AgentApprovalProvider()


def _init_plan(args: argparse.Namespace, store: RunStore) -> RunState:
    repo = _resolve_repo(args.repo)
    plan = _resolve_plan(repo, args.plan)
    refs = _source_refs(args.source)
    # Resolve the immutable base and extract every claimed exact section before
    # creating durable state. A rejected request must not leave a runnable run.
    base_oid = _oid(repo, args.base)
    packet = build_packet(refs) if refs else None
    state = store.create(RunState.new(
        "plan", str(plan), str(repo), args.base,
        verification_commands=_test_commands(
            tuple(args.verify) + tuple(args.test), repo=repo
        ),
        knowledge_sources=[str(ref.path) for ref in refs], base_oid=base_oid,
        context_checksum=packet.checksum if packet is not None else None,
        review_executables={
            "codex": resolve_executable("codex", path=os.environ.get("PATH")),
            "claude": resolve_executable("claude", path=os.environ.get("PATH")),
        },
    ))
    if packet is not None:
        PlanWorkflow(store, None, None, context_packet=packet)._persist_context_packet(store._run_directory(state), packet)
    return state


def _init_code(args: argparse.Namespace, store: RunStore) -> RunState:
    state = store.load(args.plan_run)
    if state.kind != "plan" or state.approval_attestation is None:
        raise CliInputError("Plan is not human-approved")
    repo = _resolve_repo(args.repo)
    if state.manifest is None or repo != Path(state.manifest.repo_path):
        raise CliInputError("repository must match the signed Plan repository")
    if _oid(repo, args.base) != state.manifest.base_oid:
        raise CliInputError("explicit base must match the signed Plan base commit")
    plan_artifacts = store._run_directory(state)
    try:
        code = RunState.new_code_from_approved_plan(state, store.authority)
    except ValueError as error:
        raise CliInputError(str(error)) from error
    code = store.create(code)
    if state.manifest.context_checksum is not None:
        packet = PlanWorkflow(store, None, None)._load_context_packet(plan_artifacts)
        if packet.checksum != state.manifest.context_checksum:
            raise CliInputError("approved Plan context packet checksum is invalid")
        target = store._run_directory(code)
        PlanWorkflow(store, None, None, context_packet=packet)._persist_context_packet(target, packet)
        store._atomic_write(target / "approved-plan-context.json", {
            "plan_run_id": state.run_id,
            "manifest_digest": state.manifest.digest(),
            "packet_checksum": packet.checksum,
            "packet_revision": packet.revision,
        })
    return code


def _init_review(args: argparse.Namespace, store: RunStore) -> RunState:
    repo = _resolve_repo(args.repo)
    base_oid = _oid(repo, args.base)
    refs = _source_refs(args.source)
    packet = build_packet(refs) if refs else None
    commands = _review_commands(args.verify, repo=repo)
    patch_bytes, _production_lines = capture_diff_bytes(repo, base_oid)
    if not patch_bytes:
        raise CliInputError("Review patch must not be empty")
    brief = args.brief.strip()
    manifest = ReviewManifest(
        kind="review", repo_path=str(repo), base_ref=args.base, base_oid=base_oid,
        brief=brief,
        brief_digest=hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        profile=args.profile,
        initial_patch_digest=hashlib.sha256(patch_bytes).hexdigest(),
        verification_commands=commands,
        knowledge_sources=[str(ref.path) for ref in refs],
        context_checksum=packet.checksum if packet is not None else None,
        review_executables={
            "codex": resolve_executable("codex", path=os.environ.get("PATH")),
            "claude": resolve_executable("claude", path=os.environ.get("PATH")),
        },
    )
    state = store.create(RunState.new_review(manifest))
    artifacts = store._run_directory(state)
    store.write_artifact_bytes(artifacts / "patches" / "round-0000.patch", patch_bytes)
    if packet is not None:
        PlanWorkflow(store, None, None, context_packet=packet)._persist_context_packet(
            artifacts, packet
        )
    return state


def _init_doc(args: argparse.Namespace, store: RunStore) -> RunState:
    """Create one read-only document review bound to exact bytes and one lens.

    The Plan and Review init paths build their packet on ``build_packet``'s own
    defaults and so never read policy at all.  A doc run must not inherit that:
    its caps are the policy's doc caps, read here, so the source cap and the
    token budget a caller is held to are the ones the run is created under.
    """
    repo = _resolve_repo(args.repo)
    document = _resolve_document(repo, args.doc)
    policy = _central_policy()
    max_sources, max_tokens = policy.context_limits("doc")
    refs = _doc_source_refs(args.source, max_sources=max_sources)
    # Freeze the base and prove every claimed section exists before any durable
    # state is created; a rejected request must leave no runnable run behind.
    base_oid = _oid(repo, args.base)
    packet = build_packet(
        refs, max_sources=max_sources, max_tokens=max_tokens,
        budget_method=BUDGET_METHOD_DOC,
    ) if refs else None
    brief = args.brief.strip()
    manifest = DocManifest(
        kind="doc", repo_path=str(repo), doc_path=str(document),
        base_ref=args.base, base_oid=base_oid,
        lens=args.lens, lens_reason=args.lens_reason, brief=brief,
        brief_digest=hashlib.sha256(brief.encode("utf-8")).hexdigest(),
        doc_digest=hashlib.sha256(document.read_bytes()).hexdigest(),
        knowledge_sources=[str(ref.path) for ref in refs],
        context_checksum=packet.checksum if packet is not None else None,
        # A doc run only ever invokes Codex, but the identity binding is whole
        # or empty; dropping Claude here would quietly weaken the check.
        review_executables={
            "codex": resolve_executable("codex", path=os.environ.get("PATH")),
            "claude": resolve_executable("claude", path=os.environ.get("PATH")),
        },
    )
    state = store.create(RunState.new_doc(manifest))
    if packet is not None:
        PlanWorkflow(store, None, None, context_packet=packet)._persist_context_packet(
            store._run_directory(state), packet
        )
    return state


def _rebound_doc_run(args: argparse.Namespace, store: RunStore) -> RunState:
    """Rebind one finished doc review to the document's new bytes.

    This is the second half of the human loop: read the findings, edit the
    document, ask again.  The run is parked at ``AWAITING_HUMAN_DOC_REVIEW``,
    which the doc workflow halts on, so it takes a deliberate command to move
    it back to ``RUNNING`` — and the previous round's findings already reach
    the next ``_review_input`` through ``unresolved-findings.json``, so nothing
    is carried forward by hand here.
    """
    state = store.load(args.run_id)
    if state.kind != "doc" or not isinstance(state.manifest, DocManifest):
        raise CliInputError("re-review requires a doc run")
    if state.status != Status.AWAITING_HUMAN_DOC_REVIEW:
        raise CliInputError(
            "re-review requires a doc review awaiting its reader, not %s"
            % state.status.value
        )
    repo = _resolve_repo(state.manifest.repo_path)
    # The same rule init used: readable, a regular file, and inside the repo.
    document = _resolve_document(repo, state.manifest.doc_path)
    try:
        digest = hashlib.sha256(document.read_bytes()).hexdigest()
    except OSError as error:
        raise CliInputError("document must be a readable regular file") from error
    if hmac.compare_digest(digest, state.manifest.doc_digest):
        # Reviewing the same bytes again would spend a whole Codex session to
        # produce the findings the operator is already holding.
        raise CliInputError("document is unchanged since the last review")
    state.rebind_document(digest)
    store.save(state)
    return state


def _approve(args: argparse.Namespace, store: RunStore) -> RunState:
    state = store.load(args.run_id)
    if state.kind != "plan" or state.manifest is None:
        raise CliInputError("approve-plan requires a Plan run")
    if state.status != Status.AWAITING_HUMAN_PLAN_REVIEW:
        raise CliInputError("Plan is not awaiting human review")
    repo = _resolve_repo(state.manifest.repo_path)
    try:
        Path(state.manifest.plan_path).resolve().relative_to(repo)
    except ValueError as error:
        raise CliInputError("Plan file is no longer inside the approved repository") from error
    if not state.manifest.base_oid or _oid(repo, state.manifest.base_oid) != state.manifest.base_oid:
        raise CliInputError("frozen Plan base commit is unavailable")
    actual = hashlib.sha256(Path(state.manifest.plan_path).read_bytes()).hexdigest()
    # This command has exactly one approval mechanism.  There is no command,
    # configuration, environment, or import-level selection point for a
    # caller-provided provider; the local macOS dialog is the production gate.
    receipt = MacOSHumanApprovalProvider().approve(
        run_id=state.run_id, plan_digest=actual, base_oid=state.manifest.base_oid,
        verification_commands=[
            command.to_dict() for command in state.manifest.verification_commands
        ],
        verification_digest=state.manifest.verification_digest(),
    )
    state.approve_plan(receipt=receipt, authority=store.authority)
    store.save(state)
    return state


def _validate_review_approval_inputs(
    store: RunStore, state: RunState
) -> tuple[str, str]:
    """Recapture every mutable Review binding before user-presence approval."""
    manifest = state.manifest
    if not isinstance(manifest, ReviewManifest):
        raise CliInputError("Review approval requires a Review manifest")
    repo = _resolve_repo(manifest.repo_path)
    if repo != Path(manifest.repo_path):
        raise CliInputError("Review repository binding changed")
    current_base_oid = _oid(repo, manifest.base_ref)
    current_patch, _production_lines = capture_diff_bytes(repo, current_base_oid)
    current_patch_digest = hashlib.sha256(current_patch).hexdigest()
    if current_base_oid != manifest.base_oid:
        raise CliInputError("current base does not match the Review base")
    if current_patch_digest != manifest.initial_patch_digest:
        raise CliInputError("current patch does not match the Review patch")
    artifacts = store._run_directory(state)
    initial = store.read_artifact_bytes(artifacts / "patches" / "round-0000.patch")
    if not hmac.compare_digest(hashlib.sha256(initial).hexdigest(), manifest.initial_patch_digest):
        raise CliInputError("initial Review patch artifact does not match the manifest")
    if manifest.context_checksum is not None:
        packet = PlanWorkflow(store, None, None)._load_context_packet(artifacts)
        if packet.checksum != manifest.context_checksum:
            raise CliInputError("Review context packet does not match the manifest")
        rebuilt = build_packet(
            tuple(
                SourceRef(source.path, source.section, source.reason, source.priority)
                for source in packet.sources
            ),
            max_tokens=packet.max_context_tokens,
            # The packet's own source count, not the Plan default: a packet that
            # legitimately carries more would otherwise fail the rebuild with a
            # cap error instead of the checksum comparison this gate is for.
            max_sources=len(packet.sources),
            budget_method=packet.budget_method,
        )
        if rebuilt.checksum != manifest.context_checksum:
            raise CliInputError("Review context sources changed before approval")
    return current_patch_digest, current_base_oid


def _approve_review(args: argparse.Namespace, store: RunStore) -> RunState:
    state = store.load(args.run_id)
    if (
        state.kind != "review"
        or not isinstance(state.manifest, ReviewManifest)
        or state.status != Status.AWAITING_REVIEW_APPROVAL
    ):
        raise CliInputError("approve-review requires Review awaiting approval")
    manifest = state.manifest
    _validate_review_approval_inputs(store, state)
    provider = _review_gate_provider(
        args, store, run_id=state.run_id, command="approve-review"
    )
    receipt = provider.approve_review(
        run_id=state.run_id, manifest_digest=manifest.digest(), brief=manifest.brief,
        base_oid=manifest.base_oid, initial_patch_digest=manifest.initial_patch_digest,
        verification_commands=[command.to_dict() for command in manifest.verification_commands],
    )
    # The dialog is open for as long as a person takes to read it, so the
    # pre-dialog capture is stale by construction. Recapture and rebind here:
    # approving bytes that are no longer on disk would let unapproved content
    # reach Codex under a signed scope approval.
    after_patch_digest, after_base_oid = _validate_review_approval_inputs(store, state)
    state.approve_review(receipt=receipt, authority=store.authority)
    state.validate_review_binding(after_patch_digest, after_base_oid)
    store.save(state)
    return state


MAX_PREFLIGHT_FILE_BYTES = 256 * 1024


def _read_preflight_submission(path_text: str) -> Any:
    """Read one bounded, regular specialist submission file."""
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        raise CliInputError("preflight findings path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CliInputError("preflight findings file cannot be read") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise CliInputError("preflight findings must be a regular file")
    if metadata.st_size > MAX_PREFLIGHT_FILE_BYTES:
        raise CliInputError("preflight findings file exceeds the allowed size")
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CliInputError("preflight findings file cannot be decoded") from error
    try:
        return load_preflight_text(contents)
    except PreflightError as error:
        raise CliInputError(str(error)) from error


def _approve_risk(args: argparse.Namespace, store: RunStore) -> RunState:
    """Bind one native approval to the exact high-risk patch awaiting a decision."""
    state = store.load(args.run_id)
    if (
        state.kind != "review"
        or not isinstance(state.manifest, ReviewManifest)
        or state.status != Status.PAUSED
    ):
        raise CliInputError("approve-risk requires a paused Review run")
    artifacts = store._run_directory(state)
    request_path = artifacts / "risk-approval-request.json"
    if not store.artifact_exists(request_path):
        raise CliInputError("run has no pending high-risk approval request")
    request = strict_json_loads(store.read_artifact_bytes(request_path).decode("utf-8"))
    if not isinstance(request, dict) or request.get("status") != "pending":
        raise CliInputError("run has no pending high-risk approval request")
    round_number = request.get("round")
    if type(round_number) is not int or round_number < 1:
        raise CliInputError("high-risk approval request is invalid")
    if request.get("manifest_digest") != state.manifest.digest():
        raise CliInputError("high-risk approval request does not match the Review manifest")
    categories = request.get("categories")
    evidence = request.get("evidence")
    if not isinstance(categories, list) or not categories or not isinstance(evidence, list):
        raise CliInputError("high-risk approval request is invalid")
    # Recapture before showing the dialog: a person must only ever be asked to
    # approve the bytes that are on disk right now.
    recorded = store.read_artifact_bytes(
        artifacts / "patches" / ("round-%04d.patch" % round_number)
    )
    current, _production_lines = capture_diff_bytes(
        Path(state.manifest.repo_path), state.manifest.base_oid,
    )
    patch_digest = hashlib.sha256(recorded).hexdigest()
    if patch_digest != request.get("patch_digest"):
        raise CliInputError("recorded high-risk patch does not match its request")
    if not hmac.compare_digest(hashlib.sha256(current).hexdigest(), patch_digest):
        raise CliInputError("current patch does not match the high-risk patch under review")
    provider = _review_gate_provider(
        args, store, run_id=state.run_id, command="approve-risk"
    )
    receipt = provider.approve_risk(
        run_id=state.run_id, manifest_digest=state.manifest.digest(),
        patch_digest=patch_digest, categories=[str(item) for item in categories],
        paths=sorted({
            str(item.get("path")) for item in evidence
            if isinstance(item, Mapping) and item.get("path")
        }),
    )
    record = sign_risk_approval(receipt, store.authority)
    verify_risk_approval(
        record, store.authority, run_id=state.run_id,
        manifest_digest=state.manifest.digest(), patch_digest=patch_digest,
    )
    store.write_artifact_bytes(
        artifacts / "risk-approvals" / ("round-%04d.json" % round_number),
        json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )
    return state


_CODE_CANDIDATE_FIELDS = {
    "run_id", "manifest_digest", "state_digest", "review_digest",
    "reviewed_patch_digest", "verification_snapshot_digest",
    "summary_digest", "knowledge_candidate_digest",
}
_CODE_APPROVAL_FIELDS = {
    "approved_at", "provider", "actor", "run_id", "candidate_digest",
    "receipt_digest", "nonce", "signature",
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _candidate_digest(candidate: Mapping[str, Any]) -> str:
    return _sha256(
        json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _code_approval_binding(run_id: str, candidate_digest: str) -> str:
    return _sha256(
        json.dumps([run_id, candidate_digest], separators=(",", ":")).encode("utf-8")
    )


def _validate_terminal_binding(store: RunStore, state: RunState) -> None:
    """Recheck the immutable approval binding for whichever kind reached PASS."""
    state.validate(store.authority)
    if state.kind == "code":
        state.validate_code_binding()
        return
    if state.kind != "review" or not isinstance(state.manifest, ReviewManifest):
        raise CliInputError("terminal approval requires a Code or Review run")
    if not isinstance(state.approval_attestation, ReviewApprovalAttestation):
        raise CliInputError("Review run lacks a signed scope approval")
    if state.approval_attestation.manifest_digest != state.manifest.digest():
        raise CliInputError("Review approval does not match the current manifest")
    repo = Path(state.manifest.repo_path)
    if git_worktree_root(repo) != repo:
        raise CliInputError("Review repository is not its own approved worktree root")
    if _oid(repo, state.manifest.base_oid) != state.manifest.base_oid:
        raise CliInputError("approved Review base commit is unavailable")


def _reviewed_code_candidate(
    store: RunStore, state: RunState, *, refresh_summary: bool = True
) -> tuple[dict[str, Any], str]:
    _validate_terminal_binding(store, state)
    artifacts = store._run_directory(state)
    if refresh_summary:
        generate_outputs(store, state.run_id)
    summary = store.read_artifact_bytes(artifacts / "final-summary.md")
    knowledge = store.read_optional_artifact_bytes(artifacts / "knowledge-candidate.md")
    patches = store.list_artifacts(artifacts / "patches", ".patch")
    actions = store.list_artifacts(artifacts / "code-review-actions", ".json")
    if not patches or not actions:
        raise CliInputError("Code candidate lacks reviewed patch or terminal evidence")
    terminal = strict_json_loads(store.read_artifact_bytes(actions[-1]).decode("utf-8"))
    if (
        not isinstance(terminal, dict)
        or set(terminal) != {
            "action", "review_sequence", "verification_round",
            "patch_name", "patch_digest",
        }
        or terminal.get("action") != "pass"
        or type(terminal.get("review_sequence")) is not int
        or terminal.get("verification_round") != terminal.get("review_sequence")
    ):
        raise CliInputError("terminal Code journal is not one atomic PASS sequence")
    sequence = terminal["review_sequence"]
    review_path = artifacts / "reviews" / ("%04d.json" % sequence)
    snapshot_path = artifacts / "verification-rounds" / (
        "%04d.json" % terminal["verification_round"]
    )
    patch_path = artifacts / "patches" / terminal["patch_name"]
    if not all(
        store.artifact_exists(path) for path in (review_path, snapshot_path, patch_path)
    ):
        raise CliInputError("terminal PASS journal references missing evidence")
    reviewed_patch = store.read_artifact_bytes(patch_path)
    if terminal["patch_digest"] != _sha256(reviewed_patch):
        raise CliInputError("terminal PASS patch digest is invalid")
    current_patch, _production_lines = capture_diff_bytes(
        Path(state.manifest.repo_path), state.manifest.base_oid
    )
    if not hmac.compare_digest(_sha256(current_patch), _sha256(reviewed_patch)):
        raise CliInputError("worktree changed after the latest reviewed patch")
    review_bytes = store.read_artifact_bytes(review_path)
    review = validate_codex_review(strict_json_loads(review_bytes.decode("utf-8")))
    if review["verdict"] != "PASS":
        raise CliInputError("terminal Code journal does not reference semantic PASS")
    snapshot_bytes = store.read_artifact_bytes(snapshot_path)
    snapshot = strict_json_loads(snapshot_bytes.decode("utf-8"))
    expected_verification = [
        list(command.argv) for command in state.manifest.verification_commands
    ]
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"results"}
        or not isinstance(snapshot["results"], list)
        or len(snapshot["results"]) != len(expected_verification)
        or any(
            not isinstance(item, dict)
            or set(item) != {
                "name", "argv", "exit_code", "duration_seconds", "stdout_path",
                "stderr_path", "relevant_output",
            }
            or item.get("exit_code") != 0
            or item.get("argv") != expected_verification[index]
            for index, item in enumerate(snapshot["results"])
        )
    ):
        raise CliInputError("terminal verification snapshot is invalid or failing")
    candidate = {
        "run_id": state.run_id,
        "manifest_digest": state.manifest.digest(),
        "state_digest": _sha256(
            json.dumps(
                state.to_dict(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ),
        "review_digest": _sha256(review_bytes),
        "reviewed_patch_digest": _sha256(reviewed_patch),
        "verification_snapshot_digest": _sha256(snapshot_bytes),
        "summary_digest": _sha256(summary),
        "knowledge_candidate_digest": _sha256(knowledge) if knowledge is not None else None,
    }
    digest = _candidate_digest(candidate)
    if refresh_summary:
        store._atomic_write(artifacts / "code-review-candidate.json", candidate)
    return candidate, digest


def _approve_code(args: argparse.Namespace, store: RunStore) -> RunState:
    state = store.load(args.run_id)
    if (
        state.kind not in ("code", "review")
        or state.status != Status.AWAITING_HUMAN_CODE_REVIEW
    ):
        raise CliInputError("approve-code requires reviewed Code awaiting human review")
    candidate, digest = _reviewed_code_candidate(store, state)
    # The terminal gate has exactly one approval mechanism.  Review's --auto
    # stops at the loop's edge: a person reads this diff.
    presence = MacOSHumanApprovalProvider().approve_code(
        run_id=state.run_id, candidate_digest=digest
    )
    nonce = secrets.token_hex(16)
    receipt_digest = hashlib.sha256(
        json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    approval = {
        **presence, "run_id": state.run_id, "candidate_digest": digest,
        "receipt_digest": receipt_digest, "nonce": nonce,
    }
    approval["signature"] = store.authority.sign(
        approval["actor"], approval["provider"], approval["approved_at"],
        _code_approval_binding(state.run_id, digest), nonce, receipt_digest,
    )
    store._atomic_write(store._run_directory(state) / "code-approval.json", approval)
    return state


def _writeback_knowledge(
    store: RunStore, state: RunState, *, workspace_root: Path = PRODUCTION_WORKSPACE_ROOT
) -> Path:
    if (
        state.kind not in ("code", "review")
        or state.status != Status.AWAITING_HUMAN_CODE_REVIEW
    ):
        raise CliInputError("knowledge writeback requires completed Code review")
    artifacts = store._run_directory(state)
    try:
        candidate = strict_json_loads(
            store.read_artifact_bytes(artifacts / "code-review-candidate.json").decode("utf-8")
        )
        approval = strict_json_loads(
            store.read_artifact_bytes(artifacts / "code-approval.json").decode("utf-8")
        )
    except (UnicodeDecodeError, ValueError, OSError) as error:
        raise CliInputError("Code candidate or approval is malformed") from error
    if (
        not isinstance(candidate, dict)
        or set(candidate) != _CODE_CANDIDATE_FIELDS
        or not isinstance(approval, dict)
        or set(approval) != _CODE_APPROVAL_FIELDS
        or any(not isinstance(approval.get(key), str) or not approval[key] for key in _CODE_APPROVAL_FIELDS)
    ):
        raise CliInputError("Code candidate or approval has an invalid shape")
    candidate_digest = _candidate_digest(candidate)
    receipt_digest = _candidate_digest(candidate)
    expected = store.authority.sign(
        approval["actor"], approval["provider"], approval["approved_at"],
        _code_approval_binding(state.run_id, candidate_digest),
        approval["nonce"], approval["receipt_digest"],
    )
    if (
        approval.get("run_id") != state.run_id
        or approval.get("candidate_digest") != candidate_digest
        or approval.get("receipt_digest") != receipt_digest
        or not hmac.compare_digest(approval.get("signature", ""), expected)
    ):
        raise CliInputError("Code approval is invalid or does not match candidate")
    current_candidate, current_digest = _reviewed_code_candidate(
        store, state, refresh_summary=False
    )
    if current_candidate != candidate or current_digest != candidate_digest:
        raise CliInputError("Code candidate evidence changed after approval")
    body = store.read_artifact_bytes(artifacts / "knowledge-candidate.md")
    if hashlib.sha256(body).hexdigest() != candidate.get("knowledge_candidate_digest"):
        raise CliInputError("knowledge candidate is absent or changed")
    root = Path(workspace_root).expanduser().resolve()
    if workspace_root == PRODUCTION_WORKSPACE_ROOT and root != PRODUCTION_WORKSPACE_ROOT:
        raise CliInputError("canonical workspace is unavailable")
    descriptor = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in ("second-brain", "ai-review"):
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        leaf = "%s.md" % state.run_id
        temporary = ".%s-%s.tmp" % (state.run_id, secrets.token_hex(8))
        temp_fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=descriptor,
        )
        try:
            offset = 0
            while offset < len(body):
                offset += os.write(temp_fd, body[offset:])
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        try:
            os.link(temporary, leaf, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
        return root / "second-brain" / "ai-review" / leaf
    finally:
        os.close(descriptor)


def main(
    argv: Optional[list[str]] = None,
    *,
    store_factory: Callable[[argparse.Namespace], RunStore] = _store_from_args,
    workflow_factory: Callable[..., Any] = _default_workflow_factory,
    status_store_factory: Callable[[argparse.Namespace], RunStore] = _status_store_from_args,
) -> int:
    """Run one command with injectable real-boundary factories for tests."""
    try:
        args = _parser().parse_args(argv)
        store = status_store_factory(args) if args.command == "status" else store_factory(args)
        if args.command == "init":
            if args.kind == "plan":
                state = _init_plan(args, store)
            elif args.kind == "code":
                state = _init_code(args, store)
            elif args.kind == "doc":
                state = _init_doc(args, store)
            else:
                state = _init_review(args, store)
        elif args.command == "status":
            state = store.load_status(args.run_id)  # Status is deliberately read-only.
        elif args.command == "approve-plan":
            state = _approve(args, store)
        elif args.command == "approve-code":
            state = _approve_code(args, store)
        elif args.command == "approve-review":
            state = _approve_review(args, store)
        elif args.command == "approve-risk":
            state = _approve_risk(args, store)
        elif args.command == "writeback-knowledge":
            state = store.load(args.run_id)
            _writeback_knowledge(store, state)
        elif args.command == "answer":
            answers = _read_json_object(args.answers)
            state = store.load(args.run_id)
            # Both checked here, not inside the workflow, so a wrong state or a
            # bad file costs the operator a retry instead of the whole run.
            _answerable(state)
            answers = _checked_answers(store, state, answers)
            state = _workflow(store, state, workflow_factory).answer(state.run_id, answers)
        elif args.command == "submit-preflight":
            submission = _read_preflight_submission(args.findings)
            state = store.load(args.run_id)
            if state.kind != "review":
                raise CliInputError("submit-preflight requires a Review run")
            state = _workflow(store, state, workflow_factory).submit_preflight(
                state.run_id, submission
            )
        elif args.command == "re-review":
            # Rebind first, then run exactly as `run` does, so a second round
            # is backgroundable and resumable like every other long command.
            state = _rebound_doc_run(args, store)
            state = _workflow(store, state, workflow_factory).run(state.run_id)
        elif args.command == "expand-context":
            state = store.load(args.run_id)
            state = _workflow(store, state, workflow_factory).provide_context(
                state.run_id, _source_refs(args.source)
            )
        else:
            state = store.load(args.run_id)
            state = _workflow(store, state, workflow_factory).run(state.run_id)
        if args.command != "status" and state.status in (
            Status.AWAITING_HUMAN_PLAN_REVIEW,
            Status.AWAITING_HUMAN_CODE_REVIEW,
            Status.AWAITING_HUMAN_DOC_REVIEW,
            Status.PAUSED,
        ):
            generate_outputs(store, state.run_id)
        _emit(_payload(store, state))
        return 0
    except RunnerInterrupted:
        # Workflows persist PAUSED before propagating the interruption. Reload
        # that durable state so callers still receive the compact recovery
        # payload and generated summary together with exit 3.
        try:
            interrupted = store.load(args.run_id)
            if interrupted.status == Status.PAUSED:
                generate_outputs(store, interrupted.run_id)
                _emit(_payload(store, interrupted))
        except (NameError, AttributeError, ValueError, OSError, RunnerError):
            pass
        sys.stderr.write("ai-review: interrupted external execution\n")
        return EXIT_INTERRUPTED
    except KeyboardInterrupt:
        sys.stderr.write("ai-review: interrupted external execution\n")
        return EXIT_INTERRUPTED
    except AutoApprovalRateLimited as error:
        sys.stderr.write(
            "ai-review: %s (limit %d per %ds)\n"
            % (_safe_error(error), MAX_AUTO_APPROVALS, int(WINDOW_SECONDS))
        )
        return EXIT_RATE_LIMITED
    except (CliInputError, ValueError, OSError, RunnerError) as error:
        sys.stderr.write("ai-review: %s\n" % _safe_error(error))
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
