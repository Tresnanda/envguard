#!/usr/bin/env bash
set -e

APP_NAME="envguard"
REPO_SPEC="git+https://github.com/Tresnanda/envguard.git"
YES=0

for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    -h|--help)
      echo "Usage: install.sh [--yes]"
      exit 0
      ;;
  esac
done

log() { printf '%s\n' "$*"; }
has_tty() { [ "$YES" -eq 0 ] && [ -r /dev/tty ]; }
ask_yes_no() {
  prompt="$1"
  default="${2:-y}"
  if ! has_tty; then
    [ "$default" = "y" ]
    return
  fi
  if [ "$default" = "y" ]; then
    suffix="[Y/n]"
  else
    suffix="[y/N]"
  fi
  printf '%s %s ' "$prompt" "$suffix" >/dev/tty
  read -r answer </dev/tty || answer=""
  answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
  [ -z "$answer" ] && answer="$default"
  [ "$answer" = "y" ] || [ "$answer" = "yes" ]
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    return 1
  fi
}

log "envguard installer"
PYTHON="$(find_python)" || {
  log "Error: Python 3.9+ is required."
  exit 1
}
log "[ok] Python: $("$PYTHON" --version 2>&1)"

if "$PYTHON" -m pipx --version >/dev/null 2>&1; then
  log "[ok] pipx found"
else
  log "[warn] pipx not found"
  if ask_yes_no "Install pipx with this Python?" "y"; then
    "$PYTHON" -m pip install --user pipx
    "$PYTHON" -m pipx ensurepath >/dev/null 2>&1 || true
  else
    log "Install pipx and rerun this installer."
    exit 1
  fi
fi

log "Environment checks:"
if command -v supabase >/dev/null 2>&1; then
  log "[ok] Supabase CLI found"
else
  log "[info] Supabase CLI not found"
fi
if [ -n "${SUPABASE_ACCESS_TOKEN:-}" ]; then
  log "[ok] SUPABASE_ACCESS_TOKEN is set"
else
  log "[info] SUPABASE_ACCESS_TOKEN is not set"
fi

log "Installing $APP_NAME from GitHub..."
"$PYTHON" -m pipx install --force "$REPO_SPEC"

if command -v "$APP_NAME" >/dev/null 2>&1; then
  "$APP_NAME" --help >/dev/null
  log "[ok] $APP_NAME installed"
else
  log "[warn] $APP_NAME installed, but pipx bin dir may not be on PATH."
  log "Run: python -m pipx ensurepath"
fi

if ask_yes_no "Run $APP_NAME wizard now?" "y"; then
  "$APP_NAME" wizard
fi
