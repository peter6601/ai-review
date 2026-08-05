#!/usr/bin/env python3
"""Literal queued Claude stand-in used only by the end-to-end subprocess test."""

import hashlib
import json
import os
import sys
from pathlib import Path


def _pop(queue_path: Path) -> dict:
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if not queue["claude"]:
        raise ValueError("fake queue exhausted: claude")
    value = queue["claude"].pop(0)
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    if not isinstance(value, dict) or set(value) - {"phase", "expect", "output", "write_files"}:
        raise ValueError("fake Claude queue entry has an invalid shape")
    return value


def _inputs(prompt: str) -> dict:
    marker = "\n\nINPUT_JSON:\n"
    if marker not in prompt:
        raise ValueError("fake Claude prompt has no structured input")
    return json.loads(prompt.split(marker, 1)[1])


def _phase(inputs: dict) -> str:
    if "answers" in inputs:
        return "plan-update"
    # Review mode never has an initial implementation phase: the only mutating
    # call is a repair of already-reviewed findings.
    if "review_brief" in inputs:
        return "review-repair"
    if "approved_plan" not in inputs:
        return "plan-resolution"
    if "findings" in inputs:
        return "code-repair"
    return "code-initial"


def _received(inputs: dict, phase: str) -> dict:
    if phase == "review-repair":
        return {"finding_ids": list(inputs.get("finding_ids", []))}
    if phase.startswith("plan"):
        received = {"plan_digest": hashlib.sha256(inputs["plan"].encode("utf-8")).hexdigest()}
    else:
        received = {
            "finding_ids": list(inputs.get("finding_ids", [])),
            "approved_plan_digest": hashlib.sha256(inputs["approved_plan"].encode("utf-8")).hexdigest(),
        }
    if phase == "plan-resolution":
        received["finding_ids"] = list(inputs.get("finding_ids", []))
    if phase == "plan-update":
        received["answer_digest"] = inputs["answer_digest"]
    return received


def main() -> int:
    try:
        argv = sys.argv[1:]
        schema = argv[argv.index("--json-schema") + 1]
        inputs = _inputs(argv[-1])
        queue_path = Path(os.environ["AI_REVIEW_FAKE_QUEUE"])
        log_path = Path(os.environ["AI_REVIEW_FAKE_LOG"])
        action = _pop(queue_path)
        phase = _phase(inputs)
        expected = action.get("expect")
        if action.get("phase") != phase:
            raise ValueError("fake Claude phase mismatch: expected %s, received %s" % (action.get("phase"), phase))
        received = _received(inputs, phase)
        if expected != received:
            raise ValueError("fake Claude input mismatch: expected %r, received %r" % (expected, received))
        output = action.get("output")
        if not isinstance(output, dict):
            raise ValueError("fake Claude output must be a literal object")
        for relative, contents in action.get("write_files", {}).items():
            target = Path.cwd() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "tool": "claude", "argv": argv, "schema": json.loads(schema),
                "phase": phase, "received": received,
                "expected": {"phase": action["phase"], "expect": expected},
            }) + "\n")
        sys.stdout.write(json.dumps(output))
        return 0
    except (KeyError, ValueError, IndexError, TypeError) as error:
        sys.stderr.write("fake Claude: %s\n" % error)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
