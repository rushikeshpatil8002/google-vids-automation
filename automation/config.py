"""Load config.json deep-merged over the defaults in config.example.json (03 §3, §10)."""

import json
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from automation.errors import ConfigError

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = PROJECT_ROOT / "config.example.json"

# Keys whose default is null, with the type they take when set.
NULLABLE_TYPES: dict[str, type] = {
    "browser.chrome_binary": str,
    "browser.chromedriver_path": str,
    "limits.max_script_chars": int,
}
# Values that are filesystem paths. Relative ones resolve against the project root.
PATH_SECTIONS = ("paths",)
PATH_KEYS = ("browser.chrome_binary", "browser.chromedriver_path", "browser.profile_dir", "fake.scenario")
BROWSER_MODES = ("attach", "launch")


class Section(Mapping[str, Any]):
    """Read-only view of one config object. Keys are also attributes: cfg.browser.debug_port."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"no config key {name!r}") from None

    def __getitem__(self, key: str) -> Any:
        value = self._data[key]
        if isinstance(value, dict):
            return Section(value)
        if isinstance(value, list):
            return tuple(value)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"


class Config(Section):
    """The validated configuration. `root` is the project root that paths were resolved against."""

    def __init__(self, data: dict[str, Any], root: Path) -> None:
        super().__init__(data)
        self.root = root


def load_config(path: Path | None = None, *, root: Path | None = None) -> Config:
    """Return config.json deep-merged over config.example.json, type-checked, with absolute paths.

    `path` defaults to <root>/config.json. If that file is missing the defaults apply,
    but an explicitly passed `path` must exist. `root` defaults to the project root;
    tests pass a temp folder.
    """
    root = (root or PROJECT_ROOT).resolve()
    defaults = _strip_doc_keys(_read_json(DEFAULTS_FILE))
    explicit = path is not None
    path = path or root / "config.json"
    if path.exists():
        user = _strip_doc_keys(_read_json(path))
    elif explicit:
        raise ConfigError(f"Config file not found: {path}")
    else:
        log.info("No config.json at %s; using the defaults from config.example.json", path)
        user = {}

    data = _merge(defaults, user, prefix="")
    _check_values(data)
    _resolve_paths(data, root)
    return Config(data, root)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc.strerror}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a JSON object")
    return data


def _strip_doc_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys starting with '_' (documentation), at every level."""
    return {
        key: _strip_doc_keys(value) if isinstance(value, dict) else value
        for key, value in data.items()
        if not key.startswith("_")
    }


def _merge(defaults: dict[str, Any], user: dict[str, Any], prefix: str) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in user.items():
        dotted = prefix + key
        if key not in defaults:
            log.warning("Unknown config key ignored: %s", dotted)
            continue
        default = defaults[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"{dotted} must be an object, got {type(value).__name__}")
            merged[key] = _merge(default, value, prefix=dotted + ".")
        else:
            _check_type(dotted, value, default)
            merged[key] = value
    return merged


def _check_type(dotted: str, value: Any, default: Any) -> None:
    if default is None:
        expected = NULLABLE_TYPES[dotted]
        if value is not None and not _is_type(value, expected):
            raise ConfigError(f"{dotted} must be {expected.__name__} or null, got {type(value).__name__}")
        return
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ConfigError(f"{dotted} must be a list, got {type(value).__name__}")
        if default:
            for item in value:
                if not _is_type(item, type(default[0])):
                    raise ConfigError(f"{dotted} items must be {type(default[0]).__name__}, got {item!r}")
        return
    if not _is_type(value, type(default)):
        raise ConfigError(f"{dotted} must be {type(default).__name__}, got {type(value).__name__}")


def _is_type(value: Any, expected: type) -> bool:
    # bool is a subclass of int in Python; in config they never stand in for each other.
    if isinstance(value, bool) or expected is bool:
        return isinstance(value, bool) and expected is bool
    if expected is float:
        return isinstance(value, (int, float))
    return isinstance(value, expected)


def _check_values(data: dict[str, Any]) -> None:
    browser = data["browser"]
    if browser["mode"] not in BROWSER_MODES:
        raise ConfigError(f"browser.mode must be one of {', '.join(BROWSER_MODES)}, got {browser['mode']!r}")
    if browser["headless"]:
        raise ConfigError("browser.headless must be false: live runs use a visible Chrome window (05 §1)")
    if not 1024 <= browser["debug_port"] <= 65535:
        raise ConfigError(f"browser.debug_port must be between 1024 and 65535, got {browser['debug_port']}")
    if len(browser["window_size"]) != 2 or min(browser["window_size"]) <= 0:
        raise ConfigError("browser.window_size must be [width, height] with positive values")
    for key, value in data["timeouts_seconds"].items():
        if value <= 0:
            raise ConfigError(f"timeouts_seconds.{key} must be greater than 0, got {value}")


def _resolve_paths(data: dict[str, Any], root: Path) -> None:
    for section in PATH_SECTIONS:
        for key, value in data[section].items():
            data[section][key] = _absolute(value, root)
    for dotted in PATH_KEYS:
        section, key = dotted.split(".")
        if data[section][key] is not None:
            data[section][key] = _absolute(data[section][key], root)


def _absolute(value: str, root: Path) -> Path:
    # Joining an absolute path onto root keeps the absolute path unchanged.
    return (root / Path(value).expanduser()).resolve()
