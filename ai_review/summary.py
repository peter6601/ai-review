"""Deterministic, bounded human summaries for transient consensus runs.

This module deliberately treats run artifacts as untrusted until their narrow
schemas are validated.  It never reads patches, logs, prompts, model summaries,
or arbitrary files; the only persistence boundary is :class:`RunStore`.
"""

import hashlib
import hmac
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import RunState, Status, canonical_answer_submission, canonical_user_question_ids, strict_json_loads
from .runners import (
    validate_claude_resolution, validate_codex_review, validate_plan_repair,
)
from .store import RunStore


_SEQUENCE_JSON = re.compile(r"^[0-9]{4}\.json$")
_REPAIR_JSON = re.compile(r"^repair-[0-9]{4}\.json$")
_ROUND_STATS_JSON = re.compile(r"^round-[0-9]{4}\.json$")
_VERIFICATION_JSON = re.compile(r"^[0-9]{4}-[a-z]+(?:-[0-9]{2})?\.json$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SECRET_LIKE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]+-----|(?i:api[_-]?key|token|secret|password)\\s*[:=]\\s*[^\\s]+)"
)

# Evidence is intentionally small enough to inspect manually.  These limits
# apply before artifact bodies are read, so a noisy run cannot turn summary
# generation into unbounded artifact ingestion.
MAX_EVIDENCE_ARTIFACTS = 50
_CATEGORY_CAPS = {
    "state": 1,
    "reviews": 10,
    "resolutions": 10,
    "diff-stats": 6,
    "verification": 8,
    "pause": 1,
    "human-arbitration": 8,
    "human-questions": 8,
    "unresolved": 1,
    "risk": 8,
    "preflight": 1,
}
_ROUND_RISK_JSON = re.compile(r"^round-[0-9]{4}\.json$")
MAX_RISK_CATEGORIES = 8
MAX_FINDINGS = 24
MAX_RESOLUTIONS = 24
MAX_UNRESOLVED_FINDINGS = 12
MAX_ERROR_IDENTIFIERS = 12
MAX_MARKDOWN_BYTES = 16 * 1024
MAX_DYNAMIC_BYTES = 192
MAX_SECTION_ITEMS = 8

# This is intentionally the whole candidate policy.  Do not add heuristic
# triggers: knowledge candidates require one of these human-approved signals.
_CANDIDATE_TRIGGERS = (
    "THREE_OR_MORE_REPAIR_ROUNDS",
    "REPEATED_INVARIANT",
    "SCOPE_EXPANSION",
    "HUMAN_ARBITRATION",
    "REUSABLE_ARCHITECTURE_OR_TESTING_LESSON",
    "ALL_SIX_REPAIR_ROUNDS_EXHAUSTED",
)


@dataclass(frozen=True)
class SummaryOutputs:
    """Descriptor-backed artifact locations produced for one run."""

    final_summary: Path
    knowledge_candidate: Optional[Path]
    candidate_triggers: tuple[str, ...] = ()


@dataclass
class _Evidence:
    findings: list[dict]
    latest_findings: list[dict]
    resolutions: list[dict]
    unresolved: list[dict]
    diff_stats: list[dict]
    verification: list[dict]
    artifacts: set[str]
    artifact_counts: dict[str, int]
    truncation: dict[str, int]
    errors: list[str]
    error_omitted: int
    pause_reason: Optional[str]
    human_arbitration: bool
    review_findings: dict[int, list[dict]]
    review_sequences: set[int]
    selected_review_sequences: set[int]
    risk_categories: list = field(default_factory=list)
    specialist_counts: dict = field(default_factory=dict)


def generate_outputs(store: RunStore, run_id: str) -> SummaryOutputs:
    """Persist a local final summary and, only when justified, a candidate.

    ``run_id`` is loaded through ``RunStore`` so the same descriptor authority
    protects the state and every artifact used to create the output.  The
    returned paths are opaque artifact descriptors; callers should likewise use
    ``RunStore.read_artifact_bytes`` to read them.
    """
    if not isinstance(store, RunStore):
        raise TypeError("store must be a RunStore")
    state = store.load(run_id)
    artifacts = store._run_directory(state)
    candidate_path = artifacts / "knowledge-candidate.md"
    store.remove_artifact(candidate_path)
    evidence = _collect_evidence(store, artifacts, state)
    _tag_phase(evidence, state.kind)
    # Only a Code run inherits Plan evidence. A Review run has no Plan at all and
    # must never attempt to read approved Plan bytes.
    if state.kind == "code":
        _merge_approved_plan_evidence(store, state, evidence)
    elif state.kind == "review":
        _collect_review_evidence(store, artifacts, evidence)
    total_repair_rounds = _total_repair_rounds(store, state)
    final_path = artifacts / "final-summary.md"
    store.write_artifact_bytes(final_path, _final_summary(state, evidence).encode("utf-8"))

    if evidence.errors:
        return SummaryOutputs(final_path, None)
    triggers = _candidate_triggers(state, evidence, total_repair_rounds)
    if not triggers:
        return SummaryOutputs(final_path, None)
    store.write_artifact_bytes(
        candidate_path,
        _knowledge_candidate(state, evidence, triggers, total_repair_rounds).encode("utf-8"),
    )
    return SummaryOutputs(final_path, candidate_path, triggers)


def _merge_approved_plan_evidence(
    store: RunStore, state: RunState, evidence: _Evidence
) -> None:
    if state.approval_attestation is None:
        return
    try:
        plan = store.load(state.approval_attestation.receipt.run_id)
        if plan.kind != "plan" or plan.manifest != state.manifest:
            raise ValueError("approved Plan does not match Code manifest")
        plan_evidence = _collect_evidence(store, store._run_directory(plan), plan)
        _tag_phase(plan_evidence, "plan")
        _merge_phase_evidence(evidence, plan_evidence)
    except (FileNotFoundError, OSError, ValueError):
        evidence.errors.append("approved-plan-evidence")


def _collect_review_evidence(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    """Add the Review-only scope facts: risk categories and specialist counts."""
    _collect_risk_assessments(store, artifacts, evidence)
    _collect_preflight(store, artifacts, evidence)


def _tag_phase(evidence: _Evidence, phase: str) -> None:
    for collection in (
        evidence.findings, evidence.latest_findings, evidence.resolutions,
        evidence.unresolved, evidence.verification, evidence.diff_stats,
    ):
        for item in collection:
            item["phase"] = phase


def _merge_phase_evidence(target: _Evidence, source: _Evidence) -> None:
    """Merge already schema-validated Plan evidence into a Code summary."""
    target.findings = (source.findings + target.findings)[:MAX_FINDINGS]
    target.resolutions = (source.resolutions + target.resolutions)[:MAX_RESOLUTIONS]
    target.verification = (
        source.verification + target.verification
    )[:_CATEGORY_CAPS["verification"]]
    target.diff_stats = source.diff_stats + target.diff_stats
    target.artifacts.update("plan:%s" % item for item in source.artifacts)
    target.errors.extend("plan:%s" % item for item in source.errors)
    target.human_arbitration = target.human_arbitration or source.human_arbitration


def _total_repair_rounds(store: RunStore, state: RunState) -> int:
    """Include the approved Plan history when summarizing its Code successor."""
    total = state.repair_round
    receipt = state.approval_attestation.receipt if state.approval_attestation else None
    if state.kind != "code" or receipt is None:
        return total
    try:
        plan = store.load(receipt.run_id)
    except (FileNotFoundError, ValueError, OSError):
        return total
    if plan.kind != "plan" or plan.manifest != state.manifest:
        return total
    return total + plan.repair_round


def _collect_evidence(store: RunStore, artifacts: Path, state: RunState) -> _Evidence:
    evidence = _Evidence([], [], [], [], [], [], set(), {}, {}, [], 0, None, False, {}, set(), set())
    _record_artifact(evidence, "state", "state.json")
    _collect_reviews(store, artifacts, evidence)
    _collect_resolutions(store, artifacts, evidence)
    _collect_diff_stats(store, artifacts, evidence)
    _collect_verification(store, artifacts, evidence)
    _collect_pause(store, artifacts, state, evidence)
    _collect_human_arbitration(store, artifacts, evidence)
    _collect_unresolved(store, artifacts, state, evidence)
    return evidence


def _collect_reviews(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    latest: list[dict] = []
    paths = [
        path for path in store.list_artifacts(artifacts / "reviews", ".json")
        if _SEQUENCE_JSON.fullmatch(path.name)
    ]
    evidence.review_sequences = {int(path.stem) for path in paths}
    for path in _bounded_paths(paths, artifacts, "reviews", evidence):
        identifier = _artifact_identifier(path, artifacts)
        sequence = int(path.stem)
        evidence.selected_review_sequences.add(sequence)
        _record_artifact(evidence, "reviews", identifier)
        value = _read_json(store, path, identifier, evidence)
        if value is None:
            continue
        try:
            review = validate_codex_review(value)
        except (TypeError, ValueError):
            _invalid(identifier, evidence)
            continue
        findings = [_finding_view(item) for item in review["findings"]]
        evidence.review_findings[sequence] = findings
        _append_bounded(evidence, evidence.findings, findings, "findings", MAX_FINDINGS)
        latest = findings
    evidence.latest_findings = latest


def _collect_resolutions(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    candidates: list[tuple[int, Path]] = []
    for path in store.list_artifacts(artifacts / "resolutions", ".json"):
        if _SEQUENCE_JSON.fullmatch(path.name):
            candidates.append((int(path.stem), path))
    for path in store.list_artifacts(artifacts / "claude-actions", ".json"):
        if _REPAIR_JSON.fullmatch(path.name):
            candidates.append((int(path.stem.split("-", 1)[1]), path))
    selected: list[Path] = []
    seen_sequences: dict[int, list[Path]] = {}
    for sequence, path in sorted(candidates, key=lambda item: _artifact_identifier(item[1], artifacts)):
        identifier = _artifact_identifier(path, artifacts)
        seen_sequences.setdefault(sequence, []).append(path)
        if sequence not in evidence.review_sequences:
            _invalid(identifier, evidence)
            continue
        if sequence not in evidence.selected_review_sequences:
            _truncated(evidence, "resolutions")
            continue
        selected.append(path)
    duplicate_paths = {
        path for paths in seen_sequences.values() if len(paths) > 1 for path in paths
    }
    for path in _bounded_paths(selected, artifacts, "resolutions", evidence):
        identifier = _artifact_identifier(path, artifacts)
        _record_artifact(evidence, "resolutions", identifier)
        if path in duplicate_paths:
            _invalid(identifier, evidence)
            continue
        value = _read_json(store, path, identifier, evidence)
        if value is None:
            continue
        sequence = _resolution_sequence(path)
        required_ids = [item["id"] for item in evidence.review_findings.get(sequence, [])]
        if sequence not in evidence.review_findings or len(required_ids) != len(set(required_ids)):
            _invalid(identifier, evidence)
            continue
        try:
            if isinstance(value, dict) and set(value) == {
                "summary", "input_plan_digest", "decision_log_digest",
                "resolutions", "plan",
            }:
                resolution = validate_plan_repair(
                    value, required_finding_ids=required_ids,
                    current_plan_digest=value["input_plan_digest"],
                    decision_log_digest=value["decision_log_digest"],
                )
            else:
                resolution = validate_claude_resolution(
                    value, required_finding_ids=required_ids
                )
        except (TypeError, ValueError):
            _invalid(identifier, evidence)
            continue
        resolved_ids = [item["finding_id"] for item in resolution["resolutions"]]
        if len(resolved_ids) != len(required_ids) or set(resolved_ids) != set(required_ids):
            _invalid(identifier, evidence)
            continue
        _append_bounded(evidence, evidence.resolutions, [
            {"finding_id": item["finding_id"], "outcome": item["outcome"]}
            for item in resolution["resolutions"]
        ], "resolution-entries", MAX_RESOLUTIONS)


def _collect_diff_stats(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    paths = [
        path for path in store.list_artifacts(artifacts / "patch-stats", ".json")
        if _ROUND_STATS_JSON.fullmatch(path.name)
    ]
    for path in _bounded_paths(paths, artifacts, "diff-stats", evidence):
        identifier = _artifact_identifier(path, artifacts)
        _record_artifact(evidence, "diff-stats", identifier)
        value = _read_json(store, path, identifier, evidence)
        if value is None:
            continue
        if (
            not isinstance(value, dict)
            or set(value) != {"round", "patch_path", "production_added_lines"}
            or type(value["round"]) is not int
            or value["round"] < 0
            or type(value["production_added_lines"]) is not int
            or value["production_added_lines"] < 0
            or not isinstance(value["patch_path"], str)
        ):
            _invalid(identifier, evidence)
            continue
        # ``patch_path`` is deliberately validated but never retained: it may be
        # an absolute local path, which must not enter a human-facing summary.
        evidence.diff_stats.append({"round": value["round"], "production_added_lines": value["production_added_lines"]})


def _collect_verification(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    paths = [
        path for path in store.list_artifacts(artifacts / "verification", ".json")
        if _VERIFICATION_JSON.fullmatch(path.name)
    ]
    for path in _bounded_paths(paths, artifacts, "verification", evidence):
        identifier = _artifact_identifier(path, artifacts)
        _record_artifact(evidence, "verification", identifier)
        value = _read_json(store, path, identifier, evidence)
        if value is None:
            continue
        expected = {"name", "argv", "exit_code", "duration_seconds", "stdout_path", "stderr_path", "relevant_output"}
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or not isinstance(value["name"], str)
            or not value["name"]
            or type(value["exit_code"]) is not int
            or not isinstance(value["duration_seconds"], (int, float))
            or isinstance(value["duration_seconds"], bool)
            or not math.isfinite(value["duration_seconds"])
            or value["duration_seconds"] < 0
            or not isinstance(value["argv"], list)
            or not all(isinstance(item, str) and item for item in value["argv"])
            or not isinstance(value["stdout_path"], str)
            or not isinstance(value["stderr_path"], str)
            or not isinstance(value["relevant_output"], str)
        ):
            _invalid(identifier, evidence)
            continue
        # argv, paths, and relevant_output may contain sensitive details or full
        # logs.  Their existence is schema-checked but those values are dropped.
        evidence.verification.append({"name": value["name"], "exit_code": value["exit_code"]})


def _collect_pause(store: RunStore, artifacts: Path, state: RunState, evidence: _Evidence) -> None:
    path = artifacts / "pause.json"
    if not store.artifact_exists(path):
        if state.status == Status.PAUSED:
            _invalid("pause.json", evidence)
        return
    if not _record_artifact(evidence, "pause", "pause.json"):
        return
    value = _read_json(store, path, "pause.json", evidence)
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"reason", "detail"} or not isinstance(value["reason"], str) or not value["reason"] or not isinstance(value["detail"], str):
        _invalid("pause.json", evidence)
        return
    evidence.pause_reason = value["reason"]


def _collect_human_arbitration(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    cycles = [
        path for path in store.list_artifact_directories(artifacts / "question-cycles")
        if re.fullmatch(r"[0-9]{4}", path.name)
    ]
    if cycles:
        if len(cycles) > 8:
            _truncated(evidence, "human-arbitration")
            cycles = cycles[:8]
        for cycle_path in cycles:
            question_id = "question-cycles/%s/questions.json" % cycle_path.name
            answer_id = "question-cycles/%s/answers.json" % cycle_path.name
            questions_path = cycle_path / "questions.json"
            answers_path = cycle_path / "answers.json"
            if not store.artifact_exists(answers_path):
                continue
            _record_artifact(evidence, "human-questions", question_id)
            _record_artifact(evidence, "human-arbitration", answer_id)
            questions = _read_json(store, questions_path, question_id, evidence)
            answers_record = _read_json(store, answers_path, answer_id, evidence)
            if not isinstance(questions, dict) or set(questions) != {
                "cycle", "review_sequence", "questions"
            }:
                _invalid(question_id, evidence)
                continue
            try:
                question_ids = canonical_user_question_ids(questions["questions"])
            except ValueError:
                _invalid(question_id, evidence)
                continue
            questions_digest = hashlib.sha256(
                json.dumps(
                    questions, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            if (
                not isinstance(answers_record, dict)
                or set(answers_record) != {
                    "cycle", "questions_digest", "answers", "digest"
                }
                or answers_record.get("cycle") != questions.get("cycle")
                or answers_record.get("questions_digest") != questions_digest
            ):
                _invalid(answer_id, evidence)
                continue
            try:
                normalized, digest = canonical_answer_submission(
                    answers_record["answers"]
                )
            except (KeyError, ValueError):
                _invalid(answer_id, evidence)
                continue
            if (
                answers_record["answers"] != normalized
                or tuple(sorted(normalized)) != question_ids
                or not hmac.compare_digest(answers_record.get("digest", ""), digest)
            ):
                _invalid(answer_id, evidence)
                continue
            evidence.human_arbitration = True
        return
    # Legacy single-cycle fallback.
    path = artifacts / "answer-submission.json"
    if not store.artifact_exists(path):
        return
    question_path = artifacts / "user-questions.json"
    if not store.artifact_exists(question_path):
        _invalid("user-questions.json", evidence)
        return
    if not _record_artifact(evidence, "human-questions", "user-questions.json"):
        return
    questions_value = _read_json(store, question_path, "user-questions.json", evidence)
    if questions_value is None:
        return
    if not isinstance(questions_value, dict) or set(questions_value) != {"questions"}:
        _invalid("user-questions.json", evidence)
        return
    try:
        question_ids = canonical_user_question_ids(questions_value["questions"])
    except ValueError:
        _invalid("user-questions.json", evidence)
        return
    if not _record_artifact(evidence, "human-arbitration", "answer-submission.json"):
        return
    value = _read_json(store, path, "answer-submission.json", evidence)
    if value is None:
        return
    answers = value.get("answers") if isinstance(value, dict) else None
    digest = value.get("digest") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"answers", "digest"}
        or not isinstance(answers, dict)
        or not answers
        or not all(isinstance(key, str) and key and isinstance(answer, str) and answer.strip() for key, answer in answers.items())
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        _invalid("answer-submission.json", evidence)
        return
    try:
        normalized, expected_digest = canonical_answer_submission(answers)
    except ValueError:
        _invalid("answer-submission.json", evidence)
        return
    if (
        answers != normalized
        or tuple(sorted(answers)) != question_ids
        or not hmac.compare_digest(digest, expected_digest)
    ):
        _invalid("answer-submission.json", evidence)
        return
    # The existence of a verified answer is enough to identify arbitration.
    # Answers themselves are intentionally never copied to either output.
    evidence.human_arbitration = True


def _collect_unresolved(store: RunStore, artifacts: Path, state: RunState, evidence: _Evidence) -> None:
    path = artifacts / "unresolved-findings.json"
    if not store.artifact_exists(path):
        return
    if not _record_artifact(evidence, "unresolved", "unresolved-findings.json"):
        return
    identifier = "unresolved-findings.json"
    value = _read_json(store, path, identifier, evidence)
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"findings"} or not isinstance(value["findings"], list):
        _invalid(identifier, evidence)
        return
    try:
        validated = validate_codex_review({
            "verdict": "CHANGES_REQUIRED", "summary": "structured findings",
            "findings": value["findings"], "questions": [], "context_requests": [],
        })
    except (TypeError, ValueError):
        _invalid(identifier, evidence)
        return
    # A completed PASS may retain repair-loop history.  It is not an unresolved
    # blocker; only an active pause/input gate remains actionable.
    if state.status in (Status.PAUSED, Status.AWAITING_USER_INPUT):
        _append_bounded(
            evidence, evidence.unresolved, [_finding_view(item) for item in validated["findings"]],
            "unresolved-findings", MAX_UNRESOLVED_FINDINGS,
        )


def _collect_risk_assessments(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    paths = [
        path for path in store.list_artifacts(artifacts / "risk-assessments", ".json")
        if _ROUND_RISK_JSON.fullmatch(path.name)
    ]
    categories: list[str] = []
    for path in _bounded_paths(paths, artifacts, "risk", evidence):
        identifier = _artifact_identifier(path, artifacts)
        _record_artifact(evidence, "risk", identifier)
        value = _read_json(store, path, identifier, evidence)
        if value is None:
            continue
        if (
            not isinstance(value, dict)
            or set(value) != {
                "round", "patch_digest", "policy_version", "categories", "evidence",
                "introduced_categories", "introduced_evidence", "path_fingerprints",
            }
            or not isinstance(value["path_fingerprints"], dict)
            or type(value["round"]) is not int
            or value["round"] < 0
            or not _DIGEST.fullmatch(value["patch_digest"] if isinstance(value["patch_digest"], str) else "")
            or not isinstance(value["policy_version"], str)
            or not value["policy_version"]
            or not all(
                isinstance(value[key], list)
                and all(isinstance(item, str) and item for item in value[key])
                for key in ("categories", "introduced_categories")
            )
            or not all(
                isinstance(value[key], list) for key in ("evidence", "introduced_evidence")
            )
        ):
            _invalid(identifier, evidence)
            continue
        # Only the category names are retained: paths and reasons are local
        # detail that a bounded human summary does not need to repeat. The
        # gating subset is what the human acted on, so it is reported.
        categories.extend(value["introduced_categories"])
    evidence.risk_categories = sorted(set(categories))[:MAX_RISK_CATEGORIES]


def _collect_preflight(store: RunStore, artifacts: Path, evidence: _Evidence) -> None:
    path = artifacts / "preflight.json"
    if not store.artifact_exists(path):
        return
    if not _record_artifact(evidence, "preflight", "preflight.json"):
        return
    value = _read_json(store, path, "preflight.json", evidence)
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != {"profile", "patch_digest", "specialists"}
        or value["profile"] != "ios"
        or not _DIGEST.fullmatch(value["patch_digest"] if isinstance(value["patch_digest"], str) else "")
        or not isinstance(value["specialists"], list)
    ):
        _invalid("preflight.json", evidence)
        return
    counts: dict[str, int] = {}
    for specialist in value["specialists"]:
        if (
            not isinstance(specialist, dict)
            or set(specialist) != {"name", "findings"}
            or not isinstance(specialist["name"], str)
            or not specialist["name"]
            or not isinstance(specialist["findings"], list)
        ):
            _invalid("preflight.json", evidence)
            return
        counts[specialist["name"]] = len(specialist["findings"])
    evidence.specialist_counts = counts


def _read_json(store: RunStore, path: Path, identifier: str, evidence: _Evidence) -> Optional[Any]:
    try:
        return strict_json_loads(store.read_artifact_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
        _invalid(identifier, evidence)
        return None


def _invalid(identifier: str, evidence: _Evidence) -> None:
    if identifier in evidence.errors:
        return
    if len(evidence.errors) < MAX_ERROR_IDENTIFIERS:
        evidence.errors.append(identifier)
    else:
        evidence.error_omitted += 1


def _record_artifact(evidence: _Evidence, category: str, identifier: str) -> bool:
    if identifier in evidence.artifacts:
        return True
    limit = _CATEGORY_CAPS[category]
    if evidence.artifact_counts.get(category, 0) >= limit or len(evidence.artifacts) >= MAX_EVIDENCE_ARTIFACTS:
        _truncated(evidence, category)
        return False
    evidence.artifacts.add(identifier)
    evidence.artifact_counts[category] = evidence.artifact_counts.get(category, 0) + 1
    return True


def _bounded_paths(paths: Iterable[Path], artifacts: Path, category: str, evidence: _Evidence) -> list[Path]:
    ordered = sorted(paths, key=lambda item: _artifact_identifier(item, artifacts))
    capacity = min(
        _CATEGORY_CAPS[category] - evidence.artifact_counts.get(category, 0),
        MAX_EVIDENCE_ARTIFACTS - len(evidence.artifacts),
    )
    selected = ordered[:max(capacity, 0)]
    for _ in ordered[len(selected):]:
        _truncated(evidence, category)
    return selected


def _append_bounded(evidence: _Evidence, destination: list[dict], values: Iterable[dict], category: str, limit: int) -> None:
    values = list(values)
    capacity = max(limit - len(destination), 0)
    destination.extend(values[:capacity])
    for _ in values[capacity:]:
        _truncated(evidence, category)


def _truncated(evidence: _Evidence, category: str) -> None:
    evidence.truncation[category] = evidence.truncation.get(category, 0) + 1


def _resolution_sequence(path: Path) -> int:
    if _SEQUENCE_JSON.fullmatch(path.name):
        return int(path.stem)
    if _REPAIR_JSON.fullmatch(path.name):
        return int(path.stem.split("-", 1)[1])
    raise ValueError("resolution path has no known sequence")


def _artifact_identifier(path: Path, artifacts: Path) -> str:
    try:
        identifier = path.relative_to(artifacts).as_posix()
    except ValueError as error:
        raise ValueError("summary artifact escapes the selected run") from error
    if identifier.startswith("/") or ".." in Path(identifier).parts:
        raise ValueError("summary artifact identifier is unsafe")
    return identifier


def _finding_view(item: dict) -> dict:
    return {"id": item["id"], "severity": item["severity"], "invariant": item["invariant"]}


def _candidate_triggers(state: RunState, evidence: _Evidence, total_repair_rounds: int) -> tuple[str, ...]:
    repeated_invariant = any(count >= 2 for count in Counter(item["invariant"] for item in evidence.findings).values())
    verification_by_name: dict[str, list[int]] = {}
    for record in evidence.verification:
        verification_by_name.setdefault(record["name"], []).append(record["exit_code"])
    reusable_testing_lesson = any(
        any(code != 0 for code in exit_codes) and any(code == 0 for code in exit_codes)
        for exit_codes in verification_by_name.values()
    )
    active = {
        "THREE_OR_MORE_REPAIR_ROUNDS": total_repair_rounds >= 3,
        "REPEATED_INVARIANT": repeated_invariant,
        # A high-risk pause is the Review-mode form of a scope-growth gate; it
        # reuses this trigger rather than adding a seventh approved condition.
        "SCOPE_EXPANSION": evidence.pause_reason in ("SCOPE_EXPANSION", "HIGH_RISK_CHANGE"),
        "HUMAN_ARBITRATION": evidence.human_arbitration,
        "REUSABLE_ARCHITECTURE_OR_TESTING_LESSON": reusable_testing_lesson,
        "ALL_SIX_REPAIR_ROUNDS_EXHAUSTED": state.repair_round >= 6,
    }
    return tuple(trigger for trigger in _CANDIDATE_TRIGGERS if active[trigger])


def _final_summary(state: RunState, evidence: _Evidence) -> str:
    resolved = _stable_unique(
        "%s:%s (%s)" % (
            _display(evidence, item.get("phase", state.kind)),
            _display(evidence, item["finding_id"]),
            _display(evidence, item["outcome"]),
        )
        for item in evidence.resolutions if item["outcome"] in {"fixed", "not_reproduced"}
    )
    disputed = _stable_unique(
        "%s:%s" % (
            _display(evidence, item.get("phase", state.kind)),
            _display(evidence, item["finding_id"]),
        ) for item in evidence.resolutions if item["outcome"] == "disputed"
    )
    blockers = _stable_unique(
        "%s (%s)" % (_display(evidence, item["id"]), _display(evidence, item["severity"])) for item in evidence.unresolved
    )
    verification_ok = sum(record["exit_code"] == 0 for record in evidence.verification)
    latest_lines = evidence.diff_stats[-1]["production_added_lines"] if evidence.diff_stats else None
    verification_lines = []
    if evidence.verification:
        verification_lines.append("- Verification 記錄：%d；成功：%d。" % (len(evidence.verification), verification_ok))
    else:
        verification_lines.append("- 沒有可安全引用的 verification 記錄。")
    if latest_lines is not None:
        verification_lines.append("- 最新 Diff production added lines：%d。" % latest_lines)
    else:
        verification_lines.append("- 沒有可安全引用的 Diff 統計。")
    if evidence.errors:
        omitted = "；另有 %d 個無效 artifact 未列出" % evidence.error_omitted if evidence.error_omitted else ""
        decision_lines = ["- 無法安全產生完整摘要；請檢查受保護 artifact：%s%s。" % (", ".join(_display(evidence, item) for item in sorted(evidence.errors)), omitted)]
    elif state.status == Status.AWAITING_USER_INPUT or evidence.unresolved or state.status == Status.PAUSED:
        items = blockers or ["目前工作流程在人工 gate 暫停。"]
        decision_lines = ["- 需要你決定：%s" % "; ".join(_section_values(evidence, items, "decision"))]
    else:
        decision_lines = ["- 目前沒有需要人工仲裁的項目。"]
    return _render_document("# Consensus Run Summary", (
        ("## 目前狀態", [
            "- 狀態：%s" % _display(evidence, state.status.value),
            "- 修復輪數：%d" % state.repair_round,
            *_review_scope_lines(state, evidence),
        ]),
        ("## 已解決項目", _section_bullets(evidence, resolved, "- 沒有可安全引用的已解決項目。", "resolved")),
        ("## 未解決 Blockers", _section_bullets(evidence, blockers, "- 沒有已確認的未解決 blocker。", "blockers")),
        ("## Claude 與 Codex 的分歧", _section_bullets(evidence, disputed, "- 沒有已確認的結構化分歧。", "disputed")),
        ("## Verification 與 Diff", verification_lines),
        ("## 需要你決定", decision_lines),
    ))


def _review_scope_lines(state: RunState, evidence: _Evidence) -> list[str]:
    """Name the approved Review scope, without ever consulting a Plan."""
    if state.kind != "review" or state.manifest is None:
        return []
    lines = [
        "- Run kind：review；Profile：%s。" % _display(evidence, state.manifest.profile),
        "- Frozen base OID：%s。" % _display(evidence, state.manifest.base_oid),
        "- Review brief：%s" % _display(evidence, state.manifest.brief),
        "- Codex review 輪數：%d。" % len(evidence.review_sequences),
    ]
    if evidence.specialist_counts:
        lines.append("- 唯讀 preflight findings：%s。" % "; ".join(
            "%s=%d" % (_display(evidence, name), evidence.specialist_counts[name])
            for name in sorted(evidence.specialist_counts)
        ))
    else:
        lines.append("- 沒有已提交的唯讀 preflight findings。")
    if evidence.risk_categories:
        lines.append("- 高風險類別：%s。" % ", ".join(
            _display(evidence, category) for category in evidence.risk_categories
        ))
    else:
        lines.append("- 沒有偵測到高風險變更類別。")
    return lines


def _knowledge_candidate(
    state: RunState, evidence: _Evidence, triggers: tuple[str, ...], total_repair_rounds: int,
) -> str:
    rejected = _stable_unique(
        _display(evidence, item["finding_id"]) for item in evidence.resolutions if item["outcome"] in {"not_reproduced", "disputed"}
    )
    corrected = _stable_unique(
        "%s:%s" % (
            _display(evidence, item.get("phase", state.kind)),
            _display(evidence, item["id"]),
        ) for item in evidence.findings
    )
    return _render_document("# Knowledge Candidate", (
        ("## 任務與結果", ["- Run kind：%s；目前狀態：%s。" % (_display(evidence, state.kind), _display(evidence, state.status.value))]),
        ("## 輪數與關鍵轉折", ["- 修復輪數：%d。" % total_repair_rounds, "- Candidate triggers：%s。" % ", ".join(_display(evidence, trigger) for trigger in triggers)]),
        ("## 為什麼需要這麼多輪", ["- %s" % _why_many_rounds(triggers)]),
        ("## 被否證的假設與無效修正", _section_bullets(evidence, rejected, "- 沒有可安全引用的已否證假設。", "rejected")),
        ("## 最終 Root Cause", ["- 僅以結構化 finding/resolution identifiers 保留脈絡；請在人工審核時查看證據索引。"]),
        ("## 建立或修正的不變量", _section_bullets(evidence, corrected, "- 沒有可安全引用的不變量修正。", "invariants")),
        ("## 可以提前執行的檢查", ["- 在下一輪前確認結構化 verification 結果與 diff 統計，而非閱讀完整 logs。"]),
        ("## 下次開工 Checklist", ["- 先確認未解決 finding identifiers、人工 gate 與 verification 摘要。", "- 人工核准後，才由後續流程決定是否寫回 workspace 或第二大腦。"]),
        ("## 證據索引", _evidence_index(evidence)),
    ))


def _why_many_rounds(triggers: Iterable[str]) -> str:
    labels = {
        "THREE_OR_MORE_REPAIR_ROUNDS": "至少三輪修復表示問題需要反覆驗證。",
        "REPEATED_INVARIANT": "相同不變量多次出現，表示需要把檢查前移。",
        "SCOPE_EXPANSION": "範圍擴張或高風險變更觸發了安全 gate。",
        "HUMAN_ARBITRATION": "流程曾等待人工仲裁。",
        "REUSABLE_ARCHITECTURE_OR_TESTING_LESSON": "verification 曾由失敗轉為成功，可轉化為可重用的測試檢查。",
        "ALL_SIX_REPAIR_ROUNDS_EXHAUSTED": "已耗盡六輪修復額度，必須由人工重新判斷。",
    }
    return " ".join(labels[trigger] for trigger in triggers)


def _stable_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def _section_values(evidence: _Evidence, values: Iterable[str], category: str) -> list[str]:
    values = list(values)
    if len(values) > MAX_SECTION_ITEMS:
        _truncated(evidence, "%s-items" % category)
        values = values[:MAX_SECTION_ITEMS]
    return values


def _section_bullets(evidence: _Evidence, values: Iterable[str], empty: str, category: str) -> list[str]:
    values = _section_values(evidence, values, category)
    return ["- %s" % value for value in values] if values else [empty]


def _evidence_index(evidence: _Evidence) -> list[str]:
    lines = ["- %s" % _display(evidence, identifier) for identifier in sorted(evidence.artifacts)]
    lines.extend(
        "- [truncated] %s: %d artifact(s) omitted." % (_display(evidence, category), count)
        for category, count in sorted(evidence.truncation.items())
        if count
    )
    return lines or ["- state.json"]


def _render_document(title: str, sections: Iterable[tuple[str, list[str]]]) -> str:
    sections = tuple(sections)
    lines = [title]
    for heading, values in sections:
        lines.extend(["", heading, *values])
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) <= MAX_MARKDOWN_BYTES:
        return text
    fallback = [title]
    for heading, _values in sections:
        fallback.extend(["", heading, "- [truncated] optional summary items omitted to preserve required sections."])
    return "\n".join(fallback) + "\n"


def _display(evidence: _Evidence, value: object) -> str:
    """Escape a bounded scalar, recording truncation without raw spillover."""
    text = _SECRET_LIKE.sub("[REDACTED]", str(value)).replace("\r", " ").replace("\n", " ")
    suffix = "… [truncated]"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_DYNAMIC_BYTES:
        prefix = encoded[:MAX_DYNAMIC_BYTES - len(suffix.encode("utf-8"))].decode("utf-8", "ignore")
        text = prefix + suffix
        _truncated(evidence, "dynamic-values")
    escaped = "".join("\\" + character if character in r"\\`*[]<>()#+!|" else character for character in text)
    return escaped.replace("\\[REDACTED\\]", "[REDACTED]").replace("\\[truncated\\]", "[truncated]")
