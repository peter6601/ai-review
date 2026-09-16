#!/usr/bin/env python3
"""Queued Codex stand-in used only by the end-to-end subprocess test."""

import json
import os
import sys
from pathlib import Path


def _pop(queue_path: Path, key: str):
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    values = queue[key]
    if not values:
        raise SystemExit("fake queue exhausted: %s" % key)
    value = values.pop(0)
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    return value


def _inputs(prompt: str) -> dict:
    marker = "\n\nINPUT_JSON:\n"
    if marker not in prompt:
        raise SystemExit("fake Codex prompt has no structured input")
    return json.loads(prompt.split(marker, 1)[1])


def _phase(inputs: dict) -> str:
    if "review_brief" in inputs:
        return "review-codex"
    if "approved_plan" in inputs:
        return "code-codex"
    return "plan-codex"


def _received(inputs: dict, phase: str) -> dict:
    if phase != "review-codex":
        return {}
    stats = inputs.get("patch_stats")
    return {
        "profile": inputs.get("profile"),
        "repair_round": stats.get("round") if isinstance(stats, dict) else None,
    }


def main() -> int:
    argv = sys.argv[1:]
    # The scope is checked positionally and includes ``-m``: the model must be
    # pinned by the caller, never inherited from ~/.codex/config.toml, so a
    # run that omits it fails here rather than silently using whatever that
    # global file names.
    if (
        len(argv) < 14
        or argv[:3] != ["-a", "never", "exec"]
        or argv[3] != "-m"
        or not argv[4]
        or argv[5] != "-C"
        or argv[7:9] != ["-s", "read-only"]
        or argv[9] != "--output-schema"
        or argv[11] != "-o"
    ):
        raise SystemExit("fake Codex received an invalid option scope")
    model = argv[4]
    output = Path(argv[12])
    schema = argv[10]
    prompt = argv[13]
    queue_path = Path(os.environ["AI_REVIEW_FAKE_QUEUE"])
    log_path = Path(os.environ["AI_REVIEW_FAKE_LOG"])
    entry = _pop(queue_path, "codex")
    inputs = _inputs(prompt)
    phase = _phase(inputs)
    record = {
        "tool": "codex", "argv": argv, "schema": schema, "phase": phase, "model": model,
    }
    # A queue entry is either a literal review or an envelope that also asserts
    # which phase and inputs this call was made with.
    if isinstance(entry, dict) and "output" in entry and set(entry) <= {
        "phase", "expect", "output",
    }:
        if entry.get("phase") != phase:
            raise SystemExit(
                "fake Codex phase mismatch: expected %s, received %s"
                % (entry.get("phase"), phase)
            )
        received = _received(inputs, phase)
        if entry.get("expect") != received:
            raise SystemExit(
                "fake Codex input mismatch: expected %r, received %r"
                % (entry.get("expect"), received)
            )
        record["received"] = received
        result = entry["output"]
    else:
        result = entry
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    output.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
