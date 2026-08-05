# Direct review repair

Fix the supplied normalized findings and any failing verification evidence in the local worktree. Return only JSON conforming to the provided schema.

Scope:

- Fix only the supplied findings and the supplied failing verification evidence. Report one resolution per finding ID, using exactly the supplied IDs.
- You may modify any file inside the supplied repository when that is required for a correct, bounded fix, including files absent from the reviewed patch.
- Never modify `.git`, anything outside the supplied repository, or review run storage.
- Preserve behavior the review brief does not cover. Do not refactor, rename, reformat, or restructure unrelated code.
- Keep public contracts, persisted formats, and network formats unchanged unless a supplied finding requires the change.

Disclosure — this is enforced, not advisory:

- `risk_flags` is required. Return `[]` only when your changes touch none of these categories; otherwise list every category your changes touch, using exactly these names: `dependencies`, `migration`, `entitlements_signing`, `ci_cd`, `public_api`, `persistent_format`, `network_format`.
- Include a category whenever you are unsure whether it applies. A declared flag pauses the run for one human decision; an omitted flag lets an unreviewed high-risk change proceed.
- `risk_flags` supplements deterministic path and diff detection. It can only add a pause, never suppress one, so under-reporting is the only way to cause harm here.
- Also name the affected category in the relevant resolution evidence, so the human reading the summary sees why the run paused.
- Use `disputed` with concrete evidence rather than making a change you cannot justify. Use `needs_user_input` when a finding requires a human product decision.

Prohibited:

- Do not commit, push, merge, open a pull request, publish, or call any network or cloud service.
- Do not invoke another agent, sub-agent, or model.
- Do not run verification yourself; the workflow re-runs the signed verification commands and asks Codex to review your changes.
