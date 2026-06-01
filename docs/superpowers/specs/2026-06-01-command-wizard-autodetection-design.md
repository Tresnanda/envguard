# Command Wizard and Auto-Detection Design

## Goal

Make `envguard`, `git-standup`, and `ss2json` feel safe for first-time users while preserving scriptable CLI behavior. Running the bare command should guide the user interactively. Existing explicit flags, presets, and CI-oriented commands should stay non-interactive.

## Shared CLI Behavior

- Add a `wizard` command to each project.
- Treat the bare command as the guided path when stdin/stdout is interactive.
- Keep explicit commands and flags deterministic for automation.
- Show the generated command before running and ask for confirmation.
- Provide a clear non-interactive escape hatch in help text.

## Envguard

`envguard` should detect project defaults before prompting:

- dotenv template preference: configured `[tool.envguard]` value, `.env.example`, `.env.sample`, `.env.template`, then `.env`.
- Supabase project reference: configured `[tool.envguard]`, `supabase/config.toml`, then `SUPABASE_PROJECT_REF` or `SUPABASE_PROJECT_ID`.
- Supabase Edge Functions presence: `supabase/functions`.
- CI mode: `envguard ci` remains the explicit non-interactive annotation command.

Supabase remote secrets should only be fetched when a project ref is known and `SUPABASE_ACCESS_TOKEN` is available. Missing tokens should be reported as guidance during detection, not as a hard failure unless the user explicitly requested Supabase.

## Git Standup

`git-standup` should guide users through:

- repository path,
- report preset: me, week, branch, or custom,
- output format: text, Markdown, JSON, or AI,
- optional output file,
- base branch when branch mode is selected.

## Screenshot To JSON

`ss2json` should guide users through:

- acquisition mode: screenshot, file, clipboard, or stdin,
- extraction mode: general, table, code, or form,
- provider/model defaults detected from API key environment,
- output destination: stdout, file, clipboard copy, or both.

## Testing

Add focused tests for pure planning and detection helpers first, then wire the CLI behavior to those helpers. Wizard prompt loops should be thin wrappers around testable plan builders.
