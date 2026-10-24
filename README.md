# Google Vids Automation

A local tool that generates AI video clips in **Google Vids** from parts, where each part is a script (prompt) plus reference image(s). It downloads the finished videos with predictable names, and it never loses or overwrites completed work, even after a usage limit, crash, or restart.

> **Status:** feature-complete (v1.0). Verified live on macOS with a Google AI Pro account (see `docs/07_FEASIBILITY_REPORT.md` and `docs/TEST_REPORT.md`). The clean Windows 11 setup check (A11) is the remaining acceptance step.

---

## What it does

1. You create a job: project name, mode, output folder, and the parts.
2. The app validates everything and shows a review plan.
3. You click **Start**. The app drives Google Vids in a dedicated Chrome window, one part at a time.
4. Each finished video is downloaded, checked, and saved as `ProjectName_Part01.mp4`, and so on. Where Vids supports it, a combined `ProjectName_Final.mp4` is saved too.
5. If a usage limit, error, or crash happens, finished files are kept and progress is saved. **Resume** continues where it stopped.

**Modes**

| Mode | Result |
|------|--------|
| Single video | One script + image(s) → one clip |
| Multi-part project | All parts as scenes of one Vids project → one combined `ProjectName_Final.mp4`. Vids can't download single clips from such a project, so no per-part files |
| Separate clips | Each part in its own Vids project, saved as its own file `ProjectName_PartNN.mp4` |

## Requirements

- Windows 11 (x64) or macOS 14+
- Python 3.11–3.14 (python.org installer on Windows: tick "Add python.exe to PATH")
- Google Chrome (current stable version)
- A Google account with access to Google Vids AI video generation. **Usage limits apply per account. This tool does not and cannot bypass them.**
- Google Vids UI in **English**, since some on-screen text detection is language-specific

## Install

### Windows (PowerShell)
```powershell
cd path\to\google-vids-automation
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.json config.json
```
If PowerShell blocks the activate script, run this once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### macOS (Terminal)
```bash
cd path/to/google-vids-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

## First-time sign-in

```bash
python app.py --login
```
A separate Chrome window opens with its own profile, kept in `data/chrome-profile/`. Sign in to your Google account there, complete any 2FA, and confirm Google Vids opens. You only need to do this once, or again when Google signs you out.

The tool never sees or stores your password. Use this dedicated window only. Your everyday Chrome profile is not used.

## Run

```bash
python app.py
```
Open **http://127.0.0.1:5000** in any browser.

- **New job** → fill in the details and add parts (script + image), or **Import manifest** (see `input/Sample_Project/job.json`).
- **Validate Job** → fix anything flagged → review the plan → **Start**.
- Watch the progress panel. Use **Stop Safely** to pause after the current part. Use **Resume** to continue.
- **Retry Failed Parts** reruns only failed parts. Completed files are never regenerated.

### Optional: LAN access
`python app.py --host 0.0.0.0` lets another computer on your network open the UI. **Chrome, the automation and all files stay on the computer running the app.** Use this only on a trusted network. The app has no login.

## Where files are stored

| What | Location |
|------|----------|
| Finished videos and run reports | `output/<ProjectName>/` (or the folder you chose) |
| Copies of each job's scripts and images | `input/jobs/<ProjectName>/` |
| Job and part progress (database) | `data/jobs.sqlite3` |
| Automation Chrome profile (your Google session) | `data/chrome-profile/` |
| Logs | `logs/automation.log` |
| Error screenshots | `logs/screenshots/` (may show your account avatar) |

**Delete a job's data:** use the job's **Delete job data** button. Output videos are deleted only if you tick that option.
**Sign out completely:** close the app and delete `data/chrome-profile/`.

## Testing

```bash
pytest                  # fast tests, no Google access
python app.py --fake    # full UI with a simulated Vids (no quota used)
pytest -m live          # real end-to-end test — uses 1 generation from your quota
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Sign-in needed" banner (job **Needs you**) | Sign in in the automation Chrome window (**Open browser to sign in**), then click **Resume**. |
| "Usage limit reached" (job **Blocked by quota**) | Completed files are already saved. Click **Resume** after your Google limit resets. The tool can't and won't bypass it. |
| "UI element not found" (job **Paused**) | Google changed the Vids page. The event shows the screenshot path in `logs/screenshots/`. Update `automation/vids_selectors.json`, then **Retry Failed Parts**. |
| "Image upload failed for part N" | Check that the image opens and is PNG/JPEG/WEBP under the size limit, replace it if needed, then **Resume**. |
| Part failed with "A generation may have started…" (`UNCERTAIN_SUBMIT`) | The app or browser stopped mid-generation. Check the Vids project in Drive, then **Retry Failed Parts** (this uses a new generation). |
| "The automation Chrome is running with no window open" | Switch to that Chrome and press Cmd+N / Ctrl+N, or quit it and run `python app.py --login`. |
| "Unable to obtain driver for chrome" | Selenium Manager couldn't download ChromeDriver. Run `python app.py --check-browser` to see why. Allow internet access for Python (proxy, firewall, antivirus), or download ChromeDriver for your Chrome version from the Chrome for Testing page and set `"chromedriver_path"` in `config.json`. |
| Chrome doesn't open / port in use | Close other Chrome windows using `data/chrome-profile`, or change `browser.debug_port` in `config.json`. |
| Disk full / permission denied (job **Needs you**) | Free space or fix the folder permissions shown in the message, then **Resume**. Nothing is regenerated. |
| A file name ends in `_v2` | A file with that name already existed. Nothing is ever overwritten; the event log and report list every rename. |

Logs: `logs/automation.log` (rotates at 5 MB). Script text is never logged; parts are identified by number and a 12-character hash.

## Limitations

Verified on 2026-10-24 with Chrome 154 and a Google AI Pro account (details: `docs/07_FEASIBILITY_REPORT.md`).

- Depends on the current Google Vids web interface (English UI), which Google may change at any time. Selectors live in `automation/vids_selectors.json`.
- Usage limits are set by Google per account and plan. No limit was hit during testing, so limit detection uses Google's usual wording (configurable in `text_patterns.quota`).
- Each generated clip is 10 s, 720p (Vids "Omni" defaults). Exports are MP4 (H.264 1080p + AAC).
- **Combined final video:** supported (multi-part mode).
- **Individual clips from a multi-part project:** not offered by Vids. Use **Separate clips** mode to get one file per part.
- **Reference images:** up to 2 per part. Vids uploads PNG/JPEG; WEBP files are converted losslessly to PNG in the job's own copy (your file is untouched).
- **Prompt length:** checked up to 6,000 characters; longer scripts fail validation (they're never shortened).
- A generated clip exists only in the open Vids page until it's inserted, so the app keeps its own Chrome tab open. If Chrome itself closes mid-generation, that part needs a manual Retry.

### Steps that need you

- First Google sign-in, and 2FA/CAPTCHA whenever Google asks (`python app.py --login`).
- Signing in again when the session expires (the job waits and resumes).
- Retrying a part interrupted mid-generation (see `UNCERTAIN_SUBMIT` above).

## Project documents

See `docs/`: requirements, decisions, architecture, state and recovery, Selenium guide, milestones, feasibility report, test plan and test report.
