#!/usr/bin/env python3
"""envguard — an env var dead-key detector with Supabase Edge Functions support.

Scans a codebase for environment variable references and compares them
against .env.example (or a Supabase project's Edge Function secrets).
"""

from __future__ import annotations

import argparse
import getpass
import html
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9/3.10
    import tomli as tomllib  # type: ignore[no-redef]

try:
    from rich import print as rprint
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
except ImportError:
    Console = None  # type: ignore
    Table = None  # type: ignore
    Confirm = None  # type: ignore
    Prompt = None  # type: ignore
    Panel = None  # type: ignore
    rprint = print  # fallback


APP_NAME = "envguard"
DIST_NAME = "envguard"
REPO_URL = "https://github.com/Tresnanda/envguard.git"
REPO_SPEC = f"git+{REPO_URL}"
MIN_PYTHON = (3, 9)


# ─── Data Structures ───────────────────────────────────────────────────────


@dataclass
class EnvReference:
    """An environment variable reference found in source code."""

    key: str
    file: str
    line: int
    pattern_type: str  # e.g. "os.getenv", "process.env", "$VAR"
    requirement: str = "required"  # required, optional, or external
    reason: str = ""


@dataclass
class ScanResult:
    """Results of scanning a codebase for env var references."""

    references: Dict[str, List[EnvReference]] = field(default_factory=dict)
    """key -> list of references"""

    unused: List[str] = field(default_factory=list)
    """Keys in .env.example / Supabase but never referenced in code."""

    missing: List[str] = field(default_factory=list)
    """Required keys referenced in code but not in .env.example / Supabase."""

    optional_missing: List[str] = field(default_factory=list)
    """Optional/defaulted keys referenced in code but not in config."""

    external_missing: List[str] = field(default_factory=list)
    """Keys used in an external/runtime context, not required in local config."""

    ignored_missing: List[str] = field(default_factory=list)
    """Missing keys intentionally ignored by project configuration."""

    supabase_orphans: List[str] = field(default_factory=list)
    """Keys in Supabase secrets but not referenced in code nor .env.example."""


@dataclass
class SecretsMatrixRow:
    """Availability/readiness for one referenced environment key."""

    key: str
    requirement: str
    status: str
    dotenv: bool
    environment: bool
    supabase: Optional[bool]
    references: int


@dataclass
class SecretsMatrix:
    """Doctor/matrix report that never includes secret values."""

    rows: List[SecretsMatrixRow]
    dotenv_path: Optional[Path] = None
    supabase_project: Optional[str] = None
    supabase_checked: bool = False
    unused_dotenv: List[str] = field(default_factory=list)
    supabase_orphans: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class EnvguardConfig:
    """Configuration loaded from [tool.envguard] in pyproject.toml."""

    dotenv: Optional[str] = None
    exclude: List[str] = field(default_factory=list)
    supabase_project: Optional[str] = None
    baseline: Optional[str] = None
    optional: List[str] = field(default_factory=list)
    external: List[str] = field(default_factory=list)
    ignore_missing: List[str] = field(default_factory=list)


@dataclass
class CITemplatePlan:
    """Detected inputs for a copy-pasteable GitHub Actions workflow."""

    project_arg: str
    dotenv_arg: Optional[str] = None
    uses_project_config: bool = False
    has_safe_dotenv: bool = False
    has_real_dotenv_only: bool = False
    has_supabase_edge_functions: bool = False
    has_supabase_project_config: bool = False


@dataclass
class UpdateCheck:
    """Result of a best-effort GitHub update check."""

    available: bool
    current_commit: Optional[str] = None
    latest_commit: Optional[str] = None


# ─── Detection Patterns ────────────────────────────────────────────────────


# Each pattern is a (regex, pattern_name, language_scopes) tuple.
# The regex must have exactly one capture group for the env var name.
#
# IMPORTANT: Do not run shell-style $KEY/${KEY}/%KEY% regexes over every file.
# Modern codebases use the same syntax for JS template literals, TS properties,
# Python strftime, Flutter/iOS build files, and generated bundles. Scopes keep
# each pattern constrained to file types where it means "environment variable".
PATTERNS: List[Tuple[re.Pattern, str, frozenset[str]]] = [
    # Python
    (
        re.compile(r'os\.getenv\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "os.getenv",
        frozenset({"python"}),
    ),
    (
        re.compile(r'os\.environ\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "os.environ[]",
        frozenset({"python"}),
    ),
    (
        re.compile(r'os\.environ\.get\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "os.environ.get",
        frozenset({"python"}),
    ),
    # Node/JS/TS/Deno
    (
        re.compile(r'process\.env\.([A-Za-z_][A-Za-z0-9_]*)'),
        "process.env.KEY",
        frozenset({"js"}),
    ),
    (
        re.compile(r'process\.env\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        'process.env["KEY"]',
        frozenset({"js"}),
    ),
    (
        re.compile(r'import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)'),
        "import.meta.env.KEY",
        frozenset({"js"}),
    ),
    (
        re.compile(r'import\.meta\.env\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        'import.meta.env["KEY"]',
        frozenset({"js"}),
    ),
    (
        re.compile(r'Deno\.env\.get\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "Deno.env.get",
        frozenset({"js"}),
    ),
    # Ruby
    (
        re.compile(r'ENV\.fetch\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "ENV.fetch",
        frozenset({"ruby"}),
    ),
    (
        re.compile(r'ENV\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "ENV[]",
        frozenset({"ruby"}),
    ),
    # Go
    (
        re.compile(r'os\.Getenv\s*\(\s*["`]([A-Za-z_][A-Za-z0-9_]*)["`]'),
        "os.Getenv",
        frozenset({"go"}),
    ),
    (
        re.compile(r'os\.LookupEnv\s*\(\s*["`]([A-Za-z_][A-Za-z0-9_]*)["`]'),
        "os.LookupEnv",
        frozenset({"go"}),
    ),
    # Rust
    (
        re.compile(r'(?:std::)?env::var(?:_os)?\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "std::env::var",
        frozenset({"rust"}),
    ),
    # PHP / Laravel
    (
        re.compile(r'getenv\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "getenv",
        frozenset({"php"}),
    ),
    (
        re.compile(r'\$_(?:ENV|SERVER)\s*\[\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "$_ENV[]",
        frozenset({"php"}),
    ),
    (
        re.compile(r'(?<![A-Za-z0-9_])env\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "env()",
        frozenset({"php"}),
    ),
    # JVM
    (
        re.compile(r'System\.getenv\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "System.getenv",
        frozenset({"jvm"}),
    ),
    (
        re.compile(r'System\.getenv\s*\(\s*\)\.get\s*\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'),
        "System.getenv().get",
        frozenset({"jvm"}),
    ),
    # GitHub Actions expression syntax. Keep separate from shell ${KEY} so
    # ${{ secrets.KEY }} does not become a bogus shell reference.
    (
        re.compile(r'\$\{\{\s*secrets\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}'),
        "github-actions secrets.KEY",
        frozenset({"github_actions"}),
    ),
    (
        re.compile(r'\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}'),
        "github-actions env.KEY",
        frozenset({"github_actions"}),
    ),
    # Shell / Docker Compose. Supports ${KEY}, ${KEY:-default}, ${KEY?err}, etc.
    (
        re.compile(r'\$\{(?!\{)\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*(?::?[-=?+])[^}]*)?\}'),
        "${KEY}",
        frozenset({"shell"}),
    ),
    (
        re.compile(r'(?<![\w$])\$([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])'),
        "$KEY",
        frozenset({"shell"}),
    ),
    # Windows batch syntax. Never apply to Python/JS/etc.; it conflicts with strftime.
    (
        re.compile(r'%([A-Za-z_][A-Za-z0-9_]*)%'),
        "%KEY%",
        frozenset({"windows_batch"}),
    ),
]

JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
PYTHON_SUFFIXES = {".py", ".pyw"}
SHELL_SUFFIXES = {".sh", ".bash", ".zsh", ".ksh", ".envrc"}
RUBY_SUFFIXES = {".rb", ".rake"}
GO_SUFFIXES = {".go"}
RUST_SUFFIXES = {".rs"}
PHP_SUFFIXES = {".php"}
JVM_SUFFIXES = {".java", ".kt", ".kts", ".scala", ".groovy"}
WINDOWS_BATCH_SUFFIXES = {".bat", ".cmd"}


def _pattern_scopes_for_path(file_path: Path) -> set[str]:
    """Return scanner scopes that are semantically valid for this file."""
    suffix = file_path.suffix.lower()
    name = file_path.name.lower()
    scopes: set[str] = set()

    if suffix in PYTHON_SUFFIXES:
        scopes.add("python")
    if suffix in JS_SUFFIXES:
        scopes.add("js")
    if suffix in RUBY_SUFFIXES or name in {"gemfile", "rakefile"}:
        scopes.add("ruby")
    if suffix in GO_SUFFIXES:
        scopes.add("go")
    if suffix in RUST_SUFFIXES:
        scopes.add("rust")
    if suffix in PHP_SUFFIXES:
        scopes.add("php")
    if suffix in JVM_SUFFIXES:
        scopes.add("jvm")
    if suffix in SHELL_SUFFIXES or name in {"dockerfile", "makefile", "gnumakefile"}:
        scopes.add("shell")
    if suffix in WINDOWS_BATCH_SUFFIXES:
        scopes.add("windows_batch")
    if _is_docker_compose_file(file_path):
        scopes.add("shell")
    if _is_github_actions_workflow(file_path):
        scopes.add("github_actions")
        scopes.add("shell")

    return scopes


def _is_github_actions_workflow(file_path: Path) -> bool:
    parts = {part.lower() for part in file_path.parts}
    return (
        ".github" in parts
        and "workflows" in parts
        and file_path.suffix.lower() in {".yml", ".yaml"}
    )


def _is_docker_compose_file(file_path: Path) -> bool:
    name = file_path.name.lower()
    return name in {
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    } or name.startswith("docker-compose.") and name.endswith((".yml", ".yaml"))


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_offset_in_raw_js_template(text: str, offset: int) -> bool:
    """Return True if offset sits in literal text inside a JS template string.

    `${process.env.KEY}` is executable JS and should stay local. A raw
    `process.env.KEY` inside a backtick-delimited script body is just string
    content that is often executed in another runtime/container.
    """
    in_template = False
    in_expr = False
    expr_depth = 0
    i = 0
    while i < min(offset, len(text)):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if not in_template:
            if ch == "`":
                in_template = True
            i += 1
            continue

        if not in_expr:
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                in_template = False
                i += 1
                continue
            if ch == "$" and nxt == "{":
                in_expr = True
                expr_depth = 1
                i += 2
                continue
            i += 1
            continue

        if ch in {'"', "'"}:
            quote = ch
            i += 1
            while i < min(offset, len(text)):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            expr_depth += 1
        elif ch == "}":
            expr_depth -= 1
            if expr_depth <= 0:
                in_expr = False
        i += 1

    return in_template and not in_expr


def _has_inline_default_or_guard(
    line: str,
    match_start: int,
    match_end: int,
    *,
    allow_call_default: bool,
) -> bool:
    """Detect common inline optional/default idioms around a reference."""
    tail = line[match_end:]
    statement_tail = tail.split(";", 1)[0]
    call_tail = tail.split(")", 1)[0]

    fallback = re.search(r"(?:\|\||\?\?)\s*([^,;)]+)", statement_tail)
    if fallback:
        default_value = fallback.group(1).strip()
        if default_value not in {"''", '""', "``"}:
            return True
    if re.match(r"\s*(?:={2,3}|!={1,2})\s*(?:['\"`]|true\b|false\b|0\b|1\b)", tail):
        return True
    if allow_call_default and "," in call_tail:
        return True
    if re.search(r"\)\s*(?:\|\||\?\?)", statement_tail):
        return True
    return False


def _classify_reference(
    pattern_type: str,
    line: str,
    match_start: int,
    match_end: int,
    match_text: str,
    full_text: str,
    absolute_offset: int,
) -> tuple[str, str]:
    """Classify whether a reference is required, optional, or external."""
    if pattern_type.startswith("process.env") and _is_offset_in_raw_js_template(
        full_text,
        absolute_offset,
    ):
        return "external", "inside JavaScript template string/runtime payload"

    if pattern_type == "${KEY}":
        if "?" in match_text:
            return "required", "shell expansion requires value"
        if re.search(r":?[-=+]", match_text):
            return "optional", "shell expansion provides a default/alternate"

    call_default_patterns = {
        "os.getenv",
        "os.environ.get",
        "ENV.fetch",
        "env()",
    }
    if pattern_type in {
        "process.env.KEY",
        'process.env["KEY"]',
        "Deno.env.get",
        "os.getenv",
        "os.environ.get",
        "ENV.fetch",
        "getenv",
        "env()",
    } and _has_inline_default_or_guard(
        line,
        match_start,
        match_end,
        allow_call_default=pattern_type in call_default_patterns,
    ):
        return "optional", "inline default or guard"

    return "required", ""


def _zod_key_requirement(entry: str) -> tuple[str, str]:
    if re.search(r"\.(?:optional|nullish)\s*\(", entry) or ".default(" in entry:
        return "optional", "zod schema marks key optional/defaulted"
    return "required", ""


def _pydantic_field_requirement(line: str) -> tuple[str, str]:
    default = line.split("=", 1)[1].strip() if "=" in line else ""
    if re.match(r"Field\s*\(\s*(?:\.\.\.|Ellipsis)\s*(?:,|\))", default):
        return "required", "pydantic Field(...) marks key required"
    if "=" in line:
        return "optional", "pydantic field has a default"
    return "required", ""


def _add_ref(
    refs: List[EnvReference],
    seen: set[tuple[str, int, str]],
    key: str,
    file_path: Path,
    line: int,
    pattern_type: str,
    requirement: str = "required",
    reason: str = "",
) -> None:
    identity = (key, line, pattern_type)
    if identity in seen:
        return
    seen.add(identity)
    refs.append(
        EnvReference(
            key=key,
            file=str(file_path),
            line=line,
            pattern_type=pattern_type,
            requirement=requirement,
            reason=reason,
        )
    )


def _detect_sveltekit_refs(
    file_path: Path,
    text: str,
    refs: List[EnvReference],
    seen: set[tuple[str, int, str]],
) -> None:
    """Detect SvelteKit $env/static imports and $env/dynamic env.KEY usage."""
    if "$env/" not in text:
        return

    static_import = re.compile(
        r'import\s*\{(?P<names>[^}]+)\}\s*from\s*["\']\$env/static/(?:private|public)["\']',
        re.MULTILINE | re.DOTALL,
    )
    for match in static_import.finditer(text):
        line = _line_for_offset(text, match.start())
        for item in match.group("names").split(","):
            name = item.strip().split(" as ", 1)[0].strip()
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                _add_ref(refs, seen, name, file_path, line, "$env/static import")

    dynamic_aliases: set[str] = set()
    dynamic_import = re.compile(
        r'import\s*\{(?P<names>[^}]+)\}\s*from\s*["\']\$env/dynamic/(?:private|public)["\']',
        re.MULTILINE | re.DOTALL,
    )
    for match in dynamic_import.finditer(text):
        for item in match.group("names").split(","):
            item = item.strip()
            alias_match = re.fullmatch(r'env\s+as\s+([A-Za-z_][A-Za-z0-9_]*)', item)
            if item == "env":
                dynamic_aliases.add("env")
            elif alias_match:
                dynamic_aliases.add(alias_match.group(1))

    for alias in dynamic_aliases:
        alias_pattern = re.compile(rf'\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)')
        for match in alias_pattern.finditer(text):
            _add_ref(
                refs,
                seen,
                match.group(1),
                file_path,
                _line_for_offset(text, match.start()),
                "$env/dynamic.KEY",
            )


def _detect_zod_process_env_schema_refs(
    file_path: Path,
    text: str,
    refs: List[EnvReference],
    seen: set[tuple[str, int, str]],
) -> None:
    """Treat ALL_CAPS z.object schema keys parsed from process.env as env refs."""
    if "z.object" not in text:
        return

    env_inputs = {"process.env"}
    for match in re.finditer(
        r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:NodeJS\.)?ProcessEnv\b',
        text,
    ):
        env_inputs.add(match.group("name"))

    env_schema_names: set[str] = set()
    parse_pattern = re.compile(
        r'(?P<schema>[A-Za-z_][A-Za-z0-9_]*)\.(?:safeParse|parse)\s*\(\s*'
        r'(?P<input>process\.env|[A-Za-z_][A-Za-z0-9_]*)\s*\)'
    )
    for match in parse_pattern.finditer(text):
        if match.group("input") in env_inputs:
            env_schema_names.add(match.group("schema"))

    schema_pattern = re.compile(
        r'(?:const|let|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
        r'z\.object\s*\(\s*\{(?P<body>.*?)\}\s*\)',
        re.DOTALL,
    )
    key_pattern = re.compile(r'(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]*)\s*:')
    for schema in schema_pattern.finditer(text):
        if schema.group("name") not in env_schema_names:
            continue
        body = schema.group("body")
        for key_match in key_pattern.finditer(body):
            absolute = schema.start("body") + key_match.start(1)
            entry_end = body.find("\n", key_match.start())
            if entry_end == -1:
                entry_end = len(body)
            entry = body[key_match.start() : entry_end]
            requirement, reason = _zod_key_requirement(entry)
            _add_ref(
                refs,
                seen,
                key_match.group(1),
                file_path,
                _line_for_offset(text, absolute),
                "zod process.env schema",
                requirement,
                reason,
            )


def _detect_pydantic_settings_refs(
    file_path: Path,
    text: str,
    refs: List[EnvReference],
    seen: set[tuple[str, int, str]],
) -> None:
    """Map Pydantic BaseSettings fields to their default uppercase env names."""
    if "BaseSettings" not in text:
        return

    in_settings_class = False
    class_indent = 0
    field_pattern = re.compile(r'^(?P<indent>\s+)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:')
    for lineno, line in enumerate(text.splitlines(), start=1):
        class_match = re.match(r'^(?P<indent>\s*)class\s+\w+\([^)]*BaseSettings[^)]*\)\s*:', line)
        if class_match:
            in_settings_class = True
            class_indent = len(class_match.group("indent"))
            continue
        if not in_settings_class:
            continue
        if line.strip() and len(line) - len(line.lstrip()) <= class_indent:
            in_settings_class = False
            continue
        field_match = field_pattern.match(line)
        if field_match:
            key = field_match.group("name").upper()
            requirement, reason = _pydantic_field_requirement(line)
            _add_ref(
                refs,
                seen,
                key,
                file_path,
                lineno,
                "pydantic BaseSettings",
                requirement,
                reason,
            )


def detect_references(file_path: Path) -> List[EnvReference]:
    """Scan a single file for environment variable references."""
    refs: List[EnvReference] = []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return refs

    scopes = _pattern_scopes_for_path(file_path)
    seen: set[tuple[str, int, str]] = set()

    lines = text.splitlines()
    line_offsets: List[int] = []
    offset = 0
    for line in lines:
        line_offsets.append(offset)
        offset += len(line) + 1

    for lineno, line in enumerate(lines, start=1):
        for pattern, pname, pattern_scopes in PATTERNS:
            if not (scopes & pattern_scopes):
                continue
            for match in pattern.finditer(line):
                absolute_offset = line_offsets[lineno - 1] + match.start()
                requirement, reason = _classify_reference(
                    pname,
                    line,
                    match.start(),
                    match.end(),
                    match.group(0),
                    text,
                    absolute_offset,
                )
                _add_ref(
                    refs,
                    seen,
                    match.group(1),
                    file_path,
                    lineno,
                    pname,
                    requirement,
                    reason,
                )

    if "js" in scopes:
        _detect_sveltekit_refs(file_path, text, refs, seen)
        _detect_zod_process_env_schema_refs(file_path, text, refs, seen)
    if "python" in scopes:
        _detect_pydantic_settings_refs(file_path, text, refs, seen)

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
        ".dart_tool",
        ".expo",
        ".turbo",
        ".svelte-kit",
        ".angular",
        ".vite",
        ".parcel-cache",
        ".codex",
        "Pods",
        "DerivedData",
        "docs",
        "generated",
        ".generated",
        "__generated__",
        "codegen",
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
        ".map",
        ".log",
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".pbxproj",
        ".xcconfig",
        ".xcfilelist",
        ".xcscheme",
        ".iml",
        ".ps1",
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
        if _looks_generated_or_minified(file_path):
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


def _looks_generated_or_minified(path: Path) -> bool:
    """Skip giant/minified generated text bundles that create regex noise."""
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:32768]
        size = path.stat().st_size
    except OSError:
        return False

    if size < 10_000 or not sample:
        return False

    lines = sample.splitlines() or [sample]
    average_line_length = sum(len(line) for line in lines) / max(len(lines), 1)
    very_long_lines = sum(1 for line in lines if len(line) > 1000)

    return average_line_length > 500 or very_long_lines >= 3


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
    baseline = raw_config.get("baseline")
    exclude = raw_config.get("exclude", [])
    optional = raw_config.get("optional", [])
    external = raw_config.get("external", [])
    ignore_missing = raw_config.get("ignore_missing", [])

    def list_of_strings(value: object) -> List[str]:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    return EnvguardConfig(
        dotenv=dotenv if isinstance(dotenv, str) else None,
        exclude=list_of_strings(exclude),
        supabase_project=supabase_project if isinstance(supabase_project, str) else None,
        baseline=baseline if isinstance(baseline, str) else None,
        optional=list_of_strings(optional),
        external=list_of_strings(external),
        ignore_missing=list_of_strings(ignore_missing),
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


# ─── Project auto-detection ─────────────────────────────────────────────────


DOTENV_CANDIDATES = (".env.example", ".env.sample", ".env.template", ".env")


def discover_dotenv_paths(scan_path: Path, config: EnvguardConfig) -> List[Path]:
    """Find available dotenv files for a scanned project, in default priority order."""
    project_root = scan_path if scan_path.is_dir() else scan_path.parent
    paths: List[Path] = []
    seen: set[Path] = set()

    if config.dotenv:
        configured = _resolve_config_path(config.dotenv, scan_path)
        if configured.exists():
            resolved = configured.resolve()
            paths.append(configured)
            seen.add(resolved)

    for name in DOTENV_CANDIDATES:
        candidate = project_root / name
        if candidate.exists():
            resolved = candidate.resolve()
            if resolved not in seen:
                paths.append(candidate)
                seen.add(resolved)
    return paths


def discover_dotenv_path(scan_path: Path, config: EnvguardConfig) -> Optional[Path]:
    """Find the default dotenv file for a scanned project."""
    paths = discover_dotenv_paths(scan_path, config)
    if paths:
        return paths[0]
    return None


def _display_path(path: Path, base: Optional[Path] = None) -> str:
    """Return a stable POSIX-ish path for commands and generated docs."""
    if base is not None:
        try:
            relative = path.resolve().relative_to(base.resolve())
            return relative.as_posix() or "."
        except ValueError:
            pass
    return path.as_posix()


def _is_safe_ci_dotenv(path: Path) -> bool:
    """Return True for dotenv templates that are reasonable to reference in CI."""
    return path.name in {".env.example", ".env.sample", ".env.template"}


def _ci_template_project_arg(scan_path: Path, base_path: Path) -> str:
    display = _display_path(scan_path, base_path)
    return display if display else "."


def _ci_template_dotenv_arg(
    scan_path: Path,
    config: EnvguardConfig,
    base_path: Path,
) -> Optional[str]:
    """Find a safe dotenv template path to include explicitly in CI."""
    if config.dotenv:
        return None
    for candidate in discover_dotenv_paths(scan_path, config):
        if _is_safe_ci_dotenv(candidate):
            return _display_path(candidate, base_path)
    return None


def build_ci_template_plan(scan_path: Path, base_path: Optional[Path] = None) -> CITemplatePlan:
    """Detect project settings for the CI template without reading secret values."""
    base = base_path or Path.cwd()
    config = load_project_config(scan_path)
    dotenv_paths = discover_dotenv_paths(scan_path, config)
    safe_dotenvs = [path for path in dotenv_paths if _is_safe_ci_dotenv(path)]
    real_dotenv_only = bool(dotenv_paths) and not safe_dotenvs
    project_arg = _ci_template_project_arg(scan_path, base)
    edge_functions = has_supabase_edge_functions(scan_path)
    supabase_project = config.supabase_project or detect_supabase_project_ref(
        scan_path,
        env={},
        config=EnvguardConfig(),
    )

    return CITemplatePlan(
        project_arg=project_arg,
        dotenv_arg=_ci_template_dotenv_arg(scan_path, config, base),
        uses_project_config=config != EnvguardConfig(),
        has_safe_dotenv=bool(safe_dotenvs or config.dotenv),
        has_real_dotenv_only=real_dotenv_only,
        has_supabase_edge_functions=edge_functions,
        has_supabase_project_config=bool(supabase_project),
    )


def _ci_shell_command(plan: CITemplatePlan) -> str:
    args = ["envguard", "ci"]
    if plan.project_arg != ".":
        args.append(plan.project_arg)
    if plan.dotenv_arg:
        args.extend(["--dotenv", plan.dotenv_arg])
    return " ".join(shlex.quote(arg) for arg in args)


def render_ci_template(plan: CITemplatePlan) -> str:
    """Render a copy-pasteable GitHub Actions workflow for envguard."""
    comments = [
        "# Generated by `envguard ci-template` (dry output; no files were written).",
        "# Paste this into .github/workflows/envguard.yml and adjust branch/install "
        "pinning as needed.",
    ]
    if plan.uses_project_config:
        comments.append("# Detected [tool.envguard]; CI will reuse your project defaults.")
    if plan.dotenv_arg:
        comments.append(f"# Detected dotenv template: {plan.dotenv_arg}")
    elif plan.has_real_dotenv_only:
        comments.append(
            "# Found only a real .env locally, so the template does not reference it. "
            "Commit a .env.example for stricter CI."
        )
    elif not plan.has_safe_dotenv:
        comments.append(
            "# Tip: add a .env.example so envguard can compare code against a contract."
        )
    if plan.has_supabase_edge_functions and plan.has_supabase_project_config:
        comments.append(
            "# Supabase Edge Functions detected; add a repository secret named "
            "SUPABASE_ACCESS_TOKEN to include remote secrets."
        )
    elif plan.has_supabase_edge_functions:
        comments.append(
            "# Supabase Edge Functions detected; add supabase/config.toml or "
            "[tool.envguard].supabase_project to enable remote secret comparison."
        )

    command = _ci_shell_command(plan)
    lines = [
        *comments,
        "name: Envguard",
        "",
        "on:",
        "  pull_request:",
        "  push:",
        "    branches: [main]",
        "",
        "jobs:",
        "  envguard:",
        "    name: Env var drift check",
        "    runs-on: ubuntu-latest",
        "    permissions:",
        "      contents: read",
        "    steps:",
        "      - name: Check out code",
        "        uses: actions/checkout@v4",
        "",
        "      - name: Set up Python",
        "        uses: actions/setup-python@v5",
        "        with:",
        '          python-version: "3.x"',
        "",
        "      - name: Install envguard",
        f"        run: python -m pip install {shlex.quote(REPO_SPEC)}",
        "",
        "      - name: 🛡️ Scan env contracts",
        "        run: |",
        f"          {command}",
        "        # envguard emits GitHub annotations and never prints dotenv values.",
    ]
    if plan.has_supabase_edge_functions and plan.has_supabase_project_config:
        lines.extend(
            [
                "        env:",
                "          # Optional: omit this secret to skip remote Supabase comparison.",
                "          SUPABASE_ACCESS_TOKEN: ${{ secrets.SUPABASE_ACCESS_TOKEN }}",
            ]
        )
    return "\n".join(lines) + "\n"


def build_ci_template(scan_path: Path, base_path: Optional[Path] = None) -> str:
    """Build a dry-output-only GitHub Actions workflow template."""
    return render_ci_template(build_ci_template_plan(scan_path, base_path))


def _clean_dotenv_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    cleaned: List[str] = []
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            cleaned.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            cleaned.append(char)
            escaped = True
            continue
        if quote:
            cleaned.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            cleaned.append(char)
            continue
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            break
        cleaned.append(char)

    value = "".join(cleaned).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def parse_dotenv_value(path: Path, key: str) -> Optional[str]:
    """Read a single dotenv value without exposing it in output."""
    if not path.exists():
        return None

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    pattern = re.compile(
        rf"^(?:export\s+)?{re.escape(key)}\s*=\s*(.*)$",
    )
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(stripped)
        if not match:
            continue
        value = _clean_dotenv_value(match.group(1))
        return value or None
    return None


def detect_supabase_access_token(
    scan_path: Path,
    dotenv_path: Optional[Path],
    env: Mapping[str, str],
) -> Optional[Tuple[str, str]]:
    """Detect a Supabase access token from shell env, selected dotenv, or .env."""
    env_token = env.get("SUPABASE_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token, "environment"

    candidates: List[Path] = []
    if dotenv_path is not None:
        candidates.append(dotenv_path)
    project_root = scan_path if scan_path.is_dir() else scan_path.parent
    candidates.append(project_root / ".env")

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        token = parse_dotenv_value(candidate, "SUPABASE_ACCESS_TOKEN")
        if token:
            return token, str(candidate)
    return None


def detect_supabase_project_ref(
    scan_path: Path,
    env: Mapping[str, str],
    config: EnvguardConfig,
) -> Optional[str]:
    """Detect a Supabase project reference from config files or environment."""
    if config.supabase_project:
        return config.supabase_project

    project_root = scan_path if scan_path.is_dir() or not scan_path.exists() else scan_path.parent
    supabase_config = project_root / "supabase" / "config.toml"
    if supabase_config.exists():
        try:
            data = tomllib.loads(supabase_config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        for key in ("project_id", "project_ref", "ref"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return env.get("SUPABASE_PROJECT_REF") or env.get("SUPABASE_PROJECT_ID")


def has_supabase_edge_functions(scan_path: Path) -> bool:
    """Return True when a project contains local Supabase Edge Functions."""
    project_root = scan_path if scan_path.is_dir() else scan_path.parent
    functions_dir = project_root / "supabase" / "functions"
    return functions_dir.exists()


def should_auto_fetch_supabase(
    scan_path: Path,
    project_ref: Optional[str],
    env: Mapping[str, str],
) -> bool:
    """Return True when envguard can safely include Supabase remote secrets."""
    return bool(
        project_ref
        and env.get("SUPABASE_ACCESS_TOKEN")
        and has_supabase_edge_functions(scan_path)
    )


def build_wizard_args(answers: Mapping[str, object]) -> List[str]:
    """Build a deterministic envguard command from wizard answers."""
    args: List[str] = ["--path", str(answers.get("path") or ".")]
    dotenv = answers.get("dotenv")
    if dotenv:
        args.extend(["--dotenv", str(dotenv)])
    if answers.get("github_annotations"):
        args.append("--github-annotations")
    if answers.get("fix"):
        args.append("--fix")
    if answers.get("use_supabase") and answers.get("supabase_project"):
        args.extend(["--supabase-project", str(answers["supabase_project"])])
    return args


def _ask_text(message: str, default: Optional[str] = None) -> str:
    if Prompt is not None:
        return Prompt.ask(message, default=default)
    suffix = f" [{default}]" if default else ""
    value = input(f"{message}{suffix}: ").strip()
    return value or (default or "")


def _ask_secret(message: str) -> str:
    if Prompt is not None:
        return Prompt.ask(message, password=True, default="")
    return getpass.getpass(f"{message}: ").strip()


def _ask_confirm(message: str, default: bool = False) -> bool:
    if Confirm is not None:
        return Confirm.ask(message, default=default)
    suffix = "Y/n" if default else "y/N"
    value = input(f"{message} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def _choose_dotenv_path(paths: List[Path]) -> str:
    """Choose a dotenv file for the wizard, including real .env files."""
    if not paths:
        return _ask_text("Dotenv file (.env.example, .env, or blank to skip)", "")
    if len(paths) == 1:
        return _ask_text("Dotenv file (.env.example or .env)", str(paths[0]))

    print("Detected dotenv files:")
    for index, path in enumerate(paths, start=1):
        print(f"  {index}) {path}")
    print(f"  {len(paths) + 1}) Custom path")
    print(f"  {len(paths) + 2}) Skip dotenv file")
    choice = _ask_text("Dotenv file choice", "1").strip()
    if choice.isdigit():
        selected = int(choice)
        if 1 <= selected <= len(paths):
            return str(paths[selected - 1])
        if selected == len(paths) + 1:
            return _ask_text("Custom dotenv file", "")
        if selected == len(paths) + 2:
            return ""
    return choice


def _format_command(args: List[str]) -> str:
    return "envguard " + " ".join(shlex.quote(item) for item in args)


def _run_main_with_temporary_token(args: List[str], token: Optional[str]) -> None:
    if not token:
        main(args)
        return

    had_existing = "SUPABASE_ACCESS_TOKEN" in os.environ
    previous = os.environ.get("SUPABASE_ACCESS_TOKEN")
    os.environ["SUPABASE_ACCESS_TOKEN"] = token
    try:
        main(args)
    finally:
        if had_existing and previous is not None:
            os.environ["SUPABASE_ACCESS_TOKEN"] = previous
        else:
            os.environ.pop("SUPABASE_ACCESS_TOKEN", None)


def run_wizard() -> None:
    """Interactive command builder for envguard."""
    scan_path = Path(_ask_text("Project path", ".")).expanduser().resolve()
    config = load_project_config(scan_path)
    detected_dotenv_paths = discover_dotenv_paths(scan_path, config)
    detected_dotenv = detected_dotenv_paths[0] if detected_dotenv_paths else None
    detected_supabase = detect_supabase_project_ref(scan_path, os.environ, config)
    edge_functions = has_supabase_edge_functions(scan_path)

    dotenv = _choose_dotenv_path(detected_dotenv_paths)
    selected_dotenv = Path(dotenv) if dotenv else detected_dotenv
    token_info = detect_supabase_access_token(scan_path, selected_dotenv, os.environ)
    supabase_token = token_info[0] if token_info else None
    use_supabase_default = bool(detected_supabase and supabase_token)
    use_supabase = False
    supabase_project = detected_supabase or ""
    if edge_functions or detected_supabase:
        use_supabase = _ask_confirm("Compare Supabase Edge Function secrets", use_supabase_default)
        if use_supabase:
            supabase_project = _ask_text("Supabase project ref", supabase_project)
            if supabase_token:
                print(f"Supabase access token detected in {token_info[1]}.")
            else:
                entered_token = _ask_secret(
                    "Supabase access token (blank to skip remote secrets)"
                ).strip()
                if entered_token:
                    supabase_token = entered_token
                else:
                    print("Tip: set SUPABASE_ACCESS_TOKEN to fetch remote Supabase secrets.")
                    use_supabase = False

    args = build_wizard_args(
        {
            "path": str(scan_path),
            "dotenv": dotenv,
            "use_supabase": use_supabase,
            "supabase_project": supabase_project,
            "github_annotations": _ask_confirm("Use GitHub Actions annotations", False),
            "fix": _ask_confirm("Offer to prune unused dotenv keys", False),
        }
    )
    print(f"\nGenerated command:\n  {_format_command(args)}\n")
    if _ask_confirm("Run it now", True):
        _run_main_with_temporary_token(args, supabase_token if use_supabase else None)


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
SUPABASE_REDACTION = "[REDACTED]"
SUPABASE_SENSITIVE_FIELD_RE = re.compile(
    r"(?:^|[_-])(?:"
    r"access[_-]?token|refresh[_-]?token|auth[_-]?token|api[_-]?key|"
    r"service[_-]?role[_-]?key|anon[_-]?key|secret[_-]?value|secret|"
    r"password|passwd|pwd|authorization|jwt"
    r")(?:$|[_-])",
    re.IGNORECASE,
)
SUPABASE_SENSITIVE_PAIR_RE = re.compile(
    r"(?i)((?<![A-Za-z0-9_])[\"']?(?:access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|api[_-]?key|service[_-]?role[_-]?key|anon[_-]?key|"
    r"secret[_-]?value|password|passwd|pwd|jwt)[\"']?\s*[:=]\s*)"
    r"([\"']?)([^\s,;}\"']+)([\"']?)"
)
SUPABASE_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
SUPABASE_ACCESS_TOKEN_RE = re.compile(r"\bsbp_[A-Za-z0-9_=-]{8,}\b")
SUPABASE_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)
SUPABASE_PREFIXED_TOKEN_RE = re.compile(
    r"\b(?:sk|pk|rk)_(?:live|test|secret|anon)?_?[A-Za-z0-9]{16,}\b"
)


def _is_supabase_sensitive_field(key: str) -> bool:
    """Return True for response field names likely to carry secret material."""
    return bool(SUPABASE_SENSITIVE_FIELD_RE.search(key))


def _redact_supabase_secret_text(text: str) -> str:
    """Redact tokens from free-form Supabase API error text."""

    def redact_pair(match: re.Match[str]) -> str:
        prefix, open_quote, _value, close_quote = match.groups()
        quote = close_quote if open_quote and close_quote == open_quote else open_quote
        return f"{prefix}{quote}{SUPABASE_REDACTION}{quote}"

    redacted = SUPABASE_SENSITIVE_PAIR_RE.sub(redact_pair, text)
    redacted = SUPABASE_BEARER_TOKEN_RE.sub(f"Bearer {SUPABASE_REDACTION}", redacted)
    redacted = SUPABASE_ACCESS_TOKEN_RE.sub(SUPABASE_REDACTION, redacted)
    redacted = SUPABASE_JWT_RE.sub(SUPABASE_REDACTION, redacted)
    return SUPABASE_PREFIXED_TOKEN_RE.sub(SUPABASE_REDACTION, redacted)


def _redact_supabase_error_data(data: object, sensitive_field: bool = False) -> object:
    """Recursively redact secret values while preserving JSON response shape."""
    if isinstance(data, dict):
        return {
            key: _redact_supabase_error_data(
                value,
                sensitive_field or _is_supabase_sensitive_field(key),
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_redact_supabase_error_data(item, sensitive_field) for item in data]
    if sensitive_field:
        return SUPABASE_REDACTION if data is not None else None
    if isinstance(data, str):
        return _redact_supabase_secret_text(data)
    return data


def _redact_supabase_error_body(body: str) -> str:
    """Sanitize a Supabase API response body for safe CLI error output."""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return _redact_supabase_secret_text(body)
    return json.dumps(
        _redact_supabase_error_data(data),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _format_supabase_api_error(status: int, body: str) -> str:
    """Build a redacted Supabase API status/body error message."""
    redacted_body = _redact_supabase_error_body(body)
    return f"Error: Supabase API returned {status}: {redacted_body}"


def _parse_supabase_secret_names(data: object) -> List[str]:
    """Extract Supabase secret names from supported API response shapes."""
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        secrets = data.get("secrets")
        if not isinstance(secrets, list):
            raise ValueError(
                "unexpected Supabase secrets response shape: "
                "expected a list or an object with a 'secrets' list"
            )
        entries = secrets
    else:
        raise ValueError(
            "unexpected Supabase secrets response shape: "
            "expected a list or an object with a 'secrets' list"
        )

    names: List[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


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
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            print(_format_supabase_api_error(resp.status, body), file=sys.stderr)
            sys.exit(1)

        data = json.loads(body)
        return _parse_supabase_secret_names(data)
    except (http.client.HTTPException, OSError, json.JSONDecodeError, ValueError) as e:
        print(
            f"Error: Failed to fetch Supabase secrets: {_redact_supabase_secret_text(str(e))}",
            file=sys.stderr,
        )
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
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status not in (200, 204):
            print(_format_supabase_api_error(resp.status, body), file=sys.stderr)
            return False
        return True
    except (http.client.HTTPException, OSError, json.JSONDecodeError) as e:
        print(
            f"Error: Failed to delete Supabase secrets: {_redact_supabase_secret_text(str(e))}",
            file=sys.stderr,
        )
        return False
    finally:
        conn.close()


# ─── Analysis ──────────────────────────────────────────────────────────────


def analyze(
    ref_map: Dict[str, List[EnvReference]],
    dotenv_keys: List[str],
    supabase_keys: Optional[List[str]] = None,
    optional_keys: Optional[List[str]] = None,
    external_keys: Optional[List[str]] = None,
    ignore_keys: Optional[List[str]] = None,
) -> ScanResult:
    """Cross-reference code references against expected keys."""
    result = ScanResult(references=ref_map)

    code_keys = set(ref_map.keys())
    example_keys = set(dotenv_keys)
    supabase_set = set(supabase_keys or [])
    available_keys = example_keys | supabase_set

    # UNUSED: in .env.example but never referenced in code
    result.unused = sorted(example_keys - code_keys)

    required_key_set: set[str] = set()
    optional_key_set: set[str] = set(optional_keys or [])
    external_key_set: set[str] = set(external_keys or [])
    ignored_key_set: set[str] = set(ignore_keys or [])
    for key, refs in ref_map.items():
        if key in ignored_key_set:
            continue
        if key in external_key_set:
            continue
        if key in optional_key_set:
            continue
        requirements = {ref.requirement for ref in refs}
        if "required" in requirements:
            required_key_set.add(key)
        elif "optional" in requirements:
            optional_key_set.add(key)
        elif "external" in requirements:
            external_key_set.add(key)
        else:
            required_key_set.add(key)

    # MISSING: required references not available locally/remotely.
    result.missing = sorted(required_key_set - available_keys)
    result.optional_missing = sorted(optional_key_set & code_keys - available_keys)
    result.external_missing = sorted(external_key_set & code_keys - available_keys)
    result.ignored_missing = sorted(ignored_key_set & code_keys - available_keys)

    # If Supabase secrets were provided, find orphans
    if supabase_keys is not None:
        code_and_example = code_keys | example_keys
        result.supabase_orphans = sorted(supabase_set - code_and_example)

    return result


def _requirement_for_key(
    key: str,
    refs: List[EnvReference],
    optional_keys: set[str],
    external_keys: set[str],
    ignored_keys: set[str],
) -> str:
    """Classify a key using CLI/config overrides before inferred requirements."""
    if key in ignored_keys:
        return "ignored"
    if key in external_keys:
        return "external"
    if key in optional_keys:
        return "optional"

    requirements = {ref.requirement for ref in refs}
    if "required" in requirements:
        return "required"
    if "optional" in requirements:
        return "optional"
    if "external" in requirements:
        return "external"
    return "required"


def _matrix_status(requirement: str, available: bool) -> str:
    """Return the doctor status for a key without considering secret values."""
    if available:
        return "ready"
    if requirement == "required":
        return "missing"
    return f"{requirement}-missing"


def build_secrets_matrix(
    ref_map: Dict[str, List[EnvReference]],
    dotenv_keys: List[str],
    env: Mapping[str, str],
    supabase_keys: Optional[List[str]] = None,
    optional_keys: Optional[List[str]] = None,
    external_keys: Optional[List[str]] = None,
    ignore_keys: Optional[List[str]] = None,
    dotenv_path: Optional[Path] = None,
    supabase_project: Optional[str] = None,
    notes: Optional[List[str]] = None,
) -> SecretsMatrix:
    """Build a secret-readiness matrix without reading or exposing values."""
    dotenv_set = set(dotenv_keys)
    env_set = set(env.keys())
    supabase_set = set(supabase_keys or [])
    supabase_checked = supabase_keys is not None
    optional_set = set(optional_keys or [])
    external_set = set(external_keys or [])
    ignored_set = set(ignore_keys or [])

    rows: List[SecretsMatrixRow] = []
    for key in sorted(ref_map):
        refs = ref_map[key]
        in_dotenv = key in dotenv_set
        in_env = key in env_set
        in_supabase = key in supabase_set if supabase_checked else None
        available = in_dotenv or in_env or bool(in_supabase)
        requirement = _requirement_for_key(
            key,
            refs,
            optional_set,
            external_set,
            ignored_set,
        )
        rows.append(
            SecretsMatrixRow(
                key=key,
                requirement=requirement,
                status=_matrix_status(requirement, available),
                dotenv=in_dotenv,
                environment=in_env,
                supabase=in_supabase,
                references=len(refs),
            )
        )

    return SecretsMatrix(
        rows=rows,
        dotenv_path=dotenv_path,
        supabase_project=supabase_project,
        supabase_checked=supabase_checked,
        unused_dotenv=sorted(dotenv_set - set(ref_map)),
        supabase_orphans=(
            sorted(supabase_set - set(ref_map) - dotenv_set) if supabase_checked else []
        ),
        notes=list(notes or []),
    )


def _matrix_counts(matrix: SecretsMatrix) -> dict[str, int]:
    counts = {
        "required": 0,
        "optional": 0,
        "external": 0,
        "ignored": 0,
        "ready": 0,
        "missing": 0,
        "optional_missing": 0,
        "external_missing": 0,
        "ignored_missing": 0,
        "unused_dotenv": len(matrix.unused_dotenv),
        "supabase_orphans": len(matrix.supabase_orphans),
    }
    for row in matrix.rows:
        counts[row.requirement] += 1
        if row.status == "ready":
            counts["ready"] += 1
        else:
            normalized = row.status.replace("-", "_")
            counts[normalized] += 1
    return counts


def secrets_matrix_has_required_missing(matrix: SecretsMatrix) -> bool:
    """Return whether the doctor found required keys missing from every source."""
    return any(row.status == "missing" for row in matrix.rows)


def _availability_marker(value: Optional[bool]) -> str:
    if value is None:
        return "not checked"
    return "yes" if value else "no"


def _matrix_json_output(matrix: SecretsMatrix) -> None:
    output = {
        "summary": {
            "counts": _matrix_counts(matrix),
            "blocking": secrets_matrix_has_required_missing(matrix),
            "exit_code": 1 if secrets_matrix_has_required_missing(matrix) else 0,
        },
        "sources": {
            "dotenv": str(matrix.dotenv_path) if matrix.dotenv_path else None,
            "environment": "current process",
            "supabase_project": matrix.supabase_project,
            "supabase_checked": matrix.supabase_checked,
        },
        "notes": matrix.notes,
        "rows": [
            {
                "key": row.key,
                "requirement": row.requirement,
                "status": row.status,
                "available": {
                    "dotenv": row.dotenv,
                    "environment": row.environment,
                    "supabase": row.supabase,
                },
                "references": row.references,
            }
            for row in matrix.rows
        ],
        "unused_dotenv": matrix.unused_dotenv,
        "supabase_orphans": matrix.supabase_orphans,
    }
    print(json.dumps(output, indent=2))


def _matrix_plain_output(matrix: SecretsMatrix) -> None:
    counts = _matrix_counts(matrix)
    print("envguard doctor — secret readiness matrix")
    print(f"dotenv: {matrix.dotenv_path if matrix.dotenv_path else 'not found'}")
    if matrix.supabase_project:
        checked = "checked" if matrix.supabase_checked else "not checked"
        print(f"supabase: {matrix.supabase_project} ({checked})")
    else:
        print("supabase: not configured")
    print("values: hidden")
    print()
    print("key\trequirement\tstatus\tdotenv\tenv\tsupabase\trefs")
    for row in matrix.rows:
        print(
            f"{row.key}\t{row.requirement}\t{row.status}\t"
            f"{_availability_marker(row.dotenv)}\t"
            f"{_availability_marker(row.environment)}\t"
            f"{_availability_marker(row.supabase)}\t{row.references}"
        )
    print()
    print(
        "summary: "
        f"{counts['ready']} ready, {counts['missing']} required missing, "
        f"{counts['optional_missing']} optional missing, "
        f"{counts['external_missing']} external missing, "
        f"{counts['ignored_missing']} ignored missing"
    )
    if matrix.unused_dotenv:
        print(f"unused dotenv keys: {', '.join(matrix.unused_dotenv)}")
    if matrix.supabase_orphans:
        print(f"orphaned Supabase secrets: {', '.join(matrix.supabase_orphans)}")
    for note in matrix.notes:
        print(f"note: {note}")


def _matrix_rich_output(matrix: SecretsMatrix) -> None:
    assert Console is not None
    assert Table is not None
    assert Panel is not None
    console = Console()
    counts = _matrix_counts(matrix)
    summary = "[bold cyan]envguard doctor[/] — Secret Readiness Matrix\n"
    summary += "  • Values are hidden; only key names and source availability are shown"
    if matrix.dotenv_path:
        summary += f"\n  • dotenv: [green]{matrix.dotenv_path}[/]"
    else:
        summary += "\n  • dotenv: [yellow]not found[/]"
    if matrix.supabase_project:
        checked = "checked" if matrix.supabase_checked else "not checked"
        summary += f"\n  • Supabase project: [green]{matrix.supabase_project}[/] ({checked})"
    else:
        summary += "\n  • Supabase project: [dim]not configured[/]"
    console.print()
    console.print(Panel(summary, border_style="cyan"))
    console.print()

    table = Table(title="Key readiness", border_style="cyan")
    table.add_column("Key", no_wrap=True)
    table.add_column("Requirement")
    table.add_column("Status")
    table.add_column("dotenv")
    table.add_column("env")
    table.add_column("Supabase")
    table.add_column("Refs", justify="right")
    for row in matrix.rows:
        if row.status == "ready":
            status_style = "green"
        elif row.status == "missing":
            status_style = "red"
        else:
            status_style = "yellow"
        table.add_row(
            row.key,
            row.requirement,
            f"[{status_style}]{row.status}[/]",
            _availability_marker(row.dotenv),
            _availability_marker(row.environment),
            _availability_marker(row.supabase),
            str(row.references),
        )
    console.print(table)
    console.print()
    console.print(
        "[bold]Summary:[/] "
        f"{counts['ready']} ready, {counts['missing']} required missing, "
        f"{counts['optional_missing']} optional missing, "
        f"{counts['external_missing']} external missing, "
        f"{counts['ignored_missing']} ignored missing"
    )
    if matrix.unused_dotenv:
        console.print(f"[yellow]Unused dotenv keys:[/] {', '.join(matrix.unused_dotenv)}")
    if matrix.supabase_orphans:
        console.print(
            f"[magenta]Orphaned Supabase secrets:[/] {', '.join(matrix.supabase_orphans)}"
        )
    for note in matrix.notes:
        console.print(f"[dim]Note:[/] {note}")
    console.print()


def _matrix_output(matrix: SecretsMatrix, json_output: bool = False) -> None:
    if json_output:
        _matrix_json_output(matrix)
    elif Console is None:
        _matrix_plain_output(matrix)
    else:
        _matrix_rich_output(matrix)


BASELINE_VERSION = 1
FINDING_FIELDS = (
    "unused",
    "missing",
    "optional_missing",
    "external_missing",
    "ignored_missing",
    "supabase_orphans",
)


def _result_findings(result: ScanResult) -> dict[str, list[str]]:
    """Return only secret-safe finding key names grouped by class."""
    return {field_name: sorted(getattr(result, field_name)) for field_name in FINDING_FIELDS}


def load_baseline(path: Path) -> dict[str, set[str]]:
    """Load an envguard baseline file containing finding classes and key names."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"Error: failed to read baseline {path}: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid baseline JSON in {path}: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        print(
            f"Error: baseline {path} must be a version {BASELINE_VERSION} object",
            file=sys.stderr,
        )
        sys.exit(1)

    findings = data.get("findings")
    if not isinstance(findings, dict):
        print(f"Error: baseline {path} must contain a findings object", file=sys.stderr)
        sys.exit(1)

    baseline: dict[str, set[str]] = {}
    for field_name in FINDING_FIELDS:
        values = findings.get(field_name, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            print(
                f"Error: baseline {path} field {field_name!r} must be a list of key names",
                file=sys.stderr,
            )
            sys.exit(1)
        baseline[field_name] = set(values)
    return baseline


def write_baseline(path: Path, result: ScanResult) -> None:
    """Write current findings to a secret-safe baseline JSON file."""
    payload = {
        "version": BASELINE_VERSION,
        "findings": _result_findings(result),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def apply_baseline(result: ScanResult, baseline: dict[str, set[str]]) -> dict[str, int]:
    """Suppress findings that are already recorded in the baseline."""
    suppressed: dict[str, int] = {}
    for field_name in FINDING_FIELDS:
        values = getattr(result, field_name)
        baselined = baseline.get(field_name, set())
        filtered = [value for value in values if value not in baselined]
        suppressed_count = len(values) - len(filtered)
        setattr(result, field_name, filtered)
        if suppressed_count:
            suppressed[field_name] = suppressed_count
    return suppressed


# ─── Output Formatting ─────────────────────────────────────────────────────


def _rich_output(
    result: ScanResult,
    dotenv_path: Optional[Path],
    supabase_ref: Optional[str],
    show_details: bool = False,
    details_command: Optional[str] = None,
    baseline_path: Optional[Path] = None,
    baseline_suppressed: Optional[dict[str, int]] = None,
):
    """Pretty terminal output using rich."""
    console = Console()
    should_show_details_command = False

    # Summary header
    total_refs = sum(len(v) for v in result.references.values())
    unique_keys = len(result.references)

    console.print()
    summary = "[bold cyan]envguard[/] — Environment Variable Audit\n"
    summary += f"  • {total_refs} references found ({unique_keys} unique keys)"
    if dotenv_path:
        summary += f"\n  • dotenv file: [green]{dotenv_path}[/]"
    if supabase_ref:
        summary += f"\n  • Supabase project: [green]{supabase_ref}[/]"
    if baseline_path:
        summary += f"\n  • baseline: [green]{baseline_path}[/]"
    suppressed_total = sum((baseline_suppressed or {}).values())
    if suppressed_total:
        summary += f"\n  • {suppressed_total} baselined finding(s) suppressed"
    console.print(Panel(summary, border_style="cyan"))
    console.print()

    # UNUSED keys
    if result.unused:
        if show_details:
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
        else:
            label = "key" if len(result.unused) == 1 else "keys"
            console.print(f"[yellow]![/] {len(result.unused)} unused {label} found.")
            should_show_details_command = should_show_details_command or bool(details_command)
        console.print()
    else:
        console.print("[green]✓[/] No unused keys found in configuration.")
        console.print()

    # MISSING keys
    if result.missing:
        if show_details:
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
        else:
            label = "key" if len(result.missing) == 1 else "keys"
            console.print(f"[red]![/] {len(result.missing)} missing {label} detected.")
            should_show_details_command = should_show_details_command or bool(details_command)
        console.print()
    else:
        console.print("[green]✓[/] No missing required keys detected.")
        console.print()

    # Optional/defaulted keys absent from config (non-blocking)
    if result.optional_missing:
        if show_details:
            table = Table(
                title="[blue]OPTIONAL[/] — Defaulted keys absent from config",
                border_style="blue",
            )
            table.add_column("Key", style="blue", no_wrap=True)
            table.add_column("References", style="dim")
            for key in result.optional_missing:
                refs = result.references.get(key, [])
                locs = "; ".join(
                    f"{r.file}:{r.line} ({r.reason or r.requirement})" for r in refs[:3]
                )
                if len(refs) > 3:
                    locs += f" …and {len(refs)-3} more"
                table.add_row(key, locs)
            console.print(table)
        else:
            label = "key" if len(result.optional_missing) == 1 else "keys"
            console.print(
                f"[blue]i[/] {len(result.optional_missing)} optional/defaulted {label} "
                "absent from config."
            )
            should_show_details_command = should_show_details_command or bool(details_command)
        console.print()

    # External/runtime-context keys absent from local config (non-blocking)
    if result.external_missing:
        if show_details:
            table = Table(
                title="[cyan]EXTERNAL[/] — Runtime-context keys absent from local config",
                border_style="cyan",
            )
            table.add_column("Key", style="cyan", no_wrap=True)
            table.add_column("References", style="dim")
            for key in result.external_missing:
                refs = result.references.get(key, [])
                locs = "; ".join(
                    f"{r.file}:{r.line} ({r.reason or r.requirement})" for r in refs[:3]
                )
                if len(refs) > 3:
                    locs += f" …and {len(refs)-3} more"
                table.add_row(key, locs)
            console.print(table)
        else:
            label = "key" if len(result.external_missing) == 1 else "keys"
            console.print(
                f"[cyan]i[/] {len(result.external_missing)} external/runtime {label} "
                "absent from local config."
            )
            should_show_details_command = should_show_details_command or bool(details_command)
        console.print()

    # Supabase orphans
    if result.supabase_orphans:
        if show_details:
            table = Table(
                title="[magenta]ORPHANED[/] — Supabase secrets with no code references",
                border_style="magenta",
            )
            table.add_column("Secret", style="magenta", no_wrap=True)
            for key in result.supabase_orphans:
                table.add_row(key)
            console.print(table)
        else:
            label = "secret" if len(result.supabase_orphans) == 1 else "secrets"
            console.print(
                f"[magenta]![/] {len(result.supabase_orphans)} orphaned Supabase "
                f"{label} found."
            )
            should_show_details_command = should_show_details_command or bool(details_command)
        console.print()

    if should_show_details_command and details_command:
        console.print(f"[dim]Show details:[/] [bold]{details_command}[/]")
        console.print()

    # Overall status
    blocking_issues = bool(result.unused or result.missing or result.supabase_orphans)
    advisory_issues = bool(result.optional_missing or result.external_missing)
    if blocking_issues:
        if show_details:
            console.print("[bold red]✗[/] Issues found. Review the tables above.")
        else:
            console.print("[bold red]✗[/] Issues found.")
    elif advisory_issues:
        console.print("[bold green]✓[/] No blocking issues. Advisory items shown above.")
    else:
        console.print("[bold green]✓[/] All environment variables are accounted for!")

    console.print()


def _json_summary(
    result: ScanResult,
    allow_unused: bool = False,
    allow_missing: bool = False,
    baseline_path: Optional[Path] = None,
    baseline_suppressed: Optional[dict[str, int]] = None,
) -> dict[str, object]:
    """Return compact metadata for JSON consumers."""
    blocking = has_blocking_issues(
        result,
        allow_unused=allow_unused,
        allow_missing=allow_missing,
    )
    summary: dict[str, object] = {
        "counts": {
            "unused": len(result.unused),
            "missing": len(result.missing),
            "optional_missing": len(result.optional_missing),
            "external_missing": len(result.external_missing),
            "ignored_missing": len(result.ignored_missing),
            "supabase_orphans": len(result.supabase_orphans),
            "referenced_keys": len(result.references),
            "references": sum(len(refs) for refs in result.references.values()),
        },
        "blocking": blocking,
        "exit_code": 1 if blocking else 0,
    }
    if baseline_path or baseline_suppressed:
        summary["baseline"] = {
            "path": str(baseline_path) if baseline_path else None,
            "suppressed": {field: baseline_suppressed.get(field, 0) for field in FINDING_FIELDS}
            if baseline_suppressed
            else {field: 0 for field in FINDING_FIELDS},
        }
    return summary


def _json_output(
    result: ScanResult,
    allow_unused: bool = False,
    allow_missing: bool = False,
    baseline_path: Optional[Path] = None,
    baseline_suppressed: Optional[dict[str, int]] = None,
) -> None:
    """JSON machine-readable output."""
    output = {
        "summary": _json_summary(
            result,
            allow_unused=allow_unused,
            allow_missing=allow_missing,
            baseline_path=baseline_path,
            baseline_suppressed=baseline_suppressed,
        ),
        "unused": result.unused,
        "missing": result.missing,
        "optional_missing": result.optional_missing,
        "external_missing": result.external_missing,
        "ignored_missing": result.ignored_missing,
        "supabase_orphans": result.supabase_orphans,
        "references": {
            key: [
                {
                    "file": r.file,
                    "line": r.line,
                    "pattern": r.pattern_type,
                    "requirement": r.requirement,
                    "reason": r.reason,
                }
                for r in refs
            ]
            for key, refs in result.references.items()
        },
    }
    print(json.dumps(output, indent=2))


def _count_phrase(count: int, label: str) -> str:
    """Return a compact count phrase for summary output."""
    return f"{count} {label}"


def format_summary_line(
    result: ScanResult,
    allow_unused: bool = False,
    allow_missing: bool = False,
    baseline_suppressed: Optional[dict[str, int]] = None,
) -> str:
    """Return a one-line terminal summary for CI/chat consumers."""
    blocking = has_blocking_issues(
        result,
        allow_unused=allow_unused,
        allow_missing=allow_missing,
    )
    exit_code = 1 if blocking else 0
    counts = [
        (len(result.missing), "missing"),
        (len(result.unused), "unused"),
        (len(result.optional_missing), "optional"),
        (len(result.external_missing), "external"),
        (len(result.ignored_missing), "ignored"),
        (len(result.supabase_orphans), "orphaned"),
    ]
    actual_counts_present = any(count for count, _label in counts)
    shown_counts = [_count_phrase(count, label) for count, label in counts if count]
    suppressed_total = sum((baseline_suppressed or {}).values())
    if suppressed_total:
        shown_counts.append(_count_phrase(suppressed_total, "baselined"))
    counts_text = ", ".join(shown_counts) if shown_counts else "clean"
    status = "red" if blocking else "yellow" if actual_counts_present else "green"
    return f"envguard: {status} — {counts_text} (exit {exit_code})"


def _summary_output(
    result: ScanResult,
    allow_unused: bool = False,
    allow_missing: bool = False,
    baseline_suppressed: Optional[dict[str, int]] = None,
) -> None:
    """Print compact, non-rich terminal output."""
    print(
        format_summary_line(
            result,
            allow_unused=allow_unused,
            allow_missing=allow_missing,
            baseline_suppressed=baseline_suppressed,
        )
    )


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

    for key in result.optional_missing:
        annotations.append(
            f"::notice::Optional environment variable {_escape_annotation_message(key)} "
            "is absent from config"
        )

    for key in result.external_missing:
        annotations.append(
            f"::notice::External/runtime environment variable "
            f"{_escape_annotation_message(key)} is absent from local config"
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


SAFE_FIX_DOTENV_SUFFIXES = (
    ".example",
    ".sample",
    ".template",
    ".tmpl",
    ".dist",
)
DOTENV_ASSIGNMENT_RE = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=",
)


def is_template_dotenv_path(path: Path) -> bool:
    """Return whether --fix can safely prune this dotenv path by default."""
    name = path.name.lower()
    return name.startswith(".env.") and name.endswith(SAFE_FIX_DOTENV_SUFFIXES)


def _redacted_dotenv_assignment(line: str) -> str:
    """Return a dry-run preview that never exposes dotenv values."""
    match = DOTENV_ASSIGNMENT_RE.match(line.strip())
    if not match:
        return "<unparseable dotenv assignment>"
    return f"{match.group(1)}=<redacted>"


def _write_backup_exclusive(path: Path, content: str) -> Path:
    """Write a backup without overwriting or following existing filesystem entries."""
    index = 0
    while True:
        suffix = ".bak" if index == 0 else f".bak.{index}"
        candidate = path.with_name(f"{path.name}{suffix}")
        try:
            _write_text_exclusive(candidate, content, 0o600)
        except FileExistsError:
            index += 1
            continue
        _fsync_parent_dir(candidate)
        return candidate


def _no_follow_flags(flags: int) -> int:
    """Add O_NOFOLLOW when the platform exposes it."""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    return flags | no_follow


def _read_text_no_follow(path: Path) -> tuple[str, int]:
    """Read a regular file without following a symlink at the final path component."""
    fd = os.open(path, _no_follow_flags(os.O_RDONLY))
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError(f"Refusing to read non-regular dotenv file: {path}")
        mode = stat.S_IMODE(file_stat.st_mode)
        with os.fdopen(fd, "r", encoding="utf-8") as file_obj:
            fd = -1
            return file_obj.read(), mode
    finally:
        if fd != -1:
            os.close(fd)


def _write_text_exclusive(path: Path, content: str, mode: int) -> None:
    """Write a new file exclusively without following an existing symlink."""
    flags = _no_follow_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    fd = os.open(path, flags, mode)
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            # Some platforms/filesystems may reject fchmod; the secure creation
            # mode above is still applied by os.open.
            pass
        data = content.encode("utf-8")
        while data:
            written = os.write(fd, data)
            if written == 0:
                raise OSError(f"Failed to write data to {path}")
            data = data[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_parent_dir(path: Path) -> None:
    """Best-effort fsync of a file's parent directory after create/replace operations."""
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_no_follow(path: Path, content: str, mode: int) -> None:
    """Atomically replace path with content without following path symlinks."""
    temp_path: Optional[Path] = None
    sanitized_mode = mode & 0o777 or 0o600

    for index in range(100):
        candidate = path.with_name(f".{path.name}.envguard-{os.getpid()}-{index}.tmp")
        try:
            _write_text_exclusive(candidate, content, sanitized_mode)
        except FileExistsError:
            continue
        temp_path = candidate
        break

    if temp_path is None:
        raise FileExistsError(f"Could not create a temporary file next to {path}")

    try:
        # os.replace updates the directory entry atomically. If an attacker swaps
        # path for a symlink after our no-follow read, the symlink itself is
        # replaced; its target is not opened or truncated.
        os.replace(temp_path, path)
        _fsync_parent_dir(path)
    except OSError:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def interactive_fix(
    result: ScanResult,
    dotenv_path: Path,
    *,
    dry_run: bool = False,
    allow_real_env: bool = False,
):
    """Interactively prune unused entries from a template dotenv file."""
    if not result.unused:
        print(f"No unused keys to prune from {dotenv_path}.")
        return

    if Confirm is None:
        print("rich is required for --fix mode. Install it: pip install rich")
        return

    if not dry_run and not allow_real_env and not is_template_dotenv_path(dotenv_path):
        print(
            "Refusing to prune a real dotenv file by default: "
            f"{dotenv_path}\n"
            "Use --fix-dry-run to preview removals, or pass --fix-real-env "
            "if you intentionally want to edit this file.",
            file=sys.stderr,
        )
        return

    if not dry_run and dotenv_path.is_symlink():
        print(
            "Refusing to prune a symlinked dotenv file: "
            f"{dotenv_path}\n"
            "Use --fix-dry-run to preview removals, then edit the target file "
            "manually if needed.",
            file=sys.stderr,
        )
        return

    console = Console()

    # Filter dotenv lines to keep. Real writes use a no-follow read so a path
    # swapped after the initial symlink check is not followed before replacement.
    unused_set = set(result.unused)
    file_mode = 0o600
    try:
        if dry_run:
            text = dotenv_path.read_text(encoding="utf-8")
        else:
            text, file_mode = _read_text_no_follow(dotenv_path)
        lines = text.splitlines(keepends=True)
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

        match = DOTENV_ASSIGNMENT_RE.match(stripped)
        if match and match.group(1) in unused_set:
            key = match.group(1)
            if dry_run:
                removed_lines.append(line.rstrip())
                keep_lines.append(line)
            elif Confirm and Confirm.ask(
                f"Remove unused key [yellow]{key}[/]?",
                default=False,
            ):
                removed_lines.append(line.rstrip())
                continue
            else:
                keep_lines.append(line)
        else:
            keep_lines.append(line)

    if dry_run:
        if removed_lines:
            console.print("[bold yellow]Dry run:[/] would remove these line(s):")
            for line in removed_lines:
                console.print(f"  [yellow]-[/] {_redacted_dotenv_assignment(line)}")
        else:
            console.print("[dim]Dry run: no matching unused assignments found.[/]")
        return

    if removed_lines:
        backup_path = _write_backup_exclusive(dotenv_path, "".join(lines))
        try:
            _atomic_write_no_follow(dotenv_path, "".join(keep_lines), file_mode)
        except OSError as e:
            print(f"Error writing {dotenv_path}: {e}", file=sys.stderr)
            return
        console.print(
            f"\n[green]✓[/] Removed {len(removed_lines)} unused key(s) "
            f"from [cyan]{dotenv_path}[/]"
        )
        console.print(f"[dim]Backup written to {backup_path}[/]")
    else:
        console.print("[dim]No changes made.[/]")


# ─── Self Update ─────────────────────────────────────────────────────────────


def _pipx_binary() -> Optional[str]:
    pipx = shutil.which("pipx")
    if pipx:
        return pipx
    local_pipx = Path.home() / ".local" / "bin" / "pipx"
    if local_pipx.exists():
        return str(local_pipx)
    return None


def _is_app_pipx_python(path: str) -> bool:
    normalized = str(Path(path)).replace("\\", "/")
    return f"/pipx/venvs/{DIST_NAME}/" in normalized


def _python_version_ok(path: str) -> bool:
    code = (
        "import sys; "
        f"raise SystemExit(0 if sys.version_info >= {MIN_PYTHON!r} else 1)"
    )
    try:
        completed = subprocess.run(
            [path, "-c", code],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _host_python() -> Optional[str]:
    for name in (
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3.9",
        "python3",
        "python",
    ):
        candidate = shutil.which(name)
        if candidate and not _is_app_pipx_python(candidate) and _python_version_ok(candidate):
            return candidate
    if not _is_app_pipx_python(sys.executable) and _python_version_ok(sys.executable):
        return sys.executable
    return None


def _pipx_update_command() -> List[str]:
    python = _host_python()
    if not python:
        return []
    pipx = _pipx_binary()
    if pipx:
        return [pipx, "install", "--python", python, "--force", REPO_SPEC]
    return [python, "-m", "pipx", "install", "--python", python, "--force", REPO_SPEC]


def _data_home() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data)
    return Path.home() / ".local" / "share"


def _pipx_bootstrap_dir() -> Path:
    return _data_home() / APP_NAME / "pipx-bootstrap"


def _bootstrap_pipx(python: str) -> str:
    print("pipx was not available; installing a private pipx helper and retrying...")
    venv_dir = _pipx_bootstrap_dir()
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([python, "-m", "venv", str(venv_dir)], check=True)
    if os.name == "nt":
        venv_python = venv_dir / "Scripts" / "python.exe"
        pipx = venv_dir / "Scripts" / "pipx.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
        pipx = venv_dir / "bin" / "pipx"
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "pipx"],
        check=True,
    )
    return str(pipx)


def run_update() -> int:
    """Install the latest envguard from GitHub via pipx."""
    print(f"Updating {APP_NAME} from GitHub...")
    command = _pipx_update_command()
    if not command:
        print("Update failed: could not find a usable Python or pipx.", file=sys.stderr)
        return 1
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        if len(command) >= 3 and command[1:3] == ["-m", "pipx"]:
            try:
                pipx = _bootstrap_pipx(command[0])
                retry_command = [pipx, "install", "--python", command[0], "--force", REPO_SPEC]
                subprocess.run(retry_command, check=True)
            except subprocess.CalledProcessError as retry_exc:
                print(f"Update failed with exit code {retry_exc.returncode}.", file=sys.stderr)
                return retry_exc.returncode or 1
        else:
            print(f"Update failed with exit code {exc.returncode}.", file=sys.stderr)
            return exc.returncode or 1
    print(f"{APP_NAME} updated. Run `{APP_NAME}` again to use the latest version.")
    return 0


def _installed_git_commit() -> Optional[str]:
    try:
        distribution = importlib.metadata.distribution(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None

    for file in distribution.files or []:
        if str(file).endswith("direct_url.json"):
            try:
                data = json.loads(distribution.locate_file(file).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            vcs_info = data.get("vcs_info", {})
            commit = vcs_info.get("commit_id")
            return commit if isinstance(commit, str) else None
    return None


def _latest_git_commit(timeout: float = 3.0) -> Optional[str]:
    git = shutil.which("git")
    if not git:
        return None
    try:
        result = subprocess.run(
            [git, "ls-remote", REPO_URL, "HEAD"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.strip().split()
    return fields[0] if fields else None


def check_for_update() -> UpdateCheck:
    """Best-effort update check for pipx installs from GitHub."""
    if os.environ.get("ENVGUARD_SKIP_UPDATE_CHECK"):
        return UpdateCheck(available=False)
    current_commit = _installed_git_commit()
    latest_commit = _latest_git_commit()
    if not current_commit or not latest_commit:
        return UpdateCheck(False, current_commit, latest_commit)
    return UpdateCheck(current_commit != latest_commit, current_commit, latest_commit)


def prompt_for_update_if_available() -> bool:
    """Prompt in interactive flows. Return True when an update was attempted."""
    if Confirm is None:
        return False
    check = check_for_update()
    if not check.available:
        return False
    if Confirm.ask(f"New {APP_NAME} update found. Update now?", default=False):
        run_update()
        return True
    return False


def build_details_command(raw_argv: List[str]) -> str:
    """Build the command that repeats the current rich report with details enabled."""
    args = list(raw_argv)
    if "--details" not in args:
        args.append("--details")
    return "envguard " + " ".join(shlex.quote(arg) for arg in args)


# ─── CLI Entry Point ───────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="envguard — environment variable dead-key detector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  envguard                          Guided audit on interactive terminals\n"
            "  envguard wizard                   Build the right command interactively\n"
            "  envguard apps/web                  Scan a specific project\n"
            "  envguard doctor                    Show secret readiness matrix\n"
            "  envguard ci                        GitHub Actions annotations\n"
            "  envguard ci-template               Print a GitHub Actions workflow\n"
            "  envguard supabase xyz              Compare with Supabase secrets\n"
            "  envguard init                      Write [tool.envguard] defaults\n"
            "  envguard update                    Update envguard from GitHub\n"
            "  envguard --json                    Machine-readable output\n"
            "  envguard --summary                 One-line terminal summary\n"
            "  envguard --baseline .envguard-baseline.json\n"
            "                                    Suppress known findings\n"
            "  envguard --details                 Show detailed issue tables\n"
            "  envguard --no-wizard               Scan current directory immediately\n"
            "  envguard --fix                     Interactive fix mode\n"
        ),
    )
    parser.add_argument(
        "tokens",
        nargs="*",
        help=(
            "Optional project path or preset: wizard, ci, ci-template, "
            "doctor, matrix, supabase <project-ref>, init, update"
        ),
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
        "--summary",
        action="store_true",
        help="Output one compact terminal summary line",
    )
    parser.add_argument(
        "--github-annotations",
        action="store_true",
        help="Output GitHub Actions annotations for CI logs",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Interactively prune unused entries from a template dotenv file",
    )
    parser.add_argument(
        "--fix-dry-run",
        action="store_true",
        help="Preview unused dotenv entries that --fix would prune without writing files",
    )
    parser.add_argument(
        "--fix-real-env",
        action="store_true",
        help=(
            "Allow --fix to edit a real .env file instead of only "
            ".env.example/sample/template files"
        ),
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
        help="Path to dotenv file (default: auto-detect .env.example/.env.sample/.env)",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default=None,
        help="Path to an envguard baseline JSON file with known findings to suppress",
    )
    parser.add_argument(
        "--write-baseline",
        type=str,
        default=None,
        help="Write current findings to a secret-safe baseline JSON file and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print debug info (detected references, etc.)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Show detailed issue tables with references in rich output",
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
        "--optional",
        action="append",
        default=[],
        metavar="KEY",
        help="Mark a missing key as optional/defaulted. Can be repeated.",
    )
    parser.add_argument(
        "--external",
        action="append",
        default=[],
        metavar="KEY",
        help="Mark a missing key as owned by another runtime/container. Can be repeated.",
    )
    parser.add_argument(
        "--ignore-missing",
        action="append",
        default=[],
        metavar="KEY",
        help="Ignore a missing key entirely. Can be repeated.",
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
    parser.add_argument(
        "--no-wizard",
        action="store_true",
        help="Run the default current-directory scan instead of the interactive guide",
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
        elif first in {"doctor", "matrix"}:
            args.command = "doctor"
            if len(tokens) > 2:
                parser.error(f"{first} accepts at most one project path")
            if len(tokens) == 2:
                args.path = tokens[1]
        elif first == "ci-template":
            args.command = "ci-template"
            if len(tokens) > 2:
                parser.error("ci-template accepts at most one project path")
            if len(tokens) == 2:
                args.path = tokens[1]
        elif first == "wizard":
            args.command = "wizard"
            if len(tokens) > 1:
                parser.error("wizard does not accept extra arguments")
        elif first == "update":
            args.command = "update"
            if len(tokens) > 1:
                parser.error("update does not accept extra arguments")
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
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_cli_args(argv)

    if args.command == "update":
        status = run_update()
        if status:
            sys.exit(status)
        return

    if args.command == "wizard" or (
        not raw_argv
        and not args.no_wizard
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        if prompt_for_update_if_available():
            return
        run_wizard()
        return

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

    if args.command == "ci-template":
        print(build_ci_template(scan_path), end="")
        return

    config = load_project_config(scan_path)

    # Determine dotenv path
    if args.dotenv:
        dotenv_path = Path(args.dotenv).resolve()
    else:
        detected_dotenv = discover_dotenv_path(scan_path, config)
        dotenv_path = detected_dotenv.resolve() if detected_dotenv else scan_path / ".env.example"

    exclude_patterns = [*config.exclude, *args.exclude]
    explicit_supabase_project = args.supabase_project is not None
    supabase_project = args.supabase_project or detect_supabase_project_ref(
        scan_path,
        os.environ,
        config,
    )
    token_info = detect_supabase_access_token(scan_path, dotenv_path, os.environ)
    supabase_access_token = token_info[0] if token_info else None
    doctor_notes: List[str] = []

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
        if not supabase_access_token:
            if explicit_supabase_project and args.command != "doctor":
                print(
                    "Error: SUPABASE_ACCESS_TOKEN environment variable is required "
                    "when using --supabase-project. You can also place it in .env "
                    "or enter it through envguard wizard.",
                    file=sys.stderr,
                )
                sys.exit(1)
            doctor_notes.append(
                "Supabase project detected, but SUPABASE_ACCESS_TOKEN was not available; "
                "Supabase availability was not checked."
            )
            if args.debug and has_supabase_edge_functions(scan_path):
                print(
                    "[debug] Supabase project detected, but SUPABASE_ACCESS_TOKEN "
                    "is not set; skipping remote secrets."
                )
        elif explicit_supabase_project or should_auto_fetch_supabase(
            scan_path,
            supabase_project,
            {"SUPABASE_ACCESS_TOKEN": supabase_access_token},
        ):
            if args.debug:
                print(f"[debug] Fetching secrets for Supabase project: {supabase_project}")
            supabase_keys = fetch_supabase_secrets(supabase_project, supabase_access_token)
        elif args.debug:
            print(
                f"[debug] Supabase project detected ({supabase_project}), but no "
                "local Edge Functions were found; skipping remote secrets."
            )
        if args.debug and supabase_keys is not None:
            print(f"[debug] Supabase secrets ({len(supabase_keys)}): {supabase_keys}")
            print()

    if args.command == "doctor":
        matrix = build_secrets_matrix(
            ref_map,
            dotenv_keys,
            os.environ,
            supabase_keys,
            optional_keys=config.optional + args.optional,
            external_keys=config.external + args.external,
            ignore_keys=config.ignore_missing + args.ignore_missing,
            dotenv_path=dotenv_path if dotenv_path.exists() else None,
            supabase_project=supabase_project,
            notes=doctor_notes,
        )
        _matrix_output(matrix, json_output=args.json)
        if secrets_matrix_has_required_missing(matrix) and not args.allow_missing:
            sys.exit(1)
        return

    # ── Analyze ────────────────────────────────────────────────────────────
    result = analyze(
        ref_map,
        dotenv_keys,
        supabase_keys,
        optional_keys=config.optional + args.optional,
        external_keys=config.external + args.external,
        ignore_keys=config.ignore_missing + args.ignore_missing,
    )

    if args.write_baseline:
        output_baseline_path = Path(args.write_baseline).resolve()
        write_baseline(output_baseline_path, result)
        print(f"Wrote envguard baseline to {output_baseline_path}")
        return

    baseline_path: Optional[Path] = None
    baseline_suppressed: dict[str, int] = {}
    if args.baseline or config.baseline:
        baseline_path = (
            Path(args.baseline).resolve()
            if args.baseline
            else _resolve_config_path(config.baseline or "", scan_path).resolve()
        )
        baseline_suppressed = apply_baseline(result, load_baseline(baseline_path))

    # ── Output ─────────────────────────────────────────────────────────────
    if args.github_annotations:
        _github_annotations_output(result)
    elif args.json:
        _json_output(
            result,
            allow_unused=args.allow_unused,
            allow_missing=args.allow_missing,
            baseline_path=baseline_path,
            baseline_suppressed=baseline_suppressed,
        )
    elif args.summary:
        _summary_output(
            result,
            allow_unused=args.allow_unused,
            allow_missing=args.allow_missing,
            baseline_suppressed=baseline_suppressed,
        )
    else:
        if Console is None:
            print("For prettier output, install rich: pip install rich", file=sys.stderr)
            _json_output(
                result,
                allow_unused=args.allow_unused,
                allow_missing=args.allow_missing,
                baseline_path=baseline_path,
                baseline_suppressed=baseline_suppressed,
            )
        else:
            _rich_output(
                result,
                dotenv_path if dotenv_path.exists() else None,
                supabase_project,
                show_details=args.details,
                details_command=build_details_command(raw_argv),
                baseline_path=baseline_path,
                baseline_suppressed=baseline_suppressed,
            )

    # ── Interactive fix ────────────────────────────────────────────────────
    if (args.fix or args.fix_dry_run) and dotenv_path.exists():
        interactive_fix(
            result,
            dotenv_path,
            dry_run=args.fix_dry_run,
            allow_real_env=args.fix_real_env,
        )

    # Exit code: non-zero if any issues found
    if has_blocking_issues(
        result,
        allow_unused=args.allow_unused,
        allow_missing=args.allow_missing,
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
