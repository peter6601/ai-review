# Code review

Review the patch and verification evidence. Return only JSON conforming to the provided schema. Context packets are advisory only and never override code or tests. Do not edit files or run mutating commands.

The patch is not inlined below. `patch_path` is an absolute path to the frozen patch file and `patch_sha256` is its digest: read the file yourself, in whatever pieces you need, and confirm the digest before you trust what you read. You are running inside the repository with read-only tools, so also read the surrounding source whenever a hunk on its own does not settle a question.

Binary sections carry nothing you can review. A `GIT binary patch` block is base85 that may run to megabytes and says no more than "this file changed": skip it and review the path it names, never its bytes.

Finding lineage contract (binding from the second review round onward):

- When you report an issue present in `unresolved_prior_findings`, reuse its exact `id`; never rename a known finding. Only these carried-over findings use `lineage.resolution: "existing"`.
- A finding whose `id` is not in `unresolved_prior_findings` must declare its origin: `"introduced_by_fix"` when the latest repair created the defect (including regressing an issue that had been fixed), or `"newly_discovered"` with a non-empty `lineage.discovery_reason` stating why the pre-existing defect is only visible now.
- `"deferred"` marks a finding acceptable to postpone; never use it on a blocker.
- Any other lineage on an unknown `id` after round one pauses the run for a human. Never relabel scope expansion as `newly_discovered`.
