from pathlib import Path

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


def test_load_project_config_reads_pyproject_tool_envguard(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.envguard]",
                'dotenv = "config/example.env"',
                'exclude = ["fixtures/**", "snapshots/**"]',
                'supabase_project = "abcd1234"',
            ]
        ),
        encoding="utf-8",
    )

    config = envguard.load_project_config(tmp_path)

    assert config.dotenv == "config/example.env"
    assert config.exclude == ["fixtures/**", "snapshots/**"]
    assert config.supabase_project == "abcd1234"


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
