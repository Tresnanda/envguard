# Curl Installers and Environment Detection Design

## Goal

Add simple macOS/Linux and Windows install entrypoints for each CLI while keeping installation safe, inspectable, and useful for first-time users.

## Installer Behavior

- Add `install.sh` and `install.ps1`.
- Install via `pipx install --force git+https://github.com/Tresnanda/<repo>.git`.
- Detect Python and `pipx`; offer to install `pipx` with the active Python when missing.
- Work interactively by default when a terminal is available, including `curl | bash` by reading prompts from `/dev/tty`.
- Support `--yes` for unattended installs.
- Verify by checking the installed command help.
- Offer to run the project wizard after install.

## Envguard

`envguard` installer checks Python, `pipx`, Supabase CLI, and `SUPABASE_ACCESS_TOKEN`. It does not perform network calls beyond installing the package.

## AI Projects

`git-standup` and `ss2json` share a lightweight AI environment detector. It checks API-key environment variables, OpenAI-compatible base URLs, local CLI harnesses, and common local endpoints without spending money or sending prompts. Claude/Anthropic is intentionally excluded from installer detection.

Detected API keys:

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_API_KEY`
- `GROQ_API_KEY`
- `MISTRAL_API_KEY`
- `OPENROUTER_API_KEY`
- `TOGETHER_API_KEY`
- `PERPLEXITY_API_KEY`
- `XAI_API_KEY`
- `AZURE_OPENAI_API_KEY`

Detected CLI harnesses:

- `openai`
- `gemini`
- `ollama`
- `lms`
- `gh`
- `vercel`
- `aider`
- `opencode`
- `codex`

## Runtime Provider Support

The AI-backed apps should use additional OpenAI-compatible API keys where the app can do so safely. `git-standup` may use text-capable providers. `ss2json` may use providers with vision-capable defaults; otherwise it should report detected-but-not-usable in the installer output.
