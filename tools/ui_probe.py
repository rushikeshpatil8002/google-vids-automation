"""M1 probe: snapshot the Google Vids page the human is driving in the automation Chrome (05 §9).

Run `python app.py --login` first. Then drive Vids by hand and, at every screen change,
type a short label here and press Enter. Each snapshot goes to
docs/feasibility/raw/<NN>_<label>/ (gitignored):

  screenshot.png  the visible viewport
  dom.html        page HTML, with inline <script> elements and hidden-input values removed
                  (Google embeds session tokens there; CLAUDE.md rule 4)
  a11y.json       visible elements (plus hidden file inputs) with role, aria-label, text,
                  tag, id, data-* and more; iframes one level deep; emails masked
  url.txt         URL without query string or fragment

The probe only reads the page. It never clicks, types, or reads cookies.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # lets `python tools/ui_probe.py` import the automation package

from selenium.common.exceptions import WebDriverException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.remote.webdriver import WebDriver  # noqa: E402

from automation import browser  # noqa: E402
from automation.config import load_config  # noqa: E402
from automation.errors import BrowserStartError, ConfigError  # noqa: E402

RAW_DIR = ROOT / "docs" / "feasibility" / "raw"
TEXT_MAX = 80
EMAIL_MASK = "***@***"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
HIDDEN_TYPE_RE = re.compile(r"\btype\s*=\s*[\"']?hidden\b", re.IGNORECASE)
VALUE_ATTR_RE = re.compile(r"\s+value\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
# Query strings of links and sources can carry signed, token-bearing URLs.
URL_QUERY_RE = re.compile(r"(\s(?:href|src)\s*=\s*([\"']))([^\"'?#]*)[?#][^\"']*(\2)", re.IGNORECASE)
DELAY_RE = re.compile(r"^(.*?)\s*\+(\d{1,2})$")

# Runs in the page. Collects visible elements worth a selector, plus every file input.
COLLECT_JS = r"""
const MAX = arguments[0];
const INTERACTIVE = new Set(["A", "BUTTON", "INPUT", "TEXTAREA", "SELECT", "OPTION", "LABEL",
                             "SUMMARY", "IFRAME", "VIDEO", "IMG"]);
const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "META", "LINK"]);
const norm = s => (s || "").replace(/\s+/g, " ").trim();
const elements = [];

function visible(el, r) {
  if (r.width === 0 || r.height === 0) return false;
  const s = getComputedStyle(el);
  return s.visibility !== "hidden" && s.display !== "none";
}

function ownText(el) {
  let t = "";
  for (const n of el.childNodes) if (n.nodeType === Node.TEXT_NODE) t += n.textContent;
  return norm(t);
}

function visit(root, inShadow) {
  for (const el of root.querySelectorAll("*")) {
    if (el.shadowRoot) visit(el.shadowRoot, true);
    if (SKIP.has(el.tagName)) continue;
    const isFile = el.tagName === "INPUT" && (el.type || "").toLowerCase() === "file";
    const r = el.getBoundingClientRect();
    const shown = visible(el, r);
    if (!shown && !isFile) continue;
    const data = {};
    for (const a of el.attributes) if (a.name.startsWith("data-")) data[a.name] = a.value.slice(0, MAX);
    const role = el.getAttribute("role");
    const aria = el.getAttribute("aria-label");
    const editableRoot = el.hasAttribute("contenteditable") && el.isContentEditable;
    if (!(isFile || role || aria || el.id || Object.keys(data).length || INTERACTIVE.has(el.tagName)
          || editableRoot || el.hasAttribute("tabindex") || ownText(el))) continue;
    elements.push({
      tag: el.tagName.toLowerCase(),
      role: role,
      aria_label: aria,
      id: el.id || null,
      text: norm(el.innerText).slice(0, MAX) || null,
      data: data,
      type: el.getAttribute("type"),
      name: el.getAttribute("name"),
      placeholder: el.getAttribute("placeholder"),
      title: el.getAttribute("title"),
      disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
      contenteditable: editableRoot,
      file_input: isFile,
      accept: isFile ? el.getAttribute("accept") : null,
      multiple: isFile ? el.multiple : null,
      visible: shown,
      in_shadow: inShadow,
      src: el.tagName === "IFRAME" ? (el.src || "").split(/[?#]/)[0] : null,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    });
  }
}

visit(document, false);
return {title: document.title, viewport: [innerWidth, innerHeight], elements: elements};
"""


def sanitize_label(label: str) -> str:
    """A safe folder-name label: letters, digits, '-' and '_' only, at most 40 characters."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")[:40] or "snap"


def next_snapshot_dir(raw_dir: Path, label: str) -> Path:
    """Create raw_dir/<NN>_<label>/ with NN one above the highest existing number, so nothing is overwritten."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    numbers = [int(p.name.split("_", 1)[0]) for p in raw_dir.iterdir() if p.is_dir() and p.name[:2].isdigit()]
    number = max(numbers, default=0) + 1
    while True:
        folder = raw_dir / f"{number:02d}_{sanitize_label(label)}"
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            number += 1


def url_without_query(url: str) -> str:
    """The URL without query string, fragment or user info (DEC-18)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def mask_emails(value: Any) -> Any:
    """Replace email addresses in every string inside `value` (nested dicts and lists)."""
    if isinstance(value, str):
        return EMAIL_RE.sub(EMAIL_MASK, value)
    if isinstance(value, list):
        return [mask_emails(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_emails(v) for k, v in value.items()}
    return value


def clean_html(html: str) -> str:
    """Remove inline <script> elements, hidden-input values and URL query strings, where Google pages keep tokens."""
    html = SCRIPT_RE.sub("<!-- script removed by ui_probe -->", html)
    html = URL_QUERY_RE.sub(r"\1\3\4", html)

    def drop_hidden_value(match: re.Match[str]) -> str:
        tag = match.group(0)
        return VALUE_ATTR_RE.sub("", tag) if HIDDEN_TYPE_RE.search(tag) else tag

    return INPUT_TAG_RE.sub(drop_hidden_value, html)


def parse_label(entry: str) -> tuple[str, int]:
    """'file_menu +5' -> ('file_menu', 5): wait 5 s first, so a menu can be opened in Chrome."""
    match = DELAY_RE.match(entry.strip())
    if match:
        return match.group(1) or "snap", int(match.group(2))
    return entry.strip() or "snap", 0


def switch_to_active_tab(driver: WebDriver, port: int) -> None:
    """Point the driver at the tab the human used last.

    Chrome's /json/list puts the most recently active target first, and ChromeDriver
    window handles are those target ids. If nothing matches, the current tab is kept;
    the caller prints the title either way, so a wrong tab is easy to spot.
    """
    handles = set(driver.window_handles)
    for target in browser.debug_targets(port):
        if target.get("type") == "page" and target.get("id") in handles:
            driver.switch_to.window(target["id"])
            return


def collect(driver: WebDriver) -> dict[str, Any]:
    """Elements of the current page, plus each iframe's elements one level deep."""
    snapshot = driver.execute_script(COLLECT_JS, TEXT_MAX)
    frames = []
    for index, frame in enumerate(driver.find_elements(By.TAG_NAME, "iframe")):
        entry: dict[str, Any] = {"index": index, "src": url_without_query(frame.get_attribute("src") or "")}
        try:
            driver.switch_to.frame(frame)
            entry["elements"] = driver.execute_script(COLLECT_JS, TEXT_MAX)["elements"]
        except WebDriverException as exc:
            entry["error"] = type(exc).__name__
        finally:
            driver.switch_to.default_content()
        frames.append(entry)
    snapshot["frames"] = frames
    return snapshot


def take_snapshot(driver: WebDriver, port: int, raw_dir: Path, label: str) -> tuple[Path, str, int]:
    """Save one snapshot. Returns (folder, page title, element count)."""
    switch_to_active_tab(driver, port)
    folder = next_snapshot_dir(raw_dir, label)
    url = url_without_query(driver.current_url)
    (folder / "screenshot.png").write_bytes(driver.get_screenshot_as_png())
    (folder / "dom.html").write_text(clean_html(driver.page_source), encoding="utf-8")
    snapshot = collect(driver)
    snapshot = mask_emails({"captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "url": url, **snapshot})
    (folder / "a11y.json").write_text(json.dumps(snapshot, indent=1, ensure_ascii=False), encoding="utf-8")
    (folder / "url.txt").write_text(mask_emails(url) + "\n", encoding="utf-8")
    return folder, snapshot["title"], len(snapshot["elements"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot the live Google Vids page for M1 selector work.")
    parser.add_argument("--out", type=Path, default=RAW_DIR, help=f"snapshot folder (default: {RAW_DIR})")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        driver = browser.attach_driver(cfg)
    except (ConfigError, BrowserStartError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Attached to the automation Chrome. Drive Google Vids by hand.")
    print("At each screen change, type a short label and press Enter (empty = 'snap'). Type q to quit.")
    print("Add +N (e.g. 'file_menu +5') to wait N seconds first, so you can open a menu in Chrome.")
    try:
        while True:
            entry = input("label> ").strip()
            if entry.lower() == "q":
                break
            label, delay = parse_label(entry)
            if delay:
                print(f"  capturing in {delay} s...")
                time.sleep(delay)
            try:
                folder, title, count = take_snapshot(driver, cfg.browser.debug_port, args.out, label)
            except WebDriverException as exc:
                print(f"  snapshot failed: {type(exc).__name__}. Is the tab still open?")
                continue
            print(f"  saved {folder.name}: {count} elements, tab \"{mask_emails(title)[:60]}\"")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        # Ends the Selenium session only. ChromeDriver never closes a Chrome it attached to.
        driver.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
