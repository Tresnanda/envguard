# envguard

Environment-variable audits for Python, JavaScript, shell, and Supabase Edge Function projects.

`envguard` scans a codebase for environment variable references, compares those references with `.env.example`, and can optionally include Supabase Edge Function secrets in the same audit. It helps catch stale config, missing deployment secrets, and undocumented variables before they become production incidents.

## Why Use It

Environment variables tend to drift as projects grow. Old keys stay in example files, new keys are added directly to code, and serverless secrets can linger after features are removed. `envguard` gives you a fast local check that is easy to run before deploys or in CI.

It reports three classes of issues:

| Issue | Meaning |
| --- | --- |
| `UNUSED` | A key exists in `.env.example` but is not referenced in the scanned code. |
| `MISSING` | A key is referenced in code but is not present in `.env.example` or fetched Supabase secrets. |
| `ORPHANED` | A Supabase secret exists but is not referenced in code or documented in `.env.example`. |

## Installation

Requires Python 3.9 or newer.

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

Scan another directory:

```bash
envguard /path/to/project
```

Emit machine-readable JSON:

```bash
envguard --json
```

Emit GitHub Actions annotations in CI logs:

```bash
envguard ci
```

Use a custom dotenv example file:

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

## `.env.example` Format

`envguard` accepts standard dotenv-style keys:

```bash
DATABASE_URL=postgres://localhost
API_KEY=
BARE_SECRET
export SUPABASE_URL=https://example.supabase.co
```

Comments and blank lines are ignored. Invalid variable names are skipped.

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
```

CLI flags still work on top of this configuration. For example, `--exclude` adds more ignore patterns, and `--supabase-project` overrides the configured project.

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
usage: envguard [-h] [--path PATH] [--json] [--github-annotations] [--fix]
                [--supabase-project SUPABASE_PROJECT]
                [--dotenv DOTENV] [--debug] [--exclude PATTERN]
                [--allow-unused] [--allow-missing]
                [path|ci|supabase|init] [...]

options:
  path                  Optional project path, e.g. envguard apps/web.
  ci [path]             Shortcut for GitHub Actions annotations.
  supabase ID [path]    Shortcut for Supabase secret comparison.
  init [path]           Write or update [tool.envguard] in pyproject.toml.
  -h, --help            Show help and exit.
  --path PATH           Project path to scan. Defaults to the current directory.
  --json                Print a JSON report.
  --github-annotations  Print GitHub Actions annotations for CI logs.
  --fix                 Interactively remove unused keys from .env.example.
  --supabase-project ID Fetch Supabase Edge Function secrets for this project.
  --dotenv PATH         Path to dotenv example file. Defaults to <path>/.env.example.
  --exclude PATTERN     Glob pattern to exclude from scanning. Can be repeated.
  --allow-unused        Do not fail on unused keys or orphaned Supabase secrets.
  --allow-missing       Do not fail on missing referenced variables.
  --debug               Print detected references and parsed keys.
```

## JSON Output

```json
{
  "unused": ["OLD_API_KEY"],
  "missing": ["NEW_SECRET"],
  "supabase_orphans": ["LEGACY_EDGE_SECRET"],
  "references": {
    "DATABASE_URL": [
      {
        "file": "/path/to/app.py",
        "line": 12,
        "pattern": "os.getenv"
      }
    ]
  }
}
```

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | No unused, missing, or orphaned keys were found. |
| `1` | The audit found issues or could not complete. |

`--allow-unused` suppresses failure for unused `.env.example` keys and orphaned Supabase secrets. `--allow-missing` suppresses failure for missing referenced keys. The findings still appear in the selected output format.

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
