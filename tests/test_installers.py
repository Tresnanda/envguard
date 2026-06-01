from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_unix_installer_uses_numbered_supabase_setup() -> None:
    text = _read("install.sh")

    assert "Choose Supabase token setup:" in text
    assert "1) Paste SUPABASE_ACCESS_TOKEN now" in text
    assert "2) Show command to set it later" in text
    assert "3) Skip Supabase token setup" in text
    assert "save_secret_to_shell_profile" in text
    assert "SUPABASE_ACCESS_TOKEN" in text
    assert "Run $APP_NAME wizard now?" not in text
    assert '"$APP_NAME" wizard' not in text
    assert "Run envguard in your terminal to start the guided audit." in text


def test_windows_installer_uses_numbered_supabase_setup() -> None:
    text = _read("install.ps1")

    assert "Choose Supabase token setup:" in text
    assert "1) Paste SUPABASE_ACCESS_TOKEN now" in text
    assert "2) Show command to set it later" in text
    assert "3) Skip Supabase token setup" in text
    assert "Save-UserSecret" in text
    assert "SUPABASE_ACCESS_TOKEN" in text
    assert "Run $AppName wizard now?" not in text
    assert "& $AppName wizard" not in text
    assert "Run envguard in your terminal to start the guided audit." in text
