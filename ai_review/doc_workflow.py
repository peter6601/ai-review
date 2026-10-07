"""One read-only Codex pass over a feature document.

A doc run has no Claude and no repair loop: the findings are the deliverable,
and the document is edited — by the human, or by Claude for the findings the
human accepted after the run halts — before a re-run.  It writes exactly one file into
the reviewed repository — this round's report, created beside the document and
never on top of an existing one — and never the document itself.  What lives
here is only what makes those properties structural: the narrow review input,
the completion that never turns a verdict into a state, the report writer that
refuses rather than clobbers, and a Claude attribute that fails loudly instead
of silently existing.
"""

import contextlib
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from .context import BUDGET_METHOD_DOC, KnowledgePacket, SourceRef
from .models import DocManifest, RunState, Status, Verdict
from .policy import Policy
from .runners import RunnerInterrupted, validate_codex_review
from .store import RunStore, git_worktree_root
from .workflow import AnswerConflict, PlanWorkflow, WorkflowError, _utc_now


# The report's severity sections, in the order a reader triages them.  A
# section with no finding is omitted entirely rather than left empty.
REPORT_SEVERITIES = (
    ("blocker", "Blockers"),
    ("major", "Major"),
    ("minor", "Minor"),
    ("info", "Info"),
)


def doc_report_path(doc_path: Any, number: int) -> Path:
    """Name report ``number`` beside the document it reviewed.

    ``NN`` counts the reports that sit beside this document, across every run
    that ever reviewed it — not the review sequence inside one run.  A person
    asking "how many times has this been reviewed, and what changed between
    them?" reads the shelf; the run and the review sequence that produced each
    report are named in its header, so the audit trail back to
    ``reviews/NNNN.json`` is kept without the filename having to carry it.
    """
    if type(number) is not int or number < 1:
        raise ValueError("report number must be a positive integer")
    document = Path(doc_path)
    return document.with_name("%s-review-%02d.md" % (document.stem, number))


def next_doc_report_number(doc_path: Any) -> int:
    """Take the next number after the highest report beside this document.

    Gaps are never filled: if ``-01`` and ``-03`` are there, the next report is
    ``-04``.  Reusing ``-02`` would put a later review before an earlier one on
    a shelf people read in order.  Anything that is not this document's own
    numbered report — another document's, an unnumbered note, a different
    extension — is ignored, and a name that is taken is taken whatever it
    points at, so a symlink occupies its number rather than being followed.
    """
    document = Path(doc_path)
    pattern = re.compile(r"%s-review-([0-9]+)\.md\Z" % re.escape(document.stem))
    try:
        names = os.listdir(str(document.parent))
    except OSError as error:
        raise WorkflowError("the document's directory cannot be read") from error
    highest = 0
    for name in names:
        match = pattern.fullmatch(name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _one_line(value: str) -> str:
    """Keep one Codex-supplied string on the Markdown line it belongs to.

    Nothing is translated or reworded.  Line breaks are folded to spaces
    because a bullet or heading that spans lines would let evidence text forge
    a section heading of its own.
    """
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def render_round_report(
    state: RunState, number: int, sequence: int, review: Mapping[str, Any],
    reviewed_at: str,
) -> str:
    """Render one round exactly as it was returned, for the human who acts on it.

    ``number`` is this report's place on the shelf beside the document;
    ``sequence`` is the round inside the run that produced it.  Both are stated,
    because the filename now answers the human's question and the header has to
    keep answering the auditor's.
    """
    manifest = state.manifest
    lines = [
        "# %s review %02d" % (Path(manifest.doc_path).name, number),
        "",
        "- run: %s" % state.run_id,
        "- review sequence: %d" % sequence,
        "- lens: %s（%s）" % (manifest.lens, _one_line(manifest.lens_reason)),
        "- verdict: %s" % _one_line(review["verdict"]),
        "- reviewed at: %s" % reviewed_at,
    ]
    for severity, heading in REPORT_SEVERITIES:
        findings = [item for item in review["findings"] if item["severity"] == severity]
        if not findings:
            continue
        lines.extend(["", "## %s" % heading])
        for item in findings:
            lines.extend([
                "",
                "### %s — %s" % (_one_line(item["id"]), _one_line(item["invariant"])),
                "",
                "- location: %s" % _one_line(item["location"]),
                "- evidence: %s" % _one_line(item["evidence"]),
                "- required outcome: %s" % _one_line(item["required_outcome"]),
            ])
    if review["questions"]:
        lines.extend(["", "## Questions", ""])
        lines.extend("- %s" % _one_line(question) for question in review["questions"])
    return "\n".join(lines) + "\n"


class _AbsentClaude:
    """A Claude that cannot be used, and says so when something tries.

    A doc review is read-only by construction rather than by convention, so the
    workflow holds no model that can edit anything.  Naming the attempted call
    turns a future mistake into one clear message instead of an
    ``AttributeError`` on ``None`` three frames away.
    """

    def __getattr__(self, name: str) -> Any:
        raise WorkflowError("a doc review never invokes Claude (attempted Claude.%s)" % name)


class DocWorkflow(PlanWorkflow):
    """One read-only Codex pass over a document; no repair, no file writes."""

    # A finished doc run rests here, and ``run`` must not restart it.
    _HALT_STATUSES = PlanWorkflow._HALT_STATUSES + (Status.AWAITING_HUMAN_DOC_REVIEW,)

    # No Claude and no Plan exist in a doc run, so an answer is never pending an
    # update: it is the decision itself, and the document is what must conform.
    # Real Codex read the inherited "pending" wording as proof that an answered
    # question had not become a document requirement and re-reported the blocker.
    _DECISION_IMPACT = "settled user decision, binding on the document"

    def __init__(
        self,
        store: RunStore,
        codex: Any,
        claude: Any = None,
        *,
        policy: Optional[Policy] = None,
        context_packet: Optional[KnowledgePacket] = None,
        context_resolver: Optional[Callable[[Iterable[str]], Iterable[SourceRef]]] = None,
        fault_injector: Optional[Callable[[str], None]] = None,
    ):
        """Accept the shared workflow signature and discard any Claude given.

        Callers construct every workflow the same way; a doc run keeps the
        parameter so that symmetry holds, and replaces whatever arrives with a
        Claude that refuses to be called.
        """
        super().__init__(
            store, codex, _AbsentClaude(), policy=policy, context_packet=context_packet,
            context_resolver=context_resolver, fault_injector=fault_injector,
        )

    # ---- state -----------------------------------------------------------

    def _validate_state(self, state: RunState) -> None:
        if state.kind != "doc" or not isinstance(state.manifest, DocManifest):
            raise WorkflowError("doc workflow requires a persisted doc run")
        state.validate(self.store.authority)

    # ---- context ---------------------------------------------------------

    def _context_limits(self, state: RunState) -> tuple[int, int, str]:
        """Measure a doc packet with the estimator its documents need.

        The two caps come from the inherited lookup, which already keys off the
        run kind; only the estimator differs, because a Traditional Chinese
        document is nothing like four characters per token and the default
        method would let a packet over the budget through.
        """
        max_sources, max_tokens, _ = super()._context_limits(state)
        return max_sources, max_tokens, BUDGET_METHOD_DOC

    # ---- the reviewer's inputs -------------------------------------------

    def _review_input(self, state: RunState, artifacts: Path) -> dict:
        """Give the reviewer the document, its purpose, and the lens — nothing else.

        This exact allowlist is a trust boundary.  Codex reads the repository
        itself, read-only, through its own sandbox, so no repository contents,
        Plan text, patch, or verification evidence is inlined here.  ``max_rounds``
        is one because a doc run is one pass: the next round is a human editing
        the document and asking again.
        """
        manifest = state.manifest
        return {
            "document": self._document_text(state),
            "document_path": self._document_path(state),
            "lens": manifest.lens,
            "brief": manifest.brief,
            "decision_log": self._decision_log(artifacts),
            "context_manifest": self._context_manifest(artifacts),
            "knowledge_packet": self._knowledge_packet(artifacts),
            "policy": {"version": self.policy.version, "max_rounds": 1},
            "unresolved_prior_findings": self._unresolved_findings(artifacts),
        }

    @staticmethod
    def _document_text(state: RunState) -> str:
        """Read the document as it stands now; a re-review judges the new bytes."""
        try:
            return Path(state.manifest.doc_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise WorkflowError("document cannot be read") from error

    @staticmethod
    def _document_path(state: RunState) -> str:
        """Locate the document for the reviewer relative to the repository root."""
        try:
            root = git_worktree_root(Path(state.manifest.repo_path))
            return str(Path(state.manifest.doc_path).relative_to(root))
        except ValueError as error:
            raise WorkflowError("document must live inside the reviewed repository") from error

    # ---- one verdict, one outcome ----------------------------------------

    def _handle_review(self, state: RunState, artifacts: Path, sequence: int, raw_review: Any) -> None:
        """Turn one Codex verdict into the end of the run, a pause, or more context.

        Every outcome journals its action before changing state, and none of
        them calls ``transition``: a doc run has no verdict-driven status, and
        ``RunState.transition`` raises rather than inventing one.
        """
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
        if verdict == Verdict.CONTEXT_REQUEST:
            # Not an outcome a human reads: the round asked for evidence and
            # the next one answers it, so it leaves no report — and, for the
            # same reason, records no findings.  Codex has said it cannot judge
            # the document yet, so whatever it mentions here is provisional:
            # handing it on as outstanding would ask the next round to ratify a
            # judgement this one withheld, and letting a reportless round write
            # the artifact would let an empty context request erase what the
            # last judged round left outstanding — the very loss this
            # workflow's lineage contract exists to prevent.  The findings stay
            # in ``reviews/`` as the audit record, and the round that does
            # judge — same document, more evidence — restates whatever holds.
            self._expand_context(state, artifacts, sequence, review["context_requests"])
            if state.status == Status.RUNNING:
                self._write_json(action, {"action": "context_expanded"})
            return
        if verdict == Verdict.CHANGES_REQUIRED and not review["findings"]:
            # Refused before the report is written: an invalid round has no
            # findings to hand anyone.
            self._pause(state, artifacts, "CHANGES_WITHOUT_FINDING", "changes require at least one finding")
            return
        try:
            # The deliverable is written before any state moves.  A refused
            # write therefore leaves a paused run with nothing journalled,
            # never a finished run whose reader has nothing to read.
            self._write_round_report(state, artifacts, sequence, review)
        except WorkflowError as error:
            self._pause(state, artifacts, "DOC_REPORT_NOT_WRITTEN", str(error))
            return
        # Every verdict that wrote a report records what that report said, here
        # and nowhere else, so no branch can end a round with its findings
        # rendered and then forgotten.  It sits after the report and before the
        # journal and the state change, on the same discipline: a record that
        # fails pauses the run loudly rather than leaving it looking finished.
        self._record_round_findings(artifacts, review["findings"])
        if verdict == Verdict.PASS:
            # A PASS carries no finding, so recording this round emptied the
            # artifact: the findings a previous round left are settled, because
            # this round read the edited document and found nothing.
            # ``PlanWorkflow`` clears this artifact after a repair for the same
            # reason; a doc run's repair is the human's edit, so the PASS is
            # where it clears.  Without this, a third round would be handed
            # findings the second round already proved gone.
            self._write_json(action, {"action": "pass"})
            state.complete_doc_review()
            self.store.save(state)
            return
        if verdict == Verdict.NEEDS_USER_INPUT:
            # The most valuable output of a requirements review: the document is
            # ambiguous, and only its author can settle it.  The questions are
            # not the whole round, though — it reported findings as well, and
            # they were just recorded, so the round that runs after ``answer``
            # is told what is still open instead of starting from nothing and
            # calling every one of its own findings newly discovered.
            cycle = self._write_questions(artifacts, review["questions"], sequence)
            self._write_json(action, {"action": "awaiting_user_input", "question_cycle": cycle})
            self._set_status(state, Status.AWAITING_USER_INPUT)
            self.store.save(state)
            return
        self._handle_changes(state, artifacts, sequence, review, action)

    def _handle_changes(
        self, state: RunState, artifacts: Path, sequence: int,
        review: Mapping[str, Any], action: Path,
    ) -> None:
        """End the run on findings, which ``_handle_review`` has already recorded.

        There is no lineage gate here: the anti-ratchet rule exists because a
        repair step can move the target between Codex rounds, and a doc run has
        no repair step.  A round with no finding at all is refused in
        ``_handle_review``, before its report is written, so by here the round
        has at least one finding to hand over.
        """
        findings = list(review["findings"])
        # The journal deliberately carries no ``before_round``: the inherited
        # pending-review scan replays a "changes" entry only while the repair
        # round it recorded has not advanced, and a doc run has no repair round
        # to advance.  Adding one here would make a re-review replay this
        # outcome instead of asking Codex again.
        self._write_json(
            action,
            {"action": "changes", "finding_ids": [item["id"] for item in findings]},
        )
        state.complete_doc_review()
        self.store.save(state)

    def _record_round_findings(
        self, artifacts: Path, findings: Iterable[Mapping[str, Any]]
    ) -> None:
        """Hand this round's findings to the next one.

        Findings are the deliverable, and ``unresolved-findings.json`` is the
        only channel a doc run has for telling the next round what is still
        outstanding.  Written wholesale, it always mirrors the round that wrote
        the last report, so the list Codex is handed and the report a human is
        holding can never disagree: a PASS clears it, a question round carries
        its findings across the human's answers, and neither is a special case.

        The seen-ids set only ever grows, so a round with no finding leaves it
        untouched rather than creating it empty.  The unresolved list is not
        the same kind of thing — "nothing is outstanding" is a statement this
        round made — so it is written whether or not there are findings.
        """
        recorded = list(findings)
        self._write_json(artifacts / "unresolved-findings.json", {"findings": recorded})
        if recorded:
            self._record_seen_findings(artifacts, recorded)

    def _resume_action(self, state: RunState, artifacts: Path, action: Path) -> None:
        """Finish a doc run whose journal was written before its state change.

        The inherited version replays a repair round through ``transition``;
        neither exists here, so a journalled outcome is completed the only way a
        doc run ends.
        """
        record = self._read_json(action)
        if not isinstance(record, dict):
            raise WorkflowError("review action journal is invalid")
        if record.get("action") not in ("pass", "changes"):
            return
        if state.status in (Status.READY, Status.RUNNING):
            state.complete_doc_review()
            self.store.save(state)

    # ---- the report the human reads --------------------------------------

    def _write_round_report(
        self, state: RunState, artifacts: Path, sequence: int, review: Mapping[str, Any],
    ) -> None:
        """Create this round's report beside the document, or refuse.

        This is the only write a doc run ever performs inside the reviewed
        repository, and it is a creation.  The number comes from scanning what
        is already there, but a scan can only describe the past: the file is
        still opened with ``O_EXCL`` and ``O_NOFOLLOW``, so a writer that
        arrives in the window between the scan and the open loses the race
        loudly instead of having its report replaced.  ``RunStore`` is
        deliberately not used: it refuses to write inside the repository, which
        is exactly where this file belongs.

        A partially written report is removed before the error propagates, so
        the round can be retried; a report that was fully written and then
        crashed before its journal stops the retry, by design — the evidence on
        disk is never silently replaced.
        """
        number = next_doc_report_number(state.manifest.doc_path)
        path = doc_report_path(state.manifest.doc_path, number)
        payload = render_round_report(
            state, number, sequence, review, _utc_now()
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags, 0o644)
        except FileExistsError as error:
            raise WorkflowError(
                "a report for this round already exists: %s" % path.name
            ) from error
        except OSError as error:
            raise WorkflowError(
                "the round report cannot be created: %s" % path.name
            ) from error
        try:
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
        except OSError as error:
            with contextlib.suppress(OSError):
                os.unlink(str(path))
            raise WorkflowError(
                "the round report could not be written: %s" % path.name
            ) from error
        except BaseException:
            # An interrupt is not a failed report: drop the half-written file
            # and let it travel, rather than turning it into a pause.
            with contextlib.suppress(OSError):
                os.unlink(str(path))
            raise
        finally:
            os.close(descriptor)
        # Which report this round wrote is no longer derivable from the review
        # sequence, so the run records it.  Only the number is kept: the name
        # is rebuilt from the manifest, so no summary ever renders a filename
        # an artifact supplied.
        self._write_json(
            artifacts / "doc-reports" / ("%04d.json" % sequence),
            {"review_sequence": sequence, "report_number": number},
        )

    # ---- user answers ----------------------------------------------------

    def answer(self, run_id: str, answers: Mapping[str, str]) -> RunState:
        """Record the answers, then review again with them in the decision log.

        No model updates the document from an answer: the document is the
        human's to change.  The answers reach Codex as decisions on the next
        pass, which is exactly what the reviewer asked for.
        """
        state = self.store.load(run_id)
        try:
            self._validate_state(state)
            if state.status != Status.AWAITING_USER_INPUT:
                raise WorkflowError("doc review is not awaiting user input")
            artifacts = self._artifacts(state)
            cycle, cycle_dir = self._active_question_cycle(artifacts)
            questions = self._read_questions(artifacts, cycle)
            normalized = self._validate_answers(questions, answers)
            submission = self._answer_submission(artifacts, normalized, cycle)
            normalized = submission["answers"]
            self._write_immutable_json(cycle_dir / "answers.json", submission)
            self._append_decisions(artifacts, questions, normalized)
            self._set_running(state)
            self.store.save(state)
            return self.run(run_id)
        except AnswerConflict:
            # A conflicting retry must change neither the answer record nor the
            # state; only the original immutable answers can be retried.
            raise
        except RunnerInterrupted as error:
            self._interrupt_loaded(state, "RUNNER_INTERRUPTED", str(error))
            raise
        except Exception as error:
            return self._pause_loaded(state, "INVALID_DOC_USER_ANSWER", str(error))
