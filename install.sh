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

find_python() {
  if command -v python3 >/dev/null 2>&1; then command -v python3
  elif command -v python >/dev/null 2>&1; then command -v python
  else return 1
  fi
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

setup_supabase_token() {
  has_tty || return
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
PYTHON="$(find_python)" || { log "Error: Python 3.9+ is required."; exit 1; }
log "[ok] Python: $("$PYTHON" --version 2>&1)"

if "$PYTHON" -m pipx --version >/dev/null 2>&1; then
  log "[ok] pipx found"
elif ask_yes_no "Install pipx with this Python?" "y"; then
  "$PYTHON" -m pip install --user pipx
  "$PYTHON" -m pipx ensurepath >/dev/null 2>&1 || true
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
"$PYTHON" -m pipx install --force "$REPO_SPEC"

if command -v "$APP_NAME" >/dev/null 2>&1; then
  "$APP_NAME" --help >/dev/null
  log "[ok] $APP_NAME installed"
else
  log "[warn] $APP_NAME installed, but pipx bin dir may not be on PATH."
  log "Run: python -m pipx ensurepath"
fi

if has_tty && ask_yes_no "Run $APP_NAME wizard now?" "y"; then
  "$APP_NAME" wizard
fi
