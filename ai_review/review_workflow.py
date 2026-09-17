"""Codex-first direct review of existing local code, with bounded Claude repair.

A Review run never owns or inherits a Plan.  Its trust anchor is the natively
approved ``ReviewManifest``: a frozen base commit, the exact initial patch, a
short brief, signed verification argv, and both model executable identities.
Codex is always the first model to see the code, and only the single workflow
Claude may edit the worktree.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .models import ReviewManifest, RunState, Status, verify_risk_approval
from .preflight import (
    deduplicated, normalized_findings, submission_risk_flags, validate_preflight,
)
from .process_security import run_git
from .review_risk import (
    RiskDetectionError, detect_high_risk, is_unresolved_path,
    path_content_fingerprints, risk_categories,
)
from .runners import validate_claude_resolution, validate_codex_review
from .store import git_worktree_root
from .workflow import CodeWorkflow, WorkflowError


# Positive output contract for the single Claude repair, never three more agents.
REPAIR_LENSES = {
    "ios-distill": "remove unnecessary SwiftUI structure without removing behavior",
    "code-simplifier": "preserve behavior while improving clarity and consistency",
    "ios-polish": "finish interaction, layout, accessibility, and state details",
}


def _resolve_base_oid(repo: Path, base_oid: str) -> str:
    """Confirm the approved base commit is still present in this repository."""
    try:
        completed = run_git(
            ["-C", str(repo), "rev-parse", "--verify", "--end-of-options", base_oid + "^{commit}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise WorkflowError("approved Review base commit is unavailable") from error
    resolved = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", resolved):
        raise WorkflowError("approved Review base did not resolve to one commit")
    return resolved


class DirectReviewWorkflow(CodeWorkflow):
    """Review existing code without a Plan, reusing the durable Code loop.

    Every safety mechanism of Code mode is retained unchanged: intent-before-call
    journals, one patch capture per round, signed verification before each Codex
    review, the six-repair ceiling, question cycles, bounded context expansion,
    finding lineage, and repeated-dispute detection.  Only the inputs and the
    first snapshot differ.
    """

    def run(self, run_id: str) -> RunState:
        """Consume one signed risk approval, if present, then run the loop."""
        self._consume_risk_approval(run_id)
        state = self.store.load(run_id)
        if state.status == Status.AWAITING_PREFLIGHT:
            # Retiring `submit-preflight` removed the only way out of this gate;
            # it did not open the gate.  The runs already parked here were
            # created under a contract this code no longer implements, and their
            # `awaiting_preflight` journal would make the pending-review scan
            # skip the very round that holds the findings.
            return state
        return super().run(run_id)

    def _handle_code_review(
        self, state: RunState, artifacts: Path, sequence: int, raw_review: Any
    ) -> None:
        merged = artifacts / "merged-reviews" / ("%04d.json" % sequence)
        if self.store.artifact_exists(merged):
            # A replayed round re-enters with the *raw* persisted Codex review,
            # which the merge replaced.  The specialist findings live only in
            # the merged decision, so replaying the raw one would drop them.
            super()._handle_code_review(state, artifacts, sequence, self._read_json(merged))
            return
        if self._merges_specialists(state, artifacts, raw_review):
            self._merge_specialist_findings(state, artifacts, sequence, raw_review)
            return
        super()._handle_code_review(state, artifacts, sequence, raw_review)

    def _merges_specialists(
        self, state: RunState, artifacts: Path, raw_review: Any
    ) -> bool:
        """True for the one review the frozen specialist findings belong to.

        The specialists judged the round-zero patch, so their findings belong to
        the first decisive review of it and to no later round.  ``merged-reviews``
        is the durable record of that having happened, which keeps the rule true
        across a resume.
        """
        if state.manifest.profile != "ios":
            return False
        # The manifest, not an artifact, decides whether this run has a
        # preflight: an artifact can be removed after the human signed for it,
        # and a missing one must fail closed rather than skip the merge.
        if state.manifest.preflight_digest is None:
            return False
        if self.store.list_artifacts(artifacts / "merged-reviews", ".json"):
            return False
        try:
            review = validate_codex_review(raw_review)
        except (TypeError, ValueError):
            # Malformed output is not a merge point; the shared handler pauses.
            return False
        return review["verdict"] in ("PASS", "CHANGES_REQUIRED")

    def _signed_specialist_findings(
        self, state: RunState, artifacts: Path
    ) -> list[dict]:
        """Return the findings the human's approval actually covers.

        The derived ``preflight-normalized.json`` is a convenience, not the
        contract: the contract is ``preflight.json`` matching the
        ``preflight_digest`` frozen into the signed manifest.  The findings are
        recomputed from that source every time, so tampering with, or deleting,
        the derived artifact cannot quietly drop an approved finding.
        """
        expected = state.manifest.preflight_digest
        raw = self._read_json(artifacts / "preflight.json", default=None)
        if raw is None:
            raise WorkflowError("approved specialist preflight is missing")
        digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if digest != expected:
            raise WorkflowError("specialist preflight does not match the approved digest")
        try:
            submission = validate_preflight(raw)
        except ValueError as error:
            raise WorkflowError("approved specialist preflight is malformed") from error
        return list(normalized_findings(submission))

    def _merge_specialist_findings(
        self, state: RunState, artifacts: Path, sequence: int, raw_review: Any,
    ) -> None:
        """Merge Codex and specialist findings into one decision for this round."""
        review = validate_codex_review(raw_review)
        specialist = self._signed_specialist_findings(state, artifacts)
        merged = deduplicated([*review["findings"], *specialist])
        decision = {
            "verdict": "CHANGES_REQUIRED" if merged else "PASS",
            "summary": (
                "merged Codex and read-only specialist findings"
                if merged else "Codex and every read-only specialist reported no findings"
            ),
            "findings": list(merged),
            "questions": [],
            "context_requests": [],
        }
        validate_codex_review(decision)
        self._write_json(
            artifacts / "merged-reviews" / ("%04d.json" % sequence), decision
        )
        # Hand the merged decision to the shared handler so a PASS, a repair, and
        # the resume journal all keep their normal shape.
        super()._handle_code_review(state, artifacts, sequence, decision)

    def _round_patch_digest(self, artifacts: Path, round_number: int) -> str:
        patch = self.store.read_artifact_bytes(
            artifacts / "patches" / ("round-%04d.patch" % round_number)
        )
        return hashlib.sha256(patch).hexdigest()

    def _capture_patch(self, state: RunState, artifacts: Path, round_number: int) -> None:
        super()._capture_patch(state, artifacts, round_number)
        self._assess_round_risk(state, artifacts, round_number)

    def _assess_round_risk(self, state: RunState, artifacts: Path, round_number: int) -> None:
        """Record high-risk evidence, and stop before another model sees a repair.

        Round zero is the human-approved baseline: ``approve-review`` already
        bound that exact patch digest, so its risk is recorded as evidence but
        does not gate the loop.  Every Claude-authored round must be approved
        again before the next model call.
        """
        patch_path = artifacts / "patches" / ("round-%04d.patch" % round_number)
        patch = self.store.read_artifact_bytes(patch_path)
        patch_digest = hashlib.sha256(patch).hexdigest()
        try:
            evidence = detect_high_risk(
                Path(state.manifest.repo_path), state.manifest.base_oid, patch,
                model_flags=self._model_risk_flags(artifacts),
            )
        except RiskDetectionError as error:
            raise WorkflowError("high-risk detection failed: %s" % error) from error
        # Every round assesses the whole base-to-worktree patch, so the reviewed
        # baseline's own high-risk paths reappear in each result. Only what a
        # Claude repair *introduced* may gate the loop: the human already bound
        # the baseline by digest in `approve-review`.
        fingerprints = path_content_fingerprints(
            Path(state.manifest.repo_path),
            (item["path"] for item in evidence if item["path"]),
        )
        introduced = self._evidence_beyond_baseline(
            artifacts, round_number, evidence, fingerprints
        )
        assessment = {
            "round": round_number,
            "patch_digest": patch_digest,
            "policy_version": state.manifest.risk_policy_version,
            "categories": list(risk_categories(evidence)),
            "evidence": list(evidence),
            "introduced_categories": list(risk_categories(introduced)),
            "introduced_evidence": list(introduced),
            "path_fingerprints": fingerprints,
        }
        self._write_json(
            artifacts / "risk-assessments" / ("round-%04d.json" % round_number), assessment
        )
        if not introduced or round_number < 1:
            return
        if self._risk_already_approved(state, artifacts, round_number, patch_digest):
            return
        self._write_json(artifacts / "risk-approval-request.json", {
            "status": "pending",
            "round": round_number,
            "manifest_digest": state.manifest.digest(),
            "patch_digest": patch_digest,
            "policy_version": state.manifest.risk_policy_version,
            "categories": list(risk_categories(introduced)),
            "evidence": list(introduced),
        })
        self._pause(
            state, artifacts, "HIGH_RISK_CHANGE",
            "approve-risk must bind this exact patch before another model call",
        )

    def _evidence_beyond_baseline(
        self, artifacts: Path, round_number: int,
        evidence: Iterable[Mapping[str, str]], fingerprints: Mapping[str, str],
    ) -> tuple[dict, ...]:
        """Return the evidence a Claude repair is responsible for.

        Matching the evidence tuple alone is not enough: `(category, path, reason)`
        only records *that* a path is risky, so re-editing a file the baseline
        already touched produced an identical tuple and slipped through. Content
        fingerprints decide it — a baseline path whose bytes changed is a Claude
        mutation, however the human's own diff already classified that path.
        """
        current = tuple(evidence)
        if round_number < 1:
            return current
        baseline_path = artifacts / "risk-assessments" / "round-0000.json"
        if not self.store.artifact_exists(baseline_path):
            # Without an approved baseline record every category must gate.
            return current
        baseline_record = self._read_json(baseline_path)
        recorded = baseline_record.get("evidence")
        baseline_fingerprints = baseline_record.get("path_fingerprints")
        if not isinstance(recorded, list) or not isinstance(baseline_fingerprints, dict):
            raise WorkflowError("baseline risk assessment is invalid")
        baseline = {
            (item.get("category"), item.get("path"), item.get("reason"))
            for item in recorded if isinstance(item, Mapping)
        }
        introduced = []
        for item in current:
            path = item.get("path")
            if not path:
                # A model self-reported this risk about its own repair.
                introduced.append(dict(item))
                continue
            if is_unresolved_path(path):
                # The exact path could not be preserved, so its content can never
                # be compared. Gate rather than assume it is unchanged.
                introduced.append(dict(item))
                continue
            if (item.get("category"), path, item.get("reason")) not in baseline:
                introduced.append(dict(item))
                continue
            if fingerprints.get(path) != baseline_fingerprints.get(path):
                introduced.append(dict(item))
        return tuple(introduced)

    def _validate_repair_output(
        self, raw: Any, required_finding_ids: list[str]
    ) -> Mapping[str, Any]:
        """Require the Review repair contract's explicit risk disclosure.

        An absent ``risk_flags`` is rejected rather than read as "no risk": a
        silent omission is exactly the failure this disclosure exists to prevent,
        so the run pauses for a human instead of proceeding on an assumption.
        """
        if not isinstance(raw, dict) or "risk_flags" not in raw:
            raise ValueError("Review repair must declare risk_flags, using [] for none")
        return validate_claude_resolution(
            raw, required_finding_ids=required_finding_ids, allow_risk_flags=True,
        )

    def _model_risk_flags(self, artifacts: Path) -> tuple[str, ...]:
        """Collect additive risk flags any model already reported in this run.

        Both sources matter: the read-only iOS specialists, and Claude's own
        disclosure on each repair.  Claude's `risk_flags` are read back from the
        already-journaled raw output, so a disclosure cannot be lost between the
        model call and detection.
        """
        flags = set()
        preflight = artifacts / "preflight.json"
        if self.store.artifact_exists(preflight):
            flags.update(submission_risk_flags(self._read_json(preflight)))
        for path in self.store.list_artifacts(artifacts / "claude-actions", ".json"):
            if not path.name.startswith("repair-"):
                continue
            reported = self._read_json(path)
            if not isinstance(reported, dict):
                continue
            for flag in reported.get("risk_flags", []) or []:
                if isinstance(flag, str) and flag.strip():
                    flags.add(flag.strip())
        return tuple(sorted(flags))

    def _risk_already_approved(
        self, state: RunState, artifacts: Path, round_number: int, patch_digest: str,
    ) -> bool:
        path = artifacts / "risk-approvals" / ("round-%04d.json" % round_number)
        if not self.store.artifact_exists(path):
            return False
        try:
            verify_risk_approval(
                self._read_json(path), self.store.authority, run_id=state.run_id,
                manifest_digest=state.manifest.digest(), patch_digest=patch_digest,
            )
        except (ValueError, WorkflowError):
            return False
        return True

    def _risk_pause_pending(self, artifacts: Path) -> bool:
        path = artifacts / "risk-approval-request.json"
        if not self.store.artifact_exists(path):
            return False
        return self._read_json(path).get("status") == "pending"

    def _consume_risk_approval(self, run_id: str) -> None:
        """Return a high-risk pause to RUNNING only for a signed exact patch."""
        state = self.store.load(run_id)
        if state.kind != "review" or state.status != Status.PAUSED:
            return
        artifacts = self._artifacts(state)
        if not self._risk_pause_pending(artifacts):
            return
        request = self._read_json(artifacts / "risk-approval-request.json")
        round_number = request.get("round")
        if type(round_number) is not int or round_number < 1:
            raise WorkflowError("high-risk approval request is invalid")
        # Recapture rather than trust the request: an edit after approval must
        # invalidate it, exactly as a changed Plan invalidates a Code approval.
        self._validate_state(state)
        current = self.store.read_artifact_bytes(
            artifacts / "patches" / ("round-%04d.patch" % round_number)
        )
        if hashlib.sha256(current).hexdigest() != request.get("patch_digest"):
            return
        if not self._current_worktree_matches(state, current):
            return
        if not self._risk_already_approved(
            state, artifacts, round_number, request["patch_digest"]
        ):
            return
        self._write_json(
            artifacts / "risk-approval-request.json", dict(request, status="approved")
        )
        self._set_running(state)
        self.store.save(state)

    def _current_worktree_matches(self, state: RunState, patch: bytes) -> bool:
        from .git_diff import capture_diff_bytes

        try:
            current, _lines = capture_diff_bytes(
                Path(state.manifest.repo_path), state.manifest.base_oid,
                production_excludes=self.policy.production_excludes,
            )
        except Exception:
            return False
        return hashlib.sha256(current).hexdigest() == hashlib.sha256(patch).hexdigest()

    def _prepare_initial_snapshot(self, state: RunState, artifacts: Path) -> None:
        """Capture the already-existing worktree instead of implementing anything.

        This is what makes Codex the first model action of a Review run: no
        Claude call precedes the round-0 patch.
        """
        self._validate_state(state)
        self._capture_patch(state, artifacts, 0)
        self._require_approved_round_zero(state, artifacts)

    def _require_approved_round_zero(self, state: RunState, artifacts: Path) -> None:
        """Refuse to review a round-0 patch the human never approved.

        `_capture_patch` reads the live worktree, so without this check any edit
        made between `approve-review` and `run` would silently become the
        reviewed baseline under a signed scope approval.
        """
        captured = self._round_patch_digest(artifacts, 0)
        if captured != state.manifest.initial_patch_digest:
            raise WorkflowError(
                "worktree changed after Review scope approval; "
                "the approved initial patch is no longer on disk"
            )

    def _review_input(self, state: RunState, artifacts: Path, verification: list[dict]) -> dict:
        # The same allowlist discipline as Code mode, with the brief replacing the
        # approved Plan: no repository sweep, credentials, or model chatter.
        pointer = self._latest_patch_pointer(artifacts)
        return {
            "review_brief": state.manifest.brief,
            "review_manifest_digest": state.manifest.digest(),
            "profile": state.manifest.profile,
            "repo": state.manifest.repo_path,
            "base_oid": state.manifest.base_oid,
            "patch_path": pointer["path"],
            "patch_sha256": pointer["sha256"],
            "patch_bytes": pointer["bytes"],
            "patch_stats": self._latest_stats(artifacts),
            "verification": self._prompt_verification(verification),
            "context_manifest": self._context_manifest(artifacts),
            "knowledge_packet": self._knowledge_packet(artifacts),
            "unresolved_prior_findings": self._unresolved_findings(artifacts),
            "user_decisions": self._user_decisions(artifacts),
            "policy": self._policy_input(),
        }

    def _repair_input(
        self, state: RunState, artifacts: Path,
        findings: list[Mapping[str, Any]], verification: list[dict],
    ) -> dict:
        inputs = {
            "review_brief": state.manifest.brief,
            "review_manifest_digest": state.manifest.digest(),
            "profile": state.manifest.profile,
            "repo": state.manifest.repo_path,
            "base_oid": state.manifest.base_oid,
            "findings": findings,
            "finding_ids": [finding["id"] for finding in findings],
            "verification": self._prompt_verification(verification),
            "context_manifest": self._context_manifest(artifacts),
            "knowledge_packet": self._knowledge_packet(artifacts),
            "user_decisions": self._user_decisions(artifacts),
        }
        if state.manifest.profile == "ios":
            # Constraints on this one repair, not extra mutating agents.
            inputs["repair_lenses"] = dict(REPAIR_LENSES)
        return inputs

    def _policy_input(self) -> dict:
        return {
            "version": self.policy.version,
            "max_rounds": self.policy.max_rounds,
            "max_context_tokens": self.policy.max_context_tokens,
            "max_context_expansions": self.policy.max_context_expansions,
            "production_line_limit": self.policy.production_line_limit,
            "production_growth_percent": self.policy.production_growth_percent,
        }

    def _validate_state(self, state: RunState) -> None:
        """Revalidate every approved Review binding before and after model use."""
        if state.kind != "review" or not isinstance(state.manifest, ReviewManifest):
            raise WorkflowError("Review workflow requires a persisted Review run")
        state.validate(self.store.authority)
        if state.approval_attestation is None:
            raise WorkflowError("Review requires native scope approval before any model call")
        if state.approval_attestation.manifest_digest != state.manifest.digest():
            raise WorkflowError("Review approval does not match the current manifest")
        repo = Path(state.manifest.repo_path)
        worktree = git_worktree_root(repo)
        if worktree != repo:
            raise WorkflowError("Review repository is not its own approved worktree root")
        if _resolve_base_oid(repo, state.manifest.base_oid) != state.manifest.base_oid:
            raise WorkflowError("approved Review base commit is unavailable")
        artifacts = self._artifacts(state)
        if state.manifest.context_checksum is not None:
            packet = self._load_context_packet(artifacts)
            if packet.checksum != state.manifest.context_checksum:
                raise WorkflowError("approved Review context packet binding changed")

    # Review mode deliberately has no production line or growth gate.
    #
    # Those limits exist in Code mode to bound how much a model writes from an
    # approved Plan, where every line is model-authored and the Plan predicts the
    # size.  A Review run starts from human-authored code of arbitrary size that
    # the human already bound by digest in `approve-review`, so neither an
    # absolute line budget nor a percentage of a prior delta carries a meaningful
    # signal here: measured against the baseline the limit blocked ordinary
    # branches, and measured against a prior repair delta a one-line follow-up
    # exceeded 30% of a one-line predecessor.
    #
    # Review scope is instead bounded by the six-repair ceiling, deterministic
    # high-risk detection with a fresh human approval per patch, and the final
    # human Code gate. Code mode's policy is untouched.
    def _scope_expanded(self, artifacts: Path) -> bool:
        return False

    def _captured_scope_expanded(self, artifacts: Path) -> bool:
        return False
