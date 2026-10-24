# 08 — Test Plan

## 1. Test layers

| Layer | Files | Uses Google? | Run |
|-------|-------|--------------|-----|
| Unit | `test_validators.py`, `test_state_store.py`, `test_downloads.py`, `test_config.py`, `test_logging.py`, `test_report.py` | no | `pytest` |
| API | `test_api.py` (Flask test client) | no | `pytest` |
| Workflow (fake) | `test_workflow_fake.py`, `test_errors_fake.py`, `test_resume.py` | no | `pytest` |
| Live smoke | `test_live_smoke.py` (`@pytest.mark.live`) | **yes (1 generation)** | `pytest -m live`, only on request |

**Conventions**
- Every test uses a temp project root (`tmp_path`) with its own `data/`, `input/`, `output/`, and `logs/`. Never the real ones.
- The fake controller runs with `generation_seconds` ≤ 1 and download delays ≤ 0.5 s, so the full suite runs in under 60 s.
- Test names or docstrings cite requirement IDs.

## 2. Fixtures

- `tests/fixtures/` holds generated images: `ok.png`, `ok.jpg`, `ok.webp`, `fake_ext.png` (actually a JPEG; must be accepted and saved as `.jpg`), `not_image.png` (text; must be rejected), `huge.png` (generated over the size limit at test time), and `truncated.png`.
- `tiny.mp4` is created by a helper: bytes `00 00 00 18 66 74 79 70 69 73 6F 6D 00 00 02 00 69 73 6F 6D 69 73 6F 32` followed by padding up to 10 240 bytes.
- `bad.mp4` is 10 KB of zeros (fails the `ftyp` check). `empty.mp4` is 0 bytes.

## 3. Fake scenarios (`tests/scenarios/`) and the ER rows they prove

| Scenario file | Setup | Expected result | Proves |
|---------------|-------|-----------------|--------|
| `happy_path.json` | all capabilities true | all parts DOWNLOADED, Final verified, COMPLETED | A1, A2, A3 |
| `no_multi_scene.json` | multi_scene=false | multi_part runs as separate clips, limitation shown, no Final | MP2, MP5, DEC-02 |
| `no_per_clip_download.json` | per_clip_download=false | parts end READY, Final verified, report says "included in final export" | DEC-23 |
| `quota_after_2.json` | quota after 2 generations, 3-part job | P1, P2 DOWNLOADED, P3 BLOCKED_QUOTA, job BLOCKED_QUOTA, no 3rd submit attempted | ER1, A5 |
| `quota_after_2_no_clip_dl.json` | same, per_clip_download=false | Partial export verified, PARTIAL_DOWNLOADED | DEC-24 |
| `gen_fail_part2.json` | part 2 generation error | P2 FAILED (after 1 auto-retry), P1/P3 DOWNLOADED, COMPLETED_WITH_ERRORS. Retry → P2 DOWNLOADED, P1/P3 untouched (mtime unchanged) | ER5, A6 |
| `timeout_part1.json` | part 1 never completes | P1 FAILED with GENERATION_TIMEOUT and a screenshot path in the event | ER2, A8 |
| `network_error_part1.json` | first 2 submits raise NetworkError | STEP_RETRY events ×2, then P1 DOWNLOADED; network error logged | ER2, A8 |
| `upload_fail_part1.json` | upload fails on the first 3 attempts (= `upload_max_attempts`) | P1 stays PENDING with `error_code=UPLOAD`, job PAUSED with the DEC-26 `last_error`, P2/P3 not started, submit count = 0. Resume → upload succeeds, all parts DOWNLOADED | IM3, DEC-26 |
| `download_interrupt_part1.json` | first download leaves `.crdownload` forever, second succeeds | DOWNLOAD_RETRY event, P1 DOWNLOADED, generation count 1 | ER6 |
| `login_required.json` | login required at start | WAITING_FOR_USER, banner, no submits. Clear the flag, Resume → completes | ER3, A8 |
| `ui_missing_generate.json` | `generate_button` missing | P1 FAILED UI_MISSING, job PAUSED, P2/P3 untouched | ER4, A8 |
| `disk_error_part2.json` | safe_move raises OSError(ENOSPC) | WAITING_FOR_USER, message shows the path. Resume after clearing → completes without regenerating P2 | ER9, A8 |
| `two_consecutive_fail.json` | parts 1 and 2 fail | job PAUSED after part 2, part 3 still PENDING | DEC-12 |

Plus non-scenario tests:
- **Duplicate name:** `ProjectA_Part01.mp4` is pre-created in the output dir → the new file is `_v2`, the original is byte-identical, and a DUPLICATE_RENAMED event exists (DL5, A4).
- **Stop safely:** stop during GENERATING → that part still DOWNLOADED, the next stays PENDING, job PAUSED (DEC-15).
- **Logs:** run with a script containing a unique marker string → the marker is absent from `logs/automation.log` and the `events` table (P4).

## 4. Resume and crash tests (`test_resume.py`, A7, ER8)

For each kill point, raise `SystemExit` inside the fake at that point, then build a fresh app, worker, and store on the same DB and files, and call Resume:

| Kill point | Expected after restart + Resume |
|------------|----------------------------------|
| after SUBMIT_STARTED, before SUBMITTED | part FAILED `UNCERTAIN_SUBMIT`; no auto-resubmit; manual Retry → completes |
| after SUBMITTED | re-polls the same `vids_part_ref`; generation count unchanged |
| after GENERATION_READY | re-downloads; generation count unchanged |
| during download (temp file present) | orphan moved; re-download; DOWNLOADED |
| after DOWNLOAD_VERIFIED, before next part | part stays DOWNLOADED; next part proceeds |
| DOWNLOADED but output file deleted manually | → READY → re-downloaded, not regenerated |
| job RUNNING at startup | → PAUSED with a RECONCILED event |

"Generation count" means the fake's persisted counter. It's the proof that no work was silently regenerated.

## 5. Acceptance matrix

| ID | Criterion | Automated test(s) | Manual evidence (M5) |
|----|-----------|-------------------|----------------------|
| A1 | Single job end to end | `test_workflow_fake::test_a1_single_happy`, `test_live_smoke` | demo video part 1 |
| A2 | Multi-part mapping and order | `test_workflow_fake::test_a2_mapping_order` (fake records which image and script hash went to which submit) | demo video part 2 |
| A3 | Files verified before success | `test_downloads::test_a3_*` (empty/bad/temp files rejected) | — |
| A4 | No silent overwrite | `test_downloads::test_a4_duplicate_v2` | — |
| A5 | Quota stop saves work, resumable | `quota_after_2`, `quota_after_2_no_clip_dl` | demo video part 3 |
| A6 | Retry failed part without regenerating others | `gen_fail_part2` | demo video part 4 |
| A7 | Restart recovers state | `test_resume.py` (all rows) | demo video part 5 |
| A8 | Actionable logs for 6 failure types | `timeout_part1`, `login_required`, `ui_missing_generate`, `download_interrupt_part1`, `disk_error_part2`, `network_error_part1` | TEST_REPORT excerpt |
| A9 | UI shows complete/pending/failed/blocked | `test_api::test_a9_job_view_counts` | screenshot |
| A10 | Automated tests + live smoke | whole suite + `test_live_smoke` | TEST_REPORT |
| A11 | Clean Windows setup | — | M5.2 checklist §8 |
| A12 | No secrets | `test_logging::test_a12_redaction` | M5.4 grep output |

## 6. Live smoke test (`pytest -m live`)

1. Preconditions: signed in through `--login`, selectors verified (DEC-25), `config.json` present.
2. Creates job `Smoke_<utc>` in single mode using `input/Sample_Project/part_01`.
3. Runs it through the real worker. Timeout = generation + download timeouts + 120 s.
4. Asserts: job COMPLETED, file exists, `verify_video` passes, the name matches the template, the report exists, and no ERROR events.
5. Uses **1 generation**. It prints a warning banner before starting.

## 7. Demo video script (H6, about 6–8 min)

1. **Setup view:** app running, signed in, Sample_Project imported, validation passes, review plan shown.
2. **Single-part run:** start → progress panel → file appears in the output folder with the correct name.
3. **Multi-part run:** 3 parts → per-part progress → Final (or clips plus the limitation note) → order verified by playing them.
4. **Simulated limit/error:** `python app.py --fake --scenario tests/scenarios/quota_after_2.json` → quota banner, 2 files saved, part 3 BLOCKED.
5. **Resume:** restart the app (same scenario, with quota reset), Resume → part 3 completes, parts 1–2 untouched.
6. **Retry:** run the `gen_fail_part2` scenario → Retry Failed Parts → only part 2 reruns.
7. **Report:** Export Report → show the JSON/MD.

State aloud that steps 4–6 use the fake controller to simulate errors, because a real quota can't be triggered on demand.

## 8. Clean Windows 11 checklist (A11)

- [ ] Fresh user account or VM, no Python installed.
- [ ] Install Python from python.org with "Add to PATH" ticked. Install Chrome stable.
- [ ] Clone or unzip the repo. Follow the README commands **verbatim**.
- [ ] `pytest` passes.
- [ ] `python app.py --login` → sign in.
- [ ] Import Sample_Project → validate → run a single job → file appears in `output\`.
- [ ] Open Output Folder works.
- [ ] Note any README gaps and fix them.
