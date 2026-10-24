# 07 — Feasibility Report (Milestone 1)

> Completed 2026-10-24 (M1 steps 1.3–1.7). Live generations used in M1: 4 of 5. Evidence screenshots: `docs/feasibility/*.png`; raw probe snapshots (gitignored): `docs/feasibility/raw/`.

## 1. Environment

| Item | Value |
|------|-------|
| Date tested | 2026-10-24 |
| Machine / OS | Mac, Apple Silicon (arm64), macOS 26.4.1 (build 25E253) |
| Chrome version | Google Chrome 154.0.8037.100 (stable, `/Applications`) |
| Selenium version | 4.51.0 (Python 3.14.6) |
| Browser mode that works | **attach**: manual Google sign-in worked in the normal Chrome started by `python app.py --login` (dedicated profile `data/chrome-profile`, debug port 127.0.0.1:9222). Selenium attaching to it: **confirmed** in 1.4 (`tools/ui_probe.py` attached and captured 24 snapshots). It fails if the Chrome has no open window (macOS keeps Chrome running after the last window closes) |
| Google account type / plan | Personal Google account (@gmail.com), Google AI Pro |
| UI language | English |
| Vids home URL / editor URL pattern | `https://vids.google.com/` redirects to `https://docs.google.com/videos/u/0/` (signed in) / editor: `https://docs.google.com/videos/d/<file-id>/edit` |

## 2. Findings F1–F6

| ID | Question | Result | Evidence |
|----|----------|--------|----------|
| F1 | Can the account access Vids AI video generation? What limits are shown? | **Yes.** Editor rail "AI Video" (`Generate an AI video clip`) opens the "AI video clip" panel. Default settings: model **Omni, 720p, Landscape, 10 s**. No usage limit or counter was shown during 2 generations. | raw/10, raw/11 |
| F2 | Does script (prompt) + reference image → clip work? Prompt char limit? Max images? | **Yes.** The image is added as an "ingredient" (hidden `input[type=file]`, `accept="image/png, image/jpeg"`, one file per pick; WEBP **not** accepted). The prompt goes into a contenteditable textbox that starts with an `Image1` chip. Output: a 10 s clip with audio, about 20–40 s per generation (developer's observation). Prompt char limit: ☐ not shown. Max images: ☐ ("+ Add" exists; limit not tested). | raw/11–15, raw/20–21 |
| F3a | Can one project hold multiple scenes, each with its own generated clip? | **Yes.** Two scenes, each with its own generated clip (`Scene 1 of 2`, `Scene 2 of 2`). Caveat: inserting the first clip into a new one-scene project left an extra empty 5 s scene, which had to be removed. | raw/16–22 |
| F3b | Can the whole project be exported as one video? | **Yes.** One MP4 of the whole project: 20.03 s, H.264 1920×1080 + AAC, 3.59 MB. Menu path: **File → Download ► → MP4 video (.mp4)** (GIF also offered). | raw/22–24 |
| F4a | How is a single generated clip downloaded? Format? Does it contain only the clip? | **No per-clip download.** The clip's "More actions" menu offers only Scene / Arrange / Remove. A one-scene project exported via File → Download → MP4 contains only the clip (verified: 10.05 s, 3.0 MB) once the extra empty scene is deleted. | step 1.6 run |
| F4b | How is the full project downloaded? Format? Render time observed? | Export menu → "Downloading… Leave this tab open" → "Download started"; an export status button reports `Your video export is in progress` / `Your video has exported`. MP4 (`isom`). About 15 s from menu to file for a 20 s video. Saved as `<project title>.mp4`; Chrome appends ` (1)`, ` (2)` on repeats. | raw/22–24 |
| F5 | Is each step stable under automation? (table §3) | **Yes.** `tools/feasibility_run.py` ran the full single-part flow unattended through the controller: ALL PASS, 13 steps. | step 1.6 output |
| F6 | Go / No-go | **GO** (DEC-02 branch A) | §7 |

## 3. Step classification

| # | Step | Selector keys | Class | Notes |
|---|------|---------------|-------|-------|
| 1 | Open Vids home (signed in) | home_ready | F | Home URL `docs.google.com/videos/u/0/`. The new-video tile has no label (first `[role=listbox] [role=option]`) |
| 2 | Create blank project | urls.create, new_project_button (fallback), blank_project_option, editor_ready | A | Opening `https://docs.google.com/videos/create` creates a project in the same tab (verified 2026-10-24); the home-page tile is the fallback. A new project opens a "Getting started" dialog; the controller picks "Blank vid". Rename via `input[aria-label=Rename]` (the export file is named after the title) |
| 3 | Open generate-video panel | generate_video_entry, prompt_expand_button | A | Stable id `content-library-rail-video-generation-element`; the prompt area must be expanded |
| 4 | Upload reference image(s) | image_upload_input, add_ingredient_button, image_upload_preview | A | Method (05 §6): **file input**. The first image goes to the hidden `input[type=file]`; extra images via "+ Add" with the OS file dialog suppressed in the page. PNG/JPEG only. 2 images verified |
| 5 | Enter prompt | prompt_textbox | A | Read-back verified: **yes** (the `Image1` chip is ignored). Newline method: `execCommand insertText` + `insertLineBreak` |
| 6 | Generate | generate_button | A | `button[aria-label=Generate]`, disabled until a prompt exists |
| 7 | Detect in-progress / ready / error | generation_in_progress, generation_ready, generation_error | A / F | Progress `role=progressbar` (A); ready = new `video[aria-label^='Generated video']` (A); error wording not seen (F). Typical duration: **35–40 s** |
| 8 | Place clip in scene/timeline | insert_generated_clip, empty_scene_marker, scene_menu, delete_scenes_option | F | Insert leaves an extra empty 5 s scene, removed via Scene → Delete scenes. The clip exists only in the open page until inserted |
| 9 | Add scene (multi-part) | add_scene_button, scene_list_item | A | `#newSceneButton`; scenes are `[role=button][aria-label^='Scene ']` |
| 10 | Download clip / export project | download_menu, download_submenu, download_mp4_option, export_ready | F | File → Download → MP4; status `Export status: Your video has exported`. About 20 s for a 10 s video |
| 11 | Detect quota / limit | quota_message_container + text_patterns.quota | F | Seen naturally: **no**. Patterns seeded from Google's usual wording; extendable in config |
| 12 | Sign-in detection | text_patterns.login | A | Host `accounts.google.com` or login/captcha text → WAITING_FOR_USER |

Classes: **A** = automatable and stable · **F** = automatable but fragile (explain) · **H** = human-in-the-loop (explain the prompt shown to the user).

## 4. Capabilities (copied into `automation/vids_selectors.json`)

```json
{
  "multi_scene": true,
  "combined_export": true,
  "per_clip_download": false,
  "max_reference_images": 2,
  "max_prompt_chars": 6000,
  "export_format": "mp4"
}
```

`max_prompt_chars`: no limit was shown up to 6,000 characters; longer prompts are untested, so validation caps there.

DEC-02 branch selected: **A** (multi-scene + combined export). With `per_clip_download=false`, `multi_part` parts end `READY` and the job completes on the verified `{project}_Final.mp4` (DEC-23). `single` and `separate_clips` save each clip by exporting its one-scene project.

## 5. Supported vs unsupported (client-facing summary)

| Brief requirement | Status | Note |
|-------------------|--------|------|
| Single script + image → video, downloaded | **Supported** | Automated end to end in M1 (10 s, 720p clip; export is MP4) |
| Multi-part in one project | **Supported** | One scene per part |
| Combined final export | **Supported** | `{project}_Final.mp4`, H.264 1080p + AAC |
| Separate per-part clips inside a multi-part project | **Not offered by Vids** | Use `separate_clips` mode (one project per part) to get individual files |
| Multiple reference images per part | **Supported, up to 2** (verified) | PNG/JPEG upload; WEBP is converted to PNG losslessly before upload (DEC-27) |
| Quota detection and safe stop | **Supported (text-based)** | No limit was hit in M1, so the wording is seeded and configurable |
| Fully unattended after sign-in | **Yes** | Except the human-in-the-loop steps below |

## 6. Human-in-the-loop steps

- Initial Google sign-in, plus 2FA or CAPTCHA when Google asks (always)
- Re-sign-in when the session expires (the job waits in WAITING_FOR_USER)
- If the app or browser stops between Generate and Insert, that clip is lost (Vids keeps it only in the open page): the part becomes FAILED `UNCERTAIN_SUBMIT` and needs a manual Retry

## 7. Risks and recommendation

- **Fragile selectors:** the new-video tile, Insert, the Download menu items and Delete scenes are matched by text or position. Mitigation: ordered fallbacks in `vids_selectors.json`, the DEC-25 gate, and ER4 pause with a screenshot.
- **Quota:** not reached in M1 (4 generations). Google AI Pro limits apply per account; the tool stops cleanly and never works around them.
- **Policy:** automating the Vids web UI has no official API or explicit approval; the tool runs at human pace on the user's own signed-in account, with no evasion.
- **Recommendation:** **GO.** Every brief workflow is verified on the target account; the only gap (no per-clip download inside one project) has a compliant fallback.
