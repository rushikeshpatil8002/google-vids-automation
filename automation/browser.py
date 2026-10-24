"""Start or attach to the automation Chrome (DEC-11, 05 §1) and set its download folder (DEC-16).

Never adds stealth flags, a fake user agent or headless mode, and never uses the
user's everyday Chrome profile (05 §1 "Never", P2).
"""

import json
import logging
import subprocess
import time
import urllib.request
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.remote.webdriver import WebDriver

from automation import platform_utils
from automation.config import Config
from automation.errors import BrowserStartError, ConfigError

log = logging.getLogger(__name__)

STARTUP_POLL_SECONDS = 0.5
# Bypass any HTTP proxy from the environment: the debug endpoint is always local.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def profile_dir(cfg: Config) -> Path:
    """The dedicated automation profile. Refuses the user's everyday Chrome profile (P2)."""
    profile = cfg.browser.profile_dir
    for everyday in platform_utils.default_chrome_user_data_dirs():
        if profile == everyday or everyday in profile.parents:
            raise ConfigError(
                f"browser.profile_dir points into your everyday Chrome profile ({everyday}). "
                "Use a dedicated folder such as data/chrome-profile."
            )
    return profile


def _profile_args(cfg: Config) -> list[str]:
    width, height = cfg.browser.window_size
    return [
        f"--user-data-dir={profile_dir(cfg)}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={width},{height}",
    ]


def chrome_command(cfg: Config, chrome: Path, url: str | None = None) -> list[str]:
    """Arguments for a normal Chrome process with the automation profile and a debug port (attach mode)."""
    args = [str(chrome), *_profile_args(cfg), f"--remote-debugging-port={cfg.browser.debug_port}"]
    if url:
        args.append(url)
    return args


def debug_endpoint_alive(port: int, timeout: float = 1.0) -> bool:
    """True when a Chrome DevTools endpoint answers on 127.0.0.1:<port>."""
    try:
        with _LOCAL_OPENER.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            return "Browser" in json.load(resp)
    except (OSError, ValueError):
        return False


def debug_targets(port: int, timeout: float = 2.0) -> list[dict]:
    """Chrome's DevTools targets (tabs and others), most recently active first. Empty if unreachable."""
    try:
        with _LOCAL_OPENER.open(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as resp:
            targets = json.load(resp)
    except (OSError, ValueError):
        return []
    return targets if isinstance(targets, list) else []


def ensure_page_open(cfg: Config) -> None:
    """Make sure the running automation Chrome has at least one tab.

    macOS keeps Chrome running after its last window closes, and ChromeDriver cannot
    attach to a Chrome without tabs. Opens the Vids home page in a new tab, just as
    the user would with Cmd+N.
    """
    port = cfg.browser.debug_port
    if _has_page(port):
        return
    url = cfg.browser.vids_home_url
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
        with _LOCAL_OPENER.open(request, timeout=5):
            pass
    except OSError:
        pass
    deadline = time.monotonic() + cfg.timeouts_seconds.element
    while time.monotonic() < deadline:
        if _has_page(port):
            log.info("Opened a tab in the automation Chrome, which had no window")
            return
        time.sleep(STARTUP_POLL_SECONDS)
    raise BrowserStartError(
        "The automation Chrome is running with no window open. Open one (Cmd+N / Ctrl+N), "
        "or quit it and run `python app.py --login` again."
    )


def _has_page(port: int) -> bool:
    return any(t.get("type") == "page" for t in debug_targets(port))


def start_chrome(cfg: Config, url: str | None = None) -> bool:
    """Start the automation Chrome unless one already answers on the debug port.

    Returns True if Chrome was started, False if a running one was reused. Chrome is
    detached, so it keeps running (and stays signed in) after this process exits.
    """
    port = cfg.browser.debug_port
    if debug_endpoint_alive(port):
        log.info("Reusing the automation Chrome on 127.0.0.1:%s", port)
        ensure_page_open(cfg)
        return False

    chrome = platform_utils.find_chrome_binary(cfg.browser.chrome_binary)
    profile_dir(cfg).mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            chrome_command(cfg, chrome, url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **platform_utils.detached_popen_kwargs(),
        )
    except OSError as exc:
        raise BrowserStartError(f"Could not start Chrome at {chrome}: {exc.strerror}") from exc

    timeout = cfg.timeouts_seconds.page_load
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if debug_endpoint_alive(port):
            log.info("Started the automation Chrome on 127.0.0.1:%s", port)
            return True
        time.sleep(STARTUP_POLL_SECONDS)
    raise BrowserStartError(
        f"Chrome did not open its debugging port 127.0.0.1:{port} within {timeout} s. "
        "If a Chrome window using the automation profile is already open, close it and try again. "
        "Otherwise change browser.debug_port in config.json."
    )


def attach_driver(cfg: Config) -> WebDriver:
    """Attach Selenium to the automation Chrome that is already running."""
    port = cfg.browser.debug_port
    if not debug_endpoint_alive(port):
        raise BrowserStartError(
            f"No automation Chrome is listening on 127.0.0.1:{port}. Run `python app.py --login` first."
        )
    ensure_page_open(cfg)
    opts = webdriver.ChromeOptions()
    opts.debugger_address = f"127.0.0.1:{port}"
    # Lets Selenium Manager pick the ChromeDriver that matches this Chrome.
    opts.binary_location = str(platform_utils.find_chrome_binary(cfg.browser.chrome_binary))
    return _new_driver(opts, f"attach to Chrome on 127.0.0.1:{port}", cfg)


def get_driver(cfg: Config) -> WebDriver:
    """A WebDriver for the configured browser.mode (DEC-11)."""
    if cfg.browser.mode == "attach":
        start_chrome(cfg, cfg.browser.vids_home_url)
        return attach_driver(cfg)
    opts = webdriver.ChromeOptions()
    opts.binary_location = str(platform_utils.find_chrome_binary(cfg.browser.chrome_binary))
    profile_dir(cfg).mkdir(parents=True, exist_ok=True)
    for arg in _profile_args(cfg):
        opts.add_argument(arg)
    return _new_driver(opts, "launch Chrome", cfg)


def _new_driver(opts: webdriver.ChromeOptions, action: str, cfg: Config) -> WebDriver:
    """Start a ChromeDriver session: Selenium Manager finds the driver unless browser.chromedriver_path is set."""
    driver_path = cfg.browser.chromedriver_path
    if driver_path is not None and not driver_path.is_file():
        raise ConfigError(f"browser.chromedriver_path does not exist: {driver_path}")
    try:
        if driver_path is not None:
            return webdriver.Chrome(options=opts, service=Service(executable_path=str(driver_path)))
        return webdriver.Chrome(options=opts)
    except WebDriverException as exc:
        raise BrowserStartError(describe_driver_error(exc, action)) from exc


def _first_line(exc: BaseException) -> str:
    text = (getattr(exc, "msg", None) or str(exc)).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def describe_driver_error(exc: BaseException, action: str) -> str:
    """User-facing error with Selenium's underlying cause and, for driver problems, how to fix them."""
    message = f"Selenium could not {action}: {_first_line(exc)}"
    cause = exc.__cause__ or exc.__context__
    if cause is not None and _first_line(cause) not in message:
        message += f" (cause: {_first_line(cause)})"
    if "Unable to obtain driver" in message or "driver_location" in message:
        message += (". Selenium Manager could not get ChromeDriver. Check internet access (proxy, firewall or "
                    "antivirus), run `python app.py --check-browser` for details, or download the ChromeDriver "
                    "that matches your Chrome version and set browser.chromedriver_path in config.json.")
    return message


def set_download_dir(driver: WebDriver, path: Path) -> Path:
    """Send this browser's downloads to `path` through CDP (SE3, DEC-16). Call before every download."""
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    driver.execute_cdp_cmd(
        "Browser.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(path), "eventsEnabled": True},
    )
    return path
