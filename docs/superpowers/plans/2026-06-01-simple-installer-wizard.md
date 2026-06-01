# Simple Installer Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the envguard installer with a simple numbered setup flow.

**Architecture:** Keep the installer self-contained in `install.sh` and `install.ps1`. Add lightweight static tests that prove the installer exposes the intended UX and secret handling.

**Tech Stack:** Bash, PowerShell, pytest.

---

### Task 1: Installer Static Tests

**Files:**
- Create: `tests/test_installers.py`

- [ ] **Step 1: Write failing tests**

Create tests that read `install.sh` and `install.ps1`, then assert they include numbered Supabase setup choices, `SUPABASE_ACCESS_TOKEN`, and user environment/profile persistence.

- [ ] **Step 2: Run tests to verify failure**

Run: `rtk .venv/bin/python -m pytest tests/test_installers.py`

- [ ] **Step 3: Implement installer UX**

Modify `install.sh` and `install.ps1` to show compact summaries, numbered token setup choices, and safe secret persistence.

- [ ] **Step 4: Run tests and syntax checks**

Run: `rtk .venv/bin/python -m pytest tests/test_installers.py` and `rtk bash -n install.sh`.

### Task 2: Commit

**Files:**
- Modify: `install.sh`
- Modify: `install.ps1`
- Create: `tests/test_installers.py`

- [ ] **Step 1: Run full verification**

Run the repo's lint/test/build commands plus `rtk bash -n install.sh`.

- [ ] **Step 2: Commit and push**

Commit with `feat: simplify installer wizard`.
