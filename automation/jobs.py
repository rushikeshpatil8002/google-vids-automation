"""Job creation and editing: input copies (DEC-14), manifest import (J4), review plan (R1), delete (P3).

Scripts are stored byte-for-byte; images are copied byte-for-byte except WEBP, which is
converted losslessly to PNG because Vids accepts only PNG/JPEG (DEC-27).
"""

import io
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from automation.config import Config
from automation.controller_base import Capabilities
from automation.errors import ValidationError
from automation.models import EDITABLE_JOB_STATUSES, LIMITATION_MESSAGES, JobMode, JobStatus
from automation.state_store import Store
from automation.validators import (
    IMAGE_FORMATS,
    check_image,
    check_output_dir,
    read_script,
    render_name,
    render_names,
    sanitize_filename,
    script_hash12,
    slugify_project,
    validate_job,
)


def resolve_mode(mode: str, caps: Capabilities) -> tuple[str, list[str]]:
    """DEC-02: the workflow actually used for a job mode, plus limitation codes."""
    if mode != JobMode.MULTI_PART.value:
        return mode, []
    if not caps.multi_scene:
        return JobMode.SEPARATE_CLIPS.value, ["MULTI_SCENE_NOT_SUPPORTED"]
    if not caps.combined_export:
        if not caps.per_clip_download:
            return JobMode.SEPARATE_CLIPS.value, ["COMBINED_EXPORT_NOT_SUPPORTED"]
        return mode, ["COMBINED_EXPORT_NOT_SUPPORTED"]
    return mode, [] if caps.per_clip_download else ["NO_PER_CLIP_DOWNLOAD"]


class JobService:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg

    # ----- jobs ----------------------------------------------------------------------------

    def create_job(self, project_name: str, mode: str, output_dir: str | None = None,
                   clip_template: str | None = None, final_template: str | None = None) -> str:
        try:
            JobMode(mode)
        except ValueError:
            raise ValidationError("Mode must be single, multi_part or separate_clips.") from None
        slug = slugify_project(project_name, self.store.slugs())
        out = output_dir or str(self.cfg.paths.default_output_root / slug)
        out_path = Path(out).expanduser()
        out_path = out_path if out_path.is_absolute() else (self.cfg.root / out_path)
        return self.store.create_job(
            project_name=project_name.strip(), project_slug=slug, mode=mode, output_dir=out_path.resolve(),
            clip_template=clip_template or self.cfg.naming.clip_template,
            final_template=final_template or self.cfg.naming.final_template,
            partial_template=self.cfg.naming.partial_template)

    def edit_job(self, job_id: str, **fields: Any) -> None:
        self._editable(job_id)
        allowed = {"mode", "output_dir", "clip_template", "final_template"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "mode" in changes:
            JobMode(changes["mode"])
        if "output_dir" in changes:
            p = Path(changes["output_dir"]).expanduser()
            changes["output_dir"] = str((p if p.is_absolute() else self.cfg.root / p).resolve())
        self.store.update_job(job_id, overall_status=JobStatus.DRAFT, **changes)

    def input_dir(self, job: dict[str, Any]) -> Path:
        return self.cfg.paths.input_dir / "jobs" / job["project_slug"]

    def _editable(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if JobStatus(job["overall_status"]) not in EDITABLE_JOB_STATUSES:
            raise PermissionError("The job can only be edited before it starts.")
        return job

    def _back_to_draft(self, job: dict[str, Any]) -> None:
        if job["overall_status"] != JobStatus.DRAFT.value:
            self.store.set_job_status(job["job_id"], JobStatus.DRAFT)

    # ----- parts ---------------------------------------------------------------------------

    def _store_inputs(self, folder: Path, script: bytes, images: list[tuple[str, bytes]]) -> tuple[Path, list[str], str]:
        text, errors = read_script(script, None)
        if text is None or not text.strip():
            raise ValidationError(errors[0] if errors else "Script is empty.")
        if not images:
            raise ValidationError("Add at least one reference image.")
        folder.mkdir(parents=True, exist_ok=True)
        script_path = folder / "script.txt"
        script_path.write_bytes(script)
        saved: list[str] = []
        for index, (name, data) in enumerate(images, start=1):
            tmp = folder / f".incoming_{uuid.uuid4().hex}"
            tmp.write_bytes(data)
            fmt, img_errors = check_image(tmp, self.cfg)
            if img_errors:
                tmp.unlink(missing_ok=True)
                raise ValidationError(img_errors[0].replace(tmp.name, sanitize_filename(Path(name).name or "image")))
            if fmt == "WEBP":
                target = folder / f"reference_{index}.png"
                with Image.open(tmp) as img:
                    img.save(target, format="PNG", optimize=False)  # lossless (DEC-27)
                tmp.unlink()
            else:
                target = folder / f"reference_{index}{IMAGE_FORMATS[fmt]}"
                tmp.replace(target)
            saved.append(str(target))
        return script_path, saved, text

    def add_part(self, job_id: str, script: bytes, images: list[tuple[str, bytes]],
                 title: str | None = None) -> str:
        job = self._editable(job_id)
        folder = self.input_dir(job) / f"part_{uuid.uuid4().hex[:8]}"
        try:
            script_path, saved, text = self._store_inputs(folder, script, images)
        except ValidationError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        part_id = self.store.add_part(job_id, title=(title or "").strip() or None, script_path=script_path,
                                      script_sha12=script_hash12(text), script_chars=len(text), image_paths=saved)
        self._back_to_draft(job)
        return part_id

    def replace_part_inputs(self, part_id: str, script: bytes | None = None,
                            images: list[tuple[str, bytes]] | None = None, title: str | None = None) -> None:
        part = self.store.get_part(part_id)
        if part is None:
            raise KeyError(part_id)
        job = self._editable(part["job_id"])
        folder = Path(part["script_path"]).parent
        fields: dict[str, Any] = {}
        if script is not None or images is not None:
            new_script = script if script is not None else Path(part["script_path"]).read_bytes()
            new_images = images if images is not None else [(Path(p).name, Path(p).read_bytes()) for p in part["image_paths"]]
            staging = folder.with_name(folder.name + "_new")
            shutil.rmtree(staging, ignore_errors=True)
            try:
                _, _, text = self._store_inputs(staging, new_script, new_images)
            except ValidationError:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            shutil.rmtree(folder, ignore_errors=True)
            staging.rename(folder)
            fields.update(script_path=str(folder / "script.txt"), script_sha12=script_hash12(text),
                          script_chars=len(text),
                          image_paths=[str(folder / Path(p).name) for p in sorted(folder.glob("reference_*"))])
        if title is not None:
            fields["title"] = title.strip() or None
        if fields:
            self.store.update_part(part_id, validation_errors=[], **fields)
        self._back_to_draft(job)

    def remove_part(self, part_id: str) -> None:
        part = self.store.get_part(part_id)
        if part is None:
            raise KeyError(part_id)
        job = self._editable(part["job_id"])
        self.store.remove_part(part_id)
        shutil.rmtree(Path(part["script_path"]).parent, ignore_errors=True)
        self._back_to_draft(job)

    def reorder(self, job_id: str, order: list[str]) -> None:
        job = self._editable(job_id)
        self.store.reorder_parts(job_id, order)
        self._back_to_draft(job)

    # ----- manifest (J4, DEC-14) ---------------------------------------------------------------

    def import_manifest(self, manifest: dict[str, Any]) -> str:
        for key in ("project_name", "mode", "parts"):
            if key not in manifest:
                raise ValidationError(f"Manifest is missing '{key}'.")
        parts = manifest["parts"]
        if not isinstance(parts, list) or not parts:
            raise ValidationError("Manifest 'parts' must be a non-empty list.")
        job_id = self.create_job(manifest["project_name"], manifest["mode"], manifest.get("output_dir"),
                                 manifest.get("clip_template"), manifest.get("final_template"))
        try:
            for entry in sorted(parts, key=lambda p: p.get("part", 0)):
                label = f"Part {entry.get('part', '?')}"
                if "script_text" in entry:
                    script = str(entry["script_text"]).encode("utf-8")
                elif "script_file" in entry:
                    script = self._manifest_path(entry["script_file"], label).read_bytes()
                else:
                    raise ValidationError(f"{label}: needs script_file or script_text.")
                refs = entry.get("reference_images")
                if not isinstance(refs, list) or not refs:
                    raise ValidationError(f"{label}: reference_images must be a non-empty list.")
                images = [(Path(r).name, self._manifest_path(r, label).read_bytes()) for r in refs]
                try:
                    self.add_part(job_id, script, images, entry.get("title"))
                except ValidationError as exc:
                    raise ValidationError(f"{label}: {exc}") from None
        except Exception:
            self.delete_job_data(job_id, delete_outputs=False)
            raise
        return job_id

    def _manifest_path(self, value: str, label: str) -> Path:
        path = Path(value).expanduser()
        path = path if path.is_absolute() else self.cfg.root / path
        if not path.is_file():
            raise ValidationError(f"{label}: file not found: {value}")
        return path

    # ----- validation and plan --------------------------------------------------------------

    def validate(self, job_id: str, caps: Capabilities | None = None) -> dict[str, Any]:
        job = self._editable(job_id)
        parts = self.store.get_parts(job_id)
        result = validate_job(job, parts, self.cfg,
                              max_images=min(self.cfg.limits.max_reference_images_per_part,
                                             caps.max_reference_images) if caps else None,
                              max_chars=min(filter(None, [self.cfg.limits.max_script_chars,
                                                          caps.max_prompt_chars if caps else None]), default=None))
        for part in parts:
            self.store.update_part(part["part_id"], validation_errors=result.part_errors.get(part["part_id"], []))
        n_errors = len(result.job_errors) + sum(len(e) for e in result.part_errors.values())
        if result.ok:
            self.store.set_job_status(job_id, JobStatus.VALIDATED, event=("VALIDATED", "INFO", "Validation passed"))
        else:
            self.store.set_job_status(job_id, JobStatus.DRAFT,
                                      event=("VALIDATION_FAILED", "WARNING", f"Validation found {n_errors} problem(s)"))
        return {"ok": result.ok, "job_errors": result.job_errors, "part_errors": result.part_errors}

    def plan(self, job_id: str, caps: Capabilities) -> dict[str, Any]:
        """R1 review data: mode, parts, images, intended output names, limitation warnings."""
        job = self.store.get_job(job_id)
        parts = self.store.get_parts(job_id)
        effective, limitations = resolve_mode(job["mode"], caps)
        names = render_names(job, parts)
        per_clip = effective != JobMode.MULTI_PART.value or caps.per_clip_download
        final = render_name(job["final_template"], job["project_slug"]) + ".mp4" if effective == JobMode.MULTI_PART.value and caps.combined_export else None
        return {
            "job_id": job_id, "project_name": job["project_name"], "mode": job["mode"], "effective_mode": effective,
            "output_dir": job["output_dir"], "part_count": len(parts),
            "limitations": [{"code": c, "message": LIMITATION_MESSAGES[c]} for c in limitations],
            "final_output": final,
            "parts": [{
                "part_id": p["part_id"], "sequence": p["sequence"], "title": p["title"],
                "script_file": Path(p["script_path"]).name, "script_chars": p["script_chars"],
                "images": [Path(i).name for i in p["image_paths"]],
                "output": names[p["part_id"]] + ".mp4" if per_clip else "included in final export",
            } for p in parts],
        }

    # ----- delete (P3) ------------------------------------------------------------------------

    def delete_job_data(self, job_id: str, delete_outputs: bool = False) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["overall_status"] == JobStatus.RUNNING.value:
            raise PermissionError("Stop the job before deleting its data.")
        parts = self.store.get_parts(job_id)
        shutil.rmtree(self.input_dir(job), ignore_errors=True)
        shutil.rmtree(self.cfg.paths.download_staging_dir / job_id, ignore_errors=True)
        for part in parts:
            for thumb in self.cfg.paths.thumbnails_dir.glob(f"{part['part_id']}_*.png"):
                thumb.unlink(missing_ok=True)
        if delete_outputs:
            files = [p.get("output_path") for p in parts] + [job.get("final_output_path"), job.get("partial_output_path")]
            for f in filter(None, files):
                Path(f).unlink(missing_ok=True)
            for suffix in (".json", ".md"):
                (Path(job["output_dir"]) / f"{job['project_slug']}_report{suffix}").unlink(missing_ok=True)
        self.store.delete_job(job_id)

    def thumbnail(self, part_id: str, index: int) -> Path:
        part = self.store.get_part(part_id)
        if part is None or not 0 <= index < len(part["image_paths"]):
            raise KeyError(part_id)
        thumbs = self.cfg.paths.thumbnails_dir
        thumbs.mkdir(parents=True, exist_ok=True)
        target = thumbs / f"{part_id}_{index}.png"
        if not target.exists():
            with Image.open(part["image_paths"][index]) as img:
                img.thumbnail((320, 320))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
            target.write_bytes(buf.getvalue())
        return target

    def load_manifest_file(self, path: str) -> dict[str, Any]:
        p = Path(path).expanduser()
        p = p if p.is_absolute() else self.cfg.root / p
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Manifest can't be read: {exc}") from exc

    def check_output(self, job_id: str) -> str | None:
        return check_output_dir(self.store.get_job(job_id)["output_dir"], self.cfg)[1]
