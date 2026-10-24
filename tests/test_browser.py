"""browser.py without a real Chrome: command line, profile guard, startup polling, CDP call."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from automation import browser, platform_utils
from automation.errors import BrowserStartError, ConfigError


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    path = tmp_path / "chrome"
    monkeypatch.setattr(platform_utils, "find_chrome_binary", lambda configured=None: path)
    return path


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dec11_attach_command_uses_dedicated_profile_and_local_debug_port(make_config, chrome, tmp_path):
    cfg = make_config({"browser": {"debug_port": 9333}})
    args = browser.chrome_command(cfg, chrome, "https://vids.google.com/")
    assert args[0] == str(chrome)
    assert f"--user-data-dir={tmp_path / 'data' / 'chrome-profile'}" in args
    assert "--remote-debugging-port=9333" in args
    assert args[-1] == "https://vids.google.com/"


def test_se6_no_evasion_flags_in_chrome_command(make_config, chrome):
    """CLAUDE.md rule 5: no stealth, user-agent spoofing, hidden automation flags or headless."""
    joined = " ".join(browser.chrome_command(make_config(), chrome, "https://vids.google.com/"))
    for banned in ("headless", "user-agent", "disable-blink-features", "AutomationControlled", "enable-automation"):
        assert banned not in joined


def test_p2_everyday_chrome_profile_is_refused(make_config, monkeypatch, tmp_path):
    everyday = tmp_path / "everyday"
    monkeypatch.setattr(platform_utils, "default_chrome_user_data_dirs", lambda: [everyday])
    for profile in (everyday, everyday / "Default"):
        cfg = make_config({"browser": {"profile_dir": str(profile)}})
        with pytest.raises(ConfigError, match="everyday Chrome profile"):
            browser.profile_dir(cfg)


def test_debug_endpoint_dead_when_nothing_listens():
    assert browser.debug_endpoint_alive(free_port()) is False


def test_debug_endpoint_alive_and_targets_listed():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"/json/version": {"Browser": "Chrome/140"}, "/json/list": [{"id": "A", "type": "page"}]}
            payload = json.dumps(body[self.path]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert browser.debug_endpoint_alive(port) is True
        assert browser.debug_targets(port) == [{"id": "A", "type": "page"}]
    finally:
        server.shutdown()
        server.server_close()


def test_dec11_running_chrome_is_reused_not_restarted(make_config, chrome, monkeypatch):
    """05 §1 step 2: if the debug port already answers, no second Chrome is started."""
    monkeypatch.setattr(browser, "debug_endpoint_alive", lambda port, timeout=1.0: True)
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: pytest.fail("Chrome was started"))
    assert browser.start_chrome(make_config(), "https://vids.google.com/") is False


def test_dec11_start_chrome_waits_for_debug_port(make_config, chrome, monkeypatch, tmp_path):
    answers = iter([False, False, True])
    launched = []
    monkeypatch.setattr(browser, "debug_endpoint_alive", lambda port, timeout=1.0: next(answers))
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kwargs: launched.append((args, kwargs)))
    monkeypatch.setattr(browser, "STARTUP_POLL_SECONDS", 0.01)

    assert browser.start_chrome(make_config(), "https://vids.google.com/") is True
    args, kwargs = launched[0]
    assert args[0] == str(chrome)
    assert (tmp_path / "data" / "chrome-profile").is_dir()
    assert kwargs["stdout"] is browser.subprocess.DEVNULL


def test_start_chrome_times_out_with_actionable_message(make_config, chrome, monkeypatch):
    monkeypatch.setattr(browser, "debug_endpoint_alive", lambda port, timeout=1.0: False)
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(browser, "STARTUP_POLL_SECONDS", 0.01)
    cfg = make_config({"timeouts_seconds": {"page_load": 1}})
    with pytest.raises(BrowserStartError, match="close it and try again"):
        browser.start_chrome(cfg)


def test_attach_without_running_chrome_says_to_login(make_config, chrome, monkeypatch):
    monkeypatch.setattr(browser, "debug_endpoint_alive", lambda port, timeout=1.0: False)
    with pytest.raises(BrowserStartError, match="app.py --login"):
        browser.attach_driver(make_config())


def test_se3_download_dir_set_through_cdp(tmp_path):
    """SE3, DEC-16: downloads go to the per-job staging folder via Browser.setDownloadBehavior."""

    class FakeDriver:
        def __init__(self):
            self.calls = []

        def execute_cdp_cmd(self, cmd, params):
            self.calls.append((cmd, params))

    driver = FakeDriver()
    staging = tmp_path / "data" / "downloads_staging" / "job1"
    assert browser.set_download_dir(driver, staging) == staging
    assert staging.is_dir()
    assert driver.calls == [(
        "Browser.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": str(staging), "eventsEnabled": True},
    )]


def test_ensure_page_open_opens_vids_tab_when_chrome_has_no_window(make_config, monkeypatch):
    """macOS keeps Chrome running with no window; attaching must first open a tab."""
    lists = iter([[], [], [{"id": "A", "type": "page"}]])
    opened = []
    monkeypatch.setattr(browser, "debug_targets", lambda port, timeout=2.0: next(lists))
    monkeypatch.setattr(browser._LOCAL_OPENER, "open", lambda req, timeout=5: opened.append(req) or _NullCtx())
    monkeypatch.setattr(browser, "STARTUP_POLL_SECONDS", 0.01)
    browser.ensure_page_open(make_config())
    assert opened[0].get_method() == "PUT"
    assert opened[0].full_url.endswith("/json/new?https://vids.google.com/")


def test_ensure_page_open_explains_when_no_tab_appears(make_config, monkeypatch):
    monkeypatch.setattr(browser, "debug_targets", lambda port, timeout=2.0: [])
    monkeypatch.setattr(browser._LOCAL_OPENER, "open", lambda req, timeout=5: _NullCtx())
    monkeypatch.setattr(browser, "STARTUP_POLL_SECONDS", 0.01)
    with pytest.raises(BrowserStartError, match="no window open"):
        browser.ensure_page_open(make_config({"timeouts_seconds": {"element": 1}}))


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_driver_error_shows_cause_and_fix(make_config, monkeypatch):
    """A 'Unable to obtain driver' failure must explain the real cause and how to fix it (Windows report)."""
    from selenium.common.exceptions import NoSuchDriverException, WebDriverException

    def fail(**kwargs):
        try:
            raise WebDriverException("Unsuccessful response (exit code 65): error sending request for url")
        except WebDriverException as cause:
            raise NoSuchDriverException("Unable to obtain driver for chrome; see driver_location") from cause

    monkeypatch.setattr(browser.webdriver, "Chrome", fail)
    with pytest.raises(BrowserStartError) as err:
        browser._new_driver(browser.webdriver.ChromeOptions(), "attach to Chrome", make_config())
    message = str(err.value)
    assert "Unable to obtain driver" in message and "error sending request" in message
    assert "--check-browser" in message and "chromedriver_path" in message


def test_chromedriver_path_used_when_configured(make_config, monkeypatch, tmp_path):
    driver = tmp_path / "chromedriver.exe"
    driver.write_bytes(b"")
    seen = {}
    monkeypatch.setattr(browser.webdriver, "Chrome", lambda **kwargs: seen.update(kwargs) or "driver")
    cfg = make_config({"browser": {"chromedriver_path": str(driver)}})
    assert browser._new_driver(browser.webdriver.ChromeOptions(), "launch Chrome", cfg) == "driver"
    assert seen["service"].path == str(driver)


def test_missing_chromedriver_path_is_a_config_error(make_config, tmp_path):
    cfg = make_config({"browser": {"chromedriver_path": str(tmp_path / "nope.exe")}})
    with pytest.raises(ConfigError, match="chromedriver_path does not exist"):
        browser._new_driver(browser.webdriver.ChromeOptions(), "launch Chrome", cfg)


def test_er4_missing_element_message_names_open_dialog():
    """ER4 diagnostics: the error says which dialog blocked the step (UI label only)."""
    from automation.errors import UIElementMissingError

    exc = UIElementMissingError("editor_ready", "PROJECT_CREATE", None, "Open dialog(s): 'Getting started'")
    assert str(exc) == "UI element 'editor_ready' not found during step PROJECT_CREATE. Open dialog(s): 'Getting started'"
    assert str(UIElementMissingError("x", "S")) == "UI element 'x' not found during step S"


def test_selectors_define_create_url():
    import json
    from automation.vids_controller import SELECTORS_FILE, check_selectors_verified

    sel = json.loads(SELECTORS_FILE.read_text(encoding="utf-8"))
    assert sel["urls"]["create"].startswith("https://docs.google.com/videos/")
    check_selectors_verified(sel)
