"""Run reports: {project}_report.json and .md in the output folder (DEC-20).

Reports are rewritten at the end of every run, but only over a report this app wrote for
the same job; any other file with that name is kept and the report gets a _vN suffix (DL5).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automation.models import LIMITATION_MESSAGES
from automation.state_store import Store

GENERATOR = "google-vids-automation"


def build_report(store: Store, job_id: str) -> dict[str, Any]:
    job = store.get_job(job_id)
    parts = store.get_parts(job_id)
    events = store.all_events(job_id)
    renames = [e["message"] for e in events if e["step"] == "DUPLICATE_RENAMED"]
    counts: dict[str, int] = {}
    for p in parts:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    return {
        "generator": GENERATOR,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "job": {k: job[k] for k in ("job_id", "project_name", "project_slug", "mode", "effective_mode", "overall_status",
                                    "output_dir", "created_at", "updated_at", "last_error", "final_export_status",
                                    "final_output_path", "final_original_filename", "partial_output_path")},
        "limitations": [{"code": c, "message": LIMITATION_MESSAGES.get(c, c)} for c in job["limitations"]],
        "quota_note": "Usage limits are set by Google per account; this tool stops safely and never works around them."
                      if job["overall_status"] == "BLOCKED_QUOTA" else None,
        "status_counts": counts,
        "parts": [{
            "sequence": p["sequence"], "title": p["title"], "status": p["status"],
            "script_sha12": p["script_sha12"], "script_chars": p["script_chars"],
            "images": [Path(i).name for i in p["image_paths"]],
            "output_path": p["output_path"], "original_filename": p["original_filename"], "output_bytes": p["output_bytes"],
            "retry_count": p["retry_count"], "error_code": p["error_code"], "error_message": p["error_message"],
            "note": "included in final export" if p["status"] == "READY" and job["effective_mode"] == "multi_part"
                    and job["final_export_status"] == "DOWNLOADED" else None,
        } for p in parts],
        "duplicate_renames": renames,
    }


def to_markdown(report: dict[str, Any]) -> str:
    job = report["job"]
    lines = [f"# Run report: {job['project_name']}", "",
             f"- Status: **{job['overall_status']}**",
             f"- Mode: {job['mode']} (ran as {job['effective_mode'] or job['mode']})",
             f"- Output folder: `{job['output_dir']}`",
             f"- Combined video: {job['final_export_status']}" + (f" → `{Path(job['final_output_path']).name}`" if job["final_output_path"] else ""),
             f"- Report generated: {report['generated_at']}"]
    if job["partial_output_path"]:
        lines.append(f"- Partial video (usage limit): `{Path(job['partial_output_path']).name}`")
    if job["last_error"]:
        lines.append(f"- Last message: {job['last_error']}")
    if report["quota_note"]:
        lines.append(f"- {report['quota_note']}")
    if report["limitations"]:
        lines += ["", "## Limitations", *[f"- {l['message']}" for l in report["limitations"]]]
    lines += ["", "## Parts", "", "| # | Title | Status | Output | Size | Retries | Error |", "|---|---|---|---|---|---|---|"]
    for p in report["parts"]:
        out = Path(p["output_path"]).name if p["output_path"] else (p["note"] or "—")
        size = f"{p['output_bytes']:,} B" if p["output_bytes"] else "—"
        lines.append(f"| {p['sequence']} | {p['title'] or ''} | {p['status']} | {out} | {size} | {p['retry_count']} | {p['error_message'] or ''} |")
    if report["duplicate_renames"]:
        lines += ["", "## Renamed to avoid overwriting", *[f"- {m}" for m in report["duplicate_renames"]]]
    return "\n".join(lines) + "\n"


def _target(folder: Path, base: str, suffix: str, job_id: str) -> Path:
    n = 1
    while True:
        path = folder / (f"{base}{suffix}" if n == 1 else f"{base}_v{n}{suffix}")
        if not path.exists() or _ours(path, job_id):
            return path
        n += 1


def _ours(path: Path, job_id: str) -> bool:
    json_path = path.with_suffix(".json")
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return data.get("generator") == GENERATOR and data.get("job", {}).get("job_id") == job_id
    except (OSError, ValueError, AttributeError):
        return False


def write_reports(store: Store, job_id: str) -> tuple[Path, Path]:
    report = build_report(store, job_id)
    folder = Path(report["job"]["output_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    json_path = _target(folder, f"{report['job']['project_slug']}_report", ".json", job_id)
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return json_path, md_path
