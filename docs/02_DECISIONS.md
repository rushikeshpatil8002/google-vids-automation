# 02 — Decisions (Final)

These decisions settle every ambiguity in the client brief. They're binding for development. Where a decision depends on what M1 discovers, the branch is spelled out in advance, so **there are no open questions**. M1 only selects a branch.

Format for each entry: **Decision**, **Why**, **Affects**.

---

### DEC-01 — What a "script" is
- **Decision:** A part's script is the **text prompt** submitted to the Vids AI video generation feature ("Generate video" / Veo, image-to-video), together with that part's reference image(s). It's sent **verbatim**. No trimming, rewriting, or translating. Narration, voiceover, avatars, music, and the Vids "help me create" storyboard feature are out of scope.
- **Validation:** non-empty after trimming, valid UTF-8, and length ≤ `limits.max_script_chars`. That limit is `null` until M1 records the UI's prompt limit. A script over the limit **fails validation**. It's never cut down.
- **Why:** This is the only reading where "each part has its own script and its own reference images" maps to one Vids feature.
- **Affects:** V1, IM2, F2.

### DEC-02 — Is a combined video required?
- **Decision:** A combined video is produced **only through Google Vids' own project export**, and only in `multi_part` mode. Branches, chosen by M1 capabilities:
  - `multi_scene=true, combined_export=true` → one project, one scene per part, then export `{project}_Final.mp4`.
  - `multi_scene=false` → `multi_part` jobs run with the `separate_clips` workflow. Limitation code `MULTI_SCENE_NOT_SUPPORTED` is shown at review (R1) and in the report. No Final file.
  - `multi_scene=true, combined_export=false` → individual clips only. Limitation code `COMBINED_EXPORT_NOT_SUPPORTED`.
- **Why:** The brief makes the combined export conditional and forbids pretending.
- **Affects:** MP2, MP5, R1, report.

### DEC-03 — The three modes
| Mode | Vids projects | Files produced |
|------|---------------|----------------|
| `single` | 1 new project | `{project}_Part01.mp4` |
| `separate_clips` | 1 new project **per part**, named `{project} - Part NN` | one `{project}_PartNN.mp4` per part |
| `multi_part` | 1 shared project, one scene per part, in sequence order | `{project}_Final.mp4`, plus per-part clips when `per_clip_download=true` |
- **Why:** These are the brief's three modes, with the difference between multi-part and separate clips made precise.
- **Affects:** J1, S1, MP2.

### DEC-04 — Number of reference images per part
- **Decision:** `limits.max_reference_images_per_part` defaults to **1** until M1 verifies how many images the generation feature accepts. M1 sets it to the verified number. A part with more images than the limit **fails validation**. Extra images are never silently dropped.
- **Why:** One image is the guaranteed image-to-video case. Dropping images silently would break IM2.
- **Affects:** V5, IM2.

### DEC-05 — Naming convention
- **Decision:** Per-job templates in Python `str.format` syntax. Allowed tokens: `{project}` (sanitized project slug), `{part}` (int, use `{part:02d}`), `{title}` (sanitized part title, or empty).
  - Defaults: clip `{project}_Part{part:02d}`, final `{project}_Final`, partial `{project}_Partial`.
  - The clip template must contain `{part}`. Templates are validated at job validation by rendering every part's name and checking that none collide.
  - The rendered result passes through the filename sanitizer (V3). The extension comes from the verified download, normally `.mp4`.
- **Affects:** J1, DL3.

### DEC-06 — Duplicate filenames
- **Decision:** Never ask, never overwrite. Use the first free name among `name_v2.mp4`, `name_v3.mp4`, … . Record a `WARNING` event and show it in the report.
- **Why:** Runs are unattended, and a prompt would stall the job. The brief allows either option.
- **Affects:** DL5, A4.

### DEC-07 — Platforms
- **Decision:** Acceptance target is **Windows 11 x64** with current Chrome stable. Also supported: **macOS 14+** on Apple Silicon. Python 3.11–3.14. Linux isn't a target, but nothing should break on it.
- **Why:** A clean Windows machine installs the latest Python from python.org, so the supported range must include it (A11).
- **Affects:** A11, README.

### DEC-08 — Google account
- **Decision:** Any Google account with Vids AI video generation. Development uses the developer's Google AI Pro account. The account and plan are recorded in the feasibility report. Quota and limit messages are kept as configurable text patterns, so different plans' wording can be added without code changes.
- **Affects:** F1, ER1.

### DEC-09 — Sample input
- **Decision:** `input/Sample_Project/` ships with 3 parts (prompt + reference PNG each) and `job.json`. It's used for the M2/M3 demos, the smoke test, and the handover demo.
- **Affects:** H4.

### DEC-10 — App shape and concurrency
- **Decision:** A Flask app serves the UI and a JSON API. **One** background worker thread runs **one job at a time**, using one Chrome instance. The UI polls job status every 2 seconds. No websockets. Commands (start, stop, resume, retry) go through a thread-safe queue to the worker.
- **Why:** One browser session can't safely run two jobs. Polling is simple and robust.
- **Affects:** T1, T5, E4.

### DEC-11 — Browser and sign-in
- **Decision:** A dedicated Chrome profile at `data/chrome-profile/` (gitignored).
  - **Default `browser.mode = "attach"`:** the app starts a **normal** Chrome process with `--user-data-dir=<profile>` and `--remote-debugging-port=<port>` bound to 127.0.0.1. Selenium attaches through `debuggerAddress`.
  - **Fallback `browser.mode = "launch"`:** Selenium starts Chrome itself with the same profile.
  - The human signs in once (`python app.py --login`, or the UI's "Open browser to sign in"). The session persists in the profile.
  - M1 confirms which mode lets sign-in work.
- **Why:** Google may refuse sign-in inside a Selenium-launched browser. In attach mode the human signs in to an ordinary Chrome window, so nothing is bypassed. Chrome 136+ ignores the debugging port on the *default* profile directory, but allows it on a dedicated one.
- **Affects:** P2, ER3, E1.

### DEC-12 — Timeouts, retries, failure policy
- **Decision:** All values live in config, with these defaults:
  - **Timeouts:** page load 60 s, element 30 s, upload 120 s, generation 900 s (polled every 5 s), download start 120 s, download complete 600 s, export render 1800 s.
  - **Retries:** a failed *step* retries 3 attempts with backoff of 5/15/45 s. A *part* gets 1 automatic retry of the whole part within a run, and only for ER2/ER5 causes. If 2 parts fail in a row, the job pauses.
  - Manual "Retry Failed Parts" has no limit and increments `retry_count`.
- **Affects:** ER2, ER5, E3.

### DEC-13 — Sequential generation
- **Decision:** Parts are generated **one at a time, in sequence order**, even if Vids allows parallel generations.
- **Why:** Deterministic image-to-part mapping, a clean quota stop, and simple recovery.
- **Affects:** MP1, ER1.

### DEC-14 — Manifest format and input ownership
- **Decision:** The manifest is a superset of the brief's example.
  - Required: `project_name`, `mode`, `parts[].part`, one of `parts[].script_file` / `parts[].script_text`, `parts[].reference_images` (a list).
  - Optional: `output_dir` (default `output/<project_slug>`), `clip_template`, `final_template`, `parts[].title`.
  - Relative paths resolve against the **project root**.
  - On job creation, every script and image is **copied byte-for-byte** to `input/jobs/<project_slug>/part_NN/` as `script.txt` and `reference_1.<ext>`, `reference_2.<ext>`, … . The extension comes from the detected format. The DB stores those copies. Typed scripts are written to `script.txt` as UTF-8.
  - `input/jobs/` is the app's own area. Users keep their source folders anywhere, e.g. `input/Project_A/part_01/` as in the brief, and those are never modified.
  - If another job in the DB already uses the slug, the new slug gets `_2`, `_3`, … .
- **Why:** Jobs become self-contained and resumable, even if the user edits or moves the source files. User filenames are never used as paths (V3).
- **Affects:** J4, ST2, P3.

### DEC-15 — Stop Safely semantics
- **Decision:** Stop Safely sets a stop flag that is checked at safe points.
  - Nothing new is submitted after the flag is set.
  - A generation already in flight is **awaited** (up to its timeout) and downloaded, because its quota is already spent.
  - Then the job becomes `PAUSED`.
  - Resume continues from the next part that isn't done.
- **Affects:** U3, ER8.

### DEC-16 — Download detection
- **Decision:**
  1. Before each download, set Chrome's download folder through CDP: `Browser.setDownloadBehavior` with behavior `allow` and downloadPath `data/downloads_staging/<job_id>/`.
  2. Move any leftover files there into `data/downloads_staging/_orphans/` and record a WARNING event.
  3. Snapshot the folder, then trigger the download.
  4. Poll every 1 s. Done means: a new file that isn't hidden or temp (`.crdownload`, `.tmp`, `.part`), with no temp files left, and its size unchanged across 2 consecutive polls.
  5. Verify the file (DL2), then move it to the output folder with the DL5 naming. Moves retry on `PermissionError` 10 times at 0.5 s intervals. The size is re-checked after the move.
- **Affects:** DL1, DL2, ER6.

### DEC-17 — Quota detection
- **Decision:**
  - A quota or limit is detected when visible page text matches any regex in `vids_selectors.json → text_patterns.quota` (case-insensitive), or when the generate control is present but disabled together with a matching message.
  - The patterns are seeded in M1 from the real UI (if seen) and Google's help-page wording, and can be extended in config without code changes.
  - Quota is checked before every submit and during every generation poll.
- **Affects:** ER1, E5.

### DEC-18 — Failure diagnostics
- **Decision:**
  - Normal runs save a PNG screenshot to `logs/screenshots/<job>_<partseq>_<step>_<utc>.png` and record: URL with the query string and fragment removed, page title, selector key, and step.
  - Full DOM dumps are made **only** by `tools/ui_probe.py` during M1, saved to `docs/feasibility/raw/` (gitignored).
  - Screenshots may show the account avatar. The README says so.
- **Affects:** SE5, P4.

### DEC-19 — No local video merging
- **Decision:** No ffmpeg or local concatenation. Only the brief's compliant fallback applies: individual clips plus a clear report.
- **Why:** Not requested, adds a binary dependency, and changes the deliverable.
- **Affects:** MP5 scope.

### DEC-20 — Reports
- **Decision:** At the end of every run (completed, paused, blocked, or failed), write `{project}_report.json` and `{project}_report.md` into the output folder. The Export Report button downloads the current report on demand.
- **Contents:** job info, mode, capabilities used, limitations, per-part status/output/original filename/size/retries/error, final export status, duplicate renames, timestamps.
- **Affects:** U3, E (finish), ER1.

### DEC-21 — LAN access (`--host`)
- **Decision:** Default bind is `127.0.0.1:5000`. `--host 0.0.0.0` is a developer flag.
  - It prints a warning and shows a UI banner: "Automation, browser and files are on the host machine".
  - Open Output Folder is disabled for non-loopback clients.
  - No authentication is added. LAN mode is for trusted networks only, and that's documented.
- **Affects:** U5, README.

### DEC-22 — Retry semantics
- **Decision:** Retry Failed Parts applies to `FAILED` parts only.
  - Parts with `generated_at` set and a known Vids reference → `READY` (re-download only, no new generation).
  - All others → `PENDING`.
  - `retry_count += 1`.
  - `DOWNLOADED` parts are never touched.
  - `BLOCKED_QUOTA` parts return to `PENDING` through **Resume**, not Retry.
- **Affects:** A6, ST5.

### DEC-23 — Multi-part without per-clip download
- **Decision:** If `per_clip_download=false` in `multi_part` mode, a part's successful end state is `READY` (generated and placed in the project), with `output_path = null`. The job completes only when `{project}_Final.mp4` is verified. The report says "included in final export" for each such part.
- **Affects:** MP3, MP5.

### DEC-24 — Quota hit in multi-part mode
- **Decision:**
  - If `per_clip_download=true`, completed clips are already downloaded and nothing more is needed.
  - Otherwise, if at least one part is `READY`, export the project as it is to `{project}_Partial.mp4` and verify it, with job `final_export_status = PARTIAL_DOWNLOADED`.
  - On a later Resume, generation continues in the **same** Vids project (`jobs.vids_project_ref`), and the Final export happens once all parts are done.
- **Why:** This is how ER1's "save all completed outputs" applies when only whole-project export exists.
- **Affects:** ER1, A5.

### DEC-25 — Live controller safety gate
- **Decision:** `VidsSeleniumController` refuses to start (raising `ConfigError`) if any capability in `vids_selectors.json` is `null`, or if any required selector still contains `TODO_M1`.
- **Why:** This makes it impossible to run live automation against unverified guesses.
- **Affects:** MP4, F5.

### DEC-26 — Image upload failure keeps the part pending
- **Decision:** The brief (§5C) wins over the earlier "part becomes `FAILED`" rule. When an image upload still fails after `retries.upload_max_attempts`:
  - Nothing is submitted for that part. Generate is never clicked.
  - In one transaction, the part **stays `PENDING`** with `error_code=UPLOAD` and `error_message` set, `submit_attempted_at` is cleared, and an `UPLOAD_FAILED` event is written.
  - The job pauses immediately (`PAUSED`) with `last_error` = "Image upload failed for part N. Check the image, then Resume."
  - Later parts are **not** started, which protects scene order (MP1).
  - Resume retries the upload for that part, because it is still `PENDING`. `error_code` and `error_message` are cleared when it reaches `SUBMITTED`.
  - It does not count toward `consecutive_failures` or the automatic part retry (DEC-12), because the part did not fail.
  - `PENDING → FAILED` remains only for ER4 before submit.
- **Contract:** `submit_generation` raises `UploadError` only **before** Generate is clicked. That makes clearing `submit_attempted_at` safe. Without the clear, reconciliation (`04` §6) would mark the part `UNCERTAIN_SUBMIT` instead of retrying the upload.
- **Why:** Brief §5C: "retry within a limit and keep the part pending rather than continuing with incomplete inputs."
- **Affects:** IM3, MP1, `03` §5.1–5.2, `04` §1/§3/§5/§8, `05` §6, `08` §3.

### DEC-27 — WEBP reference images
- **Decision:** WEBP is accepted at validation (brief §5C) but Vids uploads only PNG/JPEG (M1). At job creation a WEBP image is converted **losslessly** to PNG in the app's own copy (`input/jobs/<slug>/part_NN/reference_N.png`). Pixels are identical; the user's source file is never touched. The review plan (R1) shows "converted to PNG for upload".
- **Why:** Keeps the brief's format list without uploading a format Vids rejects. Not a substitution under IM2: same image, different container.
- **Affects:** V2, IM2, R1.

### DEC-28 — Prompt mismatch
- **Decision:** If the prompt read back from Vids differs from the script (05 §7), the controller raises `PromptMismatchError` (a kind of `UploadError`). It is handled exactly like DEC-26 (part stays `PENDING`, job `PAUSED`, Resume retries), with `error_code=PROMPT_MISMATCH` and `last_error` = "The prompt shown in Vids didn't match the script for part N. Resume to try again."
- **Affects:** DEC-26, IM2, 05 §7.

### DEC-29 — Generated clips live only in the open page
- **Decision:** M1 showed that Vids keeps a generated clip only in the open editor page until it is inserted. So: the controller inserts the clip immediately after `READY`; it uses its own tab (handle saved in `data/controller_tab.txt`), reuses it across runs, and never closes or reloads it mid-part. After a restart, a `SUBMITTED`/`GENERATING` part whose clip is no longer in the page becomes `FAILED` with `error_code=UNCERTAIN_SUBMIT` (04 §6).
- **Affects:** ER8, 04 §6, 05.

### DEC-30 — Clip downloads (M1 branch A, per_clip_download=false)
- **Decision:** `single` and `separate_clips` save a part's clip by exporting its one-scene project (File → Download → MP4) after deleting the empty scene Vids adds on insert; M1 verified the file contains only the clip. `multi_part` uses DEC-23: parts end `READY` and the job completes on the verified `{project}_Final.mp4`.
- **Affects:** DL3, MP5, DEC-23, 03 §5.2.
