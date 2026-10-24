"""Google Vids Automation: local Flask UI + JSON API (03 §8) and CLI flags.

python app.py                 UI at http://127.0.0.1:5000 with the live controller
python app.py --fake          fake controller (no Google access, no quota)
python app.py --fake --scenario tests/scenarios/quota_after_2.json
python app.py --login         open the automation Chrome for manual sign-in
python app.py --check-browser diagnose Chrome / ChromeDriver / attach problems (no Google actions)
python app.py --host 0.0.0.0  LAN access for UI testing (automation still runs on this machine)
"""

import argparse
import json
import logging
import platform
import socket
import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file

from automation import __version__ as VERSION, browser, platform_utils
from automation.config import PROJECT_ROOT, Config, load_config
from automation.controller_base import Capabilities, VidsController
from automation.errors import BrowserStartError, ConfigError, ValidationError
from automation.jobs import JobService
from automation.logging_setup import setup_logging
from automation.models import (
    EDITABLE_JOB_STATUSES,
    RESUMABLE_JOB_STATUSES,
    RETRYABLE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    JobStatus,
    PartStatus,
)
from automation.report import build_report, to_markdown, write_reports
from automation.state_store import Store
from automation.worker import Worker
from automation.workflow import Workflow

log = logging.getLogger("app")


def make_controller(cfg: Config, fake: bool, scenario: str | None) -> VidsController:
    if fake:
        from automation.fake_controller import FakeVidsController
        return FakeVidsController(scenario or cfg.fake.scenario, cfg.paths.data_dir, cfg.paths.screenshots_dir)
    from automation.vids_controller import VidsSeleniumController
    return VidsSeleniumController(cfg)


class _UnverifiedController:
    """Stands in when the live controller refuses to start (DEC-25); every run fails with the reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def capabilities(self) -> Capabilities:
        return Capabilities(False, False, True, 1, None)

    def start(self) -> None:
        raise ConfigError(self.reason)

    def __getattr__(self, name: str):
        def fail(*args: Any, **kwargs: Any) -> Any:
            raise ConfigError(self.reason)
        return fail


def create_app(cfg: Config | None = None, fake: bool = False, scenario: str | None = None,
               lan: bool = False, controller: VidsController | None = None) -> Flask:
    cfg = cfg or load_config()
    fake = fake or cfg.fake.enabled
    store = Store(cfg.paths.db_file)
    jobs = JobService(store, cfg)
    if controller is None:
        try:
            controller = make_controller(cfg, fake, scenario)
        except ConfigError as exc:
            controller = _UnverifiedController(str(exc))  # type: ignore[assignment]
    worker = Worker(lambda on_step: Workflow(store, cfg, controller, on_step))

    # Startup reconciliation for every non-terminal job (04 §6, A7).
    for job in store.list_jobs():
        if JobStatus(job["overall_status"]) not in TERMINAL_JOB_STATUSES | EDITABLE_JOB_STATUSES:
            worker.workflow.reconcile(job["job_id"], startup=True)

    app = Flask(__name__, template_folder=str(PROJECT_ROOT / "ui" / "templates"),
                static_folder=str(PROJECT_ROOT / "ui" / "static"))
    app.config.update(MAX_CONTENT_LENGTH=200 * 1024 * 1024)
    app.extensions["gva"] = {"cfg": cfg, "store": store, "jobs": jobs, "worker": worker, "fake": fake, "lan": lan}

    def error(code: str, message: str, status: int):
        return jsonify({"error": {"code": code, "message": message}}), status

    def get_job_or_404(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            raise LookupError(job_id)
        return job

    def require_status(job: dict[str, Any], allowed: set[JobStatus], action: str) -> None:
        if JobStatus(job["overall_status"]) not in allowed:
            raise PermissionError(f"Can't {action} while the job is {job['overall_status']}.")

    @app.errorhandler(LookupError)
    def _not_found(exc):
        return error("not_found", "Not found.", 404)

    @app.errorhandler(ValidationError)
    def _invalid(exc):
        return error("validation", str(exc), 400)

    @app.errorhandler(PermissionError)
    def _conflict(exc):
        return error("invalid_state", str(exc), 409)

    def caps() -> Capabilities:
        return worker.controller.capabilities()

    def part_view(p: dict[str, Any]) -> dict[str, Any]:
        return {k: p[k] for k in ("part_id", "sequence", "title", "script_chars", "script_sha12", "status",
                                  "validation_errors", "retry_count", "output_path", "original_filename",
                                  "output_bytes", "error_code", "error_message")} | {
            "image_count": len(p["image_paths"]), "images": [Path(i).name for i in p["image_paths"]],
            "output_name": Path(p["output_path"]).name if p["output_path"] else None}

    def counts(job_id: str) -> dict[str, int]:
        parts = store.get_parts(job_id)
        job = store.get_job(job_id)
        done = sum(1 for p in parts if p["status"] == PartStatus.DOWNLOADED.value or (
            p["status"] == PartStatus.READY.value and job["effective_mode"] == "multi_part"
            and job["final_export_status"] == "DOWNLOADED"))
        out = {"total": len(parts), "done": done}
        for p in parts:
            out[p["status"]] = out.get(p["status"], 0) + 1
        return out

    # ----- pages and status -------------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "fake_mode": fake, "lan_mode": lan, "version": VERSION})

    @app.get("/api/jobs")
    def list_jobs():
        return jsonify([{k: j[k] for k in ("job_id", "project_name", "mode", "overall_status", "created_at")}
                        | {"counts": counts(j["job_id"])} for j in store.list_jobs()])

    @app.post("/api/jobs")
    def create_job():
        data = request.get_json(force=True, silent=True) or {}
        job_id = jobs.create_job(data.get("project_name", ""), data.get("mode", "single"), data.get("output_dir") or None,
                                 data.get("clip_template") or None, data.get("final_template") or None)
        return jsonify({"job_id": job_id}), 201

    @app.post("/api/jobs/import")
    def import_job():
        if "manifest" in request.files:
            try:
                manifest = json.loads(request.files["manifest"].read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError(f"Manifest is not valid JSON: {exc}") from None
        else:
            data = request.get_json(force=True, silent=True) or {}
            manifest = jobs.load_manifest_file(data["path"]) if "path" in data else data
        return jsonify({"job_id": jobs.import_manifest(manifest)}), 201

    @app.get("/api/jobs/<job_id>")
    def job_view(job_id):
        job = get_job_or_404(job_id)
        cur = worker.current()
        current = vars(cur) if cur.job_id == job_id else None
        return jsonify({"job": job, "parts": [part_view(p) for p in store.get_parts(job_id)],
                        "events": store.events(job_id, 50), "counts": counts(job_id), "current": current,
                        "busy_job": worker.busy_job()})

    @app.patch("/api/jobs/<job_id>")
    def edit_job(job_id):
        get_job_or_404(job_id)
        jobs.edit_job(job_id, **(request.get_json(force=True, silent=True) or {}))
        return jsonify({"ok": True})

    # ----- parts ---------------------------------------------------------------------------------

    def uploaded_inputs() -> tuple[bytes | None, list[tuple[str, bytes]] | None]:
        script = None
        if request.files.get("script_file") and request.files["script_file"].filename:
            script = request.files["script_file"].read()
        elif request.form.get("script_text") is not None:
            script = request.form["script_text"].encode("utf-8")
        files = [f for f in request.files.getlist("images[]") + request.files.getlist("images") if f.filename]
        images = [(f.filename, f.read()) for f in files] or None
        return script, images

    @app.post("/api/jobs/<job_id>/parts")
    def add_part(job_id):
        get_job_or_404(job_id)
        script, images = uploaded_inputs()
        if script is None:
            raise ValidationError("Provide script_text or script_file.")
        part_id = jobs.add_part(job_id, script, images or [], request.form.get("title"))
        return jsonify({"part_id": part_id}), 201

    @app.patch("/api/jobs/<job_id>/parts/<part_id>")
    def edit_part(job_id, part_id):
        get_job_or_404(job_id)
        script, images = uploaded_inputs()
        jobs.replace_part_inputs(part_id, script, images, request.form.get("title"))
        return jsonify({"ok": True})

    @app.delete("/api/jobs/<job_id>/parts/<part_id>")
    def remove_part(job_id, part_id):
        get_job_or_404(job_id)
        jobs.remove_part(part_id)
        return jsonify({"ok": True})

    @app.post("/api/jobs/<job_id>/parts/reorder")
    def reorder(job_id):
        get_job_or_404(job_id)
        order = (request.get_json(force=True, silent=True) or {}).get("order", [])
        try:
            jobs.reorder(job_id, order)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        return jsonify({"ok": True})

    @app.get("/api/parts/<part_id>/thumb/<int:index>")
    def thumb(part_id, index):
        try:
            return send_file(jobs.thumbnail(part_id, index), mimetype="image/png")
        except (KeyError, OSError):
            raise LookupError(part_id) from None

    # ----- validate, plan, run ------------------------------------------------------------------

    @app.post("/api/jobs/<job_id>/validate")
    def validate(job_id):
        get_job_or_404(job_id)
        return jsonify(jobs.validate(job_id, caps()))

    @app.get("/api/jobs/<job_id>/plan")
    def plan(job_id):
        job = get_job_or_404(job_id)
        require_status(job, {JobStatus.VALIDATED}, "show the plan")
        return jsonify(jobs.plan(job_id, caps()))

    def ensure_idle() -> None:
        if worker.busy_job() or any(j["overall_status"] == JobStatus.RUNNING.value for j in store.list_jobs()):
            raise PermissionError("Another job is running. Wait for it to finish or stop it first.")

    @app.post("/api/jobs/<job_id>/start")
    def start(job_id):
        job = get_job_or_404(job_id)
        require_status(job, {JobStatus.VALIDATED}, "start")
        ensure_idle()
        worker.submit("START", job_id)
        return jsonify({"ok": True}), 202

    @app.post("/api/jobs/<job_id>/stop")
    def stop(job_id):
        job = get_job_or_404(job_id)
        if job["overall_status"] != JobStatus.RUNNING.value and worker.busy_job() != job_id:
            raise PermissionError("The job is not running.")
        store.add_event(job_id, None, "STOP_REQUESTED", "INFO", "Stop requested; the current generation will finish first")
        worker.submit("STOP", job_id)
        return jsonify({"ok": True}), 202

    @app.post("/api/jobs/<job_id>/resume")
    def resume(job_id):
        job = get_job_or_404(job_id)
        require_status(job, RESUMABLE_JOB_STATUSES | {JobStatus.FAILED}, "resume")
        ensure_idle()
        worker.submit("RESUME", job_id)
        return jsonify({"ok": True}), 202

    @app.post("/api/jobs/<job_id>/retry-failed")
    def retry_failed(job_id):
        job = get_job_or_404(job_id)
        require_status(job, RETRYABLE_JOB_STATUSES, "retry")
        ensure_idle()
        worker.submit("RETRY_FAILED", job_id)
        return jsonify({"ok": True}), 202

    @app.post("/api/jobs/<job_id>/open-output")
    def open_output(job_id):
        job = get_job_or_404(job_id)
        if request.remote_addr not in ("127.0.0.1", "::1"):
            raise PermissionError("Open Output Folder works only on the computer running the app.")
        folder = Path(job["output_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        platform_utils.open_folder(folder)
        return jsonify({"ok": True})

    @app.get("/api/jobs/<job_id>/report")
    def report(job_id):
        get_job_or_404(job_id)
        fmt = request.args.get("format", "json")
        try:
            json_path, md_path = write_reports(store, job_id)
            path = md_path if fmt == "md" else json_path
            return send_file(path, as_attachment=True, download_name=path.name)
        except OSError:
            data = build_report(store, job_id)
            body = to_markdown(data) if fmt == "md" else json.dumps(data, indent=2)
            return app.response_class(body, mimetype="text/markdown" if fmt == "md" else "application/json")

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id):
        get_job_or_404(job_id)
        if worker.busy_job() == job_id:
            raise PermissionError("Stop the job before deleting its data.")
        jobs.delete_job_data(job_id, delete_outputs=request.args.get("delete_outputs") == "true")
        return jsonify({"ok": True})

    # ----- browser ---------------------------------------------------------------------------------

    @app.post("/api/browser/login")
    def browser_login():
        if fake:
            return jsonify({"ok": True, "message": "Fake mode: no browser needed."})
        if worker.busy_job():
            raise PermissionError("Wait for the running job before opening the sign-in window.")
        try:
            browser.start_chrome(cfg, cfg.browser.vids_home_url)
        except (ConfigError, BrowserStartError) as exc:
            return error("browser", str(exc), 500)
        return jsonify({"ok": True, "message": "Sign in in the Chrome window that opened, then come back here."})

    @app.get("/api/browser/status")
    def browser_status():
        if fake:
            return jsonify({"browser_running": True, "signed_in": True, "fake": True})
        return jsonify({"browser_running": browser.debug_endpoint_alive(cfg.browser.debug_port), "signed_in": "unknown"})

    return app


def login(cfg: Config) -> None:
    """Open the automation Chrome on the Vids home page for a manual sign-in (ER3, DEC-11)."""
    url = cfg.browser.vids_home_url
    if cfg.browser.mode == "attach":
        if browser.start_chrome(cfg, url):
            print(f"Opened the automation Chrome (profile: {cfg.browser.profile_dir}).")
        else:
            print(f"The automation Chrome is already running on 127.0.0.1:{cfg.browser.debug_port}. Switch to it.")
        print(f"Sign in to Google there, complete any 2FA, and confirm Google Vids opens at {url}")
        print("Chrome stays open after this command exits, and the session is kept in the profile.")
        return
    driver = browser.get_driver(cfg)
    try:
        driver.get(url)
        input("Sign in to Google in the Chrome window, confirm Google Vids opens, then press Enter here to close it. ")
    finally:
        driver.quit()


def check_browser(cfg: Config) -> int:
    """Print what the app needs from Chrome and ChromeDriver, and where it fails. Does nothing on Google pages."""
    import selenium

    ok = True
    print(f"OS: {platform.platform()} | Python {platform.python_version()} | Selenium {selenium.__version__}")
    print(f"Browser mode: {cfg.browser.mode} | debug port: {cfg.browser.debug_port} | profile: {cfg.browser.profile_dir}")
    try:
        chrome = platform_utils.find_chrome_binary(cfg.browser.chrome_binary)
        print(f"[OK]   Chrome found: {chrome}")
    except ConfigError as exc:
        print(f"[FAIL] Chrome: {exc}")
        return 1
    if cfg.browser.chromedriver_path is not None:
        exists = cfg.browser.chromedriver_path.is_file()
        ok &= exists
        print(f"[{'OK' if exists else 'FAIL'}]   chromedriver_path: {cfg.browser.chromedriver_path}"
              f"{'' if exists else ' (file not found)'}")
    else:
        from selenium.webdriver.common.selenium_manager import SeleniumManager
        logging.getLogger("selenium.webdriver.common.selenium_manager").setLevel(logging.DEBUG)
        try:
            paths = SeleniumManager().binary_paths(["--browser", "chrome", "--browser-path", str(chrome)])
            print(f"[OK]   Selenium Manager ChromeDriver: {paths.get('driver_path')}")
        except Exception as exc:  # show Selenium Manager's own reason
            ok = False
            print(f"[FAIL] Selenium Manager could not get ChromeDriver: {exc}")
            print("       Fix: allow internet access for Python/selenium-manager (proxy, firewall, antivirus), or download")
            print("       ChromeDriver for your Chrome version and set browser.chromedriver_path in config.json.")
    if cfg.browser.mode == "attach":
        if browser.debug_endpoint_alive(cfg.browser.debug_port):
            print(f"[OK]   Automation Chrome is listening on 127.0.0.1:{cfg.browser.debug_port}")
            try:
                driver = browser.attach_driver(cfg)
                print(f"[OK]   Selenium attached ({len(driver.window_handles)} tab(s))")
                driver.quit()  # ends the session only; Chrome stays open
            except (ConfigError, BrowserStartError) as exc:
                ok = False
                print(f"[FAIL] Attach: {exc}")
        else:
            print("[INFO] Automation Chrome is not running. Start it with: python app.py --login")
    print("All checks passed." if ok else "Some checks failed: see [FAIL] lines above.")
    return 0 if ok else 1


def port_in_use(host: str, port: int) -> bool:
    """True if something already accepts connections on host:port.

    Windows lets a second server bind a busy port, and the old copy keeps answering,
    so the app checks before starting.
    """
    target = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with socket.create_connection((target, port), timeout=1):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Google Vids Automation")
    parser.add_argument("--login", action="store_true", help="open the automation Chrome profile for manual sign-in")
    parser.add_argument("--check-browser", action="store_true", help="diagnose Chrome / ChromeDriver / attach problems")
    parser.add_argument("--fake", action="store_true", help="use the fake controller (no Google access, no quota)")
    parser.add_argument("--scenario", help="fake scenario JSON (implies --fake)")
    parser.add_argument("--host", help="bind address (default from config; 0.0.0.0 = LAN access)")
    parser.add_argument("--port", type=int, help="port (default from config)")
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    setup_logging(cfg)
    if args.check_browser:
        return check_browser(cfg)
    if args.login:
        try:
            login(cfg)
        except (ConfigError, BrowserStartError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
        return 0
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    lan = host not in ("127.0.0.1", "localhost", "::1")
    if port_in_use(host, port):
        print(f"Error: something is already running on port {port}, probably another copy of this app "
              f"(possibly an older version). Close that window or stop it (Windows: taskkill /IM python.exe /F), "
              f"or start this one with --port {port + 1}.", file=sys.stderr)
        return 1
    if lan:
        print("WARNING: LAN mode. Anyone on this network can open the UI (no login). "
              "Chrome, the automation and all files stay on THIS computer.", file=sys.stderr)
    app = create_app(cfg, fake=args.fake or bool(args.scenario), scenario=args.scenario, lan=lan)
    mode = "FAKE controller (no Google access)" if app.extensions["gva"]["fake"] else "LIVE controller"
    print(f"Google Vids Automation {VERSION}: {mode} at http://{'127.0.0.1' if not lan else host}:{port}")
    app.run(host=host, port=port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
