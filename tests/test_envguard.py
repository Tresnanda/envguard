import subprocess
from pathlib import Path

import pytest

import envguard


def test_parse_dotenv_example_reads_keys_and_ignores_comments(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env.example"
    dotenv.write_text(
        "\n".join(
            [
                "# Database",
                "DATABASE_URL=postgres://localhost",
                "export SUPABASE_URL=https://example.supabase.co",
                "API_KEY=",
                "BARE_SECRET",
                "",
                "INVALID-KEY=value",
            ]
        ),
        encoding="utf-8",
    )

    assert envguard.parse_dotenv_example(dotenv) == [
        "DATABASE_URL",
        "SUPABASE_URL",
        "API_KEY",
        "BARE_SECRET",
    ]


def test_parse_dotenv_value_reads_secret_values_without_printing(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "# Local secrets",
                "DATABASE_URL=postgres://localhost",
                'export SUPABASE_ACCESS_TOKEN="sbp_local_token"',
            ]
        ),
        encoding="utf-8",
    )

    assert envguard.parse_dotenv_value(dotenv, "SUPABASE_ACCESS_TOKEN") == "sbp_local_token"
    assert envguard.parse_dotenv_value(dotenv, "MISSING") is None


def test_detect_references_finds_common_environment_patterns(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        "\n".join(
            [
                "import os",
                "database_url = os.getenv('DATABASE_URL')",
                "secret = os.environ['SECRET_KEY']",
                "debug = os.environ.get('DEBUG')",
            ]
        ),
        encoding="utf-8",
    )

    refs = envguard.detect_references(source)

    assert {(ref.key, ref.line, ref.pattern_type) for ref in refs} == {
        ("DATABASE_URL", 2, "os.getenv"),
        ("SECRET_KEY", 3, "os.environ[]"),
        ("DEBUG", 4, "os.environ.get"),
    }



def test_detect_references_ignores_js_interpolation_and_dollar_properties(
    tmp_path: Path,
) -> None:
    source = tmp_path / "app.tsx"
    source.write_text(
        "\n".join(
            [
                "const real = process.env.REAL_ENV;",
                'const url = `/api/${path}/${userId}`;',
                "const obj = { $client: { id: 1 } };",
                "console.log(obj.$client, url, real);",
            ]
        ),
        encoding="utf-8",
    )

    refs = envguard.detect_references(source)

    assert {(ref.key, ref.pattern_type) for ref in refs} == {
        ("REAL_ENV", "process.env.KEY")
    }


def test_detect_references_finds_modern_js_framework_env_patterns(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config.ts"
    source.write_text(
        "\n".join(
            [
                "import {",
                "  PRIVATE_API_KEY,",
                "  PUBLIC_BASE_URL as baseUrl,",
                "} from '$env/static/private';",
                "import { env as privateEnv } from '$env/dynamic/private';",
                "const vite = import.meta.env.VITE_API_URL;",
                "const secret = import.meta.env['SERVER_SECRET'];",
                "const dynamic = privateEnv.RUNTIME_SECRET;",
            ]
        ),
        encoding="utf-8",
    )

    refs = envguard.detect_references(source)

    assert {(ref.key, ref.pattern_type) for ref in refs} == {
        ("PRIVATE_API_KEY", "$env/static import"),
        ("PUBLIC_BASE_URL", "$env/static import"),
        ("VITE_API_URL", "import.meta.env.KEY"),
        ("SERVER_SECRET", 'import.meta.env["KEY"]'),
        ("RUNTIME_SECRET", "$env/dynamic.KEY"),
    }


def test_detect_references_finds_zod_process_env_schema_keys(tmp_path: Path) -> None:
    source = tmp_path / "config.ts"
    source.write_text(
        "\n".join(
            [
                'import { z } from "zod";',
                "const envSchema = z.object({",
                "  NODE_ENV: z.string(),",
                "  PORT: z.coerce.number(),",
                "  DATABASE_URL: z.string(),",
                "  API_KEY: z.string().optional(),",
                "});",
                "export function loadConfig(rawEnv: NodeJS.ProcessEnv) {",
                "  return envSchema.parse(rawEnv);",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    refs = envguard.detect_references(source)

    assert {ref.key for ref in refs} == {"NODE_ENV", "PORT", "DATABASE_URL", "API_KEY"}
    assert {ref.pattern_type for ref in refs} == {"zod process.env schema"}




def test_zod_process_env_schema_detection_ignores_unrelated_schemas(
    tmp_path: Path,
) -> None:
    source = tmp_path / "config.ts"
    source.write_text(
        "\n".join(
            [
                'import { z } from "zod";',
                "const userSchema = z.object({",
                "  USER_ID: z.string(),",
                "  USER_NAME: z.string(),",
                "});",
                "const envSchema = z.object({",
                "  DATABASE_URL: z.string(),",
                "  API_KEY: z.string(),",
                "});",
                "export function loadConfig(rawEnv: NodeJS.ProcessEnv) {",
                "  return envSchema.safeParse(rawEnv);",
                "}",
            ]
        ),
        encoding="utf-8",
    )

    refs = envguard.detect_references(source)

    assert {ref.key for ref in refs} == {"DATABASE_URL", "API_KEY"}


def test_detect_references_finds_multilanguage_env_apis(tmp_path: Path) -> None:
    cases = {
        "app.rb": (
            'db = ENV.fetch("DATABASE_URL")\nenv = ENV["RAILS_ENV"]\n',
            {"DATABASE_URL", "RAILS_ENV"},
        ),
        "main.go": (
            'package main\nimport "os"\n'
            'func main(){ _ = os.Getenv("DATABASE_URL"); _ = os.LookupEnv("PORT") }\n',
            {"DATABASE_URL", "PORT"},
        ),
        "main.rs": (
            'fn main(){ let _ = std::env::var("DATABASE_URL"); '
            'let _ = std::env::var_os("RUST_LOG"); }\n',
            {"DATABASE_URL", "RUST_LOG"},
        ),
        "index.php": (
            '<?php $env = getenv("APP_ENV"); $db = $_ENV["DATABASE_URL"]; ?>\n',
            {"APP_ENV", "DATABASE_URL"},
        ),
        "App.java": (
            'class App { void run(){ System.getenv("JAVA_ENV"); '
            'System.getenv().get("DATABASE_URL"); } }\n',
            {"JAVA_ENV", "DATABASE_URL"},
        ),
    }

    for filename, (content, expected) in cases.items():
        source = tmp_path / filename
        source.write_text(content, encoding="utf-8")

        refs = envguard.detect_references(source)

        assert {ref.key for ref in refs} == expected


def test_shell_scanner_handles_defaults_but_does_not_scan_github_actions_secrets(
    tmp_path: Path,
) -> None:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "\n".join(
            [
                "services:",
                "  db:",
                "    environment:",
                "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-dev}",
                "      REDIS_URL: ${REDIS_URL}",
            ]
        ),
        encoding="utf-8",
    )
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "\n".join(
            [
                "env:",
                "  NPM_TOKEN: ${{ secrets.NPM_TOKEN }}",
                "jobs:",
                "  deploy:",
                "    steps:",
                "      - run: echo \"$DEPLOY_TOKEN ${API_URL}\"",
            ]
        ),
        encoding="utf-8",
    )

    compose_refs = envguard.detect_references(compose)
    workflow_refs = envguard.detect_references(workflow)

    assert {ref.key for ref in compose_refs} == {"POSTGRES_PASSWORD", "REDIS_URL"}
    assert {(ref.key, ref.pattern_type) for ref in workflow_refs} == {
        ("NPM_TOKEN", "github-actions secrets.KEY"),
        ("DEPLOY_TOKEN", "$KEY"),
        ("API_URL", "${KEY}"),
    }


def test_scan_directory_skips_generated_mobile_and_bundle_noise(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text(
        "const value = process.env.APP_SECRET;\n",
        encoding="utf-8",
    )
    generated_files = [
        tmp_path / ".expo" / "dev" / "logs" / "start.log",
        tmp_path / "ios" / "Pods" / "Headers" / "Generated.hpp",
        tmp_path / "assets" / "codemirror" / "cm.bundle.js.txt",
        tmp_path / "docs" / "plan.md",
    ]
    for generated in generated_files:
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("`${fake}` $noise %MORE_NOISE%\n", encoding="utf-8")

    refs = envguard.scan_directory(tmp_path)

    assert set(refs) == {"APP_SECRET"}


def test_scan_directory_skips_virtualenv_and_cache_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "import os\nvalue = os.getenv('APP_SECRET')\n",
        encoding="utf-8",
    )
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text(
        "import os\nvalue = os.getenv('VENV_SECRET')\n",
        encoding="utf-8",
    )
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "ignored.py").write_text(
        "import os\nvalue = os.getenv('CACHE_SECRET')\n",
        encoding="utf-8",
    )

    refs = envguard.scan_directory(tmp_path)

    assert set(refs) == {"APP_SECRET"}


def test_scan_directory_honors_user_exclude_globs(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "import os\nvalue = os.getenv('APP_SECRET')\n",
        encoding="utf-8",
    )
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "snapshot.py").write_text(
        "import os\nvalue = os.getenv('FIXTURE_SECRET')\n",
        encoding="utf-8",
    )

    refs = envguard.scan_directory(tmp_path, exclude_patterns=["fixtures/**"])

    assert set(refs) == {"APP_SECRET"}


def test_analyze_treats_supabase_secrets_as_available_configuration() -> None:
    ref_map = {
        "DATABASE_URL": [
            envguard.EnvReference(
                key="DATABASE_URL",
                file="app.py",
                line=10,
                pattern_type="os.getenv",
            )
        ],
        "SUPABASE_SERVICE_ROLE_KEY": [
            envguard.EnvReference(
                key="SUPABASE_SERVICE_ROLE_KEY",
                file="supabase/functions/index.ts",
                line=3,
                pattern_type="Deno.env.get",
            )
        ],
    }

    result = envguard.analyze(
        ref_map=ref_map,
        dotenv_keys=["DATABASE_URL"],
        supabase_keys=["SUPABASE_SERVICE_ROLE_KEY", "LEGACY_SECRET"],
    )

    assert result.missing == []
    assert result.unused == []
    assert result.supabase_orphans == ["LEGACY_SECRET"]


def test_detect_references_classifies_optional_defaults_and_embedded_runtime_context(
    tmp_path: Path,
) -> None:
    source = tmp_path / "harness.mjs"
    source.write_text(
        "\n".join(
            [
                "const required = process.env.REQUIRED_SECRET;",
                "const apiId = parseInt(process.env.TELEGRAM_API_ID || '', 10);",
                "const apiHash = process.env.TELEGRAM_API_HASH || '';",
                "const model = process.env.DRIVER_MODEL || 'deepseek/deepseek-v4-pro';",
                "const headless = process.env.PLAYWRIGHT_HEADLESS !== 'false';",
                "const count = parseInt(process.env.FAN_SIM_PAUSE_MS || '5000', 10);",
                "const inner = `",
                (
                    "const s = createClient(process.env.SUPABASE_URL, "
                    "process.env.SUPABASE_SERVICE_ROLE_KEY);"
                ),
                "`;",
                "execSync(`cat ${tmp} | ssh host \"docker exec -i app node -\"`);",
            ]
        ),
        encoding="utf-8",
    )

    refs = {ref.key: ref for ref in envguard.detect_references(source)}

    assert refs["REQUIRED_SECRET"].requirement == "required"
    assert refs["TELEGRAM_API_ID"].requirement == "required"
    assert refs["TELEGRAM_API_HASH"].requirement == "required"
    assert refs["DRIVER_MODEL"].requirement == "optional"
    assert refs["PLAYWRIGHT_HEADLESS"].requirement == "optional"
    assert refs["FAN_SIM_PAUSE_MS"].requirement == "optional"
    assert refs["SUPABASE_URL"].requirement == "external"
    assert refs["SUPABASE_SERVICE_ROLE_KEY"].requirement == "external"


def test_analyze_only_blocks_required_missing_keys() -> None:
    ref_map = {
        "REQUIRED_SECRET": [
            envguard.EnvReference(
                key="REQUIRED_SECRET",
                file="app.mjs",
                line=1,
                pattern_type="process.env.KEY",
                requirement="required",
            )
        ],
        "DRIVER_MODEL": [
            envguard.EnvReference(
                key="DRIVER_MODEL",
                file="app.mjs",
                line=2,
                pattern_type="process.env.KEY",
                requirement="optional",
            )
        ],
        "SUPABASE_URL": [
            envguard.EnvReference(
                key="SUPABASE_URL",
                file="app.mjs",
                line=3,
                pattern_type="process.env.KEY",
                requirement="external",
            )
        ],
    }

    result = envguard.analyze(ref_map=ref_map, dotenv_keys=[])

    assert result.missing == ["REQUIRED_SECRET"]
    assert result.optional_missing == ["DRIVER_MODEL"]
    assert result.external_missing == ["SUPABASE_URL"]
    assert envguard.has_blocking_issues(result, allow_unused=False, allow_missing=False)

    satisfied = envguard.analyze(ref_map=ref_map, dotenv_keys=["REQUIRED_SECRET"])
    assert satisfied.missing == []
    assert satisfied.optional_missing == ["DRIVER_MODEL"]
    assert satisfied.external_missing == ["SUPABASE_URL"]
    assert not envguard.has_blocking_issues(
        satisfied,
        allow_unused=False,
        allow_missing=False,
    )


def test_load_project_config_reads_pyproject_tool_envguard(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.envguard]",
                'dotenv = "config/example.env"',
                'exclude = ["fixtures/**", "snapshots/**"]',
                'supabase_project = "abcd1234"',
                'optional = ["CLI_DEFAULT_BOT"]',
                'external = ["REMOTE_CONTAINER_SECRET"]',
                'ignore_missing = ["LEGACY_FLAG"]',
            ]
        ),
        encoding="utf-8",
    )

    config = envguard.load_project_config(tmp_path)

    assert config.dotenv == "config/example.env"
    assert config.exclude == ["fixtures/**", "snapshots/**"]
    assert config.supabase_project == "abcd1234"
    assert config.optional == ["CLI_DEFAULT_BOT"]
    assert config.external == ["REMOTE_CONTAINER_SECRET"]
    assert config.ignore_missing == ["LEGACY_FLAG"]


def test_analyze_honors_project_level_requirement_overrides() -> None:
    ref_map = {
        "CLI_DEFAULT_BOT": [
            envguard.EnvReference(
                key="CLI_DEFAULT_BOT",
                file="cli.mjs",
                line=1,
                pattern_type="process.env.KEY",
            )
        ],
        "REMOTE_CONTAINER_SECRET": [
            envguard.EnvReference(
                key="REMOTE_CONTAINER_SECRET",
                file="cli.mjs",
                line=2,
                pattern_type="process.env.KEY",
            )
        ],
        "LEGACY_FLAG": [
            envguard.EnvReference(
                key="LEGACY_FLAG",
                file="cli.mjs",
                line=3,
                pattern_type="process.env.KEY",
            )
        ],
    }

    result = envguard.analyze(
        ref_map=ref_map,
        dotenv_keys=[],
        optional_keys=["CLI_DEFAULT_BOT"],
        external_keys=["REMOTE_CONTAINER_SECRET"],
        ignore_keys=["LEGACY_FLAG"],
    )

    assert result.missing == []
    assert result.optional_missing == ["CLI_DEFAULT_BOT"]
    assert result.external_missing == ["REMOTE_CONTAINER_SECRET"]
    assert result.ignored_missing == ["LEGACY_FLAG"]


def test_github_annotations_include_file_line_and_messages() -> None:
    result = envguard.ScanResult(
        references={
            "MISSING_KEY": [
                envguard.EnvReference(
                    key="MISSING_KEY",
                    file="/repo/src/app.py",
                    line=12,
                    pattern_type="os.getenv",
                )
            ]
        },
        unused=["OLD_KEY"],
        missing=["MISSING_KEY"],
        supabase_orphans=["LEGACY_SECRET"],
    )

    annotations = envguard.build_github_annotations(result)

    assert annotations == [
        "::error file=/repo/src/app.py,line=12::Missing environment variable MISSING_KEY",
        "::warning::Unused environment variable OLD_KEY",
        "::warning::Orphaned Supabase secret LEGACY_SECRET",
    ]


def test_rich_output_hides_missing_reference_table_by_default(capsys) -> None:
    result = envguard.ScanResult(
        references={
            "MISSING_KEY": [
                envguard.EnvReference(
                    key="MISSING_KEY",
                    file="/repo/src/app.py",
                    line=12,
                    pattern_type="os.getenv",
                )
            ]
        },
        missing=["MISSING_KEY"],
    )

    envguard._rich_output(
        result,
        dotenv_path=None,
        supabase_ref=None,
        details_command="envguard apps/web --details",
    )

    out = capsys.readouterr().out
    assert "1 missing key detected" in out
    assert "envguard apps/web --details" in out
    assert "MISSING — Keys referenced in code but not in config" not in out
    assert "/repo/src/app.py:12" not in out


def test_rich_output_can_show_missing_reference_table(capsys) -> None:
    result = envguard.ScanResult(
        references={
            "MISSING_KEY": [
                envguard.EnvReference(
                    key="MISSING_KEY",
                    file="/repo/src/app.py",
                    line=12,
                    pattern_type="os.getenv",
                )
            ]
        },
        missing=["MISSING_KEY"],
    )

    envguard._rich_output(
        result,
        dotenv_path=None,
        supabase_ref=None,
        show_details=True,
    )

    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "/repo/src/app.py:12" in out


def test_allow_flags_control_blocking_issue_detection() -> None:
    result = envguard.ScanResult(
        unused=["OLD_KEY"],
        missing=["MISSING_KEY"],
        supabase_orphans=["LEGACY_SECRET"],
    )

    assert envguard.has_blocking_issues(result, allow_unused=False, allow_missing=False) is True
    assert envguard.has_blocking_issues(result, allow_unused=True, allow_missing=False) is True
    assert envguard.has_blocking_issues(result, allow_unused=False, allow_missing=True) is True
    assert envguard.has_blocking_issues(result, allow_unused=True, allow_missing=True) is False


def test_parse_cli_args_accepts_positional_path_and_presets() -> None:
    positional = envguard.parse_cli_args(["apps/web"])
    assert positional.path == "apps/web"

    ci = envguard.parse_cli_args(["ci", "apps/web"])
    assert ci.path == "apps/web"
    assert ci.github_annotations is True

    supabase = envguard.parse_cli_args(["supabase", "abcd1234"])
    assert supabase.supabase_project == "abcd1234"


def test_parse_cli_args_accepts_update_command() -> None:
    args = envguard.parse_cli_args(["update"])

    assert args.command == "update"


def test_build_details_command_adds_details_to_current_command() -> None:
    assert envguard.build_details_command(["apps/web", "--exclude", "dist/**"]) == (
        "envguard apps/web --exclude 'dist/**' --details"
    )


def test_run_update_uses_pipx_force_install(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(envguard.shutil, "which", lambda name: "/usr/local/bin/pipx")

    def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(envguard.subprocess, "run", fake_run)

    assert envguard.run_update() == 0
    assert calls == [
        [
            "/usr/local/bin/pipx",
            "install",
            "--force",
            "git+https://github.com/Tresnanda/envguard.git",
        ]
    ]


def test_run_update_bootstraps_pipx_with_host_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    install_cmd = [
        "/usr/bin/python3",
        "-m",
        "pipx",
        "install",
        "--force",
        "git+https://github.com/Tresnanda/envguard.git",
    ]

    def fake_which(name: str) -> str | None:
        return "/usr/bin/python3" if name == "python3" else None

    def fake_exists(self: Path) -> bool:
        return False

    def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd == install_cmd and calls.count(cmd) == 1:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(envguard.shutil, "which", fake_which)
    monkeypatch.setattr(envguard.Path, "exists", fake_exists)
    monkeypatch.setattr(envguard.subprocess, "run", fake_run)

    assert envguard.run_update() == 0
    assert calls == [
        install_cmd,
        ["/usr/bin/python3", "-m", "pip", "install", "--user", "pipx"],
        ["/usr/bin/python3", "-m", "pipx", "ensurepath"],
        install_cmd,
    ]


def test_write_project_config_creates_envguard_defaults(tmp_path: Path) -> None:
    envguard.write_project_config(
        tmp_path,
        dotenv="config/example.env",
        exclude=["fixtures/**"],
        supabase_project="abcd1234",
    )

    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == "\n".join(
        [
            "[tool.envguard]",
            'dotenv = "config/example.env"',
            'exclude = ["fixtures/**"]',
            'supabase_project = "abcd1234"',
            "",
        ]
    )


def test_discover_dotenv_path_prefers_templates_before_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / ".env.sample").write_text("SECRET=\n", encoding="utf-8")

    detected = envguard.discover_dotenv_path(tmp_path, envguard.EnvguardConfig())

    assert detected == tmp_path / ".env.sample"


def test_detect_supabase_project_ref_reads_config_and_environment(
    tmp_path: Path,
) -> None:
    (tmp_path / "supabase").mkdir()
    (tmp_path / "supabase" / "config.toml").write_text(
        'project_id = "local-ref"\n',
        encoding="utf-8",
    )

    detected = envguard.detect_supabase_project_ref(
        tmp_path,
        env={},
        config=envguard.EnvguardConfig(),
    )
    fallback = envguard.detect_supabase_project_ref(
        tmp_path / "missing",
        env={"SUPABASE_PROJECT_REF": "env-ref"},
        config=envguard.EnvguardConfig(),
    )

    assert detected == "local-ref"
    assert fallback == "env-ref"


def test_should_auto_fetch_supabase_requires_ref_token_and_edge_functions(
    tmp_path: Path,
) -> None:
    (tmp_path / "supabase" / "functions").mkdir(parents=True)

    assert envguard.should_auto_fetch_supabase(
        tmp_path,
        "project-ref",
        {"SUPABASE_ACCESS_TOKEN": "token"},
    )
    assert not envguard.should_auto_fetch_supabase(tmp_path, "project-ref", {})
    assert not envguard.should_auto_fetch_supabase(
        tmp_path,
        None,
        {"SUPABASE_ACCESS_TOKEN": "token"},
    )


def test_detect_supabase_access_token_prefers_shell_then_dotenv(
    tmp_path: Path,
) -> None:
    selected_dotenv = tmp_path / ".env.example"
    selected_dotenv.write_text("SUPABASE_ACCESS_TOKEN=sbp_selected\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SUPABASE_ACCESS_TOKEN=sbp_local\n", encoding="utf-8")

    shell_token = envguard.detect_supabase_access_token(
        tmp_path,
        selected_dotenv,
        {"SUPABASE_ACCESS_TOKEN": "sbp_shell"},
    )
    selected_token = envguard.detect_supabase_access_token(tmp_path, selected_dotenv, {})
    local_token = envguard.detect_supabase_access_token(tmp_path, tmp_path / ".env.missing", {})

    assert shell_token == ("sbp_shell", "environment")
    assert selected_token == ("sbp_selected", str(selected_dotenv))
    assert local_token == ("sbp_local", str(tmp_path / ".env"))


def test_run_wizard_uses_direct_token_only_for_current_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".env.example").write_text("DATABASE_URL=\n", encoding="utf-8")
    (tmp_path / "supabase" / "functions").mkdir(parents=True)

    answers = iter(
        [
            str(tmp_path),
            str(tmp_path / ".env.example"),
            "project-ref",
            "sbp_direct_token",
        ]
    )
    confirms = iter([True, False, False, True])
    captured: dict[str, object] = {}

    def fake_main(args: list[str]) -> None:
        captured["args"] = args
        captured["token"] = envguard.os.environ.get("SUPABASE_ACCESS_TOKEN")

    monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(envguard, "_ask_text", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(envguard, "_ask_secret", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(envguard, "_ask_confirm", lambda *_args, **_kwargs: next(confirms))
    monkeypatch.setattr(
        envguard,
        "detect_supabase_project_ref",
        lambda *_args, **_kwargs: "project-ref",
    )
    monkeypatch.setattr(envguard, "main", fake_main)

    envguard.run_wizard()

    assert captured["args"] == [
        "--path",
        str(tmp_path.resolve()),
        "--dotenv",
        str(tmp_path / ".env.example"),
        "--supabase-project",
        "project-ref",
    ]
    assert captured["token"] == "sbp_direct_token"
    assert envguard.os.environ.get("SUPABASE_ACCESS_TOKEN") is None
    assert "sbp_direct_token" not in capsys.readouterr().out


def test_parse_cli_args_accepts_wizard_command() -> None:
    args = envguard.parse_cli_args(["wizard"])

    assert args.command == "wizard"


def test_build_wizard_args_uses_detected_defaults(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env.example"
    dotenv.write_text("DATABASE_URL=\n", encoding="utf-8")

    args = envguard.build_wizard_args(
        {
            "path": str(tmp_path),
            "dotenv": str(dotenv),
            "use_supabase": True,
            "supabase_project": "project-ref",
            "github_annotations": False,
            "fix": False,
        }
    )

    assert args == [
        "--path",
        str(tmp_path),
        "--dotenv",
        str(dotenv),
        "--supabase-project",
        "project-ref",
    ]


def test_main_opens_wizard_for_bare_interactive_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"wizard": False}

    class Tty:
        def isatty(self) -> bool:
            return True

    def fake_wizard() -> None:
        called["wizard"] = True

    monkeypatch.setattr(envguard.sys, "stdin", Tty())
    monkeypatch.setattr(envguard.sys, "stdout", Tty())
    monkeypatch.setattr(envguard, "run_wizard", fake_wizard)

    envguard.main([])

    assert called["wizard"] is True
