"""Crash-safe Plan consensus workflow with explicit human approval gates.

The workflow owns only the Plan review loop.  It records model outputs before
interpreting them, keeps Codex's review input deliberately narrow, and never
turns a model PASS into an approval: a PASS only reaches human Plan review.
"""

import hashlib
import hmac
import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from .context import (
    BUDGET_METHOD, ContextExpansionError, KnowledgePacket, SourceExcerpt, SourceRef,
    build_packet, expand_packet, validate_knowledge_packet,
)
from .models import RunState, Status, Verdict, canonical_answer_submission, canonical_user_question_ids
from .policy import Policy, load_policy
from .git_diff import capture_diff_bytes
from .runners import (
    DEFAULT_VERIFICATION_TIMEOUT,
    RunnerError,
    RunnerInterrupted,
    VerificationResult,
    run_verification,
    validate_claude_resolution,
    validate_codex_review,
    validate_plan_repair,
    validate_plan_update,
)
from .store import RunStore, git_worktree_root
from .process_security import validate_executable_identity


class WorkflowError(ValueError):
    """A request cannot safely advance the persisted Plan workflow."""


class AnswerConflict(WorkflowError):
    """A retry supplied answers different from the immutable submission."""


class AmbiguousExternalCall(WorkflowError):
    """An external invocation may have completed but has no durable result."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_text(path: Path, contents: str) -> None:
    """Write text durably in the target directory before replacing the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".%s-" % path.name, dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class PlanWorkflow:
    """Run and resume a bounded Codex/Claude Plan consensus loop.

    ``codex`` supplies ``review(inputs)`` and ``claude`` supplies
    ``resolve(inputs)`` plus ``update_plan(inputs)``.  Keeping those small
    interfaces makes the orchestration testable while runners retain command
    safety and structured-output validation boundaries.
    """

    # Statuses that mean the loop has already stopped at a gate.  A subclass
    # whose run ends somewhere else adds that status here rather than copying
    # ``run``.
    # INTERRUPTED is deliberately absent: an interrupted run is one whose
    # external call failed for a reason outside the work, and re-entering it is
    # the whole point of the status.  The loop still stops on any status that is
    # not RUNNING.
    _HALT_STATUSES = (
        Status.AWAITING_HUMAN_PLAN_REVIEW,
        Status.AWAITING_USER_INPUT,
        Status.PAUSED,
    )

    # How many replacement candidates a human may offer for one expansion.
    # This is not an initial-source cap and does not come from
    # ``context_limits``: ``expand_packet`` swaps exactly one excerpt, so this
    # bounds the alternatives offered, never how large the packet becomes.
    _MAX_EXPANSION_CANDIDATES = 3

    # What an answered question does next, written into ``decision-log.md`` and
    # read by the next Codex round.  It belongs to the subclass because it is a
    # claim about that kind's own loop: a plan or code answer really is waiting
    # on Claude, and a kind whose answers are followed by something else says so
    # here rather than reimplementing ``_append_decisions``.
    _DECISION_IMPACT = "pending Claude Plan update"

    def __init__(
        self,
        store: RunStore,
        codex: Any,
        claude: Any,
        *,
        policy: Optional[Policy] = None,
        context_packet: Optional[KnowledgePacket] = None,
        context_resolver: Optional[Callable[[Iterable[str]], Iterable[SourceRef]]] = None,
        fault_injector: Optional[Callable[[str], None]] = None,
    ):
        self.store = store
        self.codex = codex
        self.claude = claude
        self.policy = policy or load_policy(
            Path(__file__).resolve().parent.parent / "config" / "defaults.yaml"
        )
        self.context_packet = context_packet
        self.context_resolver = context_resolver
        self.fault_injector = fault_injector

    def run(self, run_id: str) -> RunState:
        """Advance until a human/user/policy gate stops the Plan loop."""
        state = self.store.load(run_id)
        try:
            self._validate_state(state)
            if state.status in self._HALT_STATUSES:
                return state
            self._set_running(state)
            self.store.save(state)
            artifacts = self._artifacts(state)
            self._ensure_context_reference(artifacts)
            while state.status == Status.RUNNING:
                sequence, review = self._pending_review(state, artifacts)
                if review is None:
                    sequence = self._next_sequence(artifacts / "reviews")
                    review = self.codex.review(self._review_input(state, artifacts))
                    # Raw model output is an audit record, including invalid output.
                    self._write_json(artifacts / "reviews" / ("%04d.json" % sequence), review)
                self._handle_review(state, artifacts, sequence, review)
            return state
        except RunnerInterrupted as error:
            self._interrupt_loaded(state, "RUNNER_INTERRUPTED", str(error))
            raise
        except Exception as error:
            # A malformed model result, invalid state, or runner failure must never
            # silently leave a Plan runnable as though its review succeeded.
            return self._pause_loaded(state, "WORKFLOW_ERROR", str(error))

    def provide_context(self, run_id: str, sources: Iterable[SourceRef]) -> RunState:
        """Resume one persisted CONTEXT_REQUEST with exact bounded sections."""
        state = self.store.load(run_id)
        artifacts = self._artifacts(state)
        pause = self._read_json(artifacts / "pause.json")
        pending = self._read_json(artifacts / "pending-context-request.json")
        if (
            state.status != Status.PAUSED
            or pause.get("reason") != "CONTEXT_INPUT_REQUIRED"
            or not isinstance(pending.get("requests"), list)
            or pending.get("status") != "pending"
        ):
            raise WorkflowError("run is not awaiting bounded context input")
        sequence = pending.get("review_sequence")
        if type(sequence) is not int:
            raise WorkflowError("pending context request has no review sequence")
        review = self._read_json(artifacts / "reviews" / ("%04d.json" % sequence))
        requests = review.get("context_requests") if isinstance(review, dict) else None
        request_digest = hashlib.sha256(json.dumps(
            {"review_sequence": sequence, "requests": requests},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if (
            review.get("verdict") != "CONTEXT_REQUEST"
            or requests != pending["requests"]
            or not hmac.compare_digest(request_digest, pending.get("request_digest", ""))
        ):
            raise WorkflowError("pending context request is stale or unbound")
        selected = tuple(sources)
        limit = self._resume_source_limit(state, artifacts)
        if not selected or len(selected) > limit:
            raise WorkflowError("context resume requires one to %d exact sections" % limit)
        def one_shot(actual_requests: Iterable[str]) -> tuple[SourceRef, ...]:
            if tuple(actual_requests) != tuple(requests):
                raise WorkflowError("context sources do not match the pending request")
            self.context_resolver = None
            return selected
        self.context_resolver = one_shot
        self._set_running(state)
        self.store.save(state)
        return self.run(run_id)

    def answer(self, run_id: str, answers: Mapping[str, str]) -> RunState:
        """Record user answers, obtain Claude's Plan update, then resume review."""
        state = self.store.load(run_id)
        try:
            self._validate_state(state)
            if state.status != Status.AWAITING_USER_INPUT:
                raise WorkflowError("Plan is not awaiting user input")
            artifacts = self._artifacts(state)
            cycle, cycle_dir = self._active_question_cycle(artifacts)
            questions = self._read_questions(artifacts, cycle)
            normalized = self._validate_answers(questions, answers)
            submission = self._answer_submission(artifacts, normalized, cycle)
            normalized = submission["answers"]
            self._write_immutable_json(cycle_dir / "answers.json", submission)
            # Compatibility pointer only; the immutable source of truth is the
            # sequenced cycle directory.
            self._write_json(
                artifacts / "user-answers.json",
                {"answers": submission["answers"], "digest": submission["digest"]},
            )
            self._append_decisions(artifacts, questions, normalized)

            update_path = cycle_dir / "plan-update.json"
            legacy_update = artifacts / "answer-updates" / "0001.json"
            if cycle == 1 and not self.store.artifact_exists(update_path) and self.store.artifact_exists(legacy_update):
                self._write_json(update_path, self._read_json(legacy_update))
            if self.store.artifact_exists(update_path):
                update = self._read_json(update_path)
            else:
                update = self.claude.update_plan(
                    {
                        "plan": self._plan_text(state),
                        "decision_log": self._decision_log(artifacts),
                        "answers": normalized,
                        "answer_digest": submission["digest"],
                    }
                )
                self._write_json(update_path, update)
                if cycle == 1:
                    self._write_json(legacy_update, update)
            update = validate_plan_update(update)
            if update["answer_digest"] != submission["digest"]:
                raise WorkflowError("Claude Plan update does not match the immutable answer submission")
            self._apply_plan_update(state, artifacts, update, submission)
            self._set_running(state)
            self.store.save(state)
            return self.run(run_id)
        except AnswerConflict:
            # A conflicting retry must not change the Plan, answer record, or
            # state; the caller can retry only the original immutable answers.
            raise
        except RunnerInterrupted as error:
            self._interrupt_loaded(state, "RUNNER_INTERRUPTED", str(error))
            raise
        except Exception as error:
            return self._pause_loaded(state, "INVALID_USER_ANSWER_OR_PLAN_UPDATE", str(error))

    def _apply_plan_update(
        self, state: RunState, artifacts: Path, update: Mapping[str, Any], submission: Mapping[str, Any]
    ) -> None:
        """Persist exactly the validated Plan content, never a model-chosen path."""
        plan = update["plan"]
        path = Path(state.plan_path)
        current = self._plan_text(state)
        current_digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
        previous_digest = plan["previous_digest"]
        new_digest = plan["new_digest"]
        if previous_digest == new_digest:
            raise WorkflowError("Plan update must change the Plan digest")
        if current_digest == previous_digest:
            _atomic_text(path, plan["content"])
            self._fault("after_plan_persist")
            current = self._plan_text(state)
            current_digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
        elif current_digest != new_digest:
            raise WorkflowError("Plan update does not match the persisted Plan")
        if current_digest != new_digest or current != plan["content"]:
            raise WorkflowError("persisted Plan does not match the verified update")
        action = {
            "action": "plan_updated",
            "previous_digest": previous_digest,
            "new_digest": new_digest,
            "changed_sections": plan["changed_sections"],
            "answer_digest": submission["digest"],
            "answers": submission["answers"],
        }
        cycle = submission["cycle"]
        action_path = artifacts / "question-cycles" / ("%04d" % cycle) / "plan-update-action.json"
        if self.store.artifact_exists(action_path):
            if self._read_json(action_path) != action:
                raise WorkflowError("Plan update action journal conflicts with verified update")
        else:
            self._write_json(action_path, action)
        if cycle == 1:
            legacy_action = artifacts / "plan-update-actions" / "0001.json"
            if self.store.artifact_exists(legacy_action):
                if self._read_json(legacy_action) != action:
                    raise WorkflowError("legacy Plan update journal conflicts")
            else:
                self._write_json(legacy_action, action)

    def _handle_review(self, state: RunState, artifacts: Path, sequence: int, raw_review: Any) -> None:
        action = artifacts / "review-actions" / ("%04d.json" % sequence)
        if self.store.artifact_exists(action):
            self._resume_action(state, artifacts, action)
            return
        try:
            review = validate_codex_review(raw_review)
        except (TypeError, ValueError) as error:
            self._pause(state, artifacts, "INVALID_CODEX_REVIEW", str(error))
            return

        verdict = Verdict(review["verdict"])
        if verdict == Verdict.PASS:
            state.transition(verdict, self.policy.max_rounds)
            self.store.save(state)
            self._write_json(action, {"action": "pass"})
            return
        if verdict == Verdict.NEEDS_USER_INPUT:
            cycle = self._write_questions(artifacts, review["questions"], sequence)
            state.transition(verdict, self.policy.max_rounds)
            self.store.save(state)
            self._write_json(action, {"action": "awaiting_user_input", "question_cycle": cycle})
            return
        if verdict == Verdict.CONTEXT_REQUEST:
            self._expand_context(state, artifacts, sequence, review["context_requests"])
            if state.status == Status.RUNNING:
                self._write_json(action, {"action": "context_expanded"})
            return
        self._handle_changes(state, artifacts, sequence, review, action)

    def _resume_action(self, state: RunState, artifacts: Path, action: Path) -> None:
        """Finish a state transition if a crash followed an action journal write."""
        record = self._read_json(action)
        if not isinstance(record, dict):
            raise WorkflowError("review action journal is invalid")
        if record.get("action") != "changes":
            return
        before_round = record.get("before_round")
        if type(before_round) is not int or before_round < 0:
            raise WorkflowError("change action journal is invalid")
        if state.repair_round == before_round:
            state.transition(Verdict.CHANGES_REQUIRED, self.policy.max_rounds)
            self.store.save(state)
            if state.status == Status.PAUSED:
                self._write_json(
                    artifacts / "pause.json",
                    {"reason": "MAX_REPAIR_ROUNDS", "detail": "policy limit reached"},
                )
        elif state.repair_round < before_round:
            raise WorkflowError("review action journal is ahead of persisted state")

    def _handle_changes(
        self, state: RunState, artifacts: Path, sequence: int, review: Mapping[str, Any], action: Path
    ) -> None:
        if self._moves_review_gate(state, artifacts, review["findings"]):
            self._pause(state, artifacts, "REVIEW_GATE_MOVED", "new later finding lacks introduced_by_fix or newly_discovered lineage")
            return
        required_ids = [item["id"] for item in review["findings"]]
        if not required_ids:
            self._pause(state, artifacts, "CHANGES_WITHOUT_FINDING", "changes require at least one finding")
            return
        resolution_path = artifacts / "resolutions" / ("%04d.json" % sequence)
        current_plan = self._plan_text(state)
        current_digest = hashlib.sha256(current_plan.encode("utf-8")).hexdigest()
        decision_digest = hashlib.sha256(
            self._decision_log(artifacts).encode("utf-8")
        ).hexdigest()
        if self.store.artifact_exists(resolution_path):
            raw_resolution = self._read_json(resolution_path)
        else:
            raw_resolution = self.claude.resolve(
                {
                    "plan": current_plan,
                    "plan_digest": current_digest,
                    "decision_log": self._decision_log(artifacts),
                    "decision_log_digest": decision_digest,
                    "findings": review["findings"],
                    "finding_ids": required_ids,
                    "context_manifest": self._context_manifest(artifacts),
                    "knowledge_packet": self._knowledge_packet(artifacts),
                }
            )
            self._write_json(resolution_path, raw_resolution)
        try:
            input_digest = (
                raw_resolution.get("input_plan_digest")
                if isinstance(raw_resolution, dict) else None
            )
            # Derived, never model-reported: the repair contract cannot ask a
            # shell-free reviewer to hash its own output (see validate_plan_repair).
            repaired_content = (
                raw_resolution.get("plan", {}).get("content")
                if isinstance(raw_resolution, dict)
                and isinstance(raw_resolution.get("plan"), dict) else None
            )
            new_digest = (
                hashlib.sha256(repaired_content.encode("utf-8")).hexdigest()
                if isinstance(repaired_content, str) and repaired_content else None
            )
            persisted_digest = hashlib.sha256(
                self._plan_text(state).encode("utf-8")
            ).hexdigest()
            if persisted_digest not in {input_digest, new_digest}:
                raise ValueError("persisted Plan is neither repair input nor output")
            resolution = validate_plan_repair(
                raw_resolution,
                required_finding_ids=required_ids,
                current_plan_digest=input_digest,
                decision_log_digest=decision_digest,
            )
        except (TypeError, ValueError) as error:
            self._pause(state, artifacts, "INVALID_CLAUDE_RESOLUTION", str(error))
            return
        if any(item["outcome"] == "needs_user_input" for item in resolution["resolutions"]):
            self._pause(state, artifacts, "CLAUDE_NEEDS_USER_INPUT", "Claude resolution needs a structured question")
            return
        self._apply_finding_plan_repair(state, artifacts, sequence, resolution)
        if self._repeated_dispute(artifacts, review, resolution):
            self._pause(state, artifacts, "REPEATED_IDENTICAL_DISPUTE", "same review and resolution recurred")
            return

        self._write_json(
            artifacts / "unresolved-findings.json",
            {"findings": []},
        )
        self._record_seen_findings(artifacts, review["findings"])
        before_round = state.repair_round
        self._write_json(action, {
            "action": "changes", "before_round": before_round,
            "previous_digest": resolution["plan"]["previous_digest"],
            "new_digest": resolution["plan"]["new_digest"],
        })
        state.transition(Verdict.CHANGES_REQUIRED, self.policy.max_rounds)
        self.store.save(state)
        if state.status == Status.PAUSED:
            self._write_json(artifacts / "pause.json", {"reason": "MAX_REPAIR_ROUNDS", "detail": "policy limit reached"})

    def _apply_finding_plan_repair(
        self, state: RunState, artifacts: Path, sequence: int,
        repair: Mapping[str, Any],
    ) -> None:
        plan = repair["plan"]
        current = self._plan_text(state)
        digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if digest == plan["previous_digest"]:
            _atomic_text(Path(state.plan_path), plan["content"])
            self._fault("after_plan_repair_persist")
            current = self._plan_text(state)
            digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
        elif digest != plan["new_digest"]:
            raise WorkflowError("persisted Plan conflicts with Claude Plan repair")
        if current != plan["content"] or digest != plan["new_digest"]:
            raise WorkflowError("persisted Plan does not match Claude Plan repair")
        journal = {
            "review_sequence": sequence,
            "previous_digest": plan["previous_digest"],
            "new_digest": plan["new_digest"],
            "changed_sections": plan["changed_sections"],
        }
        self._write_immutable_json(
            artifacts / "plan-repair-actions" / ("%04d.json" % sequence), journal
        )
        object.__setattr__(
            state, "manifest", replace(state.manifest, plan_digest=plan["new_digest"])
        )
        self.store.save(state)

    def _context_limits(self, state: RunState) -> tuple[int, int, str]:
        """(max_sources, max_tokens, budget_method) for this run's kind.

        Resolved from the run state rather than from a class constant so that
        one workflow class driving several kinds still bootstraps each one on
        its own budget.  A subclass whose packets are measured differently
        overrides only the estimator; the two caps stay the policy's.
        """
        max_sources, max_tokens = self.policy.context_limits(state.kind)
        return max_sources, max_tokens, BUDGET_METHOD

    def _resume_source_limit(self, state: RunState, artifacts: Path) -> int:
        """How many exact sections a human may supply for a pending request.

        The two resume paths bound different things.  With no packet yet, the
        sections supplied *are* the run's initial sources, so the kind's own
        cap applies and a doc run may use all five policy grants it.  With a
        packet already in hand the sections are replacement candidates for a
        single-excerpt swap, which is a different limit entirely.

        The condition mirrors how ``_expand_context`` chooses its branch, plus
        the packet ``_ensure_context_reference`` is about to persist, so the
        number quoted to a human matches the path their resume will take.
        """
        if (
            self.store.artifact_exists(artifacts / "context-reference.json")
            or self.context_packet is not None
        ):
            return self._MAX_EXPANSION_CANDIDATES
        return self._context_limits(state)[0]

    def _expand_context(
        self, state: RunState, artifacts: Path, sequence: int, requests: Iterable[str]
    ) -> None:
        if self.context_resolver is None and not self.store.artifact_exists(artifacts / "context-expansions" / ("%04d.json" % sequence)):
            requested = list(requests)
            self._write_json(
                artifacts / "pending-context-request.json",
                {
                    "status": "pending",
                    "review_sequence": sequence,
                    "requests": requested,
                    "request_digest": hashlib.sha256(json.dumps(
                        {"review_sequence": sequence, "requests": requested},
                        sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")).hexdigest(),
                },
            )
            self._pause(
                state, artifacts, "CONTEXT_INPUT_REQUIRED",
                "provide one to %d exact Markdown sections with expand-context"
                % self._resume_source_limit(state, artifacts),
            )
            return
        try:
            intent_path = artifacts / "context-expansions" / ("%04d.json" % sequence)
            if self.store.artifact_exists(intent_path):
                intent = self._read_json(intent_path)
                if intent.get("status") == "complete":
                    self.context_packet = self._load_context_packet(artifacts)
                    self._consume_pending_context(artifacts, sequence, requests)
                    return
                packet = self._load_context_packet(artifacts, intent["before_revision"])
                if packet.checksum != intent["before_checksum"]:
                    raise WorkflowError("context expansion intent does not match its packet")
                candidates = tuple(self._source_ref(item) for item in intent["candidates"])
            else:
                candidates = tuple(self.context_resolver(tuple(requests)))
                if not self.store.artifact_exists(artifacts / "context-reference.json"):
                    max_sources, max_tokens, budget_method = self._context_limits(state)
                    packet = build_packet(
                        candidates,
                        max_sources=max_sources,
                        max_tokens=max_tokens,
                        budget_method=budget_method,
                    )
                    intent = {
                        "status": "complete",
                        "requests": list(requests),
                        "before_revision": None,
                        "before_checksum": None,
                        "candidates": [
                            self._source_ref_dict(candidate) for candidate in candidates
                        ],
                        "after_revision": packet.revision,
                        "after_checksum": packet.checksum,
                        "bootstrap": True,
                    }
                    self._persist_context_packet(artifacts, packet)
                    self._write_json(intent_path, intent)
                    self.context_packet = packet
                    if state.kind == "plan" and state.approval_attestation is None:
                        object.__setattr__(
                            state, "manifest",
                            replace(state.manifest, context_checksum=packet.checksum),
                        )
                        self.store.save(state)
                    self._consume_pending_context(artifacts, sequence, requests)
                    return
                packet = self._load_context_packet(artifacts)
                if packet.expansion_count >= self.policy.max_context_expansions:
                    self._pause(state, artifacts, "MAX_CONTEXT_EXPANSIONS", "policy context expansion limit reached")
                    return
                intent = {
                    "status": "intent",
                    "requests": list(requests),
                    "before_revision": packet.revision,
                    "before_checksum": packet.checksum,
                    "candidates": [self._source_ref_dict(candidate) for candidate in candidates],
                }
                self._write_json(intent_path, intent)
            expanded = expand_packet(packet, candidates)
            self._fault("after_context_expansion")
            self._persist_context_packet(artifacts, expanded)
            self._write_json(
                intent_path,
                dict(intent, status="complete", after_revision=expanded.revision, after_checksum=expanded.checksum),
            )
            self.context_packet = expanded
            if state.kind == "plan" and state.approval_attestation is None:
                object.__setattr__(
                    state, "manifest",
                    replace(state.manifest, context_checksum=expanded.checksum),
                )
                self.store.save(state)
            self._consume_pending_context(artifacts, sequence, requests)
        except (ContextExpansionError, TypeError, ValueError) as error:
            self._pause(state, artifacts, "CONTEXT_EXPANSION_FAILED", str(error))

    def _consume_pending_context(
        self, artifacts: Path, sequence: int, requests: Iterable[str]
    ) -> None:
        path = artifacts / "pending-context-request.json"
        pending = self._read_json(path, {})
        if (
            pending.get("status") == "pending"
            and pending.get("review_sequence") == sequence
            and pending.get("requests") == list(requests)
        ):
            self._write_json(path, dict(pending, status="consumed"))

    def _ensure_context_reference(self, artifacts: Path) -> None:
        if self.store.artifact_exists(artifacts / "context-reference.json"):
            self.context_packet = self._load_context_packet(artifacts)
        elif self.context_packet is not None:
            self._persist_context_packet(artifacts, self.context_packet)

    def _persist_context_packet(self, artifacts: Path, packet: KnowledgePacket) -> None:
        record = self._packet_record(packet)
        revision_path = artifacts / "context-packets" / ("%04d.json" % packet.revision)
        if self.store.artifact_exists(revision_path):
            if self._read_json(revision_path) != record:
                raise WorkflowError("context packet revision is immutable")
        else:
            self._write_json(revision_path, record)
        self._write_json(
            artifacts / "context-reference.json",
            {"revision": packet.revision, "checksum": packet.checksum, "advisory_only": True},
        )
        self._write_json(artifacts / "context-manifest.json", packet.manifest_dict())
        self.store.write_artifact_bytes(
            artifacts / ("knowledge-packet-r%d.md" % packet.revision), packet.markdown.encode("utf-8")
        )

    def _load_context_packet(self, artifacts: Path, revision: Optional[int] = None) -> KnowledgePacket:
        if revision is None:
            reference = self._read_json(artifacts / "context-reference.json")
            if (
                not isinstance(reference, dict)
                or type(reference.get("revision")) is not int
                or not isinstance(reference.get("checksum"), str)
                or reference.get("advisory_only") is not True
            ):
                raise WorkflowError("context reference is invalid")
            revision = reference["revision"]
            expected_checksum = reference["checksum"]
        else:
            expected_checksum = None
        record = self._read_json(artifacts / "context-packets" / ("%04d.json" % revision))
        packet = self._packet_from_record(record, artifacts)
        if expected_checksum is not None and packet.checksum != expected_checksum:
            raise WorkflowError("context reference checksum does not match packet")
        return packet

    @staticmethod
    def _source_ref_dict(source: SourceRef) -> dict:
        return {
            "path": str(source.path),
            "section": source.section,
            "reason": source.reason,
            "priority": source.priority,
        }

    @staticmethod
    def _source_ref(value: Any) -> SourceRef:
        if not isinstance(value, dict) or set(value) != {"path", "section", "reason", "priority"}:
            raise WorkflowError("context expansion candidate is invalid")
        return SourceRef(value["path"], value["section"], value["reason"], value["priority"])

    @staticmethod
    def _packet_record(packet: KnowledgePacket) -> dict:
        return {
            "sources": [
                {
                    "path": source.path,
                    "section": source.section,
                    "reason": source.reason,
                    "priority": source.priority,
                    "source_checksum": source.source_checksum,
                    "markdown": source.markdown,
                    "estimated_tokens": source.estimated_tokens,
                }
                for source in packet.sources
            ],
            "checked_not_selected": list(packet.checked_not_selected),
            "max_context_tokens": packet.max_context_tokens,
            "estimated_tokens": packet.estimated_tokens,
            "checksum": packet.checksum,
            "markdown": packet.markdown,
            "revision": packet.revision,
            "expansion_count": packet.expansion_count,
            "budget_method": packet.budget_method,
            "advisory_only": True,
        }

    @staticmethod
    def _packet_from_record(record: Any, artifacts: Path) -> KnowledgePacket:
        required = {
            "sources", "checked_not_selected", "max_context_tokens", "estimated_tokens", "checksum",
            "markdown", "revision", "expansion_count", "advisory_only",
        }
        # budget_method is optional: records written before the doc kind lack it.
        if (
            not isinstance(record, dict)
            or set(record) - {"budget_method"} != required
            or record["advisory_only"] is not True
        ):
            raise WorkflowError("persisted context packet is invalid")
        try:
            sources = tuple(SourceExcerpt(**source) for source in record["sources"])
            packet = KnowledgePacket(
                sources=sources,
                checked_not_selected=tuple(record["checked_not_selected"]),
                max_context_tokens=record["max_context_tokens"],
                estimated_tokens=record["estimated_tokens"],
                checksum=record["checksum"],
                markdown=record["markdown"],
                revision=record["revision"],
                expansion_count=record["expansion_count"],
                budget_method=record.get("budget_method", BUDGET_METHOD),
                # Workflow persists run context exclusively through RunStore;
                # passing an artifact pathname to context.expand_packet would
                # reintroduce untrusted Path-based run-artifact writes.
                output_dir=None,
            )
            return validate_knowledge_packet(packet)
        except (TypeError, ValueError) as error:
            raise WorkflowError("persisted context packet is invalid") from error

    def _fault(self, point: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(point)

    def _review_input(self, state: RunState, artifacts: Path) -> dict:
        # This exact allowlist is a trust boundary: no broad repository contents,
        # credentials, transcripts, or prior model chatter reach the reviewer.
        return {
            "plan": self._plan_text(state),
            "decision_log": self._decision_log(artifacts),
            "context_manifest": self._context_manifest(artifacts),
            "knowledge_packet": self._knowledge_packet(artifacts),
            "policy": {
                "version": self.policy.version,
                "max_rounds": self.policy.max_rounds,
                "max_context_tokens": self.policy.max_context_tokens,
                "max_context_expansions": self.policy.max_context_expansions,
            },
            "unresolved_prior_findings": self._unresolved_findings(artifacts),
        }

    def _moves_review_gate(self, state: RunState, artifacts: Path, findings: Iterable[Mapping[str, Any]]) -> bool:
        # Anti-ratchet: after round 0 the finding set may only carry over, or
        # grow with a typed lineage — introduced_by_fix, or newly_discovered
        # with a stated discovery reason. The reviewer prompt states this
        # contract; an unseen ID claiming any other lineage moves the gate.
        if state.repair_round < 1:
            return False
        seen = set(self._read_json(artifacts / "seen-findings.json", {"ids": []})["ids"])
        for item in findings:
            if item["id"] in seen:
                continue
            lineage = item["lineage"]
            if lineage["resolution"] == "introduced_by_fix":
                continue
            if lineage["resolution"] == "newly_discovered" and lineage.get("discovery_reason"):
                continue
            return True
        return False

    def _repeated_dispute(self, artifacts: Path, review: Mapping[str, Any], resolution: Mapping[str, Any]) -> bool:
        fingerprint = json.dumps(
            {"findings": review["findings"], "resolutions": resolution["resolutions"]},
            sort_keys=True,
            separators=(",", ":"),
        )
        path = artifacts / "disputes.json"
        records = self._read_json(path, {"fingerprints": []})
        if fingerprint in records["fingerprints"]:
            return True
        records["fingerprints"].append(fingerprint)
        self._write_json(path, records)
        return False

    def _record_seen_findings(self, artifacts: Path, findings: Iterable[Mapping[str, Any]]) -> None:
        path = artifacts / "seen-findings.json"
        recorded = self._read_json(path, {"ids": []})
        recorded["ids"] = sorted(set(recorded["ids"]).union(item["id"] for item in findings))
        self._write_json(path, recorded)

    def _write_questions(self, artifacts: Path, questions: Iterable[str], review_sequence: int) -> int:
        cycles = [
            int(path.name) for path in self.store.list_artifact_directories(artifacts / "question-cycles")
            if path.name.isdigit() and path.is_dir()
        ]
        requested = tuple(questions)
        for existing_cycle in cycles:
            existing = self._read_json(
                artifacts / "question-cycles" / ("%04d" % existing_cycle) / "questions.json"
            )
            if existing.get("review_sequence") == review_sequence:
                if [item.get("question") for item in existing.get("questions", [])] != list(requested):
                    raise WorkflowError("question cycle conflicts with persisted review")
                self._write_json(
                    artifacts / "user-questions.json",
                    {"questions": existing["questions"]},
                )
                return existing_cycle
        cycle = max(cycles, default=0) + 1
        prior_count = 0
        for prior in cycles:
            value = self._read_json(
                artifacts / "question-cycles" / ("%04d" % prior) / "questions.json"
            )
            prior_count += len(value.get("questions", []))
        payload = {
            "cycle": cycle,
            "review_sequence": review_sequence,
            "questions": [
                {"id": "Q-%03d" % (prior_count + index + 1), "question": question}
                for index, question in enumerate(requested)
            ],
        }
        cycle_path = artifacts / "question-cycles" / ("%04d" % cycle) / "questions.json"
        self._write_immutable_json(cycle_path, payload)
        self._write_json(artifacts / "user-questions.json", {"questions": payload["questions"]})
        return cycle

    def _active_question_cycle(self, artifacts: Path) -> tuple[int, Path]:
        cycles = [
            int(path.name) for path in self.store.list_artifact_directories(artifacts / "question-cycles")
            if path.name.isdigit() and path.is_dir()
        ]
        if not cycles:
            # A legacy first-cycle run is migrated into the sequenced layout.
            legacy = self._read_json(artifacts / "user-questions.json")
            cycle_path = artifacts / "question-cycles" / "0001"
            self._write_immutable_json(
                cycle_path / "questions.json",
                {"cycle": 1, "review_sequence": 1, "questions": legacy["questions"]},
            )
            return 1, cycle_path
        cycle = max(cycles)
        return cycle, artifacts / "question-cycles" / ("%04d" % cycle)

    def _read_questions(self, artifacts: Path, cycle: Optional[int] = None) -> list[dict]:
        if cycle is None:
            cycle, _ = self._active_question_cycle(artifacts)
        contents = self._read_json(
            artifacts / "question-cycles" / ("%04d" % cycle) / "questions.json"
        )
        questions = contents.get("questions") if isinstance(contents, dict) else None
        try:
            canonical_user_question_ids(questions)
        except ValueError as error:
            raise WorkflowError("persisted user questions are invalid") from error
        return questions

    def _validate_answers(self, questions: list[dict], answers: Mapping[str, str]) -> dict:
        if not isinstance(answers, Mapping):
            raise WorkflowError("answers must map question ids to non-empty strings")
        expected = {item["id"] for item in questions}
        if set(answers) != expected or not all(isinstance(value, str) and value.strip() for value in answers.values()):
            raise WorkflowError("answers must cover exactly the persisted questions")
        return {identifier: answers[identifier].strip() for identifier in sorted(expected)}

    def _answer_submission(
        self, artifacts: Path, answers: Mapping[str, str], cycle: Optional[int] = None
    ) -> dict:
        """Create or replay the one immutable complete answer set for this pause."""
        if cycle is None:
            cycle, _ = self._active_question_cycle(artifacts)
        normalized, digest = canonical_answer_submission(answers)
        questions_record = self._read_json(
            artifacts / "question-cycles" / ("%04d" % cycle) / "questions.json"
        )
        questions_digest = hashlib.sha256(
            json.dumps(questions_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        submission = {
            "cycle": cycle,
            "questions_digest": questions_digest,
            "answers": normalized,
            "digest": digest,
        }
        path = artifacts / "question-cycles" / ("%04d" % cycle) / "answers.json"
        if self.store.artifact_exists(path):
            persisted = self._read_json(path)
            if persisted != submission:
                raise AnswerConflict("retry answers do not match the immutable answer submission")
            return persisted
        self._write_json(path, submission)
        if cycle == 1:
            # Legacy readers may inspect this first-cycle pointer.
            self._write_json(
                artifacts / "answer-submission.json",
                {"answers": normalized, "digest": digest},
            )
        self._fault("after_answer_submission")
        return submission

    def _write_immutable_json(self, path: Path, contents: Any) -> None:
        if self.store.artifact_exists(path):
            if self._read_json(path) != contents:
                raise WorkflowError("immutable workflow artifact conflicts: %s" % path.name)
            return
        self._write_json(path, contents)

    def _append_decisions(self, artifacts: Path, questions: list[dict], answers: Mapping[str, str]) -> None:
        path = artifacts / "decision-log.md"
        existing = self._decision_log(artifacts)
        additions = []
        for item in questions:
            if ("## %s\n" % item["id"]) in existing:
                continue
            additions.extend([
                "## %s" % item["id"],
                "- Question: %s" % item["question"],
                "- Answer: %s" % answers[item["id"]],
                "- Decision impact: %s" % self._DECISION_IMPACT,
                "",
            ])
        if additions:
            self.store.write_artifact_bytes(path, (existing + "\n".join(additions)).encode("utf-8"))

    def _pending_review(self, state: RunState, artifacts: Path) -> tuple[int, Optional[Any]]:
        reviews = artifacts / "reviews"
        for path in self.store.list_artifacts(reviews, ".json"):
            sequence = int(path.stem)
            action = artifacts / "review-actions" / ("%04d.json" % sequence)
            if not self.store.artifact_exists(action):
                return sequence, self._read_json(path)
            record = self._read_json(action)
            if (
                isinstance(record, dict)
                and record.get("action") == "changes"
                and state.repair_round <= record.get("before_round", -1)
            ):
                return sequence, self._read_json(path)
        return 0, None

    def _next_sequence(self, directory: Path) -> int:
        values = [int(path.stem) for path in self.store.list_artifacts(directory, ".json") if path.stem.isdigit()]
        return max(values, default=0) + 1

    def _validate_state(self, state: RunState) -> None:
        if state.kind != "plan" or state.manifest is None:
            raise WorkflowError("Plan workflow requires a persisted Plan run")
        state.validate(self.store.authority)

    def _artifacts(self, state: RunState) -> Path:
        return self.store._run_directory(state)

    def _interrupt_loaded(self, state: RunState, reason: str, detail: str) -> RunState:
        """Record a resumable interruption instead of voiding the run.

        A paused run is terminal by design.  An interruption says only that an
        external call did not land, so every completed round stays valid and
        ``resume`` re-enters the loop where it stopped.
        """
        try:
            artifacts = self._artifacts(state)
            self._write_json(
                artifacts / "interruption.json", {"reason": reason, "detail": detail}
            )
            self._set_status(state, Status.INTERRUPTED)
            self.store.save(state)
        except BaseException:
            self._set_status(state, Status.INTERRUPTED)
        return state

    def _pause_loaded(self, state: RunState, reason: str, detail: str) -> RunState:
        try:
            artifacts = self._artifacts(state)
            self._pause(state, artifacts, reason, detail)
        except BaseException:
            # A storage failure is already fail-closed at the caller; preserve the
            # in-memory pause so a caller cannot mistake it for a runnable state.
            self._set_status(state, Status.PAUSED)
        return state

    def _pause(self, state: RunState, artifacts: Path, reason: str, detail: str) -> None:
        self._write_json(artifacts / "pause.json", {"reason": reason, "detail": detail})
        self._set_status(state, Status.PAUSED)
        self.store.save(state)

    def _set_running(self, state: RunState) -> None:
        self._set_status(state, Status.RUNNING)

    @staticmethod
    def _set_status(state: RunState, status: Status) -> None:
        object.__setattr__(state, "status", status)
        object.__setattr__(state, "updated_at", _utc_now())

    def _write_json(self, path: Path, contents: Any) -> None:
        if not isinstance(contents, (dict, list)):
            raise WorkflowError("structured model output must be a JSON object or array")
        self.store._atomic_write(path, contents)

    def _read_json(self, path: Path, default: Optional[Any] = None) -> Any:
        try:
            raw = self.store.read_artifact_bytes(path)
        except FileNotFoundError:
            if default is not None:
                return default
            raise WorkflowError("required workflow artifact is missing: %s" % path.name)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError) as error:
            raise WorkflowError("workflow artifact is invalid: %s" % path.name) from error

    @staticmethod
    def _plan_text(state: RunState) -> str:
        try:
            return Path(state.plan_path).read_text(encoding="utf-8")
        except OSError as error:
            raise WorkflowError("Plan cannot be read") from error

    def _decision_log(self, artifacts: Path) -> str:
        path = artifacts / "decision-log.md"
        try:
            return self.store.read_artifact_bytes(path).decode("utf-8")
        except FileNotFoundError:
            return ""
        except (UnicodeDecodeError, OSError, ValueError) as error:
            raise WorkflowError("decision log cannot be read") from error

    def _context_manifest(self, artifacts: Path) -> dict:
        if not self.store.artifact_exists(artifacts / "context-reference.json"):
            return {}
        return self._load_context_packet(artifacts).manifest_dict()

    def _knowledge_packet(self, artifacts: Path) -> str:
        if not self.store.artifact_exists(artifacts / "context-reference.json"):
            return ""
        return self._load_context_packet(artifacts).markdown

    def _unresolved_findings(self, artifacts: Path) -> list:
        contents = self._read_json(artifacts / "unresolved-findings.json", {"findings": []})
        if not isinstance(contents, dict) or not isinstance(contents.get("findings"), list):
            raise WorkflowError("unresolved findings artifact is invalid")
        return contents["findings"]


class CodeWorkflow(PlanWorkflow):
    """Crash-safe, bounded implementation and Code-review consensus loop.

    Code runs are intentionally accepted only from ``RunState``'s approved-plan
    factory.  Claude may edit the target worktree, but all model output, patches,
    verification evidence, and state-changing intents are durably journaled before
    a retry can advance the loop.
    """

    def __init__(
        self,
        store: RunStore,
        codex: Any,
        claude: Any,
        *,
        policy: Optional[Policy] = None,
        context_packet: Optional[KnowledgePacket] = None,
        context_resolver: Optional[Callable[[Iterable[str]], Iterable[SourceRef]]] = None,
        verification_runner: Callable[..., VerificationResult] = run_verification,
        verification_timeout: float = DEFAULT_VERIFICATION_TIMEOUT,
        diff_stats: Optional[Callable[[], int]] = None,
        fault_injector: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(
            store, codex, claude, policy=policy, context_packet=context_packet,
            context_resolver=context_resolver, fault_injector=fault_injector,
        )
        if (not isinstance(verification_timeout, (int, float)) or isinstance(verification_timeout, bool)
                or not math.isfinite(verification_timeout) or verification_timeout <= 0):
            raise ValueError("verification_timeout must be positive")
        self.verification_runner = verification_runner
        self.verification_timeout = float(verification_timeout)
        self.diff_stats = diff_stats

    def answer(self, run_id: str, answers: Mapping[str, str]) -> RunState:
        """Bind one Code clarification cycle, then continue from new evidence."""
        state = self.store.load(run_id)
        try:
            self._validate_state(state)
            if state.status != Status.AWAITING_USER_INPUT:
                raise WorkflowError("Code is not awaiting user input")
            artifacts = self._artifacts(state)
            cycle, cycle_dir = self._active_question_cycle(artifacts)
            questions = self._read_questions(artifacts, cycle)
            normalized = self._validate_answers(questions, answers)
            submission = self._answer_submission(artifacts, normalized, cycle)
            self._write_immutable_json(cycle_dir / "answers.json", submission)
            self._append_decisions(artifacts, questions, normalized)
            questions_record = self._read_json(cycle_dir / "questions.json")
            sequence = questions_record["review_sequence"]
            action_path = artifacts / "code-review-actions" / ("%04d.json" % sequence)
            action = self._read_json(action_path)
            if (
                action.get("action") not in ("awaiting_user_input", "user_answered")
                or action.get("question_cycle") != cycle
            ):
                raise WorkflowError("Code question action does not match active cycle")
            answered = {
                "action": "user_answered",
                "question_cycle": cycle,
                "answer_digest": submission["digest"],
                "questions_digest": submission["questions_digest"],
            }
            self._write_json(action_path, answered)
            self._set_running(state)
            self.store.save(state)
            return self.run(run_id)
        except AnswerConflict:
            raise
        except RunnerInterrupted as error:
            self._interrupt_loaded(state, "RUNNER_INTERRUPTED", str(error))
            raise
        except Exception as error:
            return self._pause_loaded(state, "INVALID_CODE_USER_ANSWER", str(error))

    def run(self, run_id: str) -> RunState:
        state = self.store.load(run_id)
        try:
            self._validate_state(state)
            # Reuse the shared tuple rather than a copy of it: this inline list
            # is exactly the duplicate that kept INTERRUPTED unresumable here
            # after the base class had already opened it.
            if state.status in self._HALT_STATUSES + (Status.AWAITING_HUMAN_CODE_REVIEW,):
                return state
            self._set_running(state)
            self.store.save(state)
            artifacts = self._artifacts(state)
            self._ensure_context_reference(artifacts)
            self._prepare_initial_snapshot(state, artifacts)
            while state.status == Status.RUNNING:
                sequence, review = self._pending_code_review(state, artifacts)
                if review is None:
                    # The review sequence decides the round, and the verification
                    # is taken under that same number.  Letting the two counters
                    # advance independently meant a resumed round ran a fresh
                    # verification as 0002 while retrying review 0001: judged
                    # against one snapshot, recorded against another.
                    # `_run_verification` returns an existing round untouched, so
                    # this also stops a resume from re-running the commands.
                    sequence = self._next_sequence(artifacts / "reviews")
                    verification = self._run_verification(state, artifacts, sequence)
                    if state.status != Status.RUNNING:
                        break
                    inputs = self._review_input(state, artifacts, verification)
                    intent = artifacts / "codex-intents" / ("%04d.json" % sequence)
                    if self.store.artifact_exists(intent):
                        if not self._intent_is_replayable(state, intent):
                            raise AmbiguousExternalCall("Codex review intent has no raw result")
                        self.store.remove_artifact(intent)
                    self._write_json(intent, {
                        "kind": "review", "inputs": inputs,
                        "patch_digest": self._worktree_digest(state),
                    })
                    self._validate_state(state)
                    review = self.codex.review(inputs)
                    self._write_json(artifacts / "reviews" / ("%04d.json" % sequence), review)
                self._handle_code_review(state, artifacts, sequence, review)
            return state
        except AmbiguousExternalCall as error:
            return self._pause_loaded(state, "AMBIGUOUS_EXTERNAL_CALL", str(error))
        except RunnerInterrupted as error:
            self._interrupt_loaded(state, "RUNNER_INTERRUPTED", str(error))
            raise
        except Exception as error:
            return self._pause_loaded(state, "WORKFLOW_ERROR", str(error))

    def _prepare_initial_snapshot(self, state: RunState, artifacts: Path) -> None:
        """Produce the first reviewable snapshot before any verification runs.

        Code mode must obtain Claude's implementation of the approved Plan.  A
        subclass that reviews pre-existing code overrides this to capture the
        current worktree instead, which is what keeps Codex the first model to
        see any Review run.
        """
        self._ensure_initial_implementation(state, artifacts)

    def _validate_repair_output(
        self, raw: Any, required_finding_ids: list[str]
    ) -> Mapping[str, Any]:
        """Validate one Claude repair result against the strict resolution schema."""
        return validate_claude_resolution(raw, required_finding_ids=required_finding_ids)

    def _review_ready_to_pass(
        self, state: RunState, artifacts: Path, sequence: int,
        review: Mapping[str, Any], verification: list[dict],
    ) -> bool:
        """Decide whether this Codex verdict may open the human Code gate."""
        return review["verdict"] == "PASS" and all(
            item["exit_code"] == 0 for item in verification
        )

    def _ensure_initial_implementation(self, state: RunState, artifacts: Path) -> None:
        output = artifacts / "claude-actions" / "initial.json"
        intent = artifacts / "claude-intents" / "initial.json"
        if self.store.artifact_exists(output):
            self._capture_patch(state, artifacts, 0)
            return
        if self.store.artifact_exists(intent):
            # We cannot know whether an interrupted external model request made
            # edits.  Repeating it could duplicate changes, so fail closed.
            raise AmbiguousExternalCall("initial Claude request was interrupted before its raw output was journaled")
        inputs = self._implementation_input(state, artifacts)
        self._write_json(intent, {"kind": "initial", "inputs": inputs})
        self._validate_state(state)
        raw = self.claude.implement(inputs)
        # Claude may have edited the worktree. Revalidate every signed Plan and
        # context binding immediately, before trusting output or capturing diff.
        self._validate_state(state)
        self._write_json(output, raw)
        self._fault("after_initial_claude_output")
        self._capture_patch(state, artifacts, 0)
        if self._captured_scope_expanded(artifacts):
            self._pause(state, artifacts, "SCOPE_EXPANSION", "initial implementation exceeds the approved policy")

    def _handle_code_review(self, state: RunState, artifacts: Path, sequence: int, raw_review: Any) -> None:
        action = artifacts / "code-review-actions" / ("%04d.json" % sequence)
        if self.store.artifact_exists(action):
            self._resume_code_action(state, artifacts, action)
            return
        try:
            review = validate_codex_review(raw_review)
        except (TypeError, ValueError) as error:
            self._pause(state, artifacts, "INVALID_CODEX_REVIEW", str(error))
            return
        verdict = Verdict(review["verdict"])
        if verdict == Verdict.NEEDS_USER_INPUT:
            cycle = self._write_questions(artifacts, review["questions"], sequence)
            self._write_json(
                action, {"action": "awaiting_user_input", "question_cycle": cycle}
            )
            state.transition(verdict, self.policy.max_rounds)
            self.store.save(state)
            return
        if verdict == Verdict.CONTEXT_REQUEST:
            self._expand_context(state, artifacts, sequence, review["context_requests"])
            if state.status == Status.RUNNING:
                self._write_json(action, {"action": "context_expanded"})
            return
        verification = self._verification_for_review(artifacts, sequence)
        if self._review_ready_to_pass(state, artifacts, sequence, review, verification):
            patches = self.store.list_artifacts(artifacts / "patches", ".patch")
            if not patches:
                self._pause(state, artifacts, "MISSING_REVIEWED_PATCH", "PASS has no captured patch")
                return
            patch = self.store.read_artifact_bytes(patches[-1])
            self._write_json(action, {
                "action": "pass",
                "review_sequence": sequence,
                "verification_round": sequence,
                "patch_name": patches[-1].name,
                "patch_digest": hashlib.sha256(patch).hexdigest(),
            })
            state.transition(Verdict.PASS, self.policy.max_rounds)
            self.store.save(state)
            return
        # A verification failure is repairable evidence even if Codex did not
        # identify a code finding.  PASS never opens the human Code gate unless
        # every configured argv succeeded.
        if verdict == Verdict.PASS:
            review = dict(review, findings=[])
        self._repair_after_review(state, artifacts, sequence, review, verification, action)

    def _repair_after_review(
        self, state: RunState, artifacts: Path, sequence: int, review: Mapping[str, Any],
        verification: list[dict], action: Path,
    ) -> None:
        findings = review["findings"]
        if findings and self._moves_review_gate(state, artifacts, findings):
            self._pause(state, artifacts, "REVIEW_GATE_MOVED", "new later finding lacks introduced_by_fix or newly_discovered lineage")
            return
        if state.repair_round >= self.policy.max_rounds:
            self._pause(state, artifacts, "MAX_REPAIR_ROUNDS", "a seventh Claude repair must not start")
            return
        if self._scope_expanded(artifacts):
            self._pause(state, artifacts, "SCOPE_EXPANSION", "production growth exceeds the approved policy")
            return
        required_ids = [finding["id"] for finding in findings]
        intent = artifacts / "claude-intents" / ("repair-%04d.json" % sequence)
        output = artifacts / "claude-actions" / ("repair-%04d.json" % sequence)
        inputs = self._repair_input(state, artifacts, findings, verification)
        if self.store.artifact_exists(output):
            raw = self._read_json(output)
        else:
            if self.store.artifact_exists(intent):
                if not self._intent_is_replayable(state, intent):
                    raise AmbiguousExternalCall("Claude repair intent has no raw result")
                self.store.remove_artifact(intent)
            self._write_json(intent, {
                "kind": "repair", "inputs": inputs,
                "patch_digest": self._worktree_digest(state),
            })
            self._validate_state(state)
            raw = self.claude.repair(inputs)
            self._validate_state(state)
            self._write_json(output, raw)
        try:
            resolution = self._validate_repair_output(raw, required_ids)
        except (TypeError, ValueError) as error:
            self._pause(state, artifacts, "INVALID_CLAUDE_RESOLUTION", str(error))
            return
        if any(item["outcome"] == "needs_user_input" for item in resolution["resolutions"]):
            self._pause(state, artifacts, "CLAUDE_NEEDS_USER_INPUT", "Claude repair cannot answer a product question")
            return
        if self._repeated_dispute(artifacts, review, resolution):
            self._pause(state, artifacts, "REPEATED_IDENTICAL_DISPUTE", "same review and resolution recurred")
            return
        self._write_json(artifacts / "unresolved-findings.json", {"findings": findings})
        self._record_seen_findings(artifacts, findings)
        before = state.repair_round
        self._write_json(action, {"action": "repair", "before_round": before, "sequence": sequence})
        object.__setattr__(state, "repair_round", before + 1)
        self._set_status(state, Status.RUNNING)
        self.store.save(state)
        self._fault("after_repair_action")
        self._capture_patch(state, artifacts, state.repair_round)
        if self._captured_scope_expanded(artifacts):
            self._pause(state, artifacts, "SCOPE_EXPANSION", "repair exceeds the approved policy")

    def _resume_code_action(self, state: RunState, artifacts: Path, action: Path) -> None:
        record = self._read_json(action)
        if not isinstance(record, dict):
            raise WorkflowError("code review action journal is invalid")
        if record.get("action") == "pass":
            if state.status == Status.RUNNING:
                state.transition(Verdict.PASS, self.policy.max_rounds)
                self.store.save(state)
            return
        if record.get("action") == "awaiting_user_input":
            if state.status == Status.RUNNING:
                state.transition(Verdict.NEEDS_USER_INPUT, self.policy.max_rounds)
                self.store.save(state)
            return
        if record.get("action") != "repair":
            return
        before = record.get("before_round")
        if type(before) is not int or before < 0:
            raise WorkflowError("code repair action journal is invalid")
        if state.repair_round == before:
            object.__setattr__(state, "repair_round", before + 1)
            self._set_status(state, Status.RUNNING)
            self.store.save(state)
            self._capture_patch(state, artifacts, state.repair_round)
        elif state.repair_round < before:
            raise WorkflowError("code repair action journal is ahead of persisted state")

    def _worktree_digest(self, state: RunState) -> str:
        """Digest the worktree exactly as a captured round patch would be."""
        patch, _production = capture_diff_bytes(
            Path(state.manifest.repo_path), state.manifest.base_oid,
            production_excludes=self.policy.production_excludes,
        )
        return hashlib.sha256(patch).hexdigest()

    # Only calls that cannot write are replayable.  The digest below is built
    # from `capture_diff_bytes`, which lists untracked files with
    # `--exclude-standard` and therefore cannot see a gitignored path -- and
    # nothing stops the repair Claude from writing one.  So a matching digest
    # proves nothing about a mutating call, and only a read-only kind qualifies.
    _REPLAYABLE_KINDS = frozenset({"review"})

    def _intent_is_replayable(self, state: RunState, intent: Path) -> bool:
        """True when the recorded intent provably left the worktree untouched.

        An intent with no result means an external call did not land.  For a
        mutating call that is unanswerable -- it may have written a file the
        patch cannot see -- so it stays ambiguous and fails closed, exactly as
        before.  A read-only call writes nothing by construction, and the
        recorded digest additionally proves that nothing else moved underneath
        it, so it can be asked again.
        """
        record = self._read_json(intent, default=None)
        if not isinstance(record, dict):
            return False
        if record.get("kind") not in self._REPLAYABLE_KINDS:
            return False
        recorded = record.get("patch_digest")
        if not isinstance(recorded, str) or not recorded:
            return False
        try:
            return recorded == self._worktree_digest(state)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def _capture_patch(self, state: RunState, artifacts: Path, round_number: int) -> None:
        patch_path = artifacts / "patches" / ("round-%04d.patch" % round_number)
        stats_path = artifacts / "patch-stats" / ("round-%04d.json" % round_number)
        if self.store.artifact_exists(stats_path):
            return
        patch, production_added_lines = capture_diff_bytes(
            Path(state.manifest.repo_path), state.manifest.base_oid,
            production_excludes=self.policy.production_excludes,
        )
        self.store.write_artifact_bytes(patch_path, patch)
        production = self.diff_stats() if self.diff_stats is not None else production_added_lines
        if type(production) is not int or production < 0:
            raise WorkflowError("diff statistics must be a non-negative integer")
        self._write_json(stats_path, {
            "round": round_number, "patch_path": str(patch_path),
            "production_added_lines": production,
        })

    def _scope_expanded(self, artifacts: Path) -> bool:
        stats = self.store.list_artifacts(artifacts / "patch-stats", ".json")
        if not stats:
            return False
        previous = self._read_json(stats[-1]).get("production_added_lines")
        current = self.diff_stats() if self.diff_stats is not None else previous
        if type(previous) is not int or type(current) is not int:
            raise WorkflowError("patch statistics are invalid")
        growth = current - previous
        return current > self.policy.production_line_limit or (
            previous > 0 and growth * 100 > previous * self.policy.production_growth_percent
        )

    def _captured_scope_expanded(self, artifacts: Path) -> bool:
        """Evaluate persisted patch statistics before another model can run."""
        stats = self.store.list_artifacts(artifacts / "patch-stats", ".json")
        if not stats:
            return False
        current = self._read_json(stats[-1]).get("production_added_lines")
        if type(current) is not int:
            raise WorkflowError("patch statistics are invalid")
        if current > self.policy.production_line_limit:
            return True
        if len(stats) < 2:
            return False
        previous = self._read_json(stats[-2]).get("production_added_lines")
        if type(previous) is not int:
            raise WorkflowError("patch statistics are invalid")
        return previous > 0 and (current - previous) * 100 > previous * self.policy.production_growth_percent

    def _run_verification(self, state: RunState, artifacts: Path, round_number: int) -> list[dict]:
        marker = artifacts / "verification-rounds" / ("%04d.json" % round_number)
        if self.store.artifact_exists(marker):
            return self._read_json(marker)["results"]
        results = []
        repo = Path(state.manifest.repo_path)
        for index, command in enumerate(state.manifest.verification_commands, start=1):
            name = command.kind if index == 1 else "%s-%02d" % (command.kind, index)
            stem = "%04d-%s" % (round_number, name)
            record_path = artifacts / "verification" / (stem + ".json")
            intent_path = artifacts / "verification-intents" / (stem + ".json")
            if self.store.artifact_exists(record_path):
                results.append(self._read_json(record_path))
                continue
            if self.store.artifact_exists(intent_path):
                raise AmbiguousExternalCall("verification intent has no completed result: %s" % name)
            self._write_json(intent_path, {"name": name, "argv": list(command.argv), "round": round_number})
            started = time.monotonic()
            try:
                if command.executable_identity is None:
                    raise RunnerError("verification executable has no signed identity")
                executable = validate_executable_identity(command.executable_identity)
                if executable != command.argv[0]:
                    raise RunnerError("verification executable identity does not match argv")
                result = self.verification_runner(command.argv, cwd=repo, timeout=self.verification_timeout)
                if not isinstance(result, VerificationResult):
                    raise WorkflowError("verification runner returned an invalid result")
                exit_code, stdout, stderr = result.exit_code, result.stdout, result.stderr
            except RunnerInterrupted:
                # The durable intent makes retry ambiguous; the outer workflow
                # records a pause and re-raises so the CLI returns exit 3.
                raise
            except RunnerError as error:
                exit_code, stdout, stderr = 127, "", str(error)
            duration = time.monotonic() - started
            self._fault("after_verification_call")
            stdout_path = artifacts / "verification" / (stem + ".stdout.log")
            stderr_path = artifacts / "verification" / (stem + ".stderr.log")
            self.store.write_artifact_bytes(stdout_path, stdout.encode("utf-8"))
            self.store.write_artifact_bytes(stderr_path, stderr.encode("utf-8"))
            record = {
                "name": name, "argv": list(command.argv), "exit_code": exit_code,
                "duration_seconds": duration, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
                "relevant_output": self._relevant_output(stdout, stderr),
            }
            self._write_json(record_path, record)
            results.append(record)
        self._write_json(marker, {"results": results})
        return results

    @staticmethod
    def _relevant_output(stdout: str, stderr: str) -> str:
        lines = (stderr + ("\n" if stderr and stdout else "") + stdout).splitlines()
        return "\n".join(lines[-200:])

    @staticmethod
    def _prompt_verification(records: Iterable[Mapping[str, Any]]) -> list[dict]:
        """Keep persisted log paths and raw streams outside model context."""
        allowed = ("name", "argv", "exit_code", "duration_seconds", "relevant_output")
        return [{key: record[key] for key in allowed} for record in records]

    def _verification_for_review(self, artifacts: Path, sequence: int) -> list[dict]:
        marker = artifacts / "verification-rounds" / ("%04d.json" % sequence)
        # Review and verification sequences both begin at one and advance once
        # per implementation snapshot; action recovery keeps them aligned.
        if self.store.artifact_exists(marker):
            contents = self._read_json(marker)
            if isinstance(contents, dict) and isinstance(contents.get("results"), list):
                return contents["results"]
        markers = self.store.list_artifacts(artifacts / "verification-rounds", ".json")
        if not markers:
            raise WorkflowError("verification evidence is missing")
        return self._read_json(markers[-1])["results"]

    def _pending_code_review(self, state: RunState, artifacts: Path) -> tuple[int, Optional[Any]]:
        reviews = artifacts / "reviews"
        for path in self.store.list_artifacts(reviews, ".json"):
            sequence = int(path.stem)
            action = artifacts / "code-review-actions" / ("%04d.json" % sequence)
            if not self.store.artifact_exists(action):
                return sequence, self._read_json(path)
            record = self._read_json(action)
            if isinstance(record, dict) and record.get("action") == "repair" and state.repair_round <= record.get("before_round", -1):
                return sequence, self._read_json(path)
        return 0, None

    def _implementation_input(self, state: RunState, artifacts: Path) -> dict:
        plan = self._plan_text(state)
        return {
            "approved_plan": plan, "approved_plan_digest": hashlib.sha256(plan.encode("utf-8")).hexdigest(),
            "approved_manifest_digest": state.manifest.digest(),
            "repo": state.manifest.repo_path, "base_oid": state.manifest.base_oid,
            "verification_commands": [item.to_dict() for item in state.manifest.verification_commands],
            "context_manifest": self._context_manifest(artifacts), "knowledge_packet": self._knowledge_packet(artifacts),
            "user_decisions": self._user_decisions(artifacts),
        }

    def _review_input(self, state: RunState, artifacts: Path, verification: list[dict]) -> dict:
        """Build the reviewer input for this loop's current snapshot.

        This deliberately narrows ``PlanWorkflow._review_input`` to the
        implementation loop's signature: ``CodeWorkflow`` owns ``run()`` and
        never reaches the Plan loop, so the only callers are this class and its
        Review subclass, which substitute their own trust boundary here.
        """
        return self._code_review_input(state, artifacts, verification)

    def _repair_input(
        self, state: RunState, artifacts: Path,
        findings: list[Mapping[str, Any]], verification: list[dict],
    ) -> dict:
        """Build the one mutating repair request for the supplied findings."""
        return {
            "approved_plan": self._plan_text(state),
            "approved_manifest_digest": state.manifest.digest(),
            "repo": state.manifest.repo_path,
            "base_oid": state.manifest.base_oid,
            "findings": findings,
            "finding_ids": [finding["id"] for finding in findings],
            "verification": self._prompt_verification(verification),
            "context_manifest": self._context_manifest(artifacts),
            "knowledge_packet": self._knowledge_packet(artifacts),
            "user_decisions": self._user_decisions(artifacts),
        }

    def _code_review_input(self, state: RunState, artifacts: Path, verification: list[dict]) -> dict:
        plan = self._plan_text(state)
        pointer = self._latest_patch_pointer(artifacts)
        return {
            "approved_plan": plan, "approved_plan_digest": hashlib.sha256(plan.encode("utf-8")).hexdigest(),
            "approved_manifest_digest": state.manifest.digest(),
            "repo": state.manifest.repo_path, "base_oid": state.manifest.base_oid,
            "patch_path": pointer["path"], "patch_sha256": pointer["sha256"],
            "patch_bytes": pointer["bytes"], "patch_stats": self._latest_stats(artifacts),
            "verification": self._prompt_verification(verification), "context_manifest": self._context_manifest(artifacts),
            "knowledge_packet": self._knowledge_packet(artifacts), "unresolved_prior_findings": self._unresolved_findings(artifacts),
            "user_decisions": self._user_decisions(artifacts),
            "policy": {"version": self.policy.version, "max_rounds": self.policy.max_rounds,
                       "production_line_limit": self.policy.production_line_limit,
                       "production_growth_percent": self.policy.production_growth_percent},
        }

    def _user_decisions(self, artifacts: Path) -> list[dict]:
        decisions = []
        cycles = [
            path for path in self.store.list_artifact_directories(artifacts / "question-cycles")
            if path.name.isdigit() and path.is_dir()
        ]
        for cycle_dir in cycles:
            answers_path = cycle_dir / "answers.json"
            if not self.store.artifact_exists(answers_path):
                continue
            questions = self._read_json(cycle_dir / "questions.json")["questions"]
            answers = self._read_json(answers_path)["answers"]
            for item in questions:
                decisions.append({
                    "cycle": int(cycle_dir.name),
                    "question_id": item["id"],
                    "question": item["question"],
                    "answer": answers[item["id"]],
                })
        return decisions

    def _latest_patch_pointer(self, artifacts: Path) -> dict:
        """Where the frozen patch is, rather than the patch itself.

        Codex already runs inside the repository with read-only tools, so it can
        open the patch the way a person would: in whatever pieces it needs, and
        skipping what it cannot read.  Inlining the bytes made the prompt a
        single argv item as large as the patch, so a branch carrying a few
        megabytes of artwork could not be reviewed at all — ``exec`` failed with
        ``E2BIG`` before Codex ever started, and the only symptom was that
        external review "could not start".

        The digest travels with the path because a pointer is only as good as
        the reader's ability to prove what it points at.  Inlined bytes had to be
        taken on trust; a file plus its digest can be checked.
        """
        patches = self.store.list_artifacts(artifacts / "patches", ".patch")
        if not patches:
            return {"path": "", "sha256": "", "bytes": 0}
        content = self.store.read_artifact_bytes(patches[-1])
        return {
            "path": str(Path(patches[-1]).resolve()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }

    def _latest_stats(self, artifacts: Path) -> dict:
        stats = self.store.list_artifacts(artifacts / "patch-stats", ".json")
        return self._read_json(stats[-1]) if stats else {}

    def _validate_state(self, state: RunState) -> None:
        if state.kind != "code" or state.manifest is None:
            raise WorkflowError("Code workflow requires a persisted Code run")
        state.validate(self.store.authority)
        state.validate_code_binding()
        artifacts = self._artifacts(state)
        if state.manifest.context_checksum is not None:
            binding = self._read_json(artifacts / "approved-plan-context.json")
            packet = self._load_context_packet(artifacts, binding.get("packet_revision"))
            if packet.checksum != state.manifest.context_checksum:
                raise WorkflowError("approved Plan context packet binding changed")
            if (
                binding.get("manifest_digest") != state.manifest.digest()
                or binding.get("packet_checksum") != packet.checksum
            ):
                raise WorkflowError("approved Plan context manifest binding is invalid")
        repo = git_worktree_root(Path(state.manifest.repo_path))
        try:
            Path(state.plan_path).resolve().relative_to(repo)
        except ValueError as error:
            raise WorkflowError("approved Plan must remain inside the approved repository") from error

    def _pause(self, state: RunState, artifacts: Path, reason: str, detail: str) -> None:
        # The durable source of truth is pause.json; retaining the reason on the
        # returned state makes a direct caller's policy decision inspectable too.
        super()._pause(state, artifacts, reason, detail)
        object.__setattr__(state, "pause_reason", reason)
