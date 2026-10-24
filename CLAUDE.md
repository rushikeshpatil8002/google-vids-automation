# CLAUDE.md — Google Vids Automation

This project is a local Python tool. It drives Google Vids in Chrome to generate AI video clips from parts, where each part is a script plus one or more reference images. It downloads the finished videos with predictable names and must never lose or overwrite completed work.

Read this whole file at the start of every session.

---

## 1. Source of truth (read in this order)

| # | File | What it settles |
|---|------|-----------------|
| 1 | `docs/01_REQUIREMENTS.md` | Every requirement, with IDs. Anything not listed is out of scope. |
| 2 | `docs/02_DECISIONS.md` | How each ambiguity in the client brief was resolved. **Do not re-decide these.** |
| 3 | `docs/03_ARCHITECTURE.md` | Modules, interfaces, HTTP API, threading, config schema. |
| 4 | `docs/04_STATE_AND_RECOVERY.md` | DB schema, state machine, commit points, resume reconciliation. |
| 5 | `docs/05_SELENIUM_GUIDE.md` | Browser launch/attach, login, selectors, waits, uploads, downloads. |
| 6 | `docs/06_MILESTONES.md` | Build order, step checklists, exit criteria. **Work only on the current milestone.** |
| 7 | `docs/07_FEASIBILITY_REPORT.md` | Template filled during M1. Its findings set `automation/vids_selectors.json`. |
| 8 | `docs/08_TEST_PLAN.md` | Test layers, fake scenarios, acceptance matrix A1–A12. |

`docs/00_ORIGINAL_BRIEF.pdf` is the client's brief. If any doc conflicts with the brief, stop and tell the developer. Don't pick one yourself.

## 2. Current status

```
Milestone: M5 — QA + handover — developer steps left: 2.6/5.2 Windows check, 5.5 demo video, 5.6 deliver
Last completed step: 5.4 — secret scan (M1–M4 done; live gens used: M1 4/5, M3 4/4)
```

Update this block, and the checklist in `docs/06_MILESTONES.md`, at the end of every step.

## 3. Working rules (non-negotiable)

1. **Continuous mode.** Work through the steps and milestones without waiting for a "go". Commit after each step with `pytest` green, and tick the checklist. Stop only when developer input is really needed: a human-only step, a scope decision, or a live failure you can't resolve.
2. **Generation budget.** Every live Google Vids generation uses the account's quota. The developer has pre-approved the per-milestone budgets in `docs/06_MILESTONES.md`. Stay within them, record each generation used, and ask before exceeding one.
3. **Fake first.** All development and automated tests use `FakeVidsController`. Live Selenium runs are pre-approved for the milestone steps that call for them.
4. **No credentials, ever.** Never type passwords, handle 2FA, or solve CAPTCHAs. Never read, copy, print, or log cookies or tokens. The human signs in by hand in the automation Chrome window.
5. **No evasion.** No stealth plugins, user-agent spoofing, hidden automation flags, quota workarounds, or multiple accounts to dodge limits. If Google blocks a step, record it as unsupported or human-in-the-loop.
6. **Never fake success.** A part becomes `DOWNLOADED` only after its file is verified on disk (DL2). If Vids can't do something, the live controller reports it as unsupported. It must never simulate the result.
7. **Never overwrite or delete user files.** Nothing in `output/` is ever overwritten (DL5). Files in `input/` and `output/` are deleted only through the explicit "Delete job data" action.
8. **Privacy in logs.** Never log script text. Log job and part IDs, step names, a 12-character script hash, and the character count (P4).
9. **Dependencies are fixed.** Use only what's in `requirements.txt`. Ask before adding anything.
10. **Docs move with code.** If you change a state name, schema column, config key, selector key, or API route, update the matching doc in the same change.

## 4. Tech stack (fixed)

Python 3.11–3.14 · Flask 3 · Selenium 4 (Selenium Manager handles ChromeDriver) · Pillow · SQLite via stdlib `sqlite3` · pytest · vanilla HTML/CSS/JS. No frontend framework, no build step, no CDN. The UI must work offline.

## 5. Cross-platform rules

Windows 11 is the acceptance target. macOS is also supported.

- Use `pathlib.Path` everywhere. Never concatenate path strings or hardcode `/` or `\`.
- Filename sanitizing applies **Windows rules on every OS** (see `docs/03_ARCHITECTURE.md` §6).
- Open text files with `encoding="utf-8"` explicitly.
- Wrap file moves and renames in the retry helper. Windows locks files that are still being written.
- OS-specific code lives **only** in `automation/platform_utils.py`.
- No `shell=True`, no bash-only commands, no hardcoded `python3` in app code. Use `sys.executable`.

## 6. Commands

```bash
# Setup — macOS
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp config.example.json config.json

# Setup — Windows (PowerShell)
py -3 -m venv .venv ; .venv\Scripts\Activate.ps1 ; pip install -r requirements.txt
copy config.example.json config.json

python app.py                       # UI at http://127.0.0.1:5000 (live controller)
python app.py --fake                # fake controller, no Google access, no quota used
python app.py --fake --scenario tests/scenarios/quota_after_2.json
python app.py --login               # opens the automation Chrome profile for manual sign-in
python app.py --host 0.0.0.0        # LAN access for UI testing (warns; automation still runs on THIS machine)
python tools/ui_probe.py            # M1: capture screenshot + DOM snapshot of the current Vids page

pytest                              # unit + fake integration (live tests excluded by default)
pytest -m live                      # live smoke test — USES REAL GENERATIONS, ask first
```

## 7. Definition of done (every change)

- `pytest` passes.
- New behavior has a test whose name or docstring cites the requirement ID, e.g. `test_dl5_duplicate_gets_v2_suffix`.
- Docs are updated whenever behavior, config, schema, or routes change.
- Nothing secret or user-specific is committed: `config.json`, `data/`, `logs/`, `output/`, and real inputs are all gitignored.
