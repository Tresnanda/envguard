#!/usr/bin/env python3
"""envguard — an env var dead-key detector with Supabase Edge Functions support.

Scans a codebase for environment variable references and compares them
against .env.example (or a Supabase project's Edge Function secrets).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    import tomli as tomllib  # type: ignore[no-redef]

try:
    from rich import print as rprint
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm
    from rich.table import Table
except ImportError:
    Console = None  # type: ignore
    Table = None  # type: ignore
    Confirm = None  # type: ignore
    Panel = None  # type: ignore
    rprint = print  # fallback


# ─── Data Structures ───────────────────────────────────────────────────────


@dataclass
class EnvReference:
    """An environment variable reference found in source code."""

    key: str
    file: str
    line: int
    pattern_type: str  # e.g. "os.getenv", "process.env", "$VAR"


@dataclass
class ScanResult:
    """Results of scanning a codebase for env var references."""

    references: Dict[str, List[EnvReference]] = field(default_factory=dict)
    """key -> list of references"""

    unused: List[str] = field(default_factory=list)
    """Keys in .env.example / Supabase but never referenced in code."""

    missing: List[str] = field(default_factory=list)
    """Keys referenced in code but not in .env.example / Supabase."""

    supabase_orphans: List[str] = field(default_factory=list)
    """Keys in Supabase secrets but not referenced in code nor .env.example."""


@dataclass
class EnvguardConfig:
    """Configuration loaded from [tool.envguard] in pyproject.toml."""

    dotenv: Optional[str] = None
    exclude: List[str] = field(default_factory=list)
    supabase_project: Optional[str] = None


# ─── Detection Patterns ────────────────────────────────────────────────────


# Each pattern is a (regex, pattern_name) tuple.
# The regex must have exactly one capture group for the env var name.
PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Python
    (re.compile(r'os\.getenv\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'), "os.getenv"),
    (re.compile(r'os\.environ\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'), "os.environ[]"),
    (re.compile(r'os\.environ\.get\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'), "os.environ.get"),
    (re.compile(r'os\.getenv\b'), None),  # skip, already caught by specific
    # Node/JS
    (re.compile(r'process\.env\.([A-Za-z_][A-Za-z0-9_]*)'), "process.env.KEY"),
    (re.compile(r'process\.env\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'), 'process.env["KEY"]'),
    # Deno/Edge
    (re.compile(r'Deno\.env\.get\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'), "Deno.env.get"),
    # Shell
    (re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}'), "${KEY}"),
    (re.compile(r'(?<!\$)\$([A-Za-z_][A-Za-z0-9_]+)(?![A-Za-z0-9_])'), "$KEY"),
    # Generic
    (
        re.compile(
            r'(?<![A-Za-z0-9_])env\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']',
            re.IGNORECASE,
        ),
        "env()",
    ),
    (re.compile(r'%([A-Za-z_][A-Za-z0-9_]*)%'), "%KEY%"),
]


def detect_references(file_path: Path) -> List[EnvReference]:
    """Scan a single file for environment variable references."""
    refs: List[EnvReference] = []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return refs

    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for pattern, pname in PATTERNS:
            if pname is None:
                continue
            for match in pattern.finditer(line):
                key = match.group(1)
                refs.append(
                    EnvReference(
                        key=key,
                        file=str(file_path),
                        line=lineno,
                        pattern_type=pname,
                    )
                )
    return refs


# ─── File scanning ─────────────────────────────────────────────────────────


def _matches_exclude(path: Path, exclude_patterns: Optional[List[str]]) -> bool:
    """Return True if path matches any user-provided glob pattern."""
    if not exclude_patterns:
        return False

    path_text = path.as_posix()
    for pattern in exclude_patterns:
        normalized = pattern.strip()
        if not normalized:
            continue
        if fnmatch(path_text, normalized) or fnmatch(path.name, normalized):
            return True
    return False


def should_skip(path: Path, exclude_patterns: Optional[List[str]] = None) -> bool:
    """Return True if path should be skipped (binary, hidden dirs, etc.)."""
    if _matches_exclude(path, exclude_patterns):
        return True

    skip_dirs = {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".env",
        ".tox",
        ".eggs",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",  # Rust
        "vendor",  # Go / PHP
        ".bundle",
        "coverage",
        ".ruff_cache",
        ".mypy_cache",
        ".pytest_cache",
    }
    skip_extensions = {
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".dylib",
        ".o",
        ".obj",
        ".exe",
        ".bin",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".ogg",
        ".flac",
    }

    # Skip hidden dirs and common vendored dirs
    for part in path.parts:
        if part in skip_dirs:
            return True

    # Skip hidden files (dotfiles) but NOT .env.example itself
    if path.name.startswith(".") and path.name not in (".env.example",):
        return True

    # Skip files with binary extensions
    if path.suffix.lower() in skip_extensions:
        return True

    return False


def scan_directory(
    path: Path,
    exclude_patterns: Optional[List[str]] = None,
) -> Dict[str, List[EnvReference]]:
    """Recursively scan a directory for env var references."""
    ref_map: Dict[str, List[EnvReference]] = {}

    if not path.exists():
        print(f"Error: path does not exist: {path}", file=sys.stderr)
        sys.exit(1)

    if path.is_file():
        files = [path]
    else:
        files = sorted(path.rglob("*"))

    for file_path in files:
        if not file_path.is_file():
            continue
        try:
            comparable_path = file_path.relative_to(path) if path.is_dir() else file_path
        except ValueError:
            comparable_path = file_path
        if should_skip(comparable_path, exclude_patterns) or should_skip(file_path):
            continue
        if not _is_text_file(file_path):
            continue

        refs = detect_references(file_path)
        for ref in refs:
            ref_map.setdefault(ref.key, []).append(ref)

    return ref_map


def _is_text_file(path: Path) -> bool:
    """Quick heuristic: skip files with null bytes."""
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" not in chunk
    except OSError:
        return False


# ─── Project configuration ─────────────────────────────────────────────────


def load_project_config(scan_path: Path) -> EnvguardConfig:
    """Load envguard defaults from pyproject.toml next to the scanned project."""
    project_root = scan_path if scan_path.is_dir() else scan_path.parent
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return EnvguardConfig()

    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return EnvguardConfig()

    raw_config = data.get("tool", {}).get("envguard", {})
    if not isinstance(raw_config, dict):
        return EnvguardConfig()

    dotenv = raw_config.get("dotenv")
    supabase_project = raw_config.get("supabase_project")
    exclude = raw_config.get("exclude", [])

    return EnvguardConfig(
        dotenv=dotenv if isinstance(dotenv, str) else None,
        exclude=[item for item in exclude if isinstance(item, str)]
        if isinstance(exclude, list)
        else [],
        supabase_project=supabase_project if isinstance(supabase_project, str) else None,
    )


def _resolve_config_path(value: str, scan_path: Path) -> Path:
    """Resolve a configured path relative to the scanned project."""
    path = Path(value)
    if path.is_absolute():
        return path
    project_root = scan_path if scan_path.is_dir() else scan_path.parent
    return project_root / path


def _format_project_config(
    dotenv: Optional[str],
    exclude: Optional[List[str]],
    supabase_project: Optional[str],
) -> str:
    """Format a minimal [tool.envguard] TOML block."""
    lines = ["[tool.envguard]"]
    if dotenv:
        lines.append(f'dotenv = "{dotenv}"')
    if exclude:
        quoted = ", ".join(f'"{pattern}"' for pattern in exclude)
        lines.append(f"exclude = [{quoted}]")
    if supabase_project:
        lines.append(f'supabase_project = "{supabase_project}"')
    lines.append("")
    return "\n".join(lines)


def write_project_config(
    project_path: Path,
    dotenv: Optional[str] = None,
    exclude: Optional[List[str]] = None,
    supabase_project: Optional[str] = None,
) -> Path:
    """Create or replace the [tool.envguard] block in pyproject.toml."""
    pyproject_path = project_path / "pyproject.toml"
    new_block = _format_project_config(dotenv, exclude, supabase_project)

    if not pyproject_path.exists():
        pyproject_path.write_text(new_block, encoding="utf-8")
        return pyproject_path

    existing = pyproject_path.read_text(encoding="utf-8")
    pattern = re.compile(r"(?ms)^\[tool\.envguard\]\n.*?(?=^\[|\Z)")
    if pattern.search(existing):
        updated = pattern.sub(new_block, existing).rstrip() + "\n"
    else:
        updated = existing.rstrip() + "\n\n" + new_block
    pyproject_path.write_text(updated, encoding="utf-8")
    return pyproject_path


# ─── .env.example parsing ──────────────────────────────────────────────────


def parse_dotenv_example(path: Path) -> List[str]:
    """Parse a .env.example file and return list of keys."""
    keys: List[str] = []
    if not path.exists():
        return keys

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Match KEY=value or KEY
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
        if match:
            keys.append(match.group(1))
        else:
            # Maybe just a bare KEY (no value)
            match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]+)\s*$", stripped)
            if match:
                keys.append(match.group(1))

    return keys


# ─── Supabase integration ──────────────────────────────────────────────────


SUPABASE_API_BASE = "https://api.supabase.com"


def fetch_supabase_secrets(project_ref: str, access_token: str) -> List[str]:
    """Fetch all Edge Function secrets from a Supabase project.

    Returns list of secret names.
    """
    import http.client
    import urllib.parse

    url = f"{SUPABASE_API_BASE}/v1/projects/{urllib.parse.quote(project_ref)}/secrets"
    parsed = urllib.parse.urlparse(url)

    conn = http.client.HTTPSConnection(parsed.netloc, timeout=30)
    try:
        conn.request(
            "GET",
            parsed.path,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        if resp.status != 200:
            print(
                f"Error: Supabase API returned {resp.status}: {body}",
                file=sys.stderr,
            )
            sys.exit(1)

        data = json.loads(body)
        # Expected format: [{"name": "SOME_KEY", "value": "..."}, ...]
        return [item["name"] for item in data]
    except (http.client.HTTPException, OSError, json.JSONDecodeError) as e:
        print(f"Error: Failed to fetch Supabase secrets: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def delete_supabase_secrets(project_ref: str, access_token: str, names: List[str]) -> bool:
    """Delete secrets from a Supabase project. Returns True on success."""
    import http.client
    import urllib.parse

    if not names:
        return True

    url = f"{SUPABASE_API_BASE}/v1/projects/{urllib.parse.quote(project_ref)}/secrets"
    parsed = urllib.parse.urlparse(url)

    conn = http.client.HTTPSConnection(parsed.netloc, timeout=30)
    try:
        payload = json.dumps([{"name": n} for n in names])
        conn.request(
            "DELETE",
            parsed.path,
            body=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        if resp.status not in (200, 204):
            print(
                f"Error: Supabase API returned {resp.status}: {body}",
                file=sys.stderr,
            )
            return False
        return True
    except (http.client.HTTPException, OSError, json.JSONDecodeError) as e:
        print(f"Error: Failed to delete Supabase secrets: {e}", file=sys.stderr)
        return False
    finally:
        conn.close()


# ─── Analysis ──────────────────────────────────────────────────────────────


def analyze(
    ref_map: Dict[str, List[EnvReference]],
    dotenv_keys: List[str],
    supabase_keys: Optional[List[str]] = None,
) -> ScanResult:
    """Cross-reference code references against expected keys."""
    result = ScanResult(references=ref_map)

    code_keys = set(ref_map.keys())
    example_keys = set(dotenv_keys)
    supabase_set = set(supabase_keys or [])
    available_keys = example_keys | supabase_set

    # UNUSED: in .env.example but never referenced in code
    result.unused = sorted(example_keys - code_keys)

    # MISSING: referenced in code but not in .env.example or Supabase secrets
    result.missing = sorted(code_keys - available_keys)

    # If Supabase secrets were provided, find orphans
    if supabase_keys is not None:
        code_and_example = code_keys | example_keys
        result.supabase_orphans = sorted(supabase_set - code_and_example)

    return result


# ─── Output Formatting ─────────────────────────────────────────────────────


def _rich_output(result: ScanResult, dotenv_path: Optional[Path], supabase_ref: Optional[str]):
    """Pretty terminal output using rich."""
    console = Console()

    # Summary header
    total_refs = sum(len(v) for v in result.references.values())
    unique_keys = len(result.references)

    console.print()
    summary = "[bold cyan]envguard[/] — Environment Variable Audit\n"
    summary += f"  • {total_refs} references found ({unique_keys} unique keys)"
    if dotenv_path:
        summary += f"\n  • .env.example: [green]{dotenv_path}[/]"
    if supabase_ref:
        summary += f"\n  • Supabase project: [green]{supabase_ref}[/]"
    console.print(Panel(summary, border_style="cyan"))
    console.print()

    # UNUSED keys table
    if result.unused:
        table = Table(
            title="[yellow]UNUSED[/] — Keys in config but never referenced in code",
            border_style="yellow",
        )
        table.add_column("Key", style="yellow", no_wrap=True)
        table.add_column("Source", style="dim")
        for key in result.unused:
            source = (
                "supabase"
                if supabase_ref and key in result.supabase_orphans
                else ".env.example"
            )
            table.add_row(key, source)
        console.print(table)
        console.print()
    else:
        console.print("[green]✓[/] No unused keys found in configuration.")
        console.print()

    # MISSING keys table
    if result.missing:
        table = Table(
            title="[red]MISSING[/] — Keys referenced in code but not in config",
            border_style="red",
        )
        table.add_column("Key", style="red", no_wrap=True)
        table.add_column("References", style="dim")
        for key in result.missing:
            refs = result.references.get(key, [])
            locs = "; ".join(f"{r.file}:{r.line}" for r in refs[:3])
            if len(refs) > 3:
                locs += f" …and {len(refs)-3} more"
            table.add_row(key, locs)
        console.print(table)
        console.print()
    else:
        console.print("[green]✓[/] No missing keys detected.")
        console.print()

    # Supabase orphans
    if result.supabase_orphans:
        table = Table(
            title="[magenta]ORPHANED[/] — Supabase secrets with no code references",
            border_style="magenta",
        )
        table.add_column("Secret", style="magenta", no_wrap=True)
        for key in result.supabase_orphans:
            table.add_row(key)
        console.print(table)
        console.print()

    # Overall status
    if result.unused or result.missing or result.supabase_orphans:
        console.print(
            "[bold red]✗[/] Issues found. Review the tables above."
        )
    else:
        console.print("[bold green]✓[/] All environment variables are accounted for!")

    console.print()


def _json_output(result: ScanResult):
    """JSON machine-readable output."""
    output = {
        "unused": result.unused,
        "missing": result.missing,
        "supabase_orphans": result.supabase_orphans,
        "references": {
            key: [
                {"file": r.file, "line": r.line, "pattern": r.pattern_type}
                for r in refs
            ]
            for key, refs in result.references.items()
        },
    }
    print(json.dumps(output, indent=2))


def _escape_annotation_message(value: str) -> str:
    """Escape a value for GitHub Actions workflow command output."""
    return (
        html.escape(value, quote=False)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def build_github_annotations(result: ScanResult) -> List[str]:
    """Build GitHub Actions annotations for missing, unused, and orphaned keys."""
    annotations: List[str] = []

    for key in result.missing:
        refs = result.references.get(key, [])
        if refs:
            for ref in refs:
                annotations.append(
                    (
                        f"::error file={ref.file},line={ref.line}::"
                        f"Missing environment variable {_escape_annotation_message(key)}"
                    )
                )
        else:
            annotations.append(
                f"::error::Missing environment variable {_escape_annotation_message(key)}"
            )

    for key in result.unused:
        annotations.append(
            f"::warning::Unused environment variable {_escape_annotation_message(key)}"
        )

    for key in result.supabase_orphans:
        annotations.append(
            f"::warning::Orphaned Supabase secret {_escape_annotation_message(key)}"
        )

    return annotations


def _github_annotations_output(result: ScanResult) -> None:
    """Print GitHub Actions annotations."""
    for annotation in build_github_annotations(result):
        print(annotation)


def has_blocking_issues(
    result: ScanResult,
    allow_unused: bool = False,
    allow_missing: bool = False,
) -> bool:
    """Return whether the scan result should fail the process."""
    missing_is_blocking = bool(result.missing) and not allow_missing
    unused_is_blocking = bool(result.unused or result.supabase_orphans) and not allow_unused
    return missing_is_blocking or unused_is_blocking


# ─── Interactive Fix ───────────────────────────────────────────────────────


def interactive_fix(result: ScanResult, dotenv_path: Path):
    """Interactively prune unused entries from .env.example."""
    if not result.unused:
        print("No unused keys to prune from .env.example.")
        return

    if Confirm is None:
        print("rich is required for --fix mode. Install it: pip install rich")
        return

    console = Console()

    # Filter .env.example lines to keep
    unused_set = set(result.unused)
    try:
        lines = dotenv_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as e:
        print(f"Error reading {dotenv_path}: {e}", file=sys.stderr)
        return

    console.print(
        Panel(
            f"[yellow]{len(result.unused)} unused key(s)[/] found in [cyan]{dotenv_path}[/]",
            border_style="yellow",
        )
    )
    console.print()

    keep_lines: List[str] = []
    removed_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            keep_lines.append(line)
            continue

        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", stripped)
        if match and match.group(1) in unused_set:
            key = match.group(1)
            if Confirm and Confirm.ask(
                f"Remove unused key [yellow]{key}[/]?",
                default=False,
            ):
                removed_lines.append(line.rstrip())
                continue
            else:
                keep_lines.append(line)
        else:
            keep_lines.append(line)

    if removed_lines:
        dotenv_path.write_text("".join(keep_lines), encoding="utf-8")
        console.print(
            f"\n[green]✓[/] Removed {len(removed_lines)} unused key(s) "
            f"from [cyan]{dotenv_path}[/]"
        )
    else:
        console.print("[dim]No changes made.[/]")


# ─── CLI Entry Point ───────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="envguard — environment variable dead-key detector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  envguard                          Scan current directory\n"
            "  envguard apps/web                  Scan a specific project\n"
            "  envguard ci                        GitHub Actions annotations\n"
            "  envguard supabase xyz              Compare with Supabase secrets\n"
            "  envguard init                      Write [tool.envguard] defaults\n"
            "  envguard --json                    Machine-readable output\n"
            "  envguard --fix                     Interactive fix mode\n"
        ),
    )
    parser.add_argument(
        "tokens",
        nargs="*",
        help="Optional project path or preset: ci, supabase <project-ref>, init",
    )
    parser.add_argument(
        "--path",
        type=str,
        default=".",
        help="Path to the project to scan (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format (machine-readable)",
    )
    parser.add_argument(
        "--github-annotations",
        action="store_true",
        help="Output GitHub Actions annotations for CI logs",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Interactively prune unused entries from .env.example",
    )
    parser.add_argument(
        "--supabase-project",
        type=str,
        default=None,
        help="Supabase project reference ID to fetch Edge Function secrets",
    )
    parser.add_argument(
        "--dotenv",
        type=str,
        default=None,
        help="Path to .env.example file (default: <path>/.env.example)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug info (detected references, etc.)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "Glob pattern to exclude from scanning. Can be repeated "
            "(example: --exclude 'fixtures/**')."
        ),
    )
    parser.add_argument(
        "--allow-unused",
        action="store_true",
        help="Do not fail when unused .env.example keys or Supabase orphan secrets are found",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Do not fail when referenced variables are missing from configuration",
    )
    return parser


def parse_cli_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments and friendly command presets."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    tokens = list(args.tokens)
    args.command = None

    if tokens:
        first = tokens[0]
        if first == "ci":
            args.github_annotations = True
            if len(tokens) > 2:
                parser.error("ci accepts at most one project path")
            if len(tokens) == 2:
                args.path = tokens[1]
        elif first == "supabase":
            if len(tokens) < 2:
                parser.error("supabase requires a project reference")
            if len(tokens) > 3:
                parser.error("supabase accepts: supabase <project-ref> [path]")
            args.supabase_project = tokens[1]
            if len(tokens) == 3:
                args.path = tokens[2]
        elif first == "init":
            args.command = "init"
            if len(tokens) > 2:
                parser.error("init accepts at most one project path")
            if len(tokens) == 2:
                args.path = tokens[1]
        else:
            if len(tokens) > 1:
                parser.error("expected one project path")
            args.path = first

    del args.tokens
    return args


def main(argv: Optional[List[str]] = None):
    args = parse_cli_args(argv)

    # Resolve paths
    scan_path = Path(args.path).resolve()
    if not scan_path.exists():
        print(f"Error: path does not exist: {scan_path}", file=sys.stderr)
        sys.exit(1)

    if args.command == "init":
        config_path = write_project_config(
            scan_path,
            dotenv=args.dotenv,
            exclude=args.exclude,
            supabase_project=args.supabase_project,
        )
        print(f"Wrote envguard config to {config_path}")
        return

    config = load_project_config(scan_path)

    # Determine .env.example path
    if args.dotenv:
        dotenv_path = Path(args.dotenv).resolve()
    elif config.dotenv:
        dotenv_path = _resolve_config_path(config.dotenv, scan_path).resolve()
    else:
        dotenv_path = scan_path / ".env.example"

    exclude_patterns = [*config.exclude, *args.exclude]
    supabase_project = args.supabase_project or config.supabase_project

    # ── Scan codebase ──────────────────────────────────────────────────────
    ref_map = scan_directory(scan_path, exclude_patterns=exclude_patterns)

    if args.debug:
        print(f"[debug] Scanned: {scan_path}")
        if config != EnvguardConfig():
            print(f"[debug] Loaded config: {config}")
        print(f"[debug] Found {len(ref_map)} unique env var references:")
        for key, refs in sorted(ref_map.items()):
            print(f"  {key}: {len(refs)} reference(s)")
            for r in refs:
                print(f"    {r.file}:{r.line} ({r.pattern_type})")
        print()

    # ── Parse .env.example ─────────────────────────────────────────────────
    dotenv_keys = parse_dotenv_example(dotenv_path)
    if args.debug:
        print(f"[debug] .env.example keys ({len(dotenv_keys)}): {dotenv_keys}")
        print()

    # ── Fetch Supabase secrets ─────────────────────────────────────────────
    supabase_keys: Optional[List[str]] = None
    if supabase_project:
        access_token = os.environ.get("SUPABASE_ACCESS_TOKEN")
        if not access_token:
            print(
                "Error: SUPABASE_ACCESS_TOKEN environment variable is required "
                "when using --supabase-project",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.debug:
            print(f"[debug] Fetching secrets for Supabase project: {supabase_project}")
        supabase_keys = fetch_supabase_secrets(supabase_project, access_token)
        if args.debug:
            print(f"[debug] Supabase secrets ({len(supabase_keys)}): {supabase_keys}")
            print()

    # ── Analyze ────────────────────────────────────────────────────────────
    result = analyze(ref_map, dotenv_keys, supabase_keys)

    # ── Output ─────────────────────────────────────────────────────────────
    if args.github_annotations:
        _github_annotations_output(result)
    elif args.json:
        _json_output(result)
    else:
        if Console is None:
            print("For prettier output, install rich: pip install rich", file=sys.stderr)
            _json_output(result)
        else:
            _rich_output(
                result,
                dotenv_path if dotenv_path.exists() else None,
                supabase_project,
            )

    # ── Interactive fix ────────────────────────────────────────────────────
    if args.fix and dotenv_path.exists():
        interactive_fix(result, dotenv_path)

    # Exit code: non-zero if any issues found
    if has_blocking_issues(
        result,
        allow_unused=args.allow_unused,
        allow_missing=args.allow_missing,
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
