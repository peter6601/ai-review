---
name: consensus-code
description: Use when a human-reviewed Plan is ready for bounded Claude/Codex implementation and Code consensus in a repository.
---

# Consensus Code

Start only with a Plan run whose status is `AWAITING_HUMAN_PLAN_REVIEW`. Read its compact status; retain `plan_digest` and `base_oid`, not logs or transcripts.

Ask the user: `Do you explicitly approve Plan run RUN_ID with plan digest PLAN_DIGEST for Code work?` Continue only after an explicit yes. Then call the command below and tell the user to approve the local approval dialog showing the exact digest and frozen base OID. The agent must never interact with or auto-click that dialog; Cancel, headless execution, or an unavailable dialog means stop with no approval.

```bash
ai-review approve-plan "RUN_ID"
```

Require an explicit base ref from the user. It must resolve to the signed `base_oid`; do not infer a base from the Plan or current branch.

```bash
ai-review init code \
  --repo "/absolute/path/to/target-repo" \
  --plan-run "RUN_ID" --base "BASE_REF"
ai-review run "CODE_RUN_ID"
ai-review status "CODE_RUN_ID"
```

Report a status summary only: run ID, status, repair round, next action, and summary/questions path. Never paste full logs, prompts, patches, or model transcripts.

For `AWAITING_USER_INPUT`, ask the persisted questions verbatim, write one
answers JSON object, and call `ai-review answer CODE_RUN_ID --answers FILE`.
The immutable answer cycle resumes Code review; never answer on the user's
behalf.

Stop immediately on `AWAITING_HUMAN_CODE_REVIEW` (PASS), `PAUSED`,
`INTERRUPTED`, or the sixth repair round. Present `summary_path` and
`knowledge_candidate_path` when present. `INTERRUPTED` continues with
`resume`; a `PAUSED` run with reason `WORKFLOW_ERROR` is terminal — after
fixing the cause start a fresh `init code`, never claim the old run can
continue. Only the human may run the local
user-presence Code approval:

```bash
ai-review approve-code "CODE_RUN_ID"
```

Knowledge writeback is a separate, never-automatic command and is permitted
only after that exact signed Code candidate was approved:

```bash
ai-review writeback-knowledge "CODE_RUN_ID"
```

Never commit, push, merge, or create a pull request; never publish by API. Claude runs
with safe mode and a Bash-free minimal tool set; only the orchestrator executes
the approved verification argv.

## Claude Code invocation notes

When calling `ai-review` from a Claude Code Bash tool call:

- Run `init` and `run` with the Bash sandbox disabled. Verification wraps each
  command in `/usr/bin/sandbox-exec`, and macOS rejects a nested sandbox, so a
  sandboxed outer call fails at `sandbox_apply`. The inner `codex exec` and
  `claude -p` calls also need network access. This loosens only the outer
  orchestrating Bash call; the helper still applies its own Seatbelt to every
  verification command, and the inner Codex/Claude sandbox and approval flags
  stay exactly as designed — never pass any dangerously-bypass option to them.
- `approve-plan` and `approve-code` show a native `osascript` dialog and need a
  GUI session; they hang or fail under a sandboxed or headless call. The dialog
  is bounded at 120s, so tell the human it is waiting before invoking it. The
  human clicks the dialog — the agent only invokes the command, unsandboxed.
- `run` is synchronous and routinely outlasts the Bash tool's 600s ceiling: each
  Codex review, Claude implement/repair call, and verification command is
  bounded at 1800s, and up to six repair rounds multiply them. Always run it in
  the background and poll `status`; never wait on the foreground call. A killed
  call leaves the run `INTERRUPTED`; continue it with `resume`, do not
  re-`init`.
- Executable identities for `claude` and `codex` are digest-bound at `init`.
  If either binary auto-updates before the run finishes, identity validation
  fails and the run must be recreated — finish a run in one sitting.
- The inner `claude -p` implement/repair calls start from a scrubbed environment
  and authenticate from the Keychain, so `claude` must already be logged in from
  a terminal of its own — the host Claude Code session's credentials are not
  inherited, and without a prior login it exits "Not logged in". They are also
  separate billed sessions; expect extra usage beyond the orchestrating Claude
  Code session.
