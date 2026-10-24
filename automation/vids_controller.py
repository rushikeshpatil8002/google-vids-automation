"""Live Google Vids controller: Selenium page objects driven only by vids_selectors.json (SE2, 05).

The only module that knows selectors. Never logs page text or aria-labels (they can hold
the script: the generated clip's label repeats the prompt). Never clicks Generate twice
in one attempt, and never types into Google sign-in pages.
"""

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import (
    JavascriptException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from automation import browser, platform_utils
from automation.config import PROJECT_ROOT, Config
from automation.controller_base import Capabilities, GenerationState, PollResult, VidsController
from automation.errors import (
    ConfigError,
    LoginRequiredError,
    NetworkError,
    PromptMismatchError,
    QuotaReachedError,
    UIElementMissingError,
    UploadError,
)

log = logging.getLogger(__name__)

SELECTORS_FILE = PROJECT_ROOT / "automation" / "vids_selectors.json"
REQUIRED_KEYS = (
    "home_ready new_project_button blank_project_option editor_ready project_title_input generate_video_entry "
    "image_upload_input image_drop_zone image_upload_preview prompt_textbox generate_button generation_in_progress "
    "generation_ready generation_error insert_generated_clip add_scene_button scene_list_item download_menu "
    "download_mp4_option export_in_progress export_ready quota_message_container"
).split()
ZERO_WIDTH = re.compile(r"[​‌‍﻿]")

# Visible own-text of status-like regions, skipping the prompt box and generated-video labels,
# so the user's own script can never trigger a quota or failure match.
STATUS_TEXT_JS = r"""
const out = [];
for (const el of document.querySelectorAll('body *')) {
  if (el.closest('[contenteditable="true"], video, script, style, noscript, template')) continue;
  let own = '';
  for (const n of el.childNodes) if (n.nodeType === Node.TEXT_NODE) own += n.textContent;
  own = own.replace(/\s+/g, ' ').trim();
  if (!own) continue;
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) continue;
  const s = getComputedStyle(el);
  if (s.visibility === 'hidden' || s.display === 'none') continue;
  out.push(own);
}
return out.join('\n');
"""

# Keeps a clicked file input in the page instead of opening the OS file dialog,
# so Selenium can send the file path to it (05 §6 method 1).
NO_FILE_DIALOG_JS = r"""
if (!window.__gvaNoDialog) {
  window.__gvaNoDialog = true;
  const keep = (input) => { window.__gvaFileInput = input; if (!input.isConnected) { input.style.display = 'none'; document.body.appendChild(input); } };
  const click = HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click = function () { if (this.type === 'file') return keep(this); return click.call(this); };
  const picker = HTMLInputElement.prototype.showPicker;
  if (picker) HTMLInputElement.prototype.showPicker = function () { if (this.type === 'file') return keep(this); return picker.call(this); };
}
"""

INSERT_TEXT_JS = r"""
const box = arguments[0], lines = arguments[1];
box.focus();
const sel = window.getSelection(); sel.selectAllChildren(box); sel.collapseToEnd();
lines.forEach((line, i) => {
  if (i > 0) document.execCommand('insertLineBreak');
  if (line) document.execCommand('insertText', false, line);
});
"""


def load_selectors(path: Path = SELECTORS_FILE) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read {path.name}: {exc}") from exc


def check_selectors_verified(sel: dict[str, Any]) -> None:
    """DEC-25: refuse to run against unverified selectors."""
    nulls = [k for k, v in sel.get("capabilities", {}).items() if v is None]
    if nulls:
        raise ConfigError(f"vids_selectors.json capabilities not verified (null): {', '.join(nulls)}")
    elements = sel.get("elements", {})
    missing = [k for k in REQUIRED_KEYS if k not in elements]
    if missing:
        raise ConfigError(f"vids_selectors.json is missing required keys: {', '.join(missing)}")
    def strip_docs(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip_docs(v) for k, v in value.items() if not k.startswith("_")}
        return value

    if "TODO_M1" in json.dumps(strip_docs(sel)):
        raise ConfigError("vids_selectors.json still contains TODO_M1 placeholders")


def url_without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def normalize_prompt(text: str) -> str:
    """Prompt text as Vids shows it, minus image chips and zero-width characters (05 §7)."""
    text = ZERO_WIDTH.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"^\s*(?:Image\d+\s*)+", "", text)
    return "\n".join(line.rstrip() for line in text.strip().split("\n"))


class VidsSeleniumController(VidsController):
    def __init__(self, cfg: Config, selectors: dict[str, Any] | None = None) -> None:
        self.cfg = cfg
        self.sel = selectors if selectors is not None else load_selectors()
        check_selectors_verified(self.sel)
        self.patterns = {k: [re.compile(p, re.IGNORECASE) for p in v]
                         for k, v in self.sel["text_patterns"].items() if not k.startswith("_")}
        self.driver: WebDriver | None = None
        self.handle: str | None = None
        self.step = "init"
        self.diag_prefix = "run"
        self._pending: dict[str, int] = {}  # part_ref -> generated-video count before submit
        self._known_tabs: set[str] = set()

    # ----- lifecycle -------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        c = self.sel["capabilities"]
        return Capabilities(c["multi_scene"], c["combined_export"], c["per_clip_download"],
                            c["max_reference_images"], c["max_prompt_chars"])

    def start(self) -> None:
        """Attach and use our own tab. The user may browse in other tabs of the same Chrome.

        The tab is reused across runs and never closed, because a generated clip lives
        only in the open page until it is inserted into the project.
        """
        self.step = "BROWSER_START"
        self.driver = browser.get_driver(self.cfg)
        self.driver.set_page_load_timeout(self.cfg.timeouts_seconds.page_load)
        tab_file = self.cfg.paths.data_dir / "controller_tab.txt"
        saved = tab_file.read_text(encoding="utf-8").strip() if tab_file.exists() else ""
        if saved and saved in self.driver.window_handles:
            self.driver.switch_to.window(saved)
        else:
            self.driver.switch_to.new_window("tab")
            tab_file.parent.mkdir(parents=True, exist_ok=True)
            tab_file.write_text(self.driver.current_window_handle, encoding="utf-8")
        self.handle = self.driver.current_window_handle

    def close(self) -> None:
        """End the Selenium session; the tab and the attached Chrome stay open."""
        if self.driver is None:
            return
        try:
            self.driver.quit()  # ends the session; an attached Chrome keeps running
        except WebDriverException:
            pass
        self.driver = None

    @property
    def d(self) -> WebDriver:
        if self.driver is None:
            raise ConfigError("Controller not started")
        try:
            if self.driver.current_window_handle != self.handle:
                self.driver.switch_to.window(self.handle)
        except NoSuchWindowException:
            self.driver.switch_to.window(self.handle)
        return self.driver

    # ----- element lookup (05 §4) ------------------------------------------------------

    def _strategies(self, key: str) -> list[dict[str, Any]]:
        value = self.sel["elements"].get(key)
        if value is None or value == "not_applicable":
            raise ConfigError(f"Selector '{key}' is not available in vids_selectors.json")
        return value

    @staticmethod
    def _locator(strategy: dict[str, Any]) -> tuple[str, str]:
        by, value = strategy["by"], strategy["value"]
        if by == "css":
            return By.CSS_SELECTOR, value
        if by == "xpath":
            return By.XPATH, value
        if by == "aria_label":
            return By.CSS_SELECTOR, f"[aria-label={json.dumps(value)}]"
        if by == "text":
            return By.XPATH, f"//*[normalize-space()={json.dumps(value)}]"
        if by == "role_name":
            role, name = value["role"], value["name"]
            return By.XPATH, (f"//*[(@role={json.dumps(role)} or local-name()={json.dumps(role)}) and "
                              f"(@aria-label={json.dumps(name)} or normalize-space()={json.dumps(name)})]")
        raise ConfigError(f"Unknown selector strategy {by!r}")

    def find_all(self, key: str, visible_only: bool = True) -> list[WebElement]:
        """Every match of the first strategy that matches anything (no waiting)."""
        for strategy in self._strategies(key):
            try:
                found = self.d.find_elements(*self._locator(strategy))
                if visible_only:
                    found = [e for e in found if e.is_displayed()]
            except (StaleElementReferenceException, WebDriverException):
                found = []
            if found:
                return found
        return []

    def exists(self, key: str) -> bool:
        return bool(self.find_all(key))

    def find(self, key: str, timeout: float | None = None, clickable: bool = False,
             visible_only: bool = True) -> WebElement:
        """First visible (or enabled) match within the deadline; else UIElementMissingError with a screenshot."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.cfg.timeouts_seconds.element)
        while True:
            for el in self.find_all(key, visible_only=visible_only):
                try:
                    if not clickable or (el.is_enabled() and el.get_attribute("aria-disabled") != "true"):
                        return el
                except StaleElementReferenceException:
                    continue
            if time.monotonic() > deadline:
                shot = self.capture_diagnostics(f"missing_{key}")
                raise UIElementMissingError(key, self.step, shot, self.open_dialogs_hint())
            time.sleep(0.5)

    def click(self, key: str, timeout: float | None = None) -> None:
        el = self.find(key, timeout, clickable=True)
        try:
            el.click()
        except WebDriverException:
            # Overlays (tooltips, placeholders) can intercept a native click on the same element.
            self.d.execute_script("arguments[0].click();", el)

    def wait_until(self, condition, timeout: float, what: str) -> Any:
        try:
            return WebDriverWait(self.d, timeout, poll_frequency=0.5).until(lambda _d: condition())
        except TimeoutException:
            shot = self.capture_diagnostics(f"timeout_{what}")
            raise UIElementMissingError(what, self.step, shot) from None

    def status_text(self) -> str:
        try:
            return self.d.execute_script(STATUS_TEXT_JS) or ""
        except JavascriptException:
            return ""

    def matches(self, pattern_key: str, text: str | None = None) -> bool:
        text = self.status_text() if text is None else text
        return any(p.search(text) for p in self.patterns.get(pattern_key, []))

    # ----- session checks ---------------------------------------------------------------

    def _check_login(self) -> None:
        host = urlsplit(self.d.current_url).netloc
        text = self.status_text()
        if host.startswith("accounts.google.") or self.matches("login", text) or self.matches("captcha", text):
            raise LoginRequiredError("Google sign-in is needed in the automation Chrome window")

    def _get(self, url: str) -> None:
        try:
            self.d.get(url)
        except TimeoutException as exc:
            raise NetworkError(f"Page load timed out: {url_without_query(url)}") from exc

    def ensure_ready(self) -> None:
        self.step = "LOGIN_CHECK"
        self._get(self.sel["urls"]["home"])
        deadline = time.monotonic() + self.cfg.timeouts_seconds.page_load
        while time.monotonic() < deadline:
            self._check_login()
            if self.exists("home_ready"):
                return
            time.sleep(1)
        self._check_login()
        self.find("home_ready", timeout=0)

    def check_quota(self) -> None:
        if self.matches("quota"):
            raise QuotaReachedError("Google Vids shows a usage limit message")

    # ----- projects and scenes -----------------------------------------------------------

    def open_dialogs_hint(self) -> str | None:
        """Names of open dialogs (UI labels only, never page text), to explain what blocked a step."""
        try:
            labels = self.d.execute_script(
                "return [...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true]')]"
                ".filter(e => e.getBoundingClientRect().width > 0)"
                ".map(e => e.getAttribute('aria-label') || (e.querySelector('h1,h2,[role=heading]')||{}).innerText || 'unnamed')"
                ".map(s => String(s).trim().slice(0, 60));") or []
        except WebDriverException:
            return None
        return f"Open dialog(s): {', '.join(repr(l) for l in labels)}" if labels else None

    def _wait_for_editor(self, timeout: float) -> bool:
        """True once the editor is visible in our tab, or in a tab Vids just opened (adopted as ours)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_login()
            if self.exists("editor_ready"):
                return True
            self._adopt_new_editor_tab()
            time.sleep(0.5)
        return False

    def _adopt_new_editor_tab(self) -> None:
        pattern = self.sel["urls"]["editor_url_pattern"]
        for handle in self.driver.window_handles:
            if handle == self.handle:
                continue
            self.driver.switch_to.window(handle)
            if re.match(pattern, self.driver.current_url) and handle not in self._known_tabs:
                self.handle = handle
                (self.cfg.paths.data_dir / "controller_tab.txt").write_text(handle, encoding="utf-8")
                return
        self.driver.switch_to.window(self.handle)

    def create_project(self, name: str) -> str:
        """New blank project via urls.create; the home-page tile is the fallback."""
        self.step = "PROJECT_CREATE"
        self._known_tabs = set(self.d.window_handles)
        self._get(self.sel["urls"]["create"])
        if not self._wait_for_editor(self.cfg.timeouts_seconds.page_load):
            log.warning("create URL did not open the editor; trying the home-page tile", extra={"step": self.step})
            self._get(self.sel["urls"]["home"])
            self.click("new_project_button", timeout=self.cfg.timeouts_seconds.page_load)
            if not self._wait_for_editor(self.cfg.timeouts_seconds.page_load):
                self.find("editor_ready", timeout=0)  # raises with screenshot and open-dialog names
        self._dismiss_start_dialog()
        self.wait_until(lambda: re.match(self.sel["urls"]["editor_url_pattern"], self.d.current_url),
                        self.cfg.timeouts_seconds.element, "editor_url")
        self.rename_project(name)
        return url_without_query(self.d.current_url)

    def _dismiss_start_dialog(self) -> None:
        """New projects open a 'Getting started' dialog: choose 'Blank vid'."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.exists("blank_project_option"):
                self.click("blank_project_option")
                self.wait_until(lambda: not self.exists("blank_project_option"),
                                self.cfg.timeouts_seconds.element, "start_dialog_closed")
                return
            if self.exists("generate_video_entry") and self.exists("scene_list_item"):
                time.sleep(1)
            else:
                time.sleep(0.5)

    def rename_project(self, name: str) -> None:
        """Set the project title; Vids names the exported file after it."""
        box = self.find("project_title_input", visible_only=False)
        self._safe_click(box)
        time.sleep(0.3)
        modifier = getattr(Keys, platform_utils.select_all_modifier())
        box.send_keys(modifier, "a")
        box.send_keys(Keys.BACK_SPACE)
        box.send_keys(name, Keys.ENTER)
        self.wait_until(lambda: (box.get_attribute("value") or "") == name,
                        self.cfg.timeouts_seconds.element, "project_renamed")

    def set_context(self, job_id: str | None, part_sequence: int | None) -> None:
        self.diag_prefix = f"{(job_id or 'run')[:8]}_{part_sequence or 0:02d}"

    def open_project(self, project_ref: str) -> None:
        """Open the project unless it is already the current page (a reload loses pending clips, DEC-29)."""
        self.step = "PROJECT_OPEN"
        if url_without_query(self.d.current_url) == project_ref and self.exists("editor_ready"):
            return
        self._get(project_ref)
        self._check_login()
        self.find("editor_ready", timeout=self.cfg.timeouts_seconds.page_load)

    def scene_count(self) -> int:
        return len(self.find_all("scene_list_item"))

    def add_scene(self, project_ref: str) -> str:
        self.step = "SCENE_ADD"
        scenes = self.find_all("scene_list_item")
        if scenes:
            self._safe_click(scenes[-1])
        before = len(scenes)
        self.click("add_scene_button")
        self.wait_until(lambda: self.scene_count() > before, self.cfg.timeouts_seconds.element, "scene_added")
        return f"scene:{self.scene_count()}"

    def delete_empty_scenes(self) -> int:
        """Remove scenes without objects (Vids adds one when the first clip is inserted)."""
        removed = 0
        for _ in range(10):
            scenes = self.find_all("scene_list_item")
            if len(scenes) <= 1:
                break
            empty_index = None
            for i, scene in enumerate(scenes):
                self._safe_click(scene)
                time.sleep(0.6)
                if self.exists("empty_scene_marker"):
                    empty_index = i
                    break
            if empty_index is None:
                break
            before = len(scenes)
            self.click("scene_menu")
            self.click("delete_scenes_option")
            self.wait_until(lambda: self.scene_count() < before, self.cfg.timeouts_seconds.element, "scene_deleted")
            removed += 1
        return removed

    def _safe_click(self, el: WebElement) -> None:
        try:
            el.click()
        except WebDriverException:
            self.d.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true}));", el)

    # ----- generation ---------------------------------------------------------------------

    def open_generation_panel(self) -> None:
        if not self.exists("prompt_textbox") and not self.exists("prompt_expand_button"):
            self.click("generate_video_entry")
            self.wait_until(lambda: self.exists("prompt_textbox") or self.exists("prompt_expand_button"),
                            self.cfg.timeouts_seconds.element, "generation_panel")
        if not self.exists("image_upload_input") and self.exists("prompt_expand_button"):
            self.click("prompt_expand_button")
            time.sleep(1)
        if self.exists("prompt_clear_button"):
            self.click("prompt_clear_button")
            time.sleep(1)

    def attach_images(self, image_paths: list[Path]) -> None:
        """Attach each image and confirm one preview chip per image (IM1-IM3). Raises UploadError."""
        self.step = "UPLOAD"
        self.d.execute_script(NO_FILE_DIALOG_JS)
        for index, image in enumerate(image_paths, start=1):
            before = len(self.find_all("image_upload_preview", visible_only=False))
            if before != index - 1:
                raise UploadError(f"Expected {index - 1} attached images, found {before}")
            file_input = self._next_file_input(first=index == 1)
            file_input.send_keys(str(Path(image).resolve()))
            try:
                WebDriverWait(self.d, self.cfg.timeouts_seconds.upload, poll_frequency=0.5).until(
                    lambda _d: len(self.find_all("image_upload_preview", visible_only=False)) >= index)
            except TimeoutException:
                self.capture_diagnostics("upload_timeout")
                raise UploadError(f"Image {index} did not attach within {self.cfg.timeouts_seconds.upload} s") from None
        attached = len(self.find_all("image_upload_preview", visible_only=False))
        if attached != len(image_paths):
            raise UploadError(f"Expected {len(image_paths)} attached images, found {attached}")

    def _next_file_input(self, first: bool) -> WebElement:
        self.d.execute_script("window.__gvaFileInput = null;")
        if first:
            inputs = self.find_all("image_upload_input", visible_only=False)
            if inputs:
                return inputs[-1]
        try:
            self.click("add_ingredient_button")
        except UIElementMissingError as exc:
            raise UploadError("The 'add ingredient' control is not available") from exc
        deadline = time.monotonic() + self.cfg.timeouts_seconds.element
        while time.monotonic() < deadline:
            el = self.d.execute_script("return window.__gvaFileInput || null;")
            if el is not None:
                return el
            inputs = self.find_all("image_upload_input", visible_only=False)
            if inputs:
                return inputs[-1]
            time.sleep(0.5)
        raise UploadError("No file input appeared for the next image")

    def enter_prompt(self, script_text: str) -> None:
        """Type the script verbatim and verify it by reading it back (05 §7, IM2)."""
        self.step = "PROMPT"
        expected = normalize_prompt(script_text)
        for attempt in (1, 2):
            box = self.find("prompt_textbox")
            if attempt == 2:
                self.d.execute_script(
                    "const s=getSelection(); s.selectAllChildren(arguments[0]); s.collapseToEnd();", box)
                current = normalize_prompt(box.get_attribute("innerText") or "")
                for _ in range(len(current) + 2):
                    box.send_keys(Keys.BACKSPACE)
            self.d.execute_script(INSERT_TEXT_JS, box, script_text.replace("\r\n", "\n").split("\n"))
            time.sleep(0.5)
            actual = normalize_prompt(self.find("prompt_textbox").get_attribute("innerText") or "")
            if actual == expected:
                return
        raise PromptMismatchError("The prompt shown in Vids does not match the script")

    def video_count(self) -> int:
        return len(self.find_all("generation_ready"))

    def submit_generation(self, project_ref: str, scene_ref: str | None,
                          script_text: str, image_paths: list[Path]) -> str:
        self.open_generation_panel()
        before_videos = self.video_count()
        self.attach_images(image_paths)
        self.enter_prompt(script_text)
        self.check_quota()
        self.step = "GENERATE"
        button = self.find("generate_button", clickable=True)
        button.click()  # exactly one click per attempt
        part_ref = f"gen-{uuid.uuid4().hex[:12]}"
        self._pending[part_ref] = before_videos
        # Confirm Vids accepted the request: progress, a new video, or a quota/error message.
        deadline = time.monotonic() + self.cfg.timeouts_seconds.element
        while time.monotonic() < deadline:
            if self.exists("generation_in_progress") or self.video_count() > before_videos:
                return part_ref
            if self.matches("quota"):
                raise QuotaReachedError("Google Vids shows a usage limit message")
            if self.matches("generation_failed"):
                return part_ref  # poll_generation reports the failure
            time.sleep(0.5)
        return part_ref  # no indicator yet; polling decides (the click is confirmed, quota spent)

    def poll_generation(self, part_ref: str) -> PollResult:
        self.step = "GENERATION_POLL"
        if part_ref not in self._pending:
            return PollResult(GenerationState.FAILED,
                              "The generated clip is no longer available in this browser session",
                              error_code="UNCERTAIN_SUBMIT")
        text = self.status_text()
        try:
            self._check_login()
        except LoginRequiredError:
            return PollResult(GenerationState.NEEDS_USER, "Google sign-in is needed")
        if self.video_count() > self._pending[part_ref] and not self.exists("generation_in_progress"):
            return PollResult(GenerationState.READY)
        if self.matches("quota", text):
            return PollResult(GenerationState.QUOTA, "Usage limit message shown")
        if self.matches("generation_failed", text):
            return PollResult(GenerationState.FAILED, "Google Vids reported a generation error")
        return PollResult(GenerationState.GENERATING)

    def place_generated_clip(self, part_ref: str) -> None:
        self.step = "PLACE_CLIP"
        scenes = self.find_all("scene_list_item")
        if scenes:
            self._safe_click(scenes[-1])  # insert at the end of the project (MP1 order)
        inserts = self.find_all("insert_generated_clip")
        if not inserts:
            self.find("insert_generated_clip")  # raises with diagnostics
        before = self.scene_count()
        self._safe_click(inserts[-1])  # newest generated clip is last in the panel
        time.sleep(2)
        self.wait_until(lambda: self.scene_count() >= max(before, 1), self.cfg.timeouts_seconds.element, "clip_inserted")
        self.delete_empty_scenes()

    def download_clip(self, part_ref: str, download_dir: Path) -> None:
        """Per-clip download is not available (M1): the one-scene project export is the clip."""
        self.export_project("", download_dir)

    def export_project(self, project_ref: str, download_dir: Path) -> None:
        self.step = "EXPORT"
        if project_ref and url_without_query(self.d.current_url) != project_ref:
            self.open_project(project_ref)
        self.delete_empty_scenes()
        browser.set_download_dir(self.d, download_dir)
        self.click("download_menu")
        submenu = self.find("download_submenu", clickable=True)
        ActionChains(self.d).move_to_element(submenu).perform()
        time.sleep(0.8)
        self.click("download_mp4_option")

    def export_finished(self) -> bool:
        return self.exists("export_ready") or self.matches("export_done")

    # ----- diagnostics (SE5, DEC-18) -------------------------------------------------------

    def capture_diagnostics(self, label: str) -> Path | None:
        try:
            folder = self.cfg.paths.screenshots_dir
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{self.diag_prefix}_{label}")[:80]
            path = folder / f"{safe}_{stamp}.png"
            self.driver.switch_to.window(self.handle)
            path.write_bytes(self.driver.get_screenshot_as_png())
            log.warning("diagnostics step=%s label=%s url=%s title_len=%d screenshot=%s", self.step, label,
                        url_without_query(self.driver.current_url), len(self.driver.title or ""), path.name)
            return path
        except (WebDriverException, OSError, TypeError):
            return None
