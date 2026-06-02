# envguard

Environment-variable audits for Python, JavaScript, shell, and Supabase Edge Function projects.

`envguard` scans a codebase for environment variable references, compares those references with `.env.example`, and can optionally include Supabase Edge Function secrets in the same audit. It helps catch stale config, missing deployment secrets, and undocumented variables before they become production incidents.

## Why Use It

Environment variables tend to drift as projects grow. Old keys stay in example files, new keys are added directly to code, and serverless secrets can linger after features are removed. `envguard` gives you a fast local check that is easy to run before deploys or in CI.

It reports six classes of findings:

| Issue | Meaning |
| --- | --- |
| `UNUSED` | A key exists in `.env.example` but is not referenced in the scanned code. |
| `MISSING` | A required key is referenced in code but is not present in `.env.example` or fetched Supabase secrets. This is blocking by default. |
| `OPTIONAL` | A defaulted/guarded key is absent from config. This is advisory and does not fail CI. |
| `EXTERNAL` | A key appears to belong to another runtime/container, such as an embedded script executed over SSH. This is advisory and does not fail CI. |
| `IGNORED` | A missing key was explicitly ignored by project config or CLI flags. |
| `ORPHANED` | A Supabase secret exists but is not referenced in code or documented in `.env.example`. |

## Installation

Requires Python 3.9 or newer.

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/Tresnanda/envguard/main/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/Tresnanda/envguard/main/install.ps1 | iex
```

For unattended installs, pass `--yes`:

```bash
curl -fsSL https://raw.githubusercontent.com/Tresnanda/envguard/main/install.sh | bash -s -- --yes
```

The installer uses `pipx`, checks Python and optional Supabase tooling, then offers a simple numbered Supabase token setup. If you paste `SUPABASE_ACCESS_TOKEN` during install, it is saved to your user shell environment, not to project config. It may ask whether to star the GitHub repo, defaulting to yes; if it cannot star from the terminal, it prints the repo link instead. After install, run `envguard` in your terminal to start the guided audit. The wizard checks for a newer GitHub version and asks before updating.

Manual install:

```bash
pipx install .
```

For local development:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick Start

Run the audit from the root of a project that contains `.env.example`:

```bash
envguard
```

On an interactive terminal, the bare command opens a guided command builder. It detects dotenv files including `.env.example` and real `.env`, Supabase Edge Function projects, and existing project config, then shows the generated command before running it. To force the immediate current-directory scan, use:

```bash
envguard --no-wizard
```

You can also open the guide explicitly:

```bash
envguard wizard
```

Update to the latest GitHub version at any time:

```bash
envguard update
```

## Agent Skill

`envguard` includes an optional agent skill for Codex and other agents that use
the open Skills CLI. The skill teaches agents to run envguard before changing
environment files, deployment config, Supabase Edge Functions, or CI secrets,
and to report findings without exposing secret values.

Install the skill:

```bash
npx skills add Tresnanda/envguard --skill envguard
```

Install globally for all projects:

```bash
npx skills add Tresnanda/envguard --skill envguard -g
```

List the skill without installing:

```bash
npx skills add Tresnanda/envguard -l
```

Scan another directory:

```bash
envguard /path/to/project
```

Emit machine-readable JSON:

```bash
envguard --json
```

Emit a compact one-line terminal summary for chat or CI step summaries:

```bash
envguard --summary
# envguard: red — 2 missing, 1 unused, 3 optional (exit 1)
```

Emit GitHub Actions annotations in CI logs:

```bash
envguard ci
```

Generate a copy-pasteable GitHub Actions workflow without writing files:

```bash
envguard ci-template
envguard ci-template apps/web
```

The generator reuses `[tool.envguard]` defaults, prefers committed dotenv templates such as `.env.example`, avoids referencing a real `.env` by default, and prints only secret names or GitHub secret placeholders — never dotenv values.

Use a custom dotenv file:

```bash
envguard --dotenv config/example.env
```

Exclude generated or noisy paths:

```bash
envguard --exclude "fixtures/**" --exclude "docs/examples/**"
```

Allow advisory findings without failing a workflow:

```bash
envguard --allow-unused
envguard --allow-missing
```

Mark known project-specific runtime behavior without hiding other real issues:

```bash
envguard --optional CONVO_BOT
envguard --external SUPABASE_SERVICE_ROLE_KEY
envguard --ignore-missing LEGACY_FLAG
```

By default, terminal output summarizes issue counts without printing long reference tables. Show the full tables when you need file and line details:

```bash
envguard --details
```

Interactively prune unused keys from `.env.example`:

```bash
envguard --fix
```

## Supabase Edge Functions

`envguard` can include Supabase Edge Function secrets in the audit. This is useful when some production-only values should not appear in `.env.example` but still need to satisfy references in Edge Function code.

```bash
export SUPABASE_ACCESS_TOKEN="sbp_..."
envguard supabase your-project-ref
```

When Supabase secrets are included:

- Referenced keys are considered available if they exist in either `.env.example` or Supabase.
- Supabase secrets that are not referenced and not documented in `.env.example` are reported as `ORPHANED`.
- `.env.example` keys that are not referenced are still reported as `UNUSED`.

Create an access token at [app.supabase.com/account/tokens](https://app.supabase.com/account/tokens). Your project reference is the ID in a Supabase project URL such as `https://app.supabase.com/project/your-project-ref`.

`envguard` also detects Supabase projects automatically from `[tool.envguard]`, `supabase/config.toml`, `SUPABASE_PROJECT_REF`, or `SUPABASE_PROJECT_ID`. Remote secrets are fetched only when a project ref is known, a Supabase access token is available, and local Edge Functions are present, so ordinary scans stay predictable.

The Supabase access token can come from `SUPABASE_ACCESS_TOKEN` in your shell, the selected dotenv file, the project `.env`, or a secure prompt in `envguard wizard`. Tokens entered in the wizard are used only for that run. They are not printed, written to `pyproject.toml`, or included in the generated command preview.

## CI Onboarding Generator

Run `envguard ci-template [path]` from your repository root to print a ready-to-paste GitHub Actions workflow:

```bash
envguard ci-template > /tmp/envguard-workflow.yml
```

The command is dry-output only: it prints YAML to stdout and does not create `.github/workflows` or edit your project. The generated workflow:

- Installs envguard from the GitHub repo.
- Runs `envguard ci` so findings appear as GitHub annotations.
- Adds the project path when you pass one, for monorepos such as `apps/web`.
- Includes a safe `--dotenv` argument when a committed template like `.env.example`, `.env.sample`, or `.env.template` is detected.
- Reuses `[tool.envguard]` in `pyproject.toml` automatically instead of duplicating config in CI.
- Adds a `SUPABASE_ACCESS_TOKEN: ${{ secrets.SUPABASE_ACCESS_TOKEN }}` placeholder only when Supabase Edge Functions and a project ref are detected.
- Avoids printing dotenv values or local secret values. If only a real `.env` exists locally, the workflow leaves it out and suggests committing a template contract.

Example output:

```yaml
# Generated by `envguard ci-template` (dry output; no files were written).
# Paste this into .github/workflows/envguard.yml and adjust branch/install pinning as needed.
name: Envguard

on:
  pull_request:
  push:
    branches: [main]

jobs:
  envguard:
    name: Env var drift check
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - name: Check out code
        uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.x"
      - name: Install envguard
        run: python -m pip install git+https://github.com/Tresnanda/envguard.git
      - name: 🛡️ Scan env contracts
        run: |
          envguard ci apps/web --dotenv apps/web/.env.example
```

## Dotenv Format

`envguard` accepts standard dotenv-style keys from `.env.example`, `.env.sample`, `.env.template`, or a real `.env` file passed with `--dotenv .env`:

```bash
DATABASE_URL=postgres://localhost
API_KEY=
BARE_SECRET
export SUPABASE_URL=https://example.supabase.co
SUPABASE_ACCESS_TOKEN="sbp_..." # comments are fine
```

Comments and blank lines are ignored. Invalid variable names are skipped. Values are used only to discover sensitive integration tokens such as `SUPABASE_ACCESS_TOKEN`; envguard does not print dotenv values in audit output.

## Project Configuration

You can store team defaults in `pyproject.toml` so developers and CI use the same scan settings without wrapper scripts:

```bash
envguard init --dotenv config/example.env --exclude "fixtures/**"
```

```toml
[tool.envguard]
dotenv = "config/example.env"
exclude = ["fixtures/**", "docs/examples/**"]
supabase_project = "your-project-ref"
optional = ["CLI_DEFAULT_BOT"]
external = ["REMOTE_CONTAINER_SECRET"]
ignore_missing = ["LEGACY_FLAG"]
```

CLI flags still work on top of this configuration. For example, `--exclude` adds more ignore patterns, `--optional` / `--external` / `--ignore-missing` add per-run requirement overrides, and `--supabase-project` overrides the configured project.

## Supported Reference Patterns

`envguard` detects common direct environment-variable access patterns:

| Runtime | Pattern | Example |
| --- | --- | --- |
| Python | `os.getenv("KEY")` | `os.getenv("DATABASE_URL")` |
| Python | `os.environ["KEY"]` | `os.environ["SECRET_KEY"]` |
| Python | `os.environ.get("KEY")` | `os.environ.get("DEBUG")` |
| JavaScript | `process.env.KEY` | `process.env.API_KEY` |
| JavaScript | `process.env["KEY"]` | `process.env["API_KEY"]` |
| Deno | `Deno.env.get("KEY")` | `Deno.env.get("SUPABASE_URL")` |
| Shell | `${KEY}` | `${DATABASE_URL}` |
| Shell | `$KEY` | `$PATH` |
| Generic | `env("KEY")` | `env("LOG_LEVEL")` |
| Windows-style | `%KEY%` | `%USERNAME%` |

Dynamic expressions such as `os.getenv(prefix + "_TOKEN")` are intentionally not inferred.

## CLI Reference

```text
usage: envguard [-h] [--path PATH] [--json] [--summary]
                [--github-annotations] [--fix]
                [--supabase-project SUPABASE_PROJECT]
                [--dotenv DOTENV] [--debug] [--details] [--exclude PATTERN]
                [--optional KEY] [--external KEY] [--ignore-missing KEY]
                [--allow-unused] [--allow-missing] [--no-wizard]
                [path|wizard|ci|ci-template|supabase|init|update] [...]

options:
  path                  Optional project path, e.g. envguard apps/web.
  wizard                Build and optionally run an audit command interactively.
  ci [path]             Shortcut for GitHub Actions annotations.
  ci-template [path]    Print a copy-pasteable GitHub Actions workflow.
  supabase ID [path]    Shortcut for Supabase secret comparison.
  init [path]           Write or update [tool.envguard] in pyproject.toml.
  update                Update envguard from GitHub.
  -h, --help            Show help and exit.
  --path PATH           Project path to scan. Defaults to the current directory.
  --json                Print a JSON report.
  --summary             Print one compact terminal summary line.
  --github-annotations  Print GitHub Actions annotations for CI logs.
  --fix                 Interactively remove unused keys from .env.example.
  --supabase-project ID Fetch Supabase Edge Function secrets for this project.
  --dotenv PATH         Path to dotenv example file. Defaults to <path>/.env.example.
  --exclude PATTERN     Glob pattern to exclude from scanning. Can be repeated.
  --optional KEY        Mark a missing key as optional/defaulted. Can be repeated.
  --external KEY        Mark a missing key as owned by another runtime/container. Can be repeated.
  --ignore-missing KEY  Ignore a missing key entirely. Can be repeated.
  --allow-unused        Do not fail on unused keys or orphaned Supabase secrets.
  --allow-missing       Do not fail on missing referenced variables.
  --details             Show detailed issue tables with file references.
  --no-wizard           Run the default scan instead of the interactive guide.
  --debug               Print detected references and parsed keys.
```

## Summary Output

`--summary` prints exactly one plain-text line and uses the same exit-code rules as the default report:

```text
envguard: red — 2 missing, 1 unused, 3 optional (exit 1)
```

The status is `red` when blocking findings would make envguard exit non-zero, `yellow` when findings are present but allowed/advisory, and `green` when the scan is clean. Counts include nonzero `missing`, `unused`, `optional`, `external`, `ignored`, and Supabase `orphaned` findings.

## JSON Output

```json
{
  "unused": ["OLD_API_KEY"],
  "missing": ["NEW_SECRET"],
  "optional_missing": ["LOCAL_TIMEOUT_MS"],
  "external_missing": ["REMOTE_CONTAINER_SECRET"],
  "ignored_missing": ["LEGACY_FLAG"],
  "supabase_orphans": ["LEGACY_EDGE_SECRET"],
  "references": {
    "DATABASE_URL": [
      {
        "file": "/path/to/app.py",
        "line": 12,
        "pattern": "os.getenv",
        "requirement": "required",
        "reason": ""
      }
    ]
  }
}
```

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | No blocking findings were found. Advisory optional/external/ignored findings may still be present. |
| `1` | The audit found blocking issues or could not complete. |

`--allow-unused` suppresses failure for unused `.env.example` keys and orphaned Supabase secrets. `--allow-missing` suppresses failure for required missing referenced keys. Optional, external, and ignored missing keys are advisory by default and still appear in the selected output format.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```

## License

MIT. See [LICENSE](LICENSE).
