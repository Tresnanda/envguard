---
name: envguard
description: Use when auditing environment variables, dotenv files, Supabase Edge Function secrets, CI secrets, or deployment configuration for a project. Helps agents run envguard safely, interpret findings, and avoid exposing secret values.
metadata:
  author: Tresnanda
  project: https://github.com/Tresnanda/envguard
---

# envguard

Use this skill when the user asks about environment variables, missing secrets,
dotenv drift, Supabase Edge Functions, CI secret readiness, deployment config, or
safe `.env.example` updates.

## Core Rule

Use the `envguard` CLI as the source of truth. Do not reimplement its scanner by
manually grepping source files unless the CLI is unavailable.

Never print secret values. It is okay to print key names such as
`SUPABASE_ACCESS_TOKEN`, but do not echo dotenv values, Supabase secrets, API
keys, or tokens.

## Before Running

1. Locate the project root.
2. Check whether `envguard` is installed:

```bash
envguard --help
```

3. If it is not installed, suggest:

```bash
curl -fsSL https://raw.githubusercontent.com/Tresnanda/envguard/main/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/Tresnanda/envguard/main/install.ps1 | iex
```

## Standard Audit Flow

For a normal project audit, run:

```bash
envguard --no-wizard
```

For another path, run:

```bash
envguard --path /path/to/project --no-wizard
```

For a quick chat or CI-step status where details are not needed, use the compact
one-line summary:

```bash
envguard --no-wizard --summary
```

If the output says details are available, rerun with the exact generated
`--details` command shown by envguard, or use:

```bash
envguard --no-wizard --details
```

Use JSON only when automation or precise parsing is needed:

```bash
envguard --no-wizard --json
```

## How To Interpret Findings

- `MISSING`: referenced in code but absent from local config or fetched
  Supabase secrets. Treat as the highest priority finding.
- `UNUSED`: present in config but not referenced. Usually safe to review or
  prune, but confirm with the user before editing shared templates.
- `OPTIONAL`: absent but guarded/defaulted in code. Report as advisory.
- `EXTERNAL`: probably belongs to another runtime or container. Report as
  advisory unless the user asks for strict cleanup.
- `ORPHANED`: Supabase secret exists but is not referenced or documented.
  Treat as a cleanup/security review item.

## Supabase Edge Functions

When the project has `supabase/functions`, `supabase/config.toml`, or a known
Supabase project ref, envguard can include remote Edge Function secrets.

Use:

```bash
envguard supabase <project-ref>
```

The Supabase access token may come from:

- `SUPABASE_ACCESS_TOKEN` in the shell
- the selected dotenv file
- the project `.env`
- secure input through `envguard wizard`

Do not store Supabase access tokens in repository config. If a token is missing,
ask the user to provide one or set `SUPABASE_ACCESS_TOKEN` locally.

## Updating Dotenv Templates

When the user asks to fix environment drift:

1. Run `envguard --no-wizard --details`.
2. Add missing required key names to `.env.example` or the chosen template.
3. Do not add secret values. Use blank values or safe placeholders.
4. Consider removing unused keys only after user confirmation.
5. Rerun envguard to verify.

For unused-key pruning, preview first:

```bash
envguard --fix-dry-run
```

Then use the interactive fixer when the user agrees:

```bash
envguard --fix
```

Do not use `--fix-real-env` unless the user explicitly asks to edit a real
`.env` file; prefer templates such as `.env.example`, `.env.sample`, or
`.env.template`.

## Project Defaults

When the user wants repeatable envguard settings for a repository or CI, persist
team defaults with `envguard init` instead of hand-editing config from scratch:

```bash
envguard init --dotenv config/example.env --exclude "fixtures/**"
```

Confirm before writing `pyproject.toml`. CLI flags still layer on top of the
saved `[tool.envguard]` defaults.

## Updating envguard

If envguard is installed but stale, or the user asks to update it, use the
supported updater instead of reinstalling manually:

```bash
envguard update
```

Do not run updates silently unless the user requested a tool update.

## CI Setup

When the user asks to add envguard to CI, generate a workflow first:

```bash
envguard ci-template
```

For monorepos:

```bash
envguard ci-template apps/web
```

Review the generated YAML before writing files. It should reference secret names
such as `${{ secrets.SUPABASE_ACCESS_TOKEN }}` but must not contain local secret
values.

## Response Style

When reporting results to the user:

- Lead with whether the project is ready, blocked, or has advisory cleanup.
- Mention counts and issue classes.
- Include the exact follow-up command with `--details` when details are needed.
- Do not paste long tables unless the user asks for them.
- Do not expose secret values.
