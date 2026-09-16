# Plan review

Review the approved plan against repository evidence. Return only JSON conforming to the provided schema. Context packets are advisory only and never override code or tests. Do not edit files or run mutating commands.

Finding lineage contract (binding from the second review round onward):

- When you report an issue present in `unresolved_prior_findings`, reuse its exact `id`; never rename a known finding. Only these carried-over findings use `lineage.resolution: "existing"`.
- A finding whose `id` is not in `unresolved_prior_findings` must declare its origin: `"introduced_by_fix"` when the latest repair created the defect (including regressing an issue that had been fixed), or `"newly_discovered"` with a non-empty `lineage.discovery_reason` stating why the pre-existing defect is only visible now.
- `"deferred"` marks a finding acceptable to postpone; never use it on a blocker.
- Any other lineage on an unknown `id` after round one pauses the run for a human. Never relabel scope expansion as `newly_discovered`.
