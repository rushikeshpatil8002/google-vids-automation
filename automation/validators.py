"""Input checks V1-V5, filename sanitizing and template rendering (03 §6). Never alters scripts."""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from automation.config import Config
from automation.errors import ValidationError
from automation.models import JobMode

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,60}$")
INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
IMAGE_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
TEMPLATE_FIELD_RE = re.compile(r"\{(\w+)(?:[:!][^}]*)?\}")
ALLOWED_TOKENS = {"project", "part", "title"}


@dataclass
class ValidationResult:
    job_errors: list[str] = field(default_factory=list)
    part_errors: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.job_errors and not any(self.part_errors.values())


# ----- names -----------------------------------------------------------------------------

def check_project_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Project name is required."
    if not PROJECT_NAME_RE.match(name):
        return "Project name: 1-60 characters, letters, digits, space, '-' or '_' only."
    return None


def slugify_project(name: str, taken: set[str] | None = None) -> str:
    """Spaces become '_'; a slug already used by another job gets _2, _3, ... (DEC-14)."""
    error = check_project_name(name)
    if error:
        raise ValidationError(error)
    base = re.sub(r"_+", "_", name.strip().replace(" ", "_"))
    taken_lower = {t.lower() for t in (taken or set())}
    slug, n = base, 1
    while slug.lower() in taken_lower:
        n += 1
        slug = f"{base}_{n}"
    return slug


def sanitize_filename(base: str) -> str:
    """Windows rules on every OS (03 §6). Raises ValidationError when nothing usable is left."""
    name = INVALID_CHARS_RE.sub("_", base or "").replace("..", "_")
    name = re.sub(r"_+", "_", name).rstrip(". ")
    if name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name
    name = name[:100].rstrip(". ")
    if not name or name.strip("_") == "":
        raise ValidationError(f"Filename is empty after sanitizing: {base!r}")
    return name


def check_template(template: str, needs_part: bool) -> str | None:
    tokens = set(TEMPLATE_FIELD_RE.findall(template or ""))
    if not template:
        return "Template is required."
    unknown = tokens - ALLOWED_TOKENS
    if unknown:
        return f"Template uses unknown token(s): {', '.join(sorted(unknown))}. Allowed: {{project}}, {{part}}, {{title}}."
    if needs_part and "part" not in tokens:
        return "Clip template must contain {part}."
    return None


def render_name(template: str, project_slug: str, part: int | None = None, title: str | None = None) -> str:
    try:
        raw = template.format(project=project_slug, part=part if part is not None else 0, title=title or "")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValidationError(f"Template {template!r} could not be rendered: {exc}") from exc
    return sanitize_filename(raw)


def render_names(job: dict[str, Any], parts: list[dict[str, Any]]) -> dict[str, str]:
    """part_id -> clip base name (no extension). Raises ValidationError on a case-insensitive collision."""
    names: dict[str, str] = {}
    seen: dict[str, int] = {}
    for part in parts:
        title = sanitize_filename(part["title"]) if part.get("title") else ""
        name = render_name(job["clip_template"], job["project_slug"], part["sequence"], title)
        key = name.lower()
        if key in seen:
            raise ValidationError(f"Parts {seen[key]} and {part['sequence']} would both be saved as '{name}'.")
        seen[key] = part["sequence"]
        names[part["part_id"]] = name
    return names


# ----- files -----------------------------------------------------------------------------

def read_script(data: bytes, max_chars: int | None) -> tuple[str | None, list[str]]:
    """V1: strict UTF-8, non-empty after trimming, within the limit. Returns the text unchanged."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["Script is not valid UTF-8 text."]
    if text.startswith("﻿"):
        text = text[1:]
    errors = []
    if not text.strip():
        errors.append("Script is empty.")
    if max_chars is not None and len(text) > max_chars:
        errors.append(f"Script has {len(text)} characters; the limit is {max_chars}. It is never shortened automatically.")
    return text, errors


def script_hash12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def check_image(path: Path, cfg: Config) -> tuple[str | None, list[str]]:
    """V2: exists, decodes with Pillow, real format PNG/JPEG/WEBP, within max_image_mb. Returns the format."""
    path = Path(path)
    if not path.is_file():
        return None, [f"Image not found: {path.name}"]
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > cfg.limits.max_image_mb:
        return None, [f"Image {path.name} is {size_mb:.1f} MB; the limit is {cfg.limits.max_image_mb} MB."]
    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            img.load()
            fmt = img.format
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None, [f"Image {path.name} can't be opened (corrupt or not an image)."]
    if fmt not in IMAGE_FORMATS:
        return None, [f"Image {path.name} is {fmt}; use PNG, JPEG or WEBP."]
    return fmt, []


def check_output_dir(path: str | Path, cfg: Config) -> tuple[Path | None, str | None]:
    """V4: absolute, created if missing, writable, not inside input/, data/ or logs/."""
    try:
        out = Path(path).expanduser()
        out = (out if out.is_absolute() else cfg.root / out).resolve()
    except (OSError, RuntimeError, ValueError):
        return None, f"Output folder is not a valid path: {path}"
    for forbidden in (cfg.paths.input_dir, cfg.paths.data_dir, cfg.paths.logs_dir):
        if out == forbidden or forbidden in out.parents:
            return None, f"Output folder can't be inside {forbidden.name}/."
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / f".write_test_{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return None, f"Output folder is not writable: {out} ({exc.strerror or exc})"
    return out, None


# ----- whole job (V1-V5) --------------------------------------------------------------------

def validate_job(job: dict[str, Any], parts: list[dict[str, Any]], cfg: Config,
                 max_images: int | None = None, max_chars: int | None = None) -> ValidationResult:
    result = ValidationResult()
    limits = cfg.limits
    max_images = max_images or limits.max_reference_images_per_part
    max_chars = max_chars if max_chars is not None else limits.max_script_chars

    if error := check_project_name(job["project_name"]):
        result.job_errors.append(error)
    if error := check_template(job["clip_template"], needs_part=True):
        result.job_errors.append(error)
    for key in ("final_template", "partial_template"):
        if error := check_template(job[key], needs_part=False):
            result.job_errors.append(f"{key.replace('_', ' ').capitalize()}: {error}")
    _, error = check_output_dir(job["output_dir"], cfg)
    if error:
        result.job_errors.append(error)

    mode = JobMode(job["mode"])
    if mode is JobMode.SINGLE and len(parts) != 1:
        result.job_errors.append(f"Single mode needs exactly 1 part (this job has {len(parts)}).")
    elif not parts:
        result.job_errors.append("Add at least one part.")
    elif len(parts) > limits.max_parts_per_job:
        result.job_errors.append(f"Too many parts ({len(parts)}); the limit is {limits.max_parts_per_job}.")
    sequences = sorted(p["sequence"] for p in parts)
    if sequences != list(range(1, len(parts) + 1)):
        result.job_errors.append("Part numbers must run 1..n without gaps.")

    for part in parts:
        errors: list[str] = []
        script_path = Path(part["script_path"])
        if not script_path.is_file():
            errors.append("Script file is missing.")
        else:
            _, script_errors = read_script(script_path.read_bytes(), max_chars)
            errors += script_errors
        images = part["image_paths"]
        if not images:
            errors.append("Add at least one reference image.")
        elif len(images) > max_images:
            errors.append(f"{len(images)} images; Google Vids accepts at most {max_images} per part.")
        for image in images:
            errors += check_image(Path(image), cfg)[1]
        result.part_errors[part["part_id"]] = errors

    if not result.job_errors and parts:
        try:
            render_names(job, parts)
            render_name(job["final_template"], job["project_slug"])
            render_name(job["partial_template"], job["project_slug"])
        except ValidationError as exc:
            result.job_errors.append(str(exc))
    return result
