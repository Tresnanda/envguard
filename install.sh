#!/usr/bin/env bash
set -e

APP_NAME="envguard"
REPO_SLUG="Tresnanda/envguard"
REPO_URL="https://github.com/$REPO_SLUG"
REPO_SPEC="git+https://github.com/Tresnanda/envguard.git"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=9
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
  if [ "$default" = "y" ]; then suffix="[Y/n]"; else suffix="[y/N]"; fi
  printf '%s %s ' "$prompt" "$suffix" >/dev/tty
  read -r answer </dev/tty || answer=""
  answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
  [ -z "$answer" ] && answer="$default"
  [ "$answer" = "y" ] || [ "$answer" = "yes" ]
}

ask_choice() {
  prompt="$1"
  default="$2"
  if ! has_tty; then
    echo "$default"
    return
  fi
  printf '%s [%s]: ' "$prompt" "$default" >/dev/tty
  read -r answer </dev/tty || answer=""
  if [ -n "$answer" ]; then echo "$answer"; else echo "$default"; fi
}

python_version_ok() {
  "$1" - <<PY
import sys
raise SystemExit(0 if sys.version_info >= ($MIN_PYTHON_MAJOR, $MIN_PYTHON_MINOR) else 1)
PY
}

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      path="$(command -v "$candidate")"
      if python_version_ok "$path"; then
        echo "$path"
        return 0
      fi
    fi
  done
  return 1
}

data_home() {
  echo "${XDG_DATA_HOME:-$HOME/.local/share}"
}

pipx_bin_dir() {
  echo "${PIPX_BIN_DIR:-$HOME/.local/bin}"
}

bootstrap_pipx() {
  venv_dir="$(data_home)/$APP_NAME/pipx-bootstrap"
  log "pipx was not found; installing a private pipx helper..."
  mkdir -p "$(dirname "$venv_dir")"
  if ! "$PYTHON" -m venv "$venv_dir"; then
    log "Error: could not create a Python virtual environment for pipx."
    log "Install pipx manually, then rerun this installer."
    exit 1
  fi
  "$venv_dir/bin/python" -m pip install --upgrade pip pipx
  PIPX=("$venv_dir/bin/pipx")
}

shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

shell_profile() {
  if [ -n "${ZDOTDIR:-}" ] && [ -d "$ZDOTDIR" ]; then
    echo "$ZDOTDIR/.zshrc"
  elif [ -n "${SHELL:-}" ] && [ "${SHELL##*/}" = "bash" ]; then
    echo "$HOME/.bashrc"
  else
    echo "$HOME/.zshrc"
  fi
}

save_secret_to_shell_profile() {
  name="$1"
  value="$2"
  profile="$(shell_profile)"
  mkdir -p "$(dirname "$profile")"
  {
    printf '\n# Added by %s installer\n' "$APP_NAME"
    printf 'export %s=%s\n' "$name" "$(shell_quote "$value")"
  } >>"$profile"
  export "$name=$value"
  log "[ok] Saved $name to $profile"
  log "Open a new terminal or run: source $profile"
}

offer_star_repo() {
  if ! has_tty; then
    log "Star it here: $REPO_URL"
    return
  fi
  if ! ask_yes_no "If $APP_NAME helps you, star the GitHub repo now?" "y"; then
    log "Star it here: $REPO_URL"
    return
  fi
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    if gh repo star "$REPO_SLUG" >/dev/null 2>&1; then
      log "[ok] Starred $REPO_URL"
      return
    fi
  fi
  if [ -n "${GITHUB_TOKEN:-}" ] && command -v curl >/dev/null 2>&1; then
    if curl -fsS -X PUT \
      -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer $GITHUB_TOKEN" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "https://api.github.com/user/starred/$REPO_SLUG" >/dev/null 2>&1; then
      log "[ok] Starred $REPO_URL"
      return
    fi
  fi
  log "Couldn't auto-star from this terminal."
  log "Star it here: $REPO_URL"
}

setup_supabase_token() {
  has_tty || return 0
  log ""
  if [ -n "${SUPABASE_ACCESS_TOKEN:-}" ]; then
    log "Supabase: SUPABASE_ACCESS_TOKEN already set"
    return
  fi
  log "Supabase token was not found."
  log "Choose Supabase token setup:"
  log "1) Paste SUPABASE_ACCESS_TOKEN now"
  log "2) Show command to set it later"
  log "3) Skip Supabase token setup"
  choice="$(ask_choice "Choice" "1")"
  case "$choice" in
    1)
      printf 'Enter SUPABASE_ACCESS_TOKEN: ' >/dev/tty
      stty -echo </dev/tty 2>/dev/null || true
      read -r token </dev/tty || token=""
      stty echo </dev/tty 2>/dev/null || true
      printf '\n' >/dev/tty
      if [ -n "$token" ]; then
        save_secret_to_shell_profile "SUPABASE_ACCESS_TOKEN" "$token"
      else
        log "[info] Empty token skipped"
      fi
      ;;
    2)
      log "Run this later:"
      log "  export SUPABASE_ACCESS_TOKEN=\"your-token\""
      ;;
    *)
      log "[info] Skipped Supabase token setup"
      ;;
  esac
}

log "Install envguard"
log "This checks Python, installs with pipx, and can set up Supabase access."
PYTHON="$(find_python)" || {
  log "Error: Python 3.9+ is required."
  if [ "$(uname -s 2>/dev/null)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    log "Install it with: brew install python"
  else
    log "Install Python 3.9 or newer, then rerun this installer."
  fi
  exit 1
}
log "[ok] Python: $("$PYTHON" --version 2>&1)"

PIPX=()
if command -v pipx >/dev/null 2>&1; then
  PIPX=("$(command -v pipx)")
  log "[ok] pipx found"
elif "$PYTHON" -m pipx --version >/dev/null 2>&1; then
  PIPX=("$PYTHON" -m pipx)
  log "[ok] pipx found"
elif ask_yes_no "Install pipx with this Python?" "y"; then
  bootstrap_pipx
else
  log "Install pipx and rerun this installer."
  exit 1
fi

if command -v supabase >/dev/null 2>&1; then
  log "[ok] Supabase CLI found"
else
  log "[info] Supabase CLI not found; envguard can still scan local .env files"
fi
setup_supabase_token

log "Installing $APP_NAME from GitHub..."
"${PIPX[@]}" install --python "$PYTHON" --force "$REPO_SPEC"

if command -v "$APP_NAME" >/dev/null 2>&1; then
  "$APP_NAME" --help >/dev/null
  log "[ok] $APP_NAME installed"
else
  log "[warn] $APP_NAME installed, but pipx bin dir may not be on PATH."
  log "Run: export PATH=\"$(pipx_bin_dir):\$PATH\""
fi

offer_star_repo
log "Run envguard in your terminal to start the guided audit."
