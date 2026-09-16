"""Bounded agent auto-approval for the three direct-Review gates.

The macOS dialog stays the only *human* gate.  This module adds a second,
explicitly labelled provider so `/consensus-review` can clear its own gates with
nobody at the keyboard, plus one global rate limit that stops a looping agent
from clearing them faster than a person ever could.

Two invariants keep the weakened gate auditable instead of invisible:

* every auto-approval receipt records ``agent:auto-approval`` as its provider
  and ``ai-review-agent`` as its actor, so a signed approval never claims that a
  human pressed anything; and
* every auto-approval consumes one slot from a single cross-process ledger — at
  most ``MAX_AUTO_APPROVALS`` inside any ``WINDOW_SECONDS``, counted across every
  run and every gate — so a retry loop throttles itself.

Only Review's two in-loop gates, ``approve-review`` and ``approve-risk``,
accept this provider.  Both terminal gates — ``approve-plan`` and
``approve-code`` — keep the single-mechanism contract they were built with:
whether the loop ended in a Codex PASS or exhausted its six repair rounds, a
person reads the diff before anything is approved.
"""

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import ReviewApprovalReceipt, RiskApprovalReceipt


AUTO_APPROVAL_PROVIDER = "agent:auto-approval"
AUTO_APPROVAL_ACTOR = "ai-review-agent"
AUTO_APPROVAL_COMMANDS = ("approve-review", "approve-risk")

MAX_AUTO_APPROVALS = 5
WINDOW_SECONDS = 60.0
LEDGER_NAME = "auto-approvals.json"
MAX_LEDGER_BYTES = 64 * 1024


class AutoApprovalRateLimited(Exception):
    """Raised when the sliding window is already full."""

    def __init__(self, retry_after: float, window_count: int):
        self.retry_after = max(0.0, float(retry_after))
        self.window_count = int(window_count)
        super().__init__(
            "auto-approval rate limit reached: %d approvals in the last %d seconds; "
            "retry_after=%.1fs" % (self.window_count, int(WINDOW_SECONDS), self.retry_after)
        )


def ledger_path(runs_root: Path) -> Path:
    """Keep the ledger beside the approval key, never inside the run store."""
    return Path(runs_root).parent / LEDGER_NAME


def _utc(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


def _counts_toward_window(entry: Any, now: float) -> bool:
    """Count anything not strictly older than the window.

    A timestamp in the future means the wall clock moved backwards; those still
    count, so a clock jump can never hand out extra approvals.
    """
    if not isinstance(entry, dict):
        return False
    at = entry.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    return float(at) > now - WINDOW_SECONDS


def _read_entries(descriptor: int) -> list:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, MAX_LEDGER_BYTES + 1)
    if not raw or len(raw) > MAX_LEDGER_BYTES:
        return []
    try:
        contents = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # A truncated or hand-edited ledger is treated as empty rather than as a
        # permanent lockout; the signed receipts remain the audit record.
        return []
    if not isinstance(contents, dict) or contents.get("version") != 1:
        return []
    entries = contents.get("entries")
    return entries if isinstance(entries, list) else []


def _write_entries(descriptor: int, entries: list) -> None:
    body = json.dumps(
        {"version": 1, "entries": entries}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    os.ftruncate(descriptor, 0)
    offset = 0
    while offset < len(body):
        offset += os.pwrite(descriptor, body[offset:], offset)
    os.fsync(descriptor)


def reserve(
    ledger: Path, *, run_id: str, command: str, now: Optional[float] = None
) -> dict:
    """Consume one auto-approval slot, or raise ``AutoApprovalRateLimited``.

    The whole read-prune-append cycle runs under an exclusive ``flock`` on the
    ledger itself, so concurrent ``ai-review`` processes cannot both observe the
    same free slot.
    """
    if command not in AUTO_APPROVAL_COMMANDS:
        raise ValueError("auto-approval is not available for %s" % command)
    moment = time.time() if now is None else float(now)
    path = Path(ledger)
    os.makedirs(path.parent, mode=0o700, exist_ok=True)
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        recent = [entry for entry in _read_entries(descriptor) if _counts_toward_window(entry, moment)]
        if len(recent) >= MAX_AUTO_APPROVALS:
            oldest = min(float(entry["at"]) for entry in recent)
            raise AutoApprovalRateLimited(oldest + WINDOW_SECONDS - moment, len(recent))
        record = {
            "at": moment, "approved_at": _utc(moment),
            "run_id": str(run_id), "command": command,
        }
        # Only the surviving window is kept: this is a limiter, not an audit log.
        _write_entries(descriptor, [*recent, record])
        return record
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class AgentApprovalProvider:
    """Approve a Review gate without user presence, under the shared limiter.

    The method signatures mirror ``MacOSHumanApprovalProvider`` exactly so a call
    site chooses a provider and nothing else changes.  Every digest binding,
    recapture and signature the human path performs still happens.
    """

    def approve_review(
        self, *, run_id: str, manifest_digest: str, brief: str,
        base_oid: str, initial_patch_digest: str,
        verification_commands: list,
    ) -> ReviewApprovalReceipt:
        return ReviewApprovalReceipt(
            run_id=run_id, manifest_digest=manifest_digest, approved_at=_utc(time.time()),
            provider=AUTO_APPROVAL_PROVIDER, actor=AUTO_APPROVAL_ACTOR,
        )

    def approve_risk(
        self, *, run_id: str, manifest_digest: str, patch_digest: str,
        categories: list, paths: list,
    ) -> RiskApprovalReceipt:
        return RiskApprovalReceipt(
            run_id=run_id, manifest_digest=manifest_digest, patch_digest=patch_digest,
            categories=tuple(sorted(set(categories))), approved_at=_utc(time.time()),
            provider=AUTO_APPROVAL_PROVIDER, actor=AUTO_APPROVAL_ACTOR,
        )
