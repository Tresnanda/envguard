import json
import os
import subprocess
from pathlib import Path
from typing import Optional

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


def test_parse_dotenv_value_handles_real_dotenv_quotes_and_comments(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "SUPABASE_ACCESS_TOKEN = \"sbp_local_token\" # local dev token",
                "PASSWORD='abc#123' # keep hash inside quotes",
                "PUBLIC_URL=https://example.test/path#section",
                "EMPTY_VALUE=",
            ]
        ),
        encoding="utf-8",
    )

    assert envguard.parse_dotenv_value(dotenv, "SUPABASE_ACCESS_TOKEN") == "sbp_local_token"
    assert envguard.parse_dotenv_value(dotenv, "PASSWORD") == "abc#123"
    assert envguard.parse_dotenv_value(dotenv, "PUBLIC_URL") == "https://example.test/path#section"
    assert envguard.parse_dotenv_value(dotenv, "EMPTY_VALUE") is None


def test_readme_dotenv_cli_reference_matches_parser_help() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    parser_help = envguard._build_parser().format_help()

    assert "--dotenv DOTENV" in parser_help
    assert "auto-detect" in parser_help
    assert ".env.example/.env.sample/.env" in parser_help
    assert "--dotenv PATH         Path to dotenv file." in readme
    assert "Defaults to auto-detected templates or .env." in readme
    assert "Defaults to <path>/.env.example" not in readme


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


def test_pydantic_field_ellipsis_stays_required(tmp_path: Path) -> None:
    source = tmp_path / "settings.py"
    source.write_text(
        "\n".join(
            [
                "from pydantic import Field",
                "from pydantic_settings import BaseSettings",
                "",
                "class Settings(BaseSettings):",
                "    api_key: str = Field(...)",
                "    token: str = Field(Ellipsis, description='required')",
                "    timeout: int = 30",
            ]
        ),
        encoding="utf-8",
    )

    refs = {ref.key: ref for ref in envguard.detect_references(source)}

    assert refs["API_KEY"].pattern_type == "pydantic BaseSettings"
    assert refs["API_KEY"].requirement == "required"
    assert refs["API_KEY"].reason == "pydantic Field(...) marks key required"
    assert refs["TOKEN"].requirement == "required"
    assert refs["TIMEOUT"].requirement == "optional"


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


def test_detect_references_finds_powershell_env_patterns(tmp_path: Path) -> None:
    source = tmp_path / "install.ps1"
    source.write_text(
        "\n".join(
            [
                "$token = $env:SUPABASE_ACCESS_TOKEN",
                "$url = ${env:SUPABASE_URL}",
                "$dsn = [Environment]::GetEnvironmentVariable(\"DATABASE_URL\")",
                "$api = [System.Environment]::GetEnvironmentVariable('API_KEY')",
            ]
        ),
        encoding="utf-8",
    )

    refs = envguard.detect_references(source)

    assert {(ref.key, ref.line, ref.pattern_type) for ref in refs} == {
        ("SUPABASE_ACCESS_TOKEN", 1, "$env:KEY"),
        ("SUPABASE_URL", 2, "${env:KEY}"),
        ("DATABASE_URL", 3, "[Environment]::GetEnvironmentVariable"),
        ("API_KEY", 4, "[Environment]::GetEnvironmentVariable"),
    }


def test_scan_directory_includes_install_ps1_powershell_env_refs(tmp_path: Path) -> None:
    installer = tmp_path / "install.ps1"
    installer.write_text("$token = $env:SUPABASE_ACCESS_TOKEN\n", encoding="utf-8")

    refs = envguard.scan_directory(tmp_path)

    assert set(refs) == {"SUPABASE_ACCESS_TOKEN"}
    assert refs["SUPABASE_ACCESS_TOKEN"][0].file.endswith("install.ps1")


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


def _mock_supabase_secrets_response(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    status: int = 200,
) -> None:
    import http.client

    body_text = payload if isinstance(payload, str) else json.dumps(payload)

    response_status = status

    class FakeResponse:
        status = response_status

        def read(self) -> bytes:
            return body_text.encode("utf-8")

    class FakeConnection:
        def __init__(self, netloc: str, timeout: int) -> None:
            self.netloc = netloc
            self.timeout = timeout
            self.closed = False

        def request(
            self,
            method: str,
            path: str,
            body: Optional[str] = None,
            headers: Optional[dict[str, str]] = None,
        ) -> None:
            self.method = method
            self.path = path
            self.body = body
            self.headers = headers

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)


def test_fetch_supabase_secrets_ignores_malformed_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_supabase_secrets_response(
        monkeypatch,
        [
            {"name": "API_KEY"},
            {"value": "missing name"},
            {"name": 123},
            ["not", "an", "object"],
            "not an object",
            {"name": "EDGE_SECRET"},
        ],
    )

    assert envguard.fetch_supabase_secrets("project-ref", "token") == [
        "API_KEY",
        "EDGE_SECRET",
    ]


def test_fetch_supabase_secrets_accepts_secrets_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_supabase_secrets_response(
        monkeypatch,
        {"secrets": [{"name": "FIRST_SECRET"}, {"name": None}, {"name": "SECOND_SECRET"}]},
    )

    assert envguard.fetch_supabase_secrets("project-ref", "token") == [
        "FIRST_SECRET",
        "SECOND_SECRET",
    ]


def test_fetch_supabase_secrets_reports_unexpected_top_level_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mock_supabase_secrets_response(monkeypatch, {"data": []})

    with pytest.raises(SystemExit) as exc:
        envguard.fetch_supabase_secrets("project-ref", "token")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Failed to fetch Supabase secrets" in err
    assert "expected a list or an object with a 'secrets' list" in err


def test_fetch_supabase_secrets_reports_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _mock_supabase_secrets_response(monkeypatch, "{not json")

    with pytest.raises(SystemExit) as exc:
        envguard.fetch_supabase_secrets("project-ref", "token")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Failed to fetch Supabase secrets" in err
    assert "Expecting property name enclosed in double quotes" in err


def test_fetch_supabase_secrets_redacts_json_api_error_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_token = "sbp_request_token_123456789"
    body_token = "sbp_response_token_123456789"
    body_api_key = "sk_test_abcdefghijklmnopqrstuvwxyz"
    _mock_supabase_secrets_response(
        monkeypatch,
        {
            "error": "unauthorized",
            "message": f"invalid bearer token Bearer {body_token}",
            "access_token": api_token,
            "details": {
                "api_key": body_api_key,
                "request_id": "req_12345",
            },
        },
        status=401,
    )

    with pytest.raises(SystemExit) as exc:
        envguard.fetch_supabase_secrets("project-ref", api_token)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Supabase API returned 401" in err
    assert '"error":"unauthorized"' in err
    assert '"message":"invalid bearer token Bearer [REDACTED]"' in err
    assert '"access_token":"[REDACTED]"' in err
    assert '"api_key":"[REDACTED]"' in err
    assert '"request_id":"req_12345"' in err
    assert api_token not in err
    assert body_token not in err
    assert body_api_key not in err


def test_fetch_supabase_secrets_redacts_malformed_api_error_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_token = "sbp_raw_response_token_123456789"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signatureX"
    _mock_supabase_secrets_response(
        monkeypatch,
        f"not json: access_token={raw_token}; Authorization: Bearer {jwt}",
        status=500,
    )

    with pytest.raises(SystemExit) as exc:
        envguard.fetch_supabase_secrets("project-ref", raw_token)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Supabase API returned 500" in err
    assert "access_token=[REDACTED]" in err
    assert "Authorization: Bearer [REDACTED]" in err
    assert raw_token not in err
    assert jwt not in err


def test_delete_supabase_secrets_redacts_api_error_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    leaked_secret = "sbp_delete_response_token_123456789"
    leaked_password = "correct-horse-battery-staple"
    _mock_supabase_secrets_response(
        monkeypatch,
        {
            "error": "delete_failed",
            "message": "secret cannot be deleted",
            "secret_value": leaked_secret,
            "password": leaked_password,
        },
        status=403,
    )

    assert not envguard.delete_supabase_secrets(
        "project-ref",
        "sbp_request_token_123456789",
        ["OLD_SECRET"],
    )

    err = capsys.readouterr().err
    assert "Supabase API returned 403" in err
    assert '"error":"delete_failed"' in err
    assert '"message":"secret cannot be deleted"' in err
    assert '"secret_value":"[REDACTED]"' in err
    assert '"password":"[REDACTED]"' in err
    assert leaked_secret not in err
    assert leaked_password not in err


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
                "scan_supabase = false",
                'baseline = ".envguard-baseline.json"',
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
    assert config.scan_supabase is False
    assert config.baseline == ".envguard-baseline.json"
    assert config.optional == ["CLI_DEFAULT_BOT"]
    assert config.external == ["REMOTE_CONTAINER_SECRET"]
    assert config.ignore_missing == ["LEGACY_FLAG"]


def test_scan_supabase_false_excludes_local_functions_and_remote_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.envguard]",
                "scan_supabase = false",
                'supabase_project = "project-ref"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text("APP_SECRET=\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text(
        "const value = process.env.APP_SECRET;\n",
        encoding="utf-8",
    )
    (tmp_path / "supabase" / "functions" / "hello").mkdir(parents=True)
    (tmp_path / "supabase" / "functions" / "hello" / "index.ts").write_text(
        "const edge = Deno.env.get('EDGE_ONLY_SECRET');\n",
        encoding="utf-8",
    )
    fetched = {"called": False}

    def fake_fetch(*_args: object, **_kwargs: object) -> list[str]:
        fetched["called"] = True
        return ["EDGE_ONLY_SECRET"]

    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sbp_test_token")
    monkeypatch.setattr(envguard, "fetch_supabase_secrets", fake_fetch)

    envguard.main(["--json", str(tmp_path)])

    payload = json.loads(capsys.readouterr().out)
    assert fetched["called"] is False
    assert set(payload["references"]) == {"APP_SECRET"}
    assert payload["missing"] == []


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


def test_rich_output_prints_details_command_once_for_multiple_issue_types(capsys) -> None:
    result = envguard.ScanResult(
        references={
            "MISSING_KEY": [
                envguard.EnvReference(
                    key="MISSING_KEY",
                    file="/repo/src/app.py",
                    line=12,
                    pattern_type="os.getenv",
                )
            ],
            "OPTIONAL_KEY": [
                envguard.EnvReference(
                    key="OPTIONAL_KEY",
                    file="/repo/src/app.py",
                    line=13,
                    pattern_type="os.getenv",
                )
            ],
            "EXTERNAL_KEY": [
                envguard.EnvReference(
                    key="EXTERNAL_KEY",
                    file="/repo/src/app.py",
                    line=14,
                    pattern_type="os.getenv",
                )
            ],
        },
        missing=["MISSING_KEY"],
        optional_missing=["OPTIONAL_KEY"],
        external_missing=["EXTERNAL_KEY"],
    )

    envguard._rich_output(
        result,
        dotenv_path=None,
        supabase_ref=None,
        details_command="envguard apps/web --details",
    )

    out = capsys.readouterr().out
    assert out.count("Show details:") == 1
    assert out.count("envguard apps/web --details") == 1


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


def test_baseline_suppresses_known_findings_and_keeps_new_ones() -> None:
    result = envguard.ScanResult(
        unused=["OLD_KEY", "NEW_UNUSED"],
        missing=["KNOWN_MISSING", "NEW_MISSING"],
        optional_missing=["KNOWN_OPTIONAL"],
        supabase_orphans=["KNOWN_ORPHAN"],
    )

    suppressed = envguard.apply_baseline(
        result,
        {
            "unused": {"OLD_KEY"},
            "missing": {"KNOWN_MISSING"},
            "optional_missing": {"KNOWN_OPTIONAL"},
            "external_missing": set(),
            "ignored_missing": set(),
            "supabase_orphans": {"KNOWN_ORPHAN"},
        },
    )

    assert result.unused == ["NEW_UNUSED"]
    assert result.missing == ["NEW_MISSING"]
    assert result.optional_missing == []
    assert result.supabase_orphans == []
    assert suppressed == {
        "unused": 1,
        "missing": 1,
        "optional_missing": 1,
        "supabase_orphans": 1,
    }
    assert envguard.has_blocking_issues(result)


def test_write_and_load_baseline_store_only_key_names(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".envguard-baseline.json"
    result = envguard.ScanResult(
        references={
            "DATABASE_URL": [
                envguard.EnvReference(
                    key="DATABASE_URL",
                    file="/repo/app.py",
                    line=12,
                    pattern_type="os.getenv",
                )
            ]
        },
        unused=["OLD_KEY"],
        missing=["DATABASE_URL"],
    )

    envguard.write_baseline(baseline_path, result)

    raw = baseline_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload == {
        "version": 1,
        "findings": {
            "external_missing": [],
            "ignored_missing": [],
            "missing": ["DATABASE_URL"],
            "optional_missing": [],
            "supabase_orphans": [],
            "unused": ["OLD_KEY"],
        },
    }
    assert "/repo/app.py" not in raw
    assert envguard.load_baseline(baseline_path)["missing"] == {"DATABASE_URL"}


def test_json_output_includes_summary_metadata(capsys) -> None:
    result = envguard.ScanResult(
        references={
            "MISSING_KEY": [
                envguard.EnvReference(
                    key="MISSING_KEY",
                    file="/repo/src/app.py",
                    line=12,
                    pattern_type="os.getenv",
                )
            ],
            "OPTIONAL_KEY": [
                envguard.EnvReference(
                    key="OPTIONAL_KEY",
                    file="/repo/src/app.py",
                    line=13,
                    pattern_type="os.getenv",
                    requirement="optional",
                    reason="inline default or guard",
                )
            ],
        },
        unused=["OLD_KEY"],
        missing=["MISSING_KEY"],
        optional_missing=["OPTIONAL_KEY"],
        external_missing=["EXTERNAL_KEY"],
        ignored_missing=["IGNORED_KEY"],
        supabase_orphans=["LEGACY_SECRET"],
    )

    envguard._json_output(result)
    output = json.loads(capsys.readouterr().out)

    assert output["summary"] == {
        "counts": {
            "unused": 1,
            "missing": 1,
            "optional_missing": 1,
            "external_missing": 1,
            "ignored_missing": 1,
            "supabase_orphans": 1,
            "referenced_keys": 2,
            "references": 2,
        },
        "blocking": True,
        "exit_code": 1,
    }
    assert output["unused"] == ["OLD_KEY"]
    assert output["missing"] == ["MISSING_KEY"]
    assert output["optional_missing"] == ["OPTIONAL_KEY"]
    assert output["external_missing"] == ["EXTERNAL_KEY"]
    assert output["ignored_missing"] == ["IGNORED_KEY"]
    assert output["supabase_orphans"] == ["LEGACY_SECRET"]
    assert output["references"]["MISSING_KEY"][0]["file"] == "/repo/src/app.py"

    envguard._json_output(result, allow_unused=True, allow_missing=True)
    relaxed_output = json.loads(capsys.readouterr().out)

    assert relaxed_output["summary"]["blocking"] is False
    assert relaxed_output["summary"]["exit_code"] == 0


def test_json_output_normalizes_reference_paths_inside_root_and_preserves_outside(
    capsys,
    tmp_path: Path,
) -> None:
    inside = tmp_path / "src" / "app.py"
    outside = tmp_path.parent / "outside.py"
    result = envguard.ScanResult(
        references={
            "INSIDE_KEY": [
                envguard.EnvReference(
                    key="INSIDE_KEY",
                    file=str(inside),
                    line=2,
                    pattern_type="os.getenv",
                )
            ],
            "OUTSIDE_KEY": [
                envguard.EnvReference(
                    key="OUTSIDE_KEY",
                    file=str(outside),
                    line=1,
                    pattern_type="os.getenv",
                )
            ],
        },
        missing=["INSIDE_KEY", "OUTSIDE_KEY"],
    )

    envguard._json_output(result, reference_root=tmp_path)
    payload = json.loads(capsys.readouterr().out)

    assert payload["references"]["INSIDE_KEY"][0]["file"] == "src/app.py"
    assert payload["references"]["OUTSIDE_KEY"][0]["file"] == outside.as_posix()


def test_json_output_reports_repo_relative_paths_for_root_and_subdirectory_scans(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env.example").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "import os\nroot_secret = os.getenv('ROOT_SECRET')\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as root_exit:
        envguard.main(["--json", str(tmp_path)])
    assert root_exit.value.code == 1
    root_payload = json.loads(capsys.readouterr().out)
    assert root_payload["references"]["ROOT_SECRET"][0]["file"] == "src/app.py"

    app_dir = tmp_path / "apps" / "web"
    app_dir.mkdir(parents=True)
    (app_dir / "settings.py").write_text(
        "import os\nweb_secret = os.getenv('WEB_SECRET')\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as subdir_exit:
        envguard.main(["--json", str(app_dir)])
    assert subdir_exit.value.code == 1
    subdir_payload = json.loads(capsys.readouterr().out)
    assert subdir_payload["references"]["WEB_SECRET"][0]["file"] == "apps/web/settings.py"


def test_github_annotations_use_repo_relative_paths_and_source_lines_for_subdir_scan(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    service_dir = tmp_path / "services" / "api"
    service_dir.mkdir(parents=True)
    (service_dir / "app.py").write_text(
        "import os\n\nci_secret = os.environ['CI_SECRET']\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        envguard.main(["ci", str(service_dir)])

    assert exc.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        "::error file=services/api/app.py,line=3::Missing environment variable CI_SECRET"
    ]


def test_json_output_includes_baseline_suppression_metadata(
    capsys,
    tmp_path: Path,
) -> None:
    result = envguard.ScanResult(unused=["NEW_KEY"])
    baseline_path = tmp_path / ".envguard-baseline.json"

    envguard._json_output(
        result,
        baseline_path=baseline_path,
        baseline_suppressed={"missing": 2},
    )
    output = json.loads(capsys.readouterr().out)

    assert output["summary"]["baseline"] == {
        "path": str(baseline_path),
        "suppressed": {
            "unused": 0,
            "missing": 2,
            "optional_missing": 0,
            "external_missing": 0,
            "ignored_missing": 0,
            "supabase_orphans": 0,
        },
    }


def test_summary_output_includes_baselined_findings() -> None:
    assert envguard.format_summary_line(
        envguard.ScanResult(),
        baseline_suppressed={"missing": 1, "unused": 1},
    ) == "envguard: green — 2 baselined (exit 0)"


def test_summary_output_formats_single_line_with_expected_exit(capsys) -> None:
    result = envguard.ScanResult(
        unused=["OLD_KEY"],
        missing=["MISSING_KEY"],
        optional_missing=["OPTIONAL_KEY"],
        external_missing=["EXTERNAL_KEY"],
        ignored_missing=["IGNORED_KEY"],
        supabase_orphans=["LEGACY_SECRET"],
    )

    envguard._summary_output(result)

    assert capsys.readouterr().out == (
        "envguard: red — 1 missing, 1 unused, 1 optional, 1 external, "
        "1 ignored, 1 orphaned (exit 1)\n"
    )


def test_summary_output_reflects_allow_flags_and_clean_status() -> None:
    allowed = envguard.ScanResult(
        unused=["OLD_KEY"],
        missing=["MISSING_KEY"],
    )

    assert envguard.format_summary_line(
        allowed,
        allow_unused=True,
        allow_missing=True,
    ) == "envguard: yellow — 1 missing, 1 unused (exit 0)"
    assert envguard.format_summary_line(envguard.ScanResult()) == (
        "envguard: green — clean (exit 0)"
    )


def test_parse_cli_args_accepts_positional_path_and_presets() -> None:
    positional = envguard.parse_cli_args(["apps/web"])
    assert positional.path == "apps/web"

    ci = envguard.parse_cli_args(["ci", "apps/web"])
    assert ci.path == "apps/web"
    assert ci.github_annotations is True

    supabase = envguard.parse_cli_args(["supabase", "abcd1234"])
    assert supabase.supabase_project == "abcd1234"

    ci_template = envguard.parse_cli_args(["ci-template", "apps/web"])
    assert ci_template.command == "ci-template"
    assert ci_template.path == "apps/web"

    doctor = envguard.parse_cli_args(["doctor", "apps/web"])
    assert doctor.command == "doctor"
    assert doctor.path == "apps/web"

    matrix = envguard.parse_cli_args(["matrix", "apps/api"])
    assert matrix.command == "doctor"
    assert matrix.path == "apps/api"

    summary = envguard.parse_cli_args(["--summary", "apps/web"])
    assert summary.summary is True
    assert summary.path == "apps/web"

    baseline = envguard.parse_cli_args([
        "--baseline",
        ".envguard-baseline.json",
        "--write-baseline",
        "baseline.json",
        "apps/web",
    ])
    assert baseline.baseline == ".envguard-baseline.json"
    assert baseline.write_baseline == "baseline.json"
    assert baseline.path == "apps/web"


def test_build_secrets_matrix_reports_sources_and_requirements_without_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ref_map = {
        "DATABASE_URL": [
            envguard.EnvReference("DATABASE_URL", "app.py", 1, "os.getenv")
        ],
        "SHELL_ONLY": [envguard.EnvReference("SHELL_ONLY", "app.py", 2, "os.getenv")],
        "EDGE_SECRET": [
            envguard.EnvReference("EDGE_SECRET", "edge.ts", 3, "Deno.env.get")
        ],
        "MISSING_SECRET": [
            envguard.EnvReference("MISSING_SECRET", "app.py", 4, "os.getenv")
        ],
        "OPTIONAL_KEY": [
            envguard.EnvReference(
                "OPTIONAL_KEY",
                "app.py",
                5,
                "os.getenv",
                requirement="optional",
            )
        ],
        "EXTERNAL_KEY": [
            envguard.EnvReference(
                "EXTERNAL_KEY",
                "app.py",
                6,
                "process.env.KEY",
                requirement="external",
            )
        ],
        "IGNORED_KEY": [envguard.EnvReference("IGNORED_KEY", "app.py", 7, "os.getenv")],
    }

    matrix = envguard.build_secrets_matrix(
        ref_map,
        dotenv_keys=["DATABASE_URL", "UNUSED_DOTENV"],
        env={"SHELL_ONLY": "shell-secret", "OPTIONAL_KEY": "optional-secret"},
        supabase_keys=["EDGE_SECRET", "ORPHAN_EDGE_SECRET"],
        ignore_keys=["IGNORED_KEY"],
    )

    rows = {row.key: row for row in matrix.rows}
    assert rows["DATABASE_URL"].status == "ready"
    assert rows["DATABASE_URL"].dotenv is True
    assert rows["SHELL_ONLY"].environment is True
    assert rows["EDGE_SECRET"].supabase is True
    assert rows["MISSING_SECRET"].status == "missing"
    assert rows["OPTIONAL_KEY"].status == "ready"
    assert rows["EXTERNAL_KEY"].status == "external-missing"
    assert rows["IGNORED_KEY"].requirement == "ignored"
    assert rows["IGNORED_KEY"].status == "ignored-missing"
    assert matrix.unused_dotenv == ["UNUSED_DOTENV"]
    assert matrix.supabase_orphans == ["ORPHAN_EDGE_SECRET"]
    assert envguard.secrets_matrix_has_required_missing(matrix)

    envguard._matrix_json_output(matrix)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["summary"]["counts"]["missing"] == 1
    assert payload["rows"][0]["available"]["supabase"] in {True, False}
    assert "shell-secret" not in output
    assert "optional-secret" not in output


def test_doctor_command_prints_json_matrix_without_dotenv_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "app.py").write_text(
        "\n".join(
            [
                "import os",
                'DATABASE_URL = os.getenv("DATABASE_URL")',
                'SHELL_ONLY = os.getenv("SHELL_ONLY")',
                'MISSING_SECRET = os.getenv("MISSING_SECRET")',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text(
        "DATABASE_URL=DOTENV_VALUE_SHOULD_STAY_HIDDEN\n"
        "UNUSED_SECRET=UNUSED_VALUE_SHOULD_STAY_HIDDEN\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHELL_ONLY", "ENV_VALUE_SHOULD_STAY_HIDDEN")

    envguard.main(["--json", "--allow-missing", "doctor", str(tmp_path)])
    output = capsys.readouterr().out
    payload = json.loads(output)
    rows = {row["key"]: row for row in payload["rows"]}

    assert rows["DATABASE_URL"]["available"]["dotenv"] is True
    assert rows["SHELL_ONLY"]["available"]["environment"] is True
    assert rows["MISSING_SECRET"]["status"] == "missing"
    assert payload["unused_dotenv"] == ["UNUSED_SECRET"]
    assert "DOTENV_VALUE_SHOULD_STAY_HIDDEN" not in output
    assert "UNUSED_VALUE_SHOULD_STAY_HIDDEN" not in output
    assert "ENV_VALUE_SHOULD_STAY_HIDDEN" not in output


def test_doctor_command_exits_nonzero_for_required_missing(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        'import os\nMISSING_SECRET = os.getenv("MISSING_SECRET")\n',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        envguard.main(["doctor", str(tmp_path)])

    assert exc.value.code == 1


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
    monkeypatch.setattr(envguard, "_host_python", lambda: "/usr/local/bin/python3.11")

    def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(envguard.subprocess, "run", fake_run)

    assert envguard.run_update() == 0
    assert calls == [
        [
            "/usr/local/bin/pipx",
            "install",
            "--python",
            "/usr/local/bin/python3.11",
            "--force",
            "git+https://github.com/Tresnanda/envguard.git",
        ]
    ]


def test_run_update_bootstraps_pipx_with_host_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    bootstrap_dir = Path("/tmp/envguard-pipx-bootstrap")
    install_cmd = [
        "/usr/bin/python3.11",
        "-m",
        "pipx",
        "install",
        "--python",
        "/usr/bin/python3.11",
        "--force",
        "git+https://github.com/Tresnanda/envguard.git",
    ]

    def fake_which(name: str) -> Optional[str]:
        return "/usr/bin/python3.11" if name == "python3.11" else None

    def fake_exists(self: Path) -> bool:
        return False

    def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd == install_cmd and calls.count(cmd) == 1:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(envguard, "_python_version_ok", lambda path: True)
    monkeypatch.setattr(envguard, "_pipx_bootstrap_dir", lambda: bootstrap_dir)
    monkeypatch.setattr(envguard.shutil, "which", fake_which)
    monkeypatch.setattr(envguard.Path, "exists", fake_exists)
    monkeypatch.setattr(envguard.subprocess, "run", fake_run)

    assert envguard.run_update() == 0
    assert calls == [
        install_cmd,
        ["/usr/bin/python3.11", "-m", "venv", str(bootstrap_dir)],
        [
            str(bootstrap_dir / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "pipx",
        ],
        [
            str(bootstrap_dir / "bin" / "pipx"),
            "install",
            "--python",
            "/usr/bin/python3.11",
            "--force",
            "git+https://github.com/Tresnanda/envguard.git",
        ],
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


def test_discover_dotenv_paths_lists_real_env_as_wizard_choice(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")

    detected = envguard.discover_dotenv_paths(tmp_path, envguard.EnvguardConfig())

    assert detected == [tmp_path / ".env.example", tmp_path / ".env"]


def test_build_ci_template_uses_safe_dotenv_and_relative_path(tmp_path: Path) -> None:
    project = tmp_path / "apps" / "web"
    project.mkdir(parents=True)
    (project / ".env.example").write_text(
        "API_KEY=not-a-secret-template-value\n",
        encoding="utf-8",
    )
    (project / ".env").write_text("API_KEY=super-secret-live-value\n", encoding="utf-8")

    template = envguard.build_ci_template(project, base_path=tmp_path)

    assert "name: Envguard" in template
    assert "envguard ci apps/web --dotenv apps/web/.env.example" in template
    assert "super-secret-live-value" not in template
    assert "not-a-secret-template-value" not in template
    assert "dry output; no files were written" in template


def test_build_ci_template_does_not_reference_real_dotenv_only(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgres://secret@example/db\n",
        encoding="utf-8",
    )

    template = envguard.build_ci_template(tmp_path, base_path=tmp_path)

    assert "envguard ci" in template
    assert "--dotenv .env" not in template
    assert "postgres://secret@example/db" not in template
    assert "Found only a real .env locally" in template


def test_build_ci_template_reuses_config_and_supabase_secret_reference(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.envguard]",
                'dotenv = "config/example.env"',
                'supabase_project = "project-ref"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "example.env").write_text(
        "EDGE_SECRET=top-secret-value\n",
        encoding="utf-8",
    )
    (tmp_path / "supabase" / "functions").mkdir(parents=True)

    template = envguard.build_ci_template(tmp_path, base_path=tmp_path)

    assert "Detected [tool.envguard]" in template
    assert "--dotenv config/example.env" not in template
    assert "SUPABASE_ACCESS_TOKEN: ${{ secrets.SUPABASE_ACCESS_TOKEN }}" in template
    assert "top-secret-value" not in template
    assert "project-ref" not in template


def test_ci_template_command_prints_without_writing_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".env.example").write_text("SECRET=template-only\n", encoding="utf-8")
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    envguard.main(["ci-template", str(tmp_path)])
    output = capsys.readouterr().out
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    assert "name: Envguard" in output
    assert "envguard ci" in output
    assert before == after


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


def test_detect_supabase_project_ref_reads_supabase_cli_linked_ref(tmp_path: Path) -> None:
    linked_ref = tmp_path / "supabase" / ".temp" / "project-ref"
    linked_ref.parent.mkdir(parents=True)
    linked_ref.write_text("linked-ref\n", encoding="utf-8")

    assert (
        envguard.detect_supabase_project_ref(
            tmp_path,
            env={},
            config=envguard.EnvguardConfig(),
        )
        == "linked-ref"
    )


def test_detect_supabase_project_ref_honors_scan_supabase_false(tmp_path: Path) -> None:
    linked_ref = tmp_path / "supabase" / ".temp" / "project-ref"
    linked_ref.parent.mkdir(parents=True)
    linked_ref.write_text("linked-ref\n", encoding="utf-8")

    assert (
        envguard.detect_supabase_project_ref(
            tmp_path,
            env={"SUPABASE_PROJECT_REF": "env-ref"},
            config=envguard.EnvguardConfig(scan_supabase=False),
        )
        is None
    )


def test_fetch_supabase_secrets_with_cli_parses_json_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        cwd: Path,
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(
            {
                "cmd": cmd,
                "cwd": cwd,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps([{"name": "EDGE_SECRET"}, {"name": "SECOND_SECRET"}]),
            stderr="",
        )

    monkeypatch.setattr(envguard.subprocess, "run", fake_run)

    assert envguard.fetch_supabase_secrets_with_cli("project-ref", tmp_path) == [
        "EDGE_SECRET",
        "SECOND_SECRET",
    ]
    assert captured == {
        "cmd": [
            "supabase",
            "secrets",
            "list",
            "--project-ref",
            "project-ref",
            "--output",
            "json",
        ],
        "cwd": tmp_path,
        "capture_output": True,
        "text": True,
        "timeout": 60,
        "check": False,
    }


def test_main_uses_supabase_cli_when_token_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".env.example").write_text("EDGE_SECRET=\n", encoding="utf-8")
    (tmp_path / "supabase" / "functions" / "hello").mkdir(parents=True)
    (tmp_path / "supabase" / "functions" / "hello" / "index.ts").write_text(
        "const edge = Deno.env.get('EDGE_SECRET');\n",
        encoding="utf-8",
    )
    fetched: dict[str, object] = {}

    def fake_fetch(project_ref: str, scan_path: Path) -> list[str]:
        fetched["project_ref"] = project_ref
        fetched["scan_path"] = scan_path
        return ["EDGE_SECRET"]

    monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(envguard, "detect_supabase_project_ref", lambda *_args: "project-ref")
    monkeypatch.setattr(envguard, "supabase_cli_available", lambda: True)
    monkeypatch.setattr(envguard, "fetch_supabase_secrets_with_cli", fake_fetch)

    envguard.main(["--json", str(tmp_path)])

    payload = json.loads(capsys.readouterr().out)
    assert fetched == {"project_ref": "project-ref", "scan_path": tmp_path.resolve()}
    assert payload["missing"] == []
    assert payload["supabase_orphans"] == []


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


def test_run_wizard_can_select_real_env_when_template_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / ".env.example").write_text("DATABASE_URL=\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DATABASE_URL=postgres://localhost\n", encoding="utf-8")
    answers = iter([str(tmp_path), "2"])
    confirms = iter([False, False, True])
    captured: dict[str, object] = {}

    def fake_main(args: list[str]) -> None:
        captured["args"] = args

    monkeypatch.setattr(envguard, "_ask_text", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(envguard, "_ask_confirm", lambda *_args, **_kwargs: next(confirms))
    monkeypatch.setattr(envguard, "main", fake_main)

    envguard.run_wizard()

    assert captured["args"] == [
        "--path",
        str(tmp_path.resolve()),
        "--dotenv",
        str(tmp_path / ".env"),
    ]
    out = capsys.readouterr().out
    assert "Detected dotenv files:" in out
    assert "2) " + str(tmp_path / ".env") in out


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


def test_fix_dry_run_and_real_env_flags_parse() -> None:
    args = envguard.parse_cli_args(["--fix-dry-run", "--fix-real-env"])

    assert args.fix_dry_run is True
    assert args.fix_real_env is True


def test_is_template_dotenv_path_classifies_safe_templates() -> None:
    assert envguard.is_template_dotenv_path(Path(".env.example"))
    assert envguard.is_template_dotenv_path(Path(".env.sample"))
    assert envguard.is_template_dotenv_path(Path(".env.template"))
    assert envguard.is_template_dotenv_path(Path(".env.dist"))
    assert not envguard.is_template_dotenv_path(Path(".env"))
    assert not envguard.is_template_dotenv_path(Path("production.env"))


def test_interactive_fix_refuses_real_env_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dotenv = tmp_path / ".env"
    original = "OLD_SECRET=real\nKEEP_ME=1\n"
    dotenv.write_text(original, encoding="utf-8")
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    envguard.interactive_fix(result, dotenv)

    assert dotenv.read_text(encoding="utf-8") == original
    assert "Refusing to prune a real dotenv file" in capsys.readouterr().err


def test_interactive_fix_dry_run_does_not_prompt_or_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dotenv = tmp_path / ".env.example"
    original = "export OLD_SECRET=\nKEEP_ME=1\n"
    dotenv.write_text(original, encoding="utf-8")
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    monkeypatch.setattr(
        envguard.Confirm,
        "ask",
        lambda *_args, **_kwargs: pytest.fail("dry run should not prompt"),
    )

    envguard.interactive_fix(result, dotenv, dry_run=True)

    assert dotenv.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.example.bak").exists()
    out = capsys.readouterr().out
    assert "Dry run:" in out
    assert "OLD_SECRET=<redacted>" in out
    assert "export OLD_SECRET=" not in out


def test_interactive_fix_dry_run_can_preview_real_env(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dotenv = tmp_path / ".env"
    original = "OLD_SECRET=real\nKEEP_ME=1\n"
    dotenv.write_text(original, encoding="utf-8")
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    envguard.interactive_fix(result, dotenv, dry_run=True)

    assert dotenv.read_text(encoding="utf-8") == original
    out = capsys.readouterr().out
    assert "Dry run:" in out
    assert "OLD_SECRET=<redacted>" in out
    assert "OLD_SECRET=real" not in out


def test_interactive_fix_dry_run_previews_unused_bare_dotenv_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dotenv = tmp_path / ".env.example"
    original = "# OLD_SECRET stays documented\nOLD_SECRET\nKEEP_ME=\n"
    dotenv.write_text(original, encoding="utf-8")
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    monkeypatch.setattr(
        envguard.Confirm,
        "ask",
        lambda *_args, **_kwargs: pytest.fail("dry run should not prompt"),
    )

    envguard.interactive_fix(result, dotenv, dry_run=True)

    assert dotenv.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.example.bak").exists()
    out = capsys.readouterr().out
    assert "Dry run:" in out
    assert "OLD_SECRET=<redacted>" in out
    assert "<unparseable dotenv assignment>" not in out


def test_interactive_fix_prunes_only_exact_unused_bare_dotenv_key_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env.example"
    original = "\n".join(
        [
            "# OLD_SECRET is referenced in this comment",
            "OLD_SECRET",
            "OLD_SECRET_EXTRA",
            "KEEP_ME=",
            "",
        ]
    )
    dotenv.write_text(original, encoding="utf-8")
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    monkeypatch.setattr(envguard.Confirm, "ask", lambda *_args, **_kwargs: True)

    envguard.interactive_fix(result, dotenv)

    assert dotenv.read_text(encoding="utf-8") == "\n".join(
        [
            "# OLD_SECRET is referenced in this comment",
            "OLD_SECRET_EXTRA",
            "KEEP_ME=",
            "",
        ]
    )
    assert (tmp_path / ".env.example.bak").read_text(encoding="utf-8") == original


def test_interactive_fix_creates_backup_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env.example"
    original = "OLD_SECRET=\nKEEP_ME=1\n"
    dotenv.write_text(original, encoding="utf-8")
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    monkeypatch.setattr(envguard.Confirm, "ask", lambda *_args, **_kwargs: True)

    envguard.interactive_fix(result, dotenv)

    assert dotenv.read_text(encoding="utf-8") == "KEEP_ME=1\n"
    assert (tmp_path / ".env.example.bak").read_text(encoding="utf-8") == original


def test_interactive_fix_backup_does_not_follow_existing_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env.example"
    original = "OLD_SECRET=\nKEEP_ME=1\n"
    dotenv.write_text(original, encoding="utf-8")
    malicious_target = tmp_path / "outside-target"
    (tmp_path / ".env.example.bak").symlink_to(malicious_target)
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    monkeypatch.setattr(envguard.Confirm, "ask", lambda *_args, **_kwargs: True)

    envguard.interactive_fix(result, dotenv)

    assert not malicious_target.exists()
    assert (tmp_path / ".env.example.bak").is_symlink()
    assert (tmp_path / ".env.example.bak.1").read_text(encoding="utf-8") == original


def test_interactive_fix_does_not_follow_dotenv_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "real-env-target"
    original = "OLD_SECRET=real\nKEEP_ME=1\n"
    target.write_text(original, encoding="utf-8")
    dotenv = tmp_path / ".env.example"
    dotenv.symlink_to(target)
    result = envguard.ScanResult(unused=["OLD_SECRET"])

    monkeypatch.setattr(envguard.Confirm, "ask", lambda *_args, **_kwargs: True)

    envguard.interactive_fix(result, dotenv)

    assert target.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.example.bak").exists()
    assert "Refusing to prune a symlinked dotenv file" in capsys.readouterr().err


def test_interactive_fix_atomic_write_replaces_swapped_symlink_not_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env.example"
    original = "OLD_SECRET=\nKEEP_ME=1\n"
    dotenv.write_text(original, encoding="utf-8")
    malicious_target = tmp_path / "outside-target"
    target_original = "DO_NOT_CHANGE=1\n"
    malicious_target.write_text(target_original, encoding="utf-8")
    result = envguard.ScanResult(unused=["OLD_SECRET"])
    real_backup = envguard._write_backup_exclusive

    def backup_then_swap(path: Path, content: str) -> Path:
        backup_path = real_backup(path, content)
        path.unlink()
        path.symlink_to(malicious_target)
        return backup_path

    monkeypatch.setattr(envguard.Confirm, "ask", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(envguard, "_write_backup_exclusive", backup_then_swap)

    envguard.interactive_fix(result, dotenv)

    assert malicious_target.read_text(encoding="utf-8") == target_original
    assert not dotenv.is_symlink()
    assert dotenv.read_text(encoding="utf-8") == "KEEP_ME=1\n"
    assert (tmp_path / ".env.example.bak").read_text(encoding="utf-8") == original


def test_atomic_write_no_follow_keeps_original_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env.example"
    original = "OLD_SECRET=\nKEEP_ME=1\n"
    dotenv.write_text(original, encoding="utf-8")

    def fail_replace(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        raise OSError(f"simulated replace failure: {src} -> {dst}")

    monkeypatch.setattr(envguard.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        envguard._atomic_write_no_follow(dotenv, "KEEP_ME=1\n", 0o600)

    assert dotenv.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".*.envguard-*.tmp")) == []
