"""M1 step 1.6: one scripted single-part run through the live controller (USES 1 GENERATION).

Uses only vids_selectors.json via VidsSeleniumController, attach mode, CDP download to
staging, then DL2 verification. Prints PASS/FAIL per step. The verified file is moved to
output/_feasibility/.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # lets `python tools/feasibility_run.py` import the automation package

from automation import downloads  # noqa: E402
from automation.config import load_config  # noqa: E402
from automation.controller_base import GenerationState  # noqa: E402
from automation.vids_controller import VidsSeleniumController  # noqa: E402

PART = ROOT / "input" / "Sample_Project" / "part_01"
results: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}", flush=True)
    return ok


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    cfg = load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    name = f"Feasibility_{'dry_' if dry else ''}{stamp}"
    staging = cfg.paths.download_staging_dir / "feasibility"
    out_dir = cfg.paths.default_output_root / "_feasibility"
    script = (PART / "script.txt").read_text(encoding="utf-8")
    ctl = VidsSeleniumController(cfg)
    ctl.diag_prefix = "feasibility"
    print("DRY RUN: stops before Generate (0 generations)." if dry
          else "WARNING: this run uses 1 live Google Vids generation.", flush=True)
    try:
        ctl.start(); step("attach to automation Chrome (own tab)", True)
        ctl.ensure_ready(); step("signed in, Vids home ready", True)
        ctl.check_quota(); step("no quota message before submit", True)
        ref = ctl.create_project(name); step("create blank project + rename", True, ref.rsplit("/", 2)[-2][:8] + "...")
        if dry:
            ctl.open_generation_panel(); ctl.attach_images([PART / "reference.png"])
            step("image attached (1 preview chip)", True)
            ctl.enter_prompt(script); step("prompt entered and read back verbatim", True)
            ctl.click("prompt_clear_button"); step("cleared without generating", True)
            return 0
        part_ref = ctl.submit_generation(ref, None, script, [PART / "reference.png"])
        step("upload image + prompt read-back + Generate clicked", True)
        t0, state = time.monotonic(), None
        while time.monotonic() - t0 < cfg.timeouts_seconds.generation:
            state = ctl.poll_generation(part_ref).state
            if state is not GenerationState.GENERATING:
                break
            time.sleep(cfg.timeouts_seconds.generation_poll_interval)
        if not step("generation ready (visible indicator)", state is GenerationState.READY,
                    f"{state.value if state else 'none'} after {time.monotonic() - t0:.0f} s"):
            return 1
        more = ctl.find_all("clip_more_actions")
        if more:
            ctl._safe_click(more[-1]); time.sleep(1)
            items = [e.text.strip() for e in ctl.d.find_elements("css selector", "[role=menuitem]")
                     if e.is_displayed() and e.text.strip() and not e.get_attribute("id").startswith("docs-")]
            items = [i for i in items if len(i) < 40]
            step("clip 'More actions' menu inspected", True, f"items: {items}")
            ctl.d.find_element("tag name", "body").send_keys("")  # Escape
            time.sleep(0.5)
        ctl.place_generated_clip(part_ref)
        step("clip inserted, empty scenes removed", ctl.scene_count() == 1, f"scenes now: {ctl.scene_count()}")
        staging.mkdir(parents=True, exist_ok=True)
        downloads.move_leftovers(staging, cfg.paths.download_staging_dir / "_orphans")
        before = downloads.snapshot(staging)
        ctl.export_project(ref, staging); step("File > Download > MP4 triggered", True)
        t1 = time.monotonic()
        src = downloads.wait_for_download(staging, before, cfg, start_timeout=cfg.timeouts_seconds.export_render)
        step("download completed in staging", True, f"{src.name} after {time.monotonic() - t1:.0f} s")
        check = downloads.verify_video(src, cfg)
        if not step("DL2 verification (ext, size, ftyp)", check.ok, f"{check.reason}, {check.size} bytes"):
            return 1
        duration = downloads.mp4_duration_seconds(src)
        step("export contains only the clip (about 10 s)", duration is not None and duration < 12,
             f"duration {duration:.2f} s" if duration else "unknown")
        final = downloads.safe_move(src, out_dir, name, ".mp4")
        step("moved to output without overwrite", final.exists(), str(final.relative_to(ROOT)))
    except Exception as exc:  # report every failure as a FAIL line with its type, never page text
        step(f"unexpected {type(exc).__name__}", False, str(exc)[:200])
        shot = ctl.capture_diagnostics("feasibility_failure") if ctl.driver else None
        if shot:
            print(f"      screenshot: {shot}")
        return 1
    finally:
        ctl.close()
    failed = [r for r in results if not r[1]]
    print(f"\n{'ALL PASS' if not failed else f'{len(failed)} FAILED'} ({len(results)} steps)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
