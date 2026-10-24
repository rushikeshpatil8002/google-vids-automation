# 03 — Architecture

## 1. Overview

```
 Browser (UI)                     Python process (app.py)
 ┌──────────────┐   HTTP/JSON    ┌──────────────────────────────────────────────┐
 │ index.html   │ ─────────────▶ │ Flask routes ──▶ state_store (SQLite)        │
 │ app.js       │ ◀───────────── │      │                ▲                       │
 │ (polls 2 s)  │                │      ▼ command queue  │ every transition      │
 └──────────────┘                │  Worker thread ──▶ workflow ──▶ controller ───┼──▶ Chrome ──▶ Google Vids
                                 │                       │                       │      │
                                 │                       └──▶ downloads ◀────────┼──────┘ (staging dir)
                                 │                               │               │
                                 │                               ▼               │
                                 │                          output/<project>/    │
                                 └──────────────────────────────────────────────┘
```

**Hard boundaries**
- Flask routes never call the controller directly. They only write state and enqueue commands.
- Only `vids_controller.py` knows about Selenium selectors.
- Only `workflow.py` changes part and job statuses, and it does so through `state_store`.
- `fake_controller.py` and `vids_controller.py` implement the same interface. The workflow can't tell them apart.

## 2. Folder structure (final)

```
google-vids-automation/
├── CLAUDE.md
├── README.md
├── app.py                         # Flask app + CLI flags (--host --port --fake --scenario --login)
├── requirements.txt
├── pytest.ini
├── config.example.json            # copy to config.json (gitignored)
├── .gitignore
├── automation/
│   ├── __init__.py
│   ├── config.py                  # load config.json over defaults; validate types
│   ├── models.py                  # enums + dataclasses
│   ├── errors.py                  # typed exceptions
│   ├── validators.py              # V1–V5, sanitizing, image checks, template rendering
│   ├── jobs.py                    # job creation, input copies, manifest import, plan, delete job data
│   ├── state_store.py             # ALL SQLite access; schema + migrations
│   ├── workflow.py                # orchestration, state machine, retries, quota stop, reconciliation
│   ├── worker.py                  # single background thread + command queue
│   ├── controller_base.py         # VidsController abstract interface + Capabilities
│   ├── vids_controller.py         # Selenium page-object implementation
│   ├── vids_selectors.json        # ALL selectors, text patterns, capabilities (filled in M1)
│   ├── fake_controller.py         # deterministic, scenario-driven fake
│   ├── browser.py                 # Chrome launch/attach, profile, CDP download dir
│   ├── downloads.py               # detect, wait, verify, safe move/rename
│   ├── platform_utils.py          # open folder, Chrome path detection, OS quirks
│   ├── report.py                  # JSON + Markdown run reports
│   └── logging_setup.py           # rotating file + console, redaction filter
├── ui/
│   ├── templates/index.html
│   └── static/{style.css, app.js}
├── tools/
│   └── ui_probe.py                # M1: screenshot + DOM/aria snapshot of the live page
├── tests/
│   ├── conftest.py
│   ├── fixtures/                  # tiny.mp4 (valid ftyp header), sample images, bad files
│   ├── scenarios/*.json           # fake-controller scenarios
│   └── test_*.py
├── data/                          # gitignored: jobs.sqlite3, chrome-profile/, downloads_staging/, thumbnails/
├── input/                         # user source folders (Sample_Project/ committed); jobs/ = app-owned copies (gitignored)
├── output/                        # final videos + reports (gitignored)
├── logs/                          # automation.log*, screenshots/ (gitignored)
└── docs/
    ├── 00_ORIGINAL_BRIEF.pdf … 08_TEST_PLAN.md
    ├── feasibility/               # M1 screenshots (raw/ gitignored)
    └── TEST_REPORT.md             # produced in M5
```

## 3. Module responsibilities and key functions

| Module | Responsibility | Key public API |
|--------|----------------|----------------|
| `config.py` | Deep-merge `config.json` over `config.example.json` defaults. Resolve relative paths against the project root. Fail fast on bad types. | `load_config(path=None) -> Config` |
| `models.py` | `JobMode`, `JobStatus`, `PartStatus`, `FinalExportStatus`, `GenerationState` enums; `Job`, `Part`, `Event`, `Capabilities` dataclasses | — |
| `validators.py` | All input checks; sanitizing; template rendering; collision check | `validate_job(job, parts, cfg) -> ValidationResult`, `sanitize_filename(s)`, `slugify_project(s)`, `check_image(path, cfg)`, `check_output_dir(path, cfg)`, `render_names(job, parts) -> dict[part_id, str]` |
| `jobs.py` | Create/edit jobs and parts, copy inputs (DEC-14, DEC-27), import manifests, review plan (R1), delete job data (P3), thumbnails | `JobService.create_job`, `add_part`, `import_manifest`, `validate`, `plan`, `delete_job_data`, `resolve_mode(mode, caps)` |
| `state_store.py` | Schema creation and migration; CRUD; atomic status transitions with event insert in **one transaction** | `create_job`, `add_part`, `reorder_parts`, `set_part_status(part_id, new, **fields, event=...)`, `set_job_status(...)`, `add_event`, `get_job_view(job_id)` |
| `workflow.py` | Run, resume, retry, reconcile; enforces the transition table | `run_job(job_id, controller, stop_event)`, `reconcile(job_id, controller=None)`, `retry_failed(job_id)` |
| `worker.py` | Owns the controller instance and the thread; serializes commands | `Worker.submit(cmd, job_id)`, `Worker.current() -> (job_id, step, part_id, started_at)` |
| `controller_base.py` | Abstract interface (§4) | — |
| `vids_controller.py` | Real Selenium page objects | implements §4 |
| `fake_controller.py` | Same interface, driven by a scenario JSON (§5.5) | implements §4 |
| `browser.py` | Start or attach Chrome, refuse the everyday profile (P2), set the download dir, detect sign-in page | `get_driver(cfg) -> WebDriver`, `start_chrome(cfg, url) -> bool` (False = reused a running Chrome; used by `--login` and the login route), `attach_driver(cfg) -> WebDriver`, `set_download_dir(driver, path)`, `debug_endpoint_alive(port)`, `debug_targets(port)` |
| `downloads.py` | DEC-16 algorithm, DL2 verification, DL5 safe move | `snapshot(dir)`, `wait_for_download(dir, before, cfg) -> Path`, `verify_video(path, cfg) -> VerifyResult`, `safe_move(src, out_dir, base_name) -> Path` |
| `platform_utils.py` | `open_folder(path)` (`os.startfile` / `open` / `xdg-open`), `find_chrome_binary()`, `default_chrome_user_data_dirs()` (P2 guard), `detached_popen_kwargs()` (Chrome outlives the app), `retry_on_permission_error(fn)` | — |
| `report.py` | Build the report dict; write JSON and Markdown | `build_report(job_id)`, `write_reports(job_id) -> (json_path, md_path)` |
| `logging_setup.py` | `RotatingFileHandler(logs/automation.log)`; a redaction filter that drops any record carrying the `script_text` attribute and masks emails | `setup_logging(cfg)` |

## 4. Controller interface (`controller_base.py`)

```python
@dataclass(frozen=True)
class Capabilities:
    multi_scene: bool
    combined_export: bool
    per_clip_download: bool
    max_reference_images: int
    max_prompt_chars: int | None

class GenerationState(Enum):
    GENERATING = "GENERATING"; READY = "READY"; FAILED = "FAILED"
    QUOTA = "QUOTA"; NEEDS_USER = "NEEDS_USER"

@dataclass
class PollResult:
    state: GenerationState
    message: str | None = None          # user-safe message (no script text)

class VidsController(ABC):
    def capabilities(self) -> Capabilities: ...
    def start(self) -> None: ...                       # attach/launch browser
    def close(self) -> None: ...
    def ensure_ready(self) -> None: ...                # signed in + Vids reachable; raises LoginRequiredError
    def check_quota(self) -> None: ...                 # raises QuotaReachedError if a limit message is visible
    def create_project(self, name: str) -> str: ...    # returns project ref (editor URL without query)
    def open_project(self, project_ref: str) -> None: ...
    def add_scene(self, project_ref: str) -> str: ...  # multi_part only; returns scene ref
    def submit_generation(self, project_ref: str, scene_ref: str | None,
                          script_text: str, image_paths: list[Path]) -> str: ...
                                                        # uploads images, enters prompt, clicks generate;
                                                        # returns part ref; raises UploadError / QuotaReachedError
    def poll_generation(self, part_ref: str) -> PollResult: ...
    def place_generated_clip(self, part_ref: str) -> None: ...  # insert into scene/timeline if the UI requires it
    def download_clip(self, part_ref: str, download_dir: Path) -> None: ...      # TRIGGERS the download; downloads.py waits
    def export_project(self, project_ref: str, download_dir: Path) -> None: ...  # TRIGGERS full-project MP4 export (DEC-30)
    def capture_diagnostics(self, label: str) -> Path | None: ...
```

**Typed errors** (`errors.py`): `ConfigError`, `BrowserStartError`, `ValidationError`, `LoginRequiredError`, `QuotaReachedError`, `UIElementMissingError(selector_key, step)`, `UploadError`, `PromptMismatchError` (DEC-28), `GenerationFailedError`, `GenerationTimeoutError`, `NetworkError`, `DownloadError`, `DiskError(path, os_error)`. The workflow maps each error to the ER row in `01_REQUIREMENTS.md`.

## 5. Workflow algorithms (`workflow.py`)

### 5.1 Common run loop (all modes)
```
reconcile(job)                                  # §6 of 04_STATE_AND_RECOVERY
set job RUNNING (event RUN_STARTED)
controller.start(); controller.ensure_ready()   # LoginRequiredError → WAITING_FOR_USER, return
caps = controller.capabilities(); effective_mode = resolve_mode(job.mode, caps)   # DEC-02
for part in parts ordered by sequence where status in (PENDING, READY):
    if stop_event.is_set(): break               # DEC-15 safe point
    if part.status == READY: download_or_mark(part); continue
    controller.check_quota()                    # QuotaReachedError → quota_stop(); return
    process_part(part)                          # §5.2–5.4, with step retries (DEC-12)
    on upload failure after retries: set job PAUSED; break   # DEC-26: part stays PENDING, later parts wait
    on consecutive_failures >= limit: set job PAUSED; break
finish: final export if applicable (§5.4) → compute final job status → write reports (DEC-20)
```

### 5.2 `single` and `separate_clips` — per part
```
project_ref = part.vids_project_ref or controller.create_project(name)  → save ref (commit)
set submit_attempted_at (commit, event SUBMIT_STARTED)
part_ref = controller.submit_generation(project_ref, None, script, images) → SUBMITTED (commit)
          UploadError after upload_max_attempts → part stays PENDING, error_code=UPLOAD,
          submit_attempted_at cleared (commit, event UPLOAD_FAILED, DEC-26)
poll until READY/FAILED/QUOTA/timeout; first GENERATING → status GENERATING (commit)
READY → generated_at=now, status READY (commit) → controller.place_generated_clip(part_ref)
download: event DOWNLOAD_STARTED, download_started_at (commit) → controller.download_clip(part_ref)
          → downloads.wait_for_download → verify_video → safe_move → DOWNLOADED (commit)
```
For `separate_clips` and `single`, "download clip" means the method M1 verified. If that is exporting the one-scene project, `download_clip` calls the export flow internally. M1 must also confirm that the exported file contains only the generated clip.

### 5.3 `multi_part` with `multi_scene=true`
```
job.vids_project_ref = existing or controller.create_project(project_name)  (commit)
for each part: scene_ref = (first part: default scene) else controller.add_scene(...)
               submit/poll/place as in 5.2 with scene_ref
               if caps.per_clip_download: download clip → DOWNLOADED
               else: part ends READY (DEC-23)
```

### 5.4 Final export (`multi_part`, `combined_export=true`)
Runs only when every part is `DOWNLOADED`, or `READY` under DEC-23.
`final_export_status`: `PENDING` → `EXPORTING` (commit) → `controller.export_project` → wait (`export_render` + `download_complete` timeouts) → verify → `safe_move` as `{final_template}.mp4` → `DOWNLOADED` (commit).
For a quota stop, the partial export follows DEC-24.

### 5.5 Fake controller scenarios (`tests/scenarios/*.json`)

Every scenario file has **all** of these keys:

```json
{
  "name": "quota_after_2",
  "description": "human-readable purpose",
  "capabilities": {"multi_scene": true, "combined_export": true, "per_clip_download": true,
                   "max_reference_images": 1, "max_prompt_chars": null},
  "generation_seconds": 1,
  "download_delay_seconds": 0.3,
  "login_required_checks": 0,
  "quota_after_generations": null,
  "fail_generation_parts": {},
  "timeout_generation_parts": {},
  "upload_fail_parts": {},
  "network_error_parts": {},
  "download_interrupt_parts": {},
  "ui_missing": null,
  "disk_error": null
}
```

| Key | Meaning |
|-----|---------|
| `login_required_checks` | The first N `ensure_ready()` calls raise `LoginRequiredError` (N=1 means a resume succeeds). |
| `quota_after_generations` | Total successful submits allowed (persisted across restarts) before `submit_generation`/`check_quota` raise `QuotaReachedError`. `null` = unlimited. The tests' "reset quota" helper deletes the counter file. |
| `fail_generation_parts` | `{"<seq>": n}`: the first n generation attempts of that part end with poll state `FAILED`. |
| `timeout_generation_parts` | `{"<seq>": n}`: the first n attempts stay `GENERATING` forever (the workflow timeout applies, so tests set `timeouts.generation` to 2 s). |
| `upload_fail_parts` | `{"<seq>": n}`: the first n upload attempts raise `UploadError`. |
| `network_error_parts` | `{"<seq>": n}`: the first n `submit_generation` calls raise `NetworkError`. |
| `download_interrupt_parts` | `{"<seq>": n}`: the first n download triggers leave only a `.crdownload` file that never completes. |
| `ui_missing` | `{"key": "generate_button", "part": 1}`: raises `UIElementMissingError` at that step for that part. |
| `disk_error` | `{"part": 2, "times": 1}`: `safe_move` for that part raises `OSError(errno.ENOSPC)` that many times. |

The fake persists its counters (generations per part, total generations, attempts) in `<data_dir>/fake_state.json`, so restart tests can prove that nothing was regenerated.
The fake writes `tests/fixtures/tiny.mp4`, a valid `ftyp` header of at least 10 KB, into the staging dir. It first writes a `.crdownload` temp file and renames it after a delay, to exercise DL1.
## 6. Validation and sanitizing rules (`validators.py`)

- **Project name:** 1–60 characters after trimming. Allowed: letters, digits, space, `-`, `_`. Anything else fails validation.
- **`slugify_project`:** spaces become `_`, other characters as above. A slug already used by a job in the DB gets `_2`, `_3`, … (DEC-14). Job input copies live in `input/jobs/<slug>/`.
- **`sanitize_filename(base)`:**
  - Replace `<>:"/\|?*` and characters below 0x20 with `_`.
  - Collapse runs of `_`. Strip trailing dots and spaces.
  - If the name stem (case-insensitive) is a Windows reserved name (`CON PRN AUX NUL COM1–COM9 LPT1–LPT9`), prefix `_`.
  - Max 100 characters. Empty → `ValidationError`.
  - The result must not contain `..` or a path separator.
- **Output dir:** `Path(p).expanduser().resolve()`. Must not be inside `input/`, `data/`, or `logs/`. Create it with `parents=True`. Write and delete `.write_test_<uuid>` to prove it's writable.
- **Images:**
  - `Image.open(p).verify()`, then re-open and `.load()`.
  - `img.format` must be in `{PNG, JPEG, WEBP}`.
  - Size must be ≤ `max_image_mb`.
  - The saved copy's extension comes from the detected format (`.png`, `.jpg`, `.webp`), not the user's filename.
- **Script:** decode as UTF-8 (strict). Non-empty after `strip()`. Respect `max_script_chars` if set. Stored exactly as given.
- **Templates:** render for every part, sanitize, and check uniqueness (case-insensitive, since Windows is case-insensitive). Collision → `ValidationError` naming both parts.
- **Part count and images:** see V5. Sequence numbers must be 1..n with no gaps (`reorder_parts` renumbers them).

## 7. Worker and threading

- One `Worker` per process. A daemon thread consumes `queue.Queue` commands: `START`, `RESUME`, `RETRY_FAILED`, `STOP`.
- `STOP` sets a `threading.Event`. The workflow checks it at safe points (DEC-15).
- Only one job can be `RUNNING`. Starting a second job while one runs returns HTTP 409.
- SQLite: a new connection per operation, with `PRAGMA journal_mode=WAL; foreign_keys=ON; synchronous=FULL` and a process-wide write lock. The worker and Flask threads never share a connection.
- At app startup, `reconcile()` runs for every job in a non-terminal status (`04` §6). A job left `RUNNING` by a crash becomes `PAUSED`.
- Ctrl+C: Flask shuts down, the worker sets stop, and the next startup reconciles.

## 8. HTTP API (JSON unless noted)

| Method & path | Purpose | Allowed when |
|---------------|---------|--------------|
| `GET /` | UI page (HTML) | always |
| `GET /api/jobs` | List jobs (id, name, mode, status, counts) | always |
| `POST /api/jobs` | Create a draft job `{project_name, mode, output_dir?, clip_template?, final_template?}` | always |
| `POST /api/jobs/import` | Create a job from a manifest (upload JSON, or a `{path}`) | always |
| `GET /api/jobs/<id>` | Full job view: job, parts, latest 50 events, worker current step | always |
| `PATCH /api/jobs/<id>` | Edit job fields | `DRAFT`, `VALIDATED` (edit → back to `DRAFT`) |
| `POST /api/jobs/<id>/parts` | Add a part (multipart: `script_text` or `script_file`, `images[]`, `title`) | `DRAFT`, `VALIDATED` |
| `PATCH /api/jobs/<id>/parts/<pid>` | Edit a part (replace script or images) | `DRAFT`, `VALIDATED` |
| `DELETE /api/jobs/<id>/parts/<pid>` | Remove a part (and its input copy) | `DRAFT`, `VALIDATED` |
| `POST /api/jobs/<id>/parts/reorder` | `{order: [part_id, ...]}` | `DRAFT`, `VALIDATED` |
| `GET /api/parts/<pid>/thumb/<n>` | 320 px PNG thumbnail (cached in `data/thumbnails/`) | always |
| `POST /api/jobs/<id>/validate` | Run V1–V5; save per-part errors; → `VALIDATED` or stay `DRAFT` | `DRAFT`, `VALIDATED` |
| `GET /api/jobs/<id>/plan` | R1 review data, incl. output names and limitation warnings | `VALIDATED` |
| `POST /api/jobs/<id>/start` | Enqueue `START` | `VALIDATED`, with no other job running |
| `POST /api/jobs/<id>/stop` | Stop safely | `RUNNING` |
| `POST /api/jobs/<id>/resume` | Enqueue `RESUME` (`BLOCKED_QUOTA` parts → `PENDING`) | `PAUSED`, `WAITING_FOR_USER`, `BLOCKED_QUOTA` |
| `POST /api/jobs/<id>/retry-failed` | DEC-22, then enqueue `RESUME` | `COMPLETED_WITH_ERRORS`, `PAUSED`, `FAILED` |
| `POST /api/jobs/<id>/open-output` | `platform_utils.open_folder` | loopback client only (DEC-21) |
| `GET /api/jobs/<id>/report?format=json\|md` | Download the report (file response) | any status after the first run |
| `DELETE /api/jobs/<id>?delete_outputs=false` | Delete job data (P3) | not `RUNNING` |
| `POST /api/browser/login` | Open the automation Chrome at the Vids home page for manual sign-in | no job `RUNNING` |
| `GET /api/browser/status` | `{browser_running, signed_in: true\|false\|unknown}` | always |
| `GET /api/health` | `{ok, fake_mode, lan_mode, version}` | always |

Errors return `{error: {code, message}}` with 400 (validation), 404, or 409 (invalid state). The UI disables buttons using the same "allowed when" rules (U3).

## 9. UI layout (`index.html` + `app.js`, vanilla)

1. **Header:** app name, fake/LAN mode badges, browser status with a "Sign in" button.
2. **Jobs list:** name, mode, status chip, completed/total. Click to open. "New job" and "Import manifest" buttons.
3. **Job editor:** U1 fields. The part cards (U2) form a vertical list with ↑/↓, remove, script textarea or file, an image picker with thumbnails, and an inline validation error list.
4. **Review panel (R1):** a table of part → script name/chars → images → output filename. Limitation banners. Start button.
5. **Progress panel (U4):** overall status, `x / n done`, current part and step, elapsed time, a per-part status table (status chip, retries, output name, error), the last 10 events, and the buttons from U3.
6. **Banners (U5):** sign-in needed, quota reached (with "resume after your limit resets"), unsupported workflow, LAN mode, disk/permission error with path.

Status chip colors: done = green, running = blue, pending = grey, failed = red, blocked = amber. Each chip also carries a text label, so meaning never relies on color alone.

## 10. Configuration (`config.example.json`)

Every key is described in `config.example.json`. Keys starting with `_` (e.g. `_doc`) are documentation and are ignored. Rules:
- `config.json` (gitignored) overrides the defaults. Missing keys fall back to defaults. Unknown keys log a warning.
- The default output folder for a job is `<paths.default_output_root>/<project_slug>`.
- Paths may be relative to the project root.
- `fake.enabled=true` or the `--fake` flag selects `FakeVidsController`. `--scenario` overrides `fake.scenario`.
- `browser.chrome_binary=null` means auto-detect (`platform_utils.find_chrome_binary`).

## 11. Logging

- File: `logs/automation.log`, rotating at 5 MB, 5 backups, UTF-8.
- Format: `ts level job=<id8> part=<seq> step=<STEP> msg`.
- Every state transition also writes an `events` row, which the UI shows.
- Redaction: script text never reaches a logger call. Emails matching a regex are masked as `***@***`. The Vids project URL is logged without its query string.
