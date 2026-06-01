# Changelog

All notable changes to envguard are documented here.

## Unreleased

- Added positional project paths, so `envguard apps/web` works as a shortcut for `--path`.
- Added `envguard ci` as a shortcut for GitHub Actions annotation output.
- Added `envguard supabase <project-ref>` as a shortcut for Supabase secret comparison.
- Added `envguard init` to create or update `[tool.envguard]` defaults in `pyproject.toml`.

## 1.0.0 - 2026-06-01

- Added environment-variable reference scanning for Python, JavaScript, Deno, shell, generic `env()`, and Windows-style patterns.
- Added `.env.example` comparison with unused and missing key detection.
- Added Supabase Edge Function secret comparison and orphan detection.
- Added repeatable scan exclusions with `--exclude`.
- Added project defaults through `[tool.envguard]` in `pyproject.toml`.
- Added GitHub Actions annotation output for CI with `--github-annotations`.
- Added `--allow-unused` and `--allow-missing` to tune CI strictness.
- Added JSON output, interactive `.env.example` pruning, tests, linting, packaging metadata, and CI.
