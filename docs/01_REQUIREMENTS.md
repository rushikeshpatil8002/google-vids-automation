# 01 — Requirements (Final)

**Status:** Final, v1.0 (2026-10-24). Source: `00_ORIGINAL_BRIEF.pdf`. Every ambiguity in the brief is resolved in `02_DECISIONS.md` (referenced as `DEC-xx`).

**Tags**
- **[HARD]** Must be built and demonstrated.
- **[COND]** Built and demonstrated only if M1 shows Google Vids supports it. Otherwise the documented fallback is mandatory.
- **[DOC]** A documentation or deliverable requirement.

Anything not listed here is out of scope.

---

## T — Technology and structure

- **T1** [HARD] Python 3.11+ and Selenium 4. The UI is HTML/CSS (plus vanilla JS), served by a local Flask app (DEC-10).
- **T2** [HARD] Browser-automation code and UI/web code live in separate modules. UI code never imports Selenium.
- **T3** [HARD] Job and part state live in SQLite. Job manifests and configuration use JSON.
- **T4** [HARD] Rotating log file plus structured error records (the `events` table).
- **T5** [HARD] Long-running automation runs in a background worker thread, so the UI stays responsive (DEC-10).
- **T6** [HARD] Folder layout as in `03_ARCHITECTURE.md` §2. It's a superset of the brief's recommended layout.

## J — Job creation

- **J1** [HARD] A job has: project name, output folder, naming convention (clip template), and mode: `single`, `multi_part`, or `separate_clips` (DEC-03, DEC-05).
- **J2** [HARD] A part has: sequence number, optional title, script (typed text or a `.txt` file), and 1..N reference images. N is the limit set in DEC-04.
- **J3** [HARD] Parts can be added, removed, reordered, and validated before the job starts.
- **J4** [HARD] A job can be imported from a JSON manifest in the brief's format, plus the optional fields in DEC-14.

## V — Validation (runs before any automation)

- **V1** [HARD] Every script is non-empty after trimming, is valid UTF-8, and fits within `limits.max_script_chars` when that is set (DEC-01). Scripts are never trimmed or altered.
- **V2** [HARD] Every image exists, opens and decodes with Pillow, has a real format of PNG, JPEG, or WEBP, and is within `limits.max_image_mb`.
- **V3** [HARD] Project names and generated filenames are safe: sanitized and free of path traversal (`03_ARCHITECTURE.md` §6).
- **V4** [HARD] The output directory resolves to an absolute path. It's created if missing, proven writable with a write test, and isn't inside `input/`, `data/`, or `logs/`.
- **V5** [HARD] All required fields are present. Mode `single` has exactly 1 part. The other modes have at least 1 part and at most `limits.max_parts_per_job`. Image count per part is within the limit.

## R — Review before start

- **R1** [HARD] Before Start is enabled, a plan screen shows: mode, part count, each part's script filename, character count, image filenames and thumbnails, the intended output filename for every file, and any known limitation warning (e.g. "combined export not supported").

## E — Run and monitor

- **E1** [HARD] Before submitting anything, open Vids and confirm the user is signed in and the editor is reachable.
- **E2** [HARD] Submit content using only the UI steps verified in M1. Every transition is recorded (ST4).
- **E3** [HARD] Detect generation completion from **visible UI indicators** with explicit waits. Fixed sleeps are never the completion signal.
- **E4** [HARD] The UI shows the current part, current step, elapsed time for the current part, and part status.
- **E5** [HARD] Detect and classify: success, generation failure, timeout, sign-in prompt, quota/limit message, and UI element missing.

## S — Single mode

- **S1** [HARD] One job record, one part record.
- **S2** [HARD] Download only after the UI shows the output is ready.
- **S3** [HARD] The job is `COMPLETED` only after the local file passes DL2 verification.

## MP — Multi-part

- **MP1** [HARD] The order of parts, and the mapping from each script to its images, is preserved from input through Vids to the output filenames.
- **MP2** [COND] `multi_part` mode uses **one** Vids project, with one scene per part. If unsupported, the job runs as `separate_clips` and the limitation is shown at review (R1) and in the report (DEC-02).
- **MP3** [HARD] Each part has its own status: `PENDING`, `SUBMITTED`, `GENERATING`, `READY`, `DOWNLOADED`, `FAILED`, `BLOCKED_QUOTA` (`04_STATE_AND_RECOVERY.md`).
- **MP4** [HARD] Never assume a multi-scene editor exists. Use the `capabilities` that M1 verified.
- **MP5** [COND] When combined export is supported, export the whole project as `{project}_Final.mp4` and verify it. Otherwise save individual clips and report "no combined export produced" (DEC-02).

## IM — Reference images

- **IM1** [HARD] Show thumbnails and filenames for every part before submission.
- **IM2** [HARD] Never substitute an image, attach an image to the wrong part, or change the user's script.
- **IM3** [HARD] If an image upload fails, retry up to `retries.upload_max_attempts` times. If it still fails, nothing is submitted for that part: it stays `PENDING` with `error_code=UPLOAD`, the job pauses (`PAUSED`), and later parts are not started. Resume retries the upload (DEC-26). Never generate with incomplete inputs.

## DL — Download and naming

- **DL1** [HARD] Chrome downloads go to a dedicated staging folder (`data/downloads_staging/<job_id>/`). Completion is detected by watching that folder (DEC-16).
- **DL2** [HARD] A file is verified when it exists, its extension is in `downloads.accepted_extensions`, its size is at least `limits.min_video_bytes`, and the container signature matches (`ftyp` box for MP4). Only then is it moved into the output folder and the status set.
- **DL3** [HARD] Default names come from templates: `{project}_Part{part:02d}.mp4` for clips, `{project}_Final.mp4` for a combined export, and `{project}_Partial.mp4` for a partial export (DEC-05).
- **DL4** [HARD] Store the original Chrome download filename in the part or job record.
- **DL5** [HARD] Never overwrite. If the target exists, use the first free `_v2`, `_v3`, … suffix, and record it in events and the report (DEC-06).

## ER — Error handling (all mandatory)

| ID | Situation | Required behavior |
|----|-----------|-------------------|
| **ER1** | Quota or usage limit message | Submit nothing new. Download or export everything already completed and verify it. Mark the current and all pending parts `BLOCKED_QUOTA`. Job status becomes `BLOCKED_QUOTA`. The UI and report state that the quota can't be bypassed and when to resume. |
| **ER2** | Network timeout / slow page | Explicit waits. A failed step retries with backoff (`retries.backoff_seconds`) up to `retries.step_max_attempts` times. On the final failure, capture diagnostics and mark the part `FAILED`. |
| **ER3** | Sign-in page, session expired, CAPTCHA, or 2FA | Job status becomes `WAITING_FOR_USER` with a clear banner. The human acts in the Chrome window, then clicks Resume. No credential handling. |
| **ER4** | UI element not found / UI changed | No blind repeated clicks. Capture diagnostics: screenshot, URL without query string, the missing selector key and step. Mark the part `FAILED` and **pause the job** (`PAUSED`), because the failure is likely systemic. |
| **ER5** | Generation failed (error shown in Vids, or generation timeout) | Only that part becomes `FAILED`. Other parts continue. It can be retried alone. |
| **ER6** | Download interrupted or incomplete | Detect temp files (`.crdownload`, `.tmp`, `.part`). Wait, then retry the download (not the generation) up to the limit. If it still fails, mark the part `FAILED` with the generation kept, so a retry re-downloads instead of regenerating. |
| **ER7** | Duplicate filename | Apply DL5. |
| **ER8** | Browser or Python crash | State is committed at every milestone (ST4). On startup, and again on Resume, run reconciliation (`04_STATE_AND_RECOVERY.md` §6). |
| **ER9** | Disk full / permission denied | Stop new generations. Job status becomes `WAITING_FOR_USER`, showing the exact path and OS error. State and logs are preserved. Resume works after the user fixes it. |

Extra rule (DEC-12): if `retries.consecutive_part_failures_to_pause` parts fail in a row (default 2), the job pauses instead of burning through the remaining parts.

## ST — State and resume

- **ST1** [HARD] The jobs table holds at least: job_id, project_name, mode, created_at, updated_at, overall_status, output_dir, last_error. Full schema in `04_STATE_AND_RECOVERY.md` §4.
- **ST2** [HARD] The parts table holds at least: part_id, job_id, sequence, script_path, image_paths, status, retry_count, vids refs, output_path, error_message.
- **ST3** [HARD] The events table holds: ts, job_id, part_id, step, severity, message, screenshot_path.
- **ST4** [HARD] Commit after each of: validated, submit attempted, submitted, generation started, generation ready, download started, download verified, export started, export verified.
- **ST5** [HARD] Resume checks the database **and** the actual files before acting. Never regenerate a part that is verified on disk. Never regenerate when re-downloading is possible.

## U — UI

- **U1** [HARD] Job form: project name, mode selector, output folder, naming template, add part.
- **U2** [HARD] Part card: script text box or `.txt` upload, image upload (multiple), thumbnails, move up/down, validation status, remove.
- **U3** [HARD] Buttons: **Validate Job, Start, Stop Safely, Resume, Retry Failed Parts, Open Output Folder, Export Report**. Each is enabled only in valid states (`03_ARCHITECTURE.md` §8).
- **U4** [HARD] Progress panel: overall status, per-part status, current step, retry count, last error, completed/total.
- **U5** [HARD] Clear banners for sign-in needed, permission prompts, usage limit reached, unsupported workflow, and LAN mode ("automation runs on the host machine").

## SE — Selenium standards

- **SE1** [HARD] Use `WebDriverWait` and expected conditions. `time.sleep` is allowed only inside polling loops that also check a condition and a deadline.
- **SE2** [HARD] All selectors live in `automation/vids_selectors.json`. Only the page-object layer (`vids_controller.py`) reads them. Each selector has ordered fallback strategies.
- **SE3** [HARD] Use a dedicated download directory, set through Chrome DevTools Protocol (DEC-16).
- **SE4** [DOC] Document the supported Chrome version and the verification date in `vids_selectors.json._meta` and the README.
- **SE5** [HARD] Failure diagnostics: screenshot, URL without query string, page title, selector key, and step. **No full-page HTML dumps in normal runs** (DEC-18).
- **SE6** [HARD] Respect Google's terms and limits. If something isn't allowed, stop and report it.

## P — Security and privacy

- **P1** [HARD] No passwords, recovery codes, cookies, or tokens in code, config, logs, or git.
- **P2** [HARD] Use a dedicated Chrome profile in `data/chrome-profile/` (DEC-11). Never use or copy the user's everyday Chrome profile.
- **P3** [DOC+HARD] The README documents where inputs, outputs, state, and logs live. The UI has a "Delete job data" action that removes the job's DB rows, its `input/jobs/<slug>/` copy, staging files, and thumbnails. Output videos are removed only if the user ticks a separate box.
- **P4** [HARD] Logs and events never contain script text.
- **P5** [HARD] No bypassing CAPTCHA, quota, or account controls.

## F — Milestone 1 feasibility (must come first)

- **F1** [DOC] Vids access confirmed. Account type and visible limits recorded.
- **F2** [DOC] Script + reference image → generated clip works (image-to-video). Prompt limit and image limit recorded.
- **F3** [DOC] Multi-scene support in one project, and combined export support, recorded.
- **F4** [DOC] Download method and output format recorded, for both a single clip and the full project.
- **F5** [DOC] Each step is classified as automatable-stable, automatable-fragile, or human-in-the-loop.
- **F6** [DOC] Feasibility report with screenshots, a supported/unsupported list, and a go/no-go (`07_FEASIBILITY_REPORT.md`).

## A — Acceptance criteria

The test mapping is in `08_TEST_PLAN.md` §5.

- **A1** A single-part job: created, validated, generated, downloaded, renamed, and marked complete.
- **A2** A multi-part job preserves script/image mapping and order.
- **A3** Every downloaded file is checked for existence and size before success is recorded.
- **A4** A duplicate filename never causes a silent overwrite.
- **A5** On quota, no new generation starts, completed outputs are saved, and pending parts stay resumable.
- **A6** A failed part can be retried without regenerating parts already downloaded.
- **A7** Restarting the app recovers state and reconciles files.
- **A8** Each of these is logged with an actionable message: timeout, network loss, missing element, login prompt, download failure, disk error.
- **A9** The UI clearly shows completed, pending, failed, and blocked parts.
- **A10** Automated tests cover validators and state, plus an end-to-end smoke test of the live workflow.
- **A11** Setup works on a clean Windows 11 machine.
- **A12** No secrets in git. Logs contain no credentials or tokens.

## H — Handover deliverables

- **H1** [DOC] Source code with clean module separation.
- **H2** [DOC] `requirements.txt` and `config.example.json`, with no secrets.
- **H3** [DOC] README: install, setup, run, troubleshooting, limitations.
- **H4** [DOC] A sample input project with 3 or more parts and images (`input/Sample_Project/`).
- **H5** [DOC] Automated test results, plus a quota-simulation test report (`docs/TEST_REPORT.md`, produced in M5).
- **H6** [DOC] Demo video: single-part run, multi-part run, simulated limit/error, completed work saved, resume.
- **H7** [DOC] Known limitations and human-in-the-loop steps (README §Limitations).
- **H8** [DOC] Confirmation of where outputs and logs are saved.

## DONE — Task completion

The task is complete when:
- the supported workflow runs end to end;
- completed downloads survive a quota hit and an interruption;
- resume and retry are proven;
- source and docs are delivered; and
- every Vids limitation is documented with the compliant fallback used.

Nothing unsupported is ever presented as working.
