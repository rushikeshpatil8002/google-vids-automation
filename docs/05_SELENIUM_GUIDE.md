# 05 — Selenium Guide

Google Vids has no public automation API. Everything here drives the normal web UI, at human pace, on an account the user has signed into themselves.

## 1. Browser modes (DEC-11)

### `attach` (default)
1. `browser.py` starts Chrome as a normal process:
   ```
   <chrome> --user-data-dir=<abs data/chrome-profile> --remote-debugging-port=<debug_port>
            --no-first-run --no-default-browser-check <vids_home_url>
   ```
   Use `subprocess.Popen([...])` with a list of arguments, never `shell=True`. Chrome binds the debugging port to 127.0.0.1.
2. If something is already listening on `debug_port` (check `http://127.0.0.1:<port>/json/version`), reuse that Chrome instead of starting another.
3. Selenium attaches:
   ```python
   opts = webdriver.ChromeOptions()
   opts.debugger_address = f"127.0.0.1:{port}"
   driver = webdriver.Chrome(options=opts)   # Selenium Manager resolves a matching ChromeDriver
   ```
4. ChromeDriver comes from Selenium Manager, which needs internet access the first time. If it can't (offline, proxy, antivirus), set `browser.chromedriver_path` to a ChromeDriver matching the Chrome version. `python app.py --check-browser` shows which step fails.
5. Chrome 136+ refuses remote debugging on the default user-data-dir. A dedicated `--user-data-dir` is required, which this design always uses.

### `launch` (fallback)
Selenium starts Chrome with `--user-data-dir=<profile>`. Use this only if M1 shows attach mode is unreliable.

### Never
- No stealth or undetected-chromedriver packages.
- No user-agent spoofing, and no removing `enable-automation`.
- No headless mode for live runs (`browser.headless` stays `false`).
- Never point at the user's everyday Chrome profile.

## 2. Sign-in (ER3)

- `python app.py --login`, or the UI's Sign-in button, starts Chrome in attach mode on the Vids home page. The human signs in and handles 2FA. The session persists in `data/chrome-profile/`.
- `ensure_ready()` loads `urls.home`. The user is considered **signed in** when a `home_ready` selector appears within `timeouts.page_load`. It raises `LoginRequiredError` if:
  - the current host is `accounts.google.com`, or
  - any `text_patterns.login` matches, or
  - a CAPTCHA or "verify it's you" pattern matches.
- On `LoginRequiredError` the job becomes `WAITING_FOR_USER`. The automation never types anything into Google sign-in pages.

## 3. Download directory (SE3, DEC-16)

```python
driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
    "behavior": "allow",
    "downloadPath": str(staging_dir.resolve()),
    "eventsEnabled": True,
})
```
Call this **before every download** (the path is per job). M1 step 2 verifies it works in attach mode. If it doesn't, the fallback is launch mode with the Chrome pref `download.default_directory` plus `download.prompt_for_download=false`, recorded in the feasibility report.

## 4. Selectors (`automation/vids_selectors.json`)

- Every UI element is a **key** with an **ordered list of strategies**. The first strategy that finds a *visible* element wins.
- Supported `by` values:

  | `by` | Meaning |
  |------|---------|
  | `css` | CSS selector |
  | `xpath` | XPath |
  | `aria_label` | exact `@aria-label` |
  | `role_name` | `{"role": "button", "name": "Generate"}`, matched on `@role`/tag plus aria-label or normalized text |
  | `text` | element whose normalized text equals the value |

- Preference order when writing selectors in M1: `aria_label` / `role_name` → stable `data-*` or `id` → `text` → `css` → `xpath`. Never use auto-generated class names like `.a3f-xyz` unless nothing else exists, and flag them as fragile in the report.
- Text-based selectors are language-dependent. The supported UI language is **English**, documented in the README. M1 records the account language.
- `text_patterns` holds lists of case-insensitive regexes: `quota`, `login`, `captcha`, `generation_failed`, `generation_in_progress`.
- `capabilities` and `_meta` are filled in M1. DEC-25 blocks live runs until there are no `null` or `TODO_M1` values.

### Lookup helper (the only way the controller finds elements)
```python
def find(self, key: str, timeout: float | None = None, clickable: bool = False) -> WebElement:
    """Try each strategy for `key` until one returns a visible (or clickable) element
    within the shared deadline. Raise UIElementMissingError(key, self.step) on failure,
    after capture_diagnostics(f"missing_{key}")."""
```

## 5. Waits (SE1)

- Use `WebDriverWait(driver, timeout, poll_frequency=0.5)` with `EC.visibility_of_element_located`, `EC.element_to_be_clickable`, `EC.invisibility_of_element_located`, or custom lambdas.
- Generation and export polling: a loop over `poll_generation()` that sleeps `generation_poll_interval` between checks and stops at a hard deadline. Each poll checks, in order: `quota` patterns → `generation_failed` patterns → `generation_ready` element → `generation_in_progress` element/patterns.
- After every click that should change the page, wait for the expected next element. Never "click and hope".
- Don't click the same element more than once per step attempt. Step retries (DEC-12) re-run the **whole step** from a known state, after re-finding elements.

## 6. Image upload (IM1–IM3)

**M1 result: method 1.** The first image goes to the hidden file input; each further image is attached by clicking `add_ingredient_button` with the OS file dialog suppressed in the page (the clicked input stays in the DOM and receives `send_keys`). Vids accepts PNG/JPEG only (DEC-27). The options, in order:

1. **File input:** find `image_upload_input` (`input[type=file]`, possibly hidden) and call `send_keys("\n".join(abs_paths))`. Make it visible through JS only if it is hidden *and* M1 confirmed that works.
2. **Picker iframe:** if Vids opens a Google Picker iframe, `switch_to.frame`, choose its Upload tab, find the `input[type=file]` inside, send the keys, then `switch_to.default_content()`.
3. **Drop zone:** create a temporary `input[type=file]` with JS, `send_keys` the paths into it, then dispatch `dragenter`/`dragover`/`drop` with a `DataTransfer` carrying its files onto the `image_drop_zone` element.
4. **Human-in-the-loop:** if only a native OS file dialog is available, Selenium can't drive it. Record this in the report. That step needs the user to pick the file, and the app shows a prompt with the exact file path.

**Confirm the upload succeeded:** wait for `image_upload_preview` (a thumbnail or filename) to appear for each image, up to `timeouts.upload`. Then compare the visible image count with the expected count. On mismatch, raise `UploadError`. `UploadError` must only ever be raised before Generate is clicked, because DEC-26 treats it as "nothing submitted".

## 7. Prompt entry (DEC-01)

- Click `prompt_textbox`, select-all and delete any existing text, then `send_keys(script_text)`.
- **Then read the value back** (`get_attribute("value")` or `innerText` for contenteditable). It must equal the script after normalizing line endings only. On mismatch, clear it, retype once, and if it's still wrong raise `UploadError("prompt mismatch")`. This enforces IM2.
- For contenteditable fields where `send_keys` mangles newlines, M1 decides the method: `Keys.SHIFT+Keys.ENTER` for line breaks, or a JS `insertText` command. Record which.

## 8. Diagnostics (SE5, DEC-18)

`capture_diagnostics(label)` saves `logs/screenshots/<job8>_<seq>_<label>_<utcstamp>.png` and returns its path. The workflow stores that path in the event. The event message includes the URL without query string or fragment, the page title, the selector key, and the step. It never includes page text.

## 9. M1 probe (`tools/ui_probe.py`)

An interactive helper for the feasibility milestone:
- Attaches to the running automation Chrome (attach mode), so the human drives the UI by hand.
- It snapshots the most recently active tab (Chrome's `/json/list` is ordered by activity) and prints that tab's title, so a wrong tab is obvious.
- On each Enter keypress it saves to `docs/feasibility/raw/<NN>_<label>/`. Numbers only go up, so nothing is overwritten.
  - `screenshot.png` (visible viewport)
  - `dom.html`: full HTML with inline `<script>` elements and hidden-input values removed, because Google pages embed session tokens there (CLAUDE.md rule 4)
  - `a11y.json`: every visible element with its role, aria-label, text (truncated to 80 chars), tag, id, `data-*` attributes, and an `input[type=file]` flag (hidden file inputs included). Also type, name, placeholder, title, disabled, contenteditable, bounding box, and elements inside open shadow roots. Each iframe is entered one level deep and collected the same way. Email addresses are masked as `***@***`.
  - `url.txt` (query string, fragment and user info removed)
- It only reads the page: no clicks, no typing, no cookies.
- `docs/feasibility/raw/` is gitignored, because DOM dumps may contain account details.
- Claude Code reads `a11y.json` to propose selectors, which the developer then verifies.

## 10. Required selector keys

Fill these in M1. A key may be marked `"not_applicable"` if a capability is false.

`home_ready, new_project_button, blank_project_option, editor_ready, project_title_input, generate_video_entry, image_upload_input, image_drop_zone, image_upload_preview, prompt_textbox, generate_button, generation_in_progress, generation_ready, generation_error, insert_generated_clip, add_scene_button, scene_list_item, download_menu, download_mp4_option, export_in_progress, export_ready, quota_message_container`
