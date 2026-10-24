"""Rotating file + console logging with redaction (03 §11, P4, A12).

Records that carry a `script_text` attribute are dropped; email addresses are masked.
Format: ts level job=<id8> part=<seq> step=<STEP> msg
"""

import logging
import re
from logging.handlers import RotatingFileHandler

from automation.config import Config

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FORMAT = "%(asctime)s %(levelname)s job=%(job)s part=%(part)s step=%(step)s %(message)s"


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "script_text"):
            return False
        for attr, default in (("job", "-"), ("part", "-"), ("step", "-")):
            if not hasattr(record, attr):
                setattr(record, attr, default)
        message = record.getMessage()
        masked = EMAIL_RE.sub("***@***", message)
        if masked != message:
            record.msg, record.args = masked, None
        return True


def setup_logging(cfg: Config) -> logging.Logger:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_gva", False):
            root.removeHandler(handler)
            handler.close()
    cfg.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(FORMAT)
    file_handler = RotatingFileHandler(cfg.paths.logs_dir / "automation.log", maxBytes=cfg.logging.max_bytes,
                                       backupCount=cfg.logging.backup_count, encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler._gva = True  # type: ignore[attr-defined]
        handler.setFormatter(formatter)
        handler.addFilter(RedactionFilter())
        root.addHandler(handler)
    root.setLevel(cfg.logging.level)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
