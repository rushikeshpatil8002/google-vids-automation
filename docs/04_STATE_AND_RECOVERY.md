# 04 — State and Recovery

## 1. Job statuses (`jobs.overall_status`)

| Status | Meaning | Terminal? |
|--------|---------|-----------|
| `DRAFT` | Being edited, or failed validation | no |
| `VALIDATED` | Passed V1–V5. Plan available. Editing returns it to `DRAFT`. | no |
| `RUNNING` | Worker is processing | no |
| `WAITING_FOR_USER` | Needs human action: sign-in, CAPTCHA, 2FA, permission prompt, disk/permission error. Resume after fixing. | no |
| `PAUSED` | Stopped safely, a crash was recovered, UI changed (ER4), an image upload failed (DEC-26), or too many consecutive failures | no |
| `BLOCKED_QUOTA` | Usage limit hit. Completed work saved. Resume after the limit resets. | no |
| `COMPLETED` | Every part succeeded, and the Final file is verified where applicable | yes |
| `COMPLETED_WITH_ERRORS` | Run finished, but ≥1 part `FAILED` or the final export `FAILED` | yes (Retry allowed) |
| `FAILED` | Couldn't run at all (e.g. browser won't start, selectors unverified per DEC-25). `last_error` says why. | yes (Retry/Resume allowed) |

## 2. Part statuses (`parts.status`)

`PENDING`, `SUBMITTED`, `GENERATING`, `READY`, `DOWNLOADED`, `FAILED`, `BLOCKED_QUOTA`. These are exactly the brief's states.

Validation results are kept separately in `parts.validation_errors`, a JSON list.

**Success end state:** `DOWNLOADED`. Under DEC-23 only (multi_part without per-clip download), `READY` plus a verified Final file.

## 3. Allowed transitions

`state_store` raises an error on anything else.

| From | To | Trigger |
|------|----|---------|
| `PENDING` | `SUBMITTED` | Generate click confirmed. `vids_part_ref` saved in the same transaction. |
| `PENDING` | `FAILED` | ER4 before submit, or ER2 after the last step retry |
| `PENDING` | `BLOCKED_QUOTA` | Quota stop (ER1) |
| `SUBMITTED` | `GENERATING` | Progress indicator seen |
| `SUBMITTED` | `READY` | Ready indicator seen before any progress indicator |
| `SUBMITTED`/`GENERATING` | `READY` | Ready indicator seen. `generated_at` set. |
| `SUBMITTED`/`GENERATING` | `FAILED` | Error shown, generation timeout, or ER4 |
| `SUBMITTED`/`GENERATING` | `BLOCKED_QUOTA` | Quota message appeared in response to this submit |
| `READY` | `DOWNLOADED` | DL2 verification passed and the file was moved |
| `READY` | `FAILED` | Download failed after retries. `generated_at` is kept, so a retry re-downloads. |
| `FAILED` | `READY` | Retry, when `generated_at` and refs are present (DEC-22) |
| `FAILED` | `PENDING` | Retry otherwise (DEC-22) |
| `BLOCKED_QUOTA` | `PENDING` | Resume (ER1) |
| `DOWNLOADED` | — | **Never leaves this state.** If reconciliation finds its file missing, see §6. |

Exception: reconciliation may set `DOWNLOADED → READY` **only** when the verified file has disappeared from disk. It logs a WARNING.

Not a transition: an image upload that still fails after retries leaves the part `PENDING` (IM3, DEC-26). In one transaction, `error_code=UPLOAD` and `error_message` are set and `submit_attempted_at` is cleared. `PENDING → SUBMITTED` clears `error_code` and `error_message`.

## 4. Schema (`state_store.py`, schema_version = 1)

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS jobs (
  job_id                  TEXT PRIMARY KEY,               -- uuid4 hex
  project_name            TEXT NOT NULL,                  -- as entered (validated)
  project_slug            TEXT NOT NULL UNIQUE,           -- used for input/ and filenames
  mode                    TEXT NOT NULL CHECK (mode IN ('single','multi_part','separate_clips')),
  effective_mode          TEXT,                           -- after DEC-02 resolution at run time
  output_dir              TEXT NOT NULL,                  -- absolute
  clip_template           TEXT NOT NULL,
  final_template          TEXT NOT NULL,
  partial_template        TEXT NOT NULL,
  overall_status          TEXT NOT NULL,
  vids_project_ref        TEXT,                           -- multi_part shared project (URL, no query)
  final_export_status     TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'
                          CHECK (final_export_status IN ('NOT_APPLICABLE','PENDING','EXPORTING',
                                 'DOWNLOADED','PARTIAL_DOWNLOADED','FAILED','NOT_SUPPORTED')),
  final_output_path       TEXT,
  final_original_filename TEXT,
  partial_output_path     TEXT,
  limitations             TEXT NOT NULL DEFAULT '[]',     -- JSON list of codes (DEC-02)
  consecutive_failures    INTEGER NOT NULL DEFAULT 0,
  created_at              TEXT NOT NULL,                  -- ISO-8601 UTC
  updated_at              TEXT NOT NULL,
  last_error              TEXT
);

CREATE TABLE IF NOT EXISTS parts (
  part_id              TEXT PRIMARY KEY,
  job_id               TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  sequence             INTEGER NOT NULL,                  -- 1..n, contiguous
  title                TEXT,
  script_path          TEXT NOT NULL,                     -- input/jobs/<slug>/part_NN/script.txt
  script_sha12         TEXT NOT NULL,                     -- first 12 hex of sha256 (for logs)
  script_chars         INTEGER NOT NULL,
  image_paths          TEXT NOT NULL,                     -- JSON list, order preserved
  status               TEXT NOT NULL,
  validation_errors    TEXT NOT NULL DEFAULT '[]',
  retry_count          INTEGER NOT NULL DEFAULT 0,
  vids_project_ref     TEXT,
  vids_scene_ref       TEXT,
  vids_part_ref        TEXT,
  submit_attempted_at  TEXT,
  generated_at         TEXT,
  download_started_at  TEXT,
  output_path          TEXT,
  original_filename    TEXT,
  output_bytes         INTEGER,
  error_code           TEXT,                              -- e.g. QUOTA, TIMEOUT, UI_MISSING, UPLOAD, DOWNLOAD, DISK, GEN_FAILED, UNCERTAIN_SUBMIT
  error_message        TEXT,
  updated_at           TEXT NOT NULL,
  UNIQUE (job_id, sequence)
);

CREATE TABLE IF NOT EXISTS events (
  event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  part_id         TEXT,
  step            TEXT NOT NULL,
  severity        TEXT NOT NULL CHECK (severity IN ('DEBUG','INFO','WARNING','ERROR')),
  message         TEXT NOT NULL,                          -- never script text
  screenshot_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_parts_job ON parts(job_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, event_id);
```

**Reordering with `UNIQUE(job_id, sequence)`:** in a single transaction, first set every sequence to its negative, then assign 1..n.

## 5. Commit points (ST4) and event steps

Each status change and its event row are written **in one transaction**. Event `step` values (fixed list):

`JOB_CREATED, PART_ADDED, PART_REMOVED, PARTS_REORDERED, VALIDATED, VALIDATION_FAILED, RUN_STARTED, BROWSER_STARTED, LOGIN_CHECK, LOGIN_REQUIRED, PROJECT_CREATED, SCENE_ADDED, SUBMIT_STARTED, UPLOAD_RETRY, UPLOAD_FAILED, SUBMITTED, GENERATION_STARTED, GENERATION_READY, GENERATION_FAILED, GENERATION_TIMEOUT, QUOTA_DETECTED, DOWNLOAD_STARTED, DOWNLOAD_RETRY, DOWNLOAD_VERIFIED, DOWNLOAD_FAILED, DUPLICATE_RENAMED, EXPORT_STARTED, EXPORT_VERIFIED, EXPORT_FAILED, UI_ELEMENT_MISSING, DISK_ERROR, STEP_RETRY, STOP_REQUESTED, PAUSED, RESUMED, RETRY_REQUESTED, RECONCILED, ORPHAN_DOWNLOAD, REPORT_WRITTEN, RUN_FINISHED, JOB_DATA_DELETED`

Mandatory commit sequence for one part:
1. `SUBMIT_STARTED`: set `submit_attempted_at`, **before** clicking Generate.
2. `SUBMITTED`: status and `vids_part_ref`.
3. `GENERATION_STARTED`: `GENERATING`.
4. `GENERATION_READY`: `READY` and `generated_at`, committed only after the clip is placed in the project (DEC-29), so `READY` always means "generated and in the project".
5. `DOWNLOAD_STARTED`: `download_started_at`.
6. `DOWNLOAD_VERIFIED`: `DOWNLOADED`, `output_path`, `original_filename`, `output_bytes`.

## 6. Reconciliation

Runs at app startup for non-terminal jobs, and at the start of every Resume or Retry.

| Found state | Condition | Action |
|-------------|-----------|--------|
| Job `RUNNING` | at startup (crash) | → `PAUSED`, event `RECONCILED` "recovered after unexpected stop" |
| Part `DOWNLOADED` | file exists and passes `verify_video` | keep |
| Part `DOWNLOADED` | file missing or invalid | → `READY` if `generated_at` and refs are set (re-download), else → `FAILED` with `error_code=OUTPUT_MISSING`. WARNING. |
| Part `READY` | — | keep. The run loop re-downloads it; it never regenerates. |
| Part `SUBMITTED`/`GENERATING` | `vids_part_ref` set | keep. The run loop polls the controller's saved tab instead of resubmitting. If the clip is no longer in the page (Vids keeps it only until reload, DEC-29) → `FAILED` with `error_code=UNCERTAIN_SUBMIT`. Never auto-resubmits. |
| Part `PENDING` | `submit_attempted_at` set, no `vids_part_ref` | → `FAILED`, `error_code=UNCERTAIN_SUBMIT`, message "A generation may have started before the app stopped. Check the Vids project, then use Retry." This avoids silent double generation. |
| Part `PENDING`/`BLOCKED_QUOTA`/`FAILED` | — | keep (Resume handles `BLOCKED_QUOTA` → `PENDING`) |
| Staging dir | files not linked to any part | move to `_orphans/`, event `ORPHAN_DOWNLOAD` |
| Final export `EXPORTING` | at startup | → `PENDING` (export is re-run; no generation is used) |
| Final export `DOWNLOADED` | file missing | → `PENDING` |

## 7. Quota stop procedure (ER1, DEC-24)

1. Catch `QuotaReachedError`. Event `QUOTA_DETECTED` (WARNING) with the user-safe message.
2. The part that triggered it: if it was being submitted, → `BLOCKED_QUOTA`. If it was `GENERATING`, keep polling it to its end, since it may still finish.
3. Every `PENDING` part → `BLOCKED_QUOTA`.
4. Every `READY` part with `per_clip_download=true` → download now.
5. `multi_part` with `per_clip_download=false` and ≥1 `READY` → partial export to `{partial_template}.mp4`. Then `final_export_status=PARTIAL_DOWNLOADED`.
6. Job → `BLOCKED_QUOTA`, `last_error` = "Usage limit reached. Completed outputs saved. Resume after the limit resets." Write reports.
7. **Never** retry, switch accounts, or submit again in the same run.

## 8. Final job status computation (end of run)

```
if stop requested or upload failed (DEC-26) → PAUSED
elif quota stop → BLOCKED_QUOTA
elif waiting for user → WAITING_FOR_USER
elif all parts succeeded and (final not applicable or final DOWNLOADED) → COMPLETED
else → COMPLETED_WITH_ERRORS
```
