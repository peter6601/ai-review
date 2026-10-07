# Pending queue and executable identity

Read this when the human asks what is waiting for them, or when a command
refuses a run because `claude`, `codex`, or a verification executable changed.

## `ai-review queue`

Read-only, and outside the command order. It lists every Code or Review run,
across all repositories, parked at `AWAITING_HUMAN_CODE_REVIEW` — grouped by
repository, with worktree, branch, waiting time, and summary path.
`--format json` gives `pending_count` and `approvable_count`. A run marked
`cannot approve` can no longer be approved — most often because its worktree
was deleted. Use it to review asynchronously: park runs at the
gate, then work through the queue when you have time.

## Which commands survive an executable update

Executable identities are digest-bound at `init`. `approve-code`, `status`,
`queue`, and `writeback-knowledge` execute nothing but git, so they still load a
run whose bound executable has updated since: the review and verification
evidence was produced while the identity matched. Every command that executes
something — `run`, `resume`, `answer`, `expand-context` — refuses that run, and
it must be recreated. A run already parked at `AWAITING_HUMAN_CODE_REVIEW` is
unaffected: the human can approve it later.
