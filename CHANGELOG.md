# Changelog

All notable changes to envguard are documented here.

## 1.0.0 - 2026-06-01

- Added environment-variable reference scanning for Python, JavaScript, Deno, shell, generic `env()`, and Windows-style patterns.
- Added `.env.example` comparison with unused and missing key detection.
- Added Supabase Edge Function secret comparison and orphan detection.
- Added repeatable scan exclusions with `--exclude`.
- Added project defaults through `[tool.envguard]` in `pyproject.toml`.
- Added GitHub Actions annotation output for CI with `--github-annotations`.
- Added `--allow-unused` and `--allow-missing` to tune CI strictness.
- Added JSON output, interactive `.env.example` pruning, tests, linting, packaging metadata, and CI.
