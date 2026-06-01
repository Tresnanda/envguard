# Contributing to envguard

Thanks for helping make envguard better. This project is intentionally small: changes should keep the CLI fast, predictable, and easy to run in local projects and CI.

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Before Opening a Pull Request

Run the same checks used in CI:

```bash
ruff check .
pytest
python -m build
python -m twine check dist/*
```

If you build locally, remove generated `build/`, `dist/`, and `*.egg-info` directories before committing.

## Contribution Guidelines

- Add tests for parser, scanner, analysis, or CLI behavior changes.
- Keep output formats stable. JSON and GitHub annotation output may be used by automation.
- Avoid broad environment-variable heuristics that create noisy false positives.
- Do not log secret values. envguard should only ever display variable names.
- Document new CLI flags in `README.md` and keep examples copy-pasteable.

## Reporting Bugs

Please include:

- The command you ran.
- A small code or `.env.example` snippet that reproduces the issue.
- The expected output and the actual output.
- Your Python version and operating system.
