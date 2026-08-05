"""Strict normalization of one read-only iOS specialist preflight submission.

This module is deliberately inert: it parses and validates text, and starts no
process.  The three specialists are read-only session work performed by the
skill, never subprocesses launched by ``ai-review``, so nothing here builds an
argv or imports a process boundary.
"""

import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from .models import strict_json_loads


REQUIRED_SPECIALISTS = ("resilience-auditor", "swiftui-reviewer", "ux-critique")

SPECIALIST_ID_PREFIXES = {
    "resilience-auditor": "PF-RESILIENCE-",
    "swiftui-reviewer": "PF-SWIFTUI-",
    "ux-critique": "PF-UX-",
}

SPECIALIST_CATEGORIES = {
    "resilience-auditor": "resilience",
    "swiftui-reviewer": "swiftui",
    "ux-critique": "ux",
}

ALLOWED_SEVERITIES = ("blocker", "major", "minor")
ALLOWED_CATEGORIES = ("resilience", "swiftui", "ux")

MAX_SPECIALIST_FINDINGS = 20
MAX_PREFLIGHT_STRING_BYTES = 1_000
MAX_RISK_FLAGS = 8

_FINDING_FIELDS = frozenset({
    "id", "severity", "category", "location", "evidence", "required_outcome", "risk_flags",
})

_LOCATION = re.compile(r"^(?P<path>[^\x00:]+):(?P<line>[0-9]{1,9})$")


class PreflightError(ValueError):
    """A specialist submission is malformed, oversized, or unbound."""


def load_preflight_text(contents: str) -> Any:
    """Decode a submission without permitting duplicate object keys at any depth."""
    if not isinstance(contents, str) or not contents.strip():
        raise PreflightError("preflight submission must be non-empty text")
    try:
        return strict_json_loads(contents)
    except ValueError as error:
        raise PreflightError("preflight submission is not one valid JSON object") from error


def _bounded_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise PreflightError("preflight %s must be text" % label)
    normalized = value.strip()
    if not normalized:
        raise PreflightError("preflight %s must be non-empty" % label)
    if len(normalized.encode("utf-8")) >= MAX_PREFLIGHT_STRING_BYTES:
        raise PreflightError("preflight %s exceeds the bounded size" % label)
    if any(character in normalized for character in ("\x00", "\r")):
        raise PreflightError("preflight %s contains control characters" % label)
    return normalized


def _validated_location(value: Any) -> str:
    location = _bounded_string(value, "location")
    match = _LOCATION.match(location)
    if match is None:
        raise PreflightError("preflight location must be a relative path and line")
    path = match.group("path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("/") or path.startswith("~"):
        raise PreflightError("preflight location must not be an absolute path")
    if any(part == ".." for part in pure.parts):
        raise PreflightError("preflight location must not traverse outside the repository")
    return "%s:%s" % (path, match.group("line"))


def _validated_finding(value: Any, specialist: str) -> dict:
    if not isinstance(value, dict) or set(value) != _FINDING_FIELDS:
        raise PreflightError("preflight finding has unknown or missing keys")
    severity = _bounded_string(value["severity"], "severity")
    if severity not in ALLOWED_SEVERITIES:
        raise PreflightError("preflight severity must be blocker, major, or minor")
    category = _bounded_string(value["category"], "category")
    if category not in ALLOWED_CATEGORIES:
        raise PreflightError("preflight category must be swiftui, ux, or resilience")
    if category != SPECIALIST_CATEGORIES[specialist]:
        raise PreflightError("preflight category does not match its specialist")
    flags = value["risk_flags"]
    if not isinstance(flags, list) or len(flags) > MAX_RISK_FLAGS:
        raise PreflightError("preflight risk flags must be a bounded list")
    return {
        "id": _bounded_string(value["id"], "id"),
        "severity": severity,
        "category": category,
        "location": _validated_location(value["location"]),
        "evidence": _bounded_string(value["evidence"], "evidence"),
        "required_outcome": _bounded_string(value["required_outcome"], "required outcome"),
        "risk_flags": [_bounded_string(flag, "risk flag") for flag in flags],
    }


def validate_preflight(payload: Any, *, patch_digest: str) -> dict:
    """Return the one normalized submission bound to this exact patch digest."""
    if not re.fullmatch(r"[0-9a-f]{64}", patch_digest or ""):
        raise PreflightError("preflight patch digest binding is invalid")
    if not isinstance(payload, dict) or set(payload) != {
        "profile", "patch_digest", "specialists",
    }:
        raise PreflightError("preflight submission has unknown or missing keys")
    if payload["profile"] != "ios":
        raise PreflightError("preflight submission requires the ios profile")
    submitted = payload["patch_digest"]
    if not isinstance(submitted, str) or not re.fullmatch(r"[0-9a-f]{64}", submitted):
        raise PreflightError("preflight patch digest must be SHA-256 hex")
    if submitted != patch_digest:
        raise PreflightError("preflight patch digest does not match the awaiting patch")
    specialists = payload["specialists"]
    if not isinstance(specialists, list) or len(specialists) != len(REQUIRED_SPECIALISTS):
        raise PreflightError("preflight submission requires exactly three specialists")
    normalized = {}
    identifiers = set()
    for entry in specialists:
        if not isinstance(entry, dict) or set(entry) != {"name", "findings"}:
            raise PreflightError("preflight specialist has unknown or missing keys")
        name = entry["name"]
        if name not in SPECIALIST_ID_PREFIXES:
            raise PreflightError("preflight specialist name is not a required reviewer")
        if name in normalized:
            raise PreflightError("preflight specialist appears more than once")
        findings = entry["findings"]
        if not isinstance(findings, list) or len(findings) > MAX_SPECIALIST_FINDINGS:
            raise PreflightError("preflight specialist findings exceed the bounded list")
        validated = []
        for finding in findings:
            item = _validated_finding(finding, name)
            if item["id"] in identifiers:
                raise PreflightError("preflight finding ids must be unique")
            identifiers.add(item["id"])
            validated.append(item)
        normalized[name] = validated
    if set(normalized) != set(REQUIRED_SPECIALISTS):
        raise PreflightError("preflight submission must cover every required specialist")
    return {
        "profile": "ios",
        "patch_digest": patch_digest,
        "specialists": [
            {"name": name, "findings": normalized[name]}
            for name in REQUIRED_SPECIALISTS
        ],
    }


_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2}


def normalized_findings(submission: Mapping[str, Any]) -> tuple[dict, ...]:
    """Translate specialist findings into the existing Codex finding shape.

    IDs are prefixed per specialist so a repair resolution can never confuse a
    Codex finding with a specialist one, and the source lens stays visible in the
    evidence text because the review schema's lineage carries only a resolution.

    Two lenses describing the exact same ``(category, location, required_outcome)``
    describe one repair, so they merge into one finding.  Nothing is discarded:
    the merged finding keeps the highest severity, and every contributing source
    ID is named in its evidence so the human and Claude both still see them.
    See :func:`merged_source_ids` for the machine-readable mapping.
    """
    merged: dict[tuple[str, str, str], dict] = {}
    for specialist in submission["specialists"]:
        name = specialist["name"]
        prefix = SPECIALIST_ID_PREFIXES[name]
        for item in specialist["findings"]:
            key = (item["category"], item["location"], item["required_outcome"])
            identifier = prefix + item["id"]
            existing = merged.get(key)
            if existing is None:
                merged[key] = {
                    "id": identifier,
                    "severity": item["severity"],
                    "invariant": "%s quality bar" % item["category"],
                    "location": item["location"],
                    "evidence": "[%s] %s" % (name, item["evidence"]),
                    "required_outcome": item["required_outcome"],
                    "lineage": {"resolution": "existing"},
                    "_sources": [identifier],
                }
                continue
            existing["_sources"].append(identifier)
            if _SEVERITY_ORDER[item["severity"]] < _SEVERITY_ORDER[existing["severity"]]:
                existing["severity"] = item["severity"]
            existing["evidence"] = "%s; [%s] %s" % (
                existing["evidence"], name, item["evidence"]
            )
    findings = []
    for value in merged.values():
        sources = sorted(value.pop("_sources"))
        if len(sources) > 1:
            value["evidence"] = "%s (also reported as %s)" % (
                value["evidence"], ", ".join(sources[1:])
            )
        value["evidence"] = _bounded_evidence(value["evidence"])
        findings.append(value)
    return tuple(sorted(findings, key=lambda item: item["id"]))


def _bounded_evidence(value: str) -> str:
    """Keep merged evidence inside the per-string budget on a UTF-8 boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) < MAX_PREFLIGHT_STRING_BYTES:
        return value
    suffix = " [truncated]"
    room = MAX_PREFLIGHT_STRING_BYTES - 1 - len(suffix.encode("utf-8"))
    return encoded[:room].decode("utf-8", "ignore") + suffix


def merged_source_ids(submission: Mapping[str, Any]) -> dict[str, list[str]]:
    """Map each retained finding ID to every specialist ID it represents."""
    mapping: dict[tuple[str, str, str], list[str]] = {}
    for specialist in submission["specialists"]:
        prefix = SPECIALIST_ID_PREFIXES[specialist["name"]]
        for item in specialist["findings"]:
            key = (item["category"], item["location"], item["required_outcome"])
            mapping.setdefault(key, []).append(prefix + item["id"])
    return {sources[0]: sorted(sources) for sources in mapping.values()}


def submission_risk_flags(submission: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the sorted unique risk flags every specialist reported."""
    flags = set()
    for specialist in submission.get("specialists", []):
        for finding in specialist.get("findings", []):
            flags.update(finding.get("risk_flags", []))
    return tuple(sorted(flags))


def deduplicated(findings: Iterable[Mapping[str, Any]]) -> tuple[dict, ...]:
    """Drop a finding only when an identical ID was already queued.

    Specialist merging happens in :func:`normalized_findings`, which owns the
    ``(category, location, required_outcome)`` rule and preserves source IDs.
    This pass therefore exists only to keep the combined Codex + specialist queue
    free of duplicate IDs: a Codex finding and a specialist finding are separate
    reports even when they touch the same line, and each must receive its own
    resolution from Claude.
    """
    seen = set()
    ordered = []
    for finding in findings:
        identifier = finding.get("id")
        if identifier in seen:
            continue
        seen.add(identifier)
        ordered.append(dict(finding))
    return tuple(ordered)
