# Simple Installer Wizard Design

## Goal

Make the `envguard` curl installer feel obvious for first-time users by replacing noisy diagnostics with a short setup summary and numbered choices.

## User Flow

The installer should:

1. Show a short welcome that explains what will happen.
2. Check Python and pipx.
3. Show a compact Supabase summary instead of long diagnostic noise.
4. Offer numbered Supabase token setup choices:
   - Use the existing `SUPABASE_ACCESS_TOKEN` when present.
   - Paste a token now when missing.
   - Skip token setup.
5. Save pasted tokens to the user's shell profile on macOS/Linux, or user environment on Windows.
6. Install with pipx.
7. Offer to run `envguard wizard`.

## Secret Handling

The installer may accept `SUPABASE_ACCESS_TOKEN` interactively, but it must not store the token in project config files. macOS/Linux writes an `export` line into a shell profile. Windows uses user-level environment variables.

## Non-Interactive Mode

`--yes` keeps the installer unattended. It should avoid prompts, skip secret entry, install through pipx, and skip launching the wizard.

## Testing

Use static installer tests to confirm the expected numbered prompts, env var names, and secret storage destinations exist. Use shell syntax checks for `install.sh`.
