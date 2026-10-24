# Test Report

Date: 2026-10-24 · Machine: Mac (Apple Silicon), macOS 26.4.1 · Python 3.14.6 · Chrome 154.0.8037.100 · Selenium 4.51.0

## 1. Automated suite (`pytest`)

**Result: 133 passed, 0 failed** (1 live test deselected by default) in about 18 s. No Google access, no quota.

| File | Tests passed |
|------|-------------:|
| `tests/test_api.py` | 7 |
| `tests/test_browser.py` | 12 |
| `tests/test_config.py` | 23 |
| `tests/test_downloads.py` | 12 |
| `tests/test_errors_fake.py` | 13 |
| `tests/test_logging.py` | 2 |
| `tests/test_platform_utils.py` | 11 |
| `tests/test_resume.py` | 6 |
| `tests/test_state_store.py` | 14 |
| `tests/test_ui_probe.py` | 7 |
| `tests/test_validators.py` | 20 |
| `tests/test_workflow_fake.py` | 6 |

## 2. Quota / limit simulation (fake controller, H5)

| Scenario | Expected (08 §3) | Result |
|----------|------------------|--------|
| `quota_after_2` (3 separate clips) | P1, P2 DOWNLOADED; P3 BLOCKED_QUOTA; job BLOCKED_QUOTA; no 3rd submit | **Pass**: 2 generations, 2 files saved, P3 blocked, message "Usage limit reached. Completed outputs saved. Resume after the limit resets." After the reset, Resume generated only P3 (P1/P2 file mtimes unchanged) → COMPLETED |
| `quota_after_2_no_clip_dl` (multi_part, no per-clip download) | Partial export verified, PARTIAL_DOWNLOADED | **Pass**: `Sample_Project_Partial.mp4` verified; Resume finished P3 and saved `Sample_Project_Final.mp4`; the partial file was kept |

Tests: `tests/test_errors_fake.py::test_er1_a5_quota_after_2`, `::test_dec24_quota_without_clip_downloads_exports_partial`.

## 3. Error handling and recovery (fake controller)

| Requirement | Test | Result |
|-------------|------|--------|
| ER2 timeout / network (A8) | `test_er2_a8_timeout_part1`, `test_er2_a8_network_error_retries_then_succeeds` | Pass: TIMEOUT with screenshot in the event; 2 STEP_RETRY events then success |
| ER3 sign-in (A8) | `test_er3_a8_login_required_then_resume` | Pass: WAITING_FOR_USER, no submits, Resume completes |
| ER4 UI changed (A8) | `test_er4_a8_ui_missing_generate_pauses` | Pass: part FAILED UI_MISSING, job PAUSED, other parts untouched |
| ER5 generation failed (A6) | `test_er5_a6_gen_fail_part2_then_retry` | Pass: only part 2 failed; Retry reran it alone |
| ER6 download interrupted (A8) | `test_er6_download_interrupt_retries_download_not_generation` | Pass: DOWNLOAD_RETRY, generation count 1 |
| ER9 disk full (A8) | `test_er9_a8_disk_error_waits_then_resumes_without_regenerating` | Pass: path and error shown; Resume completes without regenerating |
| IM3 / DEC-26 upload failure | `test_im3_dec26_upload_fail_keeps_part_pending_and_pauses` | Pass: part stays PENDING, job PAUSED, Resume retries the upload |
| DL5 / A4 duplicate name | `test_dl5_a4_duplicate_gets_v2_suffix`, `test_a4_duplicate_v2` | Pass: original byte-identical, new file `_v2` |
| ER8 / A7 crash recovery | `tests/test_resume.py` (6 kill points) | Pass: no silent regeneration at any kill point (fake generation counter) |
| P4 / A12 privacy | `test_p4_script_marker_absent_from_log_and_events`, `test_a12_redaction_*` | Pass |

## 4. Live runs on the target account (Google AI Pro)

| Run | Generations | Result |
|-----|------------:|--------|
| M1 step 1.6 `tools/feasibility_run.py` (single part, scripted) | 1 (+1 earlier attempt that exposed the Insert selector fix) | ALL PASS, 13 steps; clip 10.05 s, MP4 verified |
| M3.5 single job through the app | 1 | COMPLETED in 68 s; `Live_Single_1525_Part01.mp4`, 2.48 MB, 10.05 s, verified |
| M3.6 `Sample_Project` multi_part through the app | 3 | COMPLETED in 163 s; 3 scenes in order (sunset → dusk beam → starry night); `Sample_Project_Final.mp4` 7.16 MB, 30.05 s, verified |
| Live smoke test `pytest -m live` | 1 when run | Ready (`tests/test_live_smoke.py`); same path as M3.5 |

Total live generations used across M1 and M3: 8 (manual walkthrough 2, scripted 2, app runs 4).

## 5. Secret scan (A12, M5.4)

- Repo (tracked + untracked, not ignored) grep for `SID=`, `Bearer`, `password=`, `ya29.`, `AIza…`, email addresses: no secrets (only documentation mentions).
- `logs/`: no tokens or email addresses (emails are masked as `***@***` by the logging filter).
- Gitignored and confirmed: `config.json`, `data/` (DB, Chrome profile, staging), `logs/`, `output/`, `input/jobs/`, `docs/feasibility/raw/`.

## 6. Pending (developer)

- M2.6 / M5.2: demo check and clean-machine setup on Windows 11 (A11).
- M5.5: demo video (H6), following `08_TEST_PLAN.md` §7.
