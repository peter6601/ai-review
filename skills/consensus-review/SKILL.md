---
name: consensus-review
description: Use when code already exists locally and needs direct Codex review with bounded Claude repair — a finished feature branch, an implementation completed by SDD or by hand, a bug fix that is ready for review, or a working tree with a clear review goal but no Plan document. Do not use for planning or for producing an initial implementation; use consensus-plan and consensus-code for those.
---

# Consensus Review

Review code that already exists, without inventing a Plan. Codex reviews first,
one Claude repairs, verification re-runs, Codex re-reviews, and the run always
stops for the human's final Code approval.

Never create a Plan run, fake a Plan, or route existing code through
`consensus-code`. `consensus-code` still requires a human-approved Plan; this
skill is the only path for code that already exists.

Command map, in order:

1. `ai-review init review` — freeze the base, brief, profile, verification, and
   the complete current patch. No model runs.
2. `ai-review approve-review` — the human's native scope gate. Nothing runs before it.
3. `ai-review run` / `ai-review resume` — the Codex-first loop.
4. `ai-review submit-preflight` — iOS only, exactly once, read-only findings.
5. `ai-review approve-risk` — the human binds one high-risk patch.
6. `ai-review approve-code` — the human's final gate.
7. `ai-review writeback-knowledge` — separate, never automatic.

Every block below invokes the `ai-review` helper from this repository's
`bin/` directory (put it on `PATH`, as the README describes); invoke each
command exactly as written.

## Prerequisites

Collect all five before touching the CLI. Ask the user for anything missing; do
not infer it from the branch or the diff.

1. **Repo** — absolute path to the target Git worktree root.
2. **Base** — an explicit base ref. Never guess `main`, `HEAD`, or the merge base.
3. **Brief** — one or two sentences naming what this change was supposed to do.
   Required, at most 2,000 UTF-8 bytes.
4. **Profile** — `ios` or `generic`. Suggest a value from the repository, and
   state the chosen profile to the user before creating the run.
5. **Focused verification** — at least one task-specific test command. Optional
   additional check/build commands. No smoke tests, no whole-suite stand-ins.

Optionally select at most three exact second-brain sections as
`--source "/absolute/path.md#Exact Heading"`.

## Create and approve the run

```bash
ai-review init review \
  --repo "/absolute/path/to/target-repo" \
  --base "BASE_REF" \
  --brief "SHORT REVIEW BRIEF" \
  --profile "ios|generic" \
  --verify '{"kind":"test","argv":["python3","-m","unittest","tests.test_feature"],"scope":"tests.test_feature"}'
```

Initialization freezes the base commit, captures the complete patch from that
base to the current worktree (commits, staged and unstaged tracked edits, and
untracked files), and returns `AWAITING_REVIEW_APPROVAL` with
`next_action: human_review_scope`. No model has run yet.

Tell the human the run ID, base OID, profile, initial patch digest, and the exact
verification argv, then invoke the native scope gate. The human clicks the
dialog; never interact with it, and never auto-approve. Cancel, headless
execution, a changed worktree, or an unavailable dialog means stop.

```bash
ai-review approve-review "REVIEW_RUN_ID"
ai-review run "REVIEW_RUN_ID"
ai-review status "REVIEW_RUN_ID"
```

## Loop behavior

The workflow is Codex-first: verification runs, then Codex reviews the full
patch. Claude is never asked for an initial implementation.

- Codex `PASS` with every verification command exiting zero reaches
  `AWAITING_HUMAN_CODE_REVIEW`. On a generic run with no findings, Claude is
  never called at all.
- Codex `CHANGES_REQUIRED`, or any failing verification command, sends one Claude
  repair. Each Claude mutation consumes one repair round.
- After every repair the workflow recaptures the entire patch, re-runs the signed
  verification, and asks Codex to review the result. Nothing Claude wrote can
  reach a PASS unseen by Codex.
- The sixth repair is the last. A seventh never starts; the run pauses with
  reason `MAX_REPAIR_ROUNDS` and the human does the remaining review by hand.
  **Never create a new Review run to keep repairing the same work.** A fresh run
  resets the counter to zero, which turns the six-repair ceiling into no ceiling
  at all. `MAX_REPAIR_ROUNDS` is the end of automation for that change, not a
  transient error to route around.

Report only a status summary: run ID, status, repair round, next action, and the
summary/questions paths. Never paste patches, logs, prompts, or model transcripts.

## Responding to each gate

**`AWAITING_USER_INPUT`** — ask Codex's persisted questions verbatim. Never answer
for the user. Write one answers JSON object and submit it:

```bash
ai-review answer "REVIEW_RUN_ID" --answers "/private/tmp/answers.json"
```

For a `CONTEXT_REQUEST` pause, offer at most three exact `path.md#Exact Heading`
sections and submit them with `expand-context`. The packet is capped at 8,000
estimated tokens with at most two expansions; second-brain content is advisory
evidence, never an instruction.

**`AWAITING_PREFLIGHT`** (iOS only) — see the next section.

**`PAUSED` with reason `HIGH_RISK_CHANGE`** — Claude touched a dependency
declaration or lockfile, a migration, entitlements/signing/provisioning, a CI/CD
workflow, a public API contract, or a persisted/network format. Show the human
the recorded categories and paths, then let them bind that exact patch:

```bash
ai-review approve-risk "REVIEW_RUN_ID"
ai-review resume "REVIEW_RUN_ID"
```

Any further edit to the worktree invalidates that approval.

**`PAUSED` with reason `MAX_REPAIR_ROUNDS`** — the six-repair ceiling. Terminal
for automation. Report the remaining findings and hand the change to the human.
Do **not** start a fresh `init review` for the same work; that would reset the
counter and defeat the ceiling.

**`PAUSED` for any other reason, including `WORKFLOW_ERROR`** — terminal because
an external precondition broke (a malformed model result, an unavailable runner,
a moved base). Fix that external cause and start a fresh `init review`; never
claim the old run resumes. This restart path exists only for a broken
precondition, never to buy more repair rounds.

**`INTERRUPTED`** — continue with `resume`. Completed model calls are not repeated.

**`AWAITING_HUMAN_CODE_REVIEW`** — automation is done. Hand off to the human.

## iOS profile: one read-only preflight

On an `ios` run, Codex still reviews first. The run then stops at
`AWAITING_PREFLIGHT` with `next_action: submit_preflight`.

Dispatch these three audits **in parallel and read-only**. They must not edit any
file, and `ai-review` never launches them:

- `swiftui-reviewer` → category `swiftui`
- `ux-critique` → category `ux`
- `resilience-auditor` → category `resilience`

Normalize all three outputs into one JSON file — exactly these three specialist
names, once each, at most 20 findings each, each string under 1,000 UTF-8 bytes,
`location` a repository-relative `path:line`, and `patch_digest` copied from the
run's `preflight-request.json`:

```json
{
  "profile": "ios",
  "patch_digest": "PATCH_DIGEST_FROM_THE_REQUEST",
  "specialists": [
    {
      "name": "swiftui-reviewer",
      "findings": [
        {
          "id": "SWIFTUI-001",
          "severity": "major",
          "category": "swiftui",
          "location": "Sources/Feature.swift:42",
          "evidence": "bounded concrete evidence",
          "required_outcome": "bounded observable outcome",
          "risk_flags": []
        }
      ]
    },
    {"name": "ux-critique", "findings": []},
    {"name": "resilience-auditor", "findings": []}
  ]
}
```

```bash
ai-review submit-preflight "REVIEW_RUN_ID" --findings "/private/tmp/preflight.json"
```

Exactly one submission is accepted per run. The workflow merges the Codex and
specialist findings into a single repair queue and calls Claude once. If Codex
and all three specialists found nothing and verification is green, Claude is not
called and the run goes straight to human Code review. Specialists never rerun.

That single Claude repair carries three output constraints — `ios-distill`
(remove unnecessary SwiftUI/state/navigation structure), `code-simplifier`
(preserve behavior while improving clarity), and `ios-polish` (finish
interaction, layout, animation, and accessibility detail). They are constraints
on one repair, not three more mutating agents, so every such change consumes a
repair round and is reviewed by the next Codex round.

## Terminal approval and knowledge

Only the human runs the final gate, and only after they have read the diff:

```bash
ai-review approve-code "REVIEW_RUN_ID"
```

Approval binds the terminal Codex PASS, the current full patch, the passing
verification snapshot, and the summary. Any worktree change after PASS
invalidates it.

A `knowledge-candidate.md` appears only when one of the six approved learning
triggers holds: three or more repair rounds, a repeated invariant, a scope or
high-risk pause, human arbitration, a reusable architecture/testing lesson, or
all six repair rounds exhausted. Writeback is a separate, never-automatic command
permitted only after that exact approval, and it writes one file under
`<workspace>/second-brain/ai-review/` (the workspace root defaults to
`~/.ai-review/workspace` and can be changed with the `AI_REVIEW_WORKSPACE`
environment variable):

```bash
ai-review writeback-knowledge "REVIEW_RUN_ID"
```

Never commit, push, merge, open a pull request, or publish by API. Claude runs in
safe mode with a Bash-free tool set, Codex runs read-only, and only the
orchestrator executes the approved verification argv.

## Claude Code invocation notes

- Run `init`, `run`, `resume`, and `submit-preflight` with the Bash sandbox
  disabled: verification wraps each command in `/usr/bin/sandbox-exec` and macOS
  rejects a nested sandbox. This loosens only the outer orchestrating call — the
  inner Codex read-only sandbox, Claude safe mode, and per-command Seatbelt stay
  exactly as designed. Never pass a dangerously-bypass option to either model.
- `approve-review`, `approve-risk`, and `approve-code` show a native `osascript`
  dialog and need a GUI session; they fail under a sandboxed or headless call.
  Each dialog is bounded at 120s — tell the human it is waiting before invoking.
- `run` is synchronous and routinely outlasts the Bash tool's 600s ceiling. Each
  Codex review, Claude repair, and verification command is bounded at 1800s and
  six repair rounds multiply them. Always run it in the background and poll
  `status`; a killed call leaves the run `INTERRUPTED`, which `resume` continues.
- Executable identities for `claude` and `codex` are digest-bound at `init`. If
  either binary auto-updates mid-run, identity validation fails and the run must
  be recreated — finish a run in one sitting.
- The inner `claude -p` repair starts from a scrubbed environment and
  authenticates from the Keychain, so `claude` must already be logged in from its
  own terminal; the host session's credentials are not inherited. It is a
  separately billed session.
