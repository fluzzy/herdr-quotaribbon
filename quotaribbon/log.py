"""Rotating, redacting, never-raising file logger.

``redact`` is a safety net: callers still never log raw provider output, tokens or account ids.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import threading

from . import constants
from .fsutil import PRIVATE_FILE_MODE, ensure_private_dir, open_regular, sha256_hex
from .paths import Paths

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]+){0,2}")
_SECRET_KV_RE = re.compile(
    r"(?i)(\"?\b(?:access_token|refresh_token|id_token|token|secret|password|passwd|api[_-]?key|"
    r"authorization|cookie|session[_-]?key)\b\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)"
)
_HEX_RE = re.compile(r"\b[0-9A-Fa-f]{32,}\b")
# C0 controls, DEL, C1 controls and Unicode line/paragraph separators.
_WHITESPACE_CTRL_RE = re.compile(r"[\t\n\r\v\f\u2028\u2029\x85]+")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

MAX_MESSAGE_CHARS = 2000
_PRE_CAP_CHARS = 64 << 10
_TRAILING_TOKEN_RE = re.compile(r"\S*\Z")
_TRUNCATION_MARK = "…[truncated]"
_RUN_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


_UUID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z")


def _redact_run(m: re.Match[str]) -> str:
    """Mask a long token-like run; UUID path segments (temp/app dirs) stay readable."""
    run = m.group(0)
    in_path = "/" in run

    def tokenish(s: str) -> bool:
        if in_path and _UUID_RE.match(s):
            return False
        return len(s) >= 32 and any(c.isdigit() for c in s) and any(c.isalpha() for c in s)

    if any(tokenish(seg) for seg in run.split("/")):
        return "<redacted>"
    if "/" in run and not run.startswith("/"):
        has_upper = any(c.isupper() for c in run)
        has_lower = any(c.islower() for c in run)
        if has_upper and has_lower and any(c.isdigit() for c in run):
            return "<redacted>"
    return run


def strip_control(text: str) -> str:
    """Flatten to one line and drop control characters, so provider text cannot forge log lines."""
    return _CTRL_RE.sub("", _WHITESPACE_CTRL_RE.sub(" ", text))


def cap_length(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    limit = max(0, limit)
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_MARK):
        return text[:limit]
    return text[: limit - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK


def redact(text: object) -> str:
    """Sanitise ``text`` for logs and diagnostics; never raises.

    Redaction runs before the length cap, so a cut never exposes part of a secret.
    """
    try:
        s = text if isinstance(text, str) else str(text)
        if len(s) > _PRE_CAP_CHARS:
            # Bound regex work; drop the token cut by the bound so no secret fragment survives.
            s = _TRAILING_TOKEN_RE.sub("", s[:_PRE_CAP_CHARS]) + " " + _TRUNCATION_MARK
        s = strip_control(s)
        s = _EMAIL_RE.sub("<email>", s)
        s = _BEARER_RE.sub("Bearer <redacted>", s)
        s = _JWT_RE.sub("<jwt>", s)
        s = _SK_RE.sub("<key>", s)
        s = _SECRET_KV_RE.sub(lambda m: m.group(1) + "<redacted>", s)
        s = _HEX_RE.sub("<hex>", s)
        s = _RUN_RE.sub(_redact_run, s)
        return cap_length(s)
    except Exception:  # noqa: BLE001 - redaction must never raise into callers
        return "<unloggable>"


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            return redact(super().format(record))
        except Exception:  # noqa: BLE001
            return "<unformattable log record>"


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """``RotatingFileHandler`` that creates files 0600 and never reports errors."""

    def _open(self):  # type: ignore[override]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = open_regular(self.baseFilename, flags, PRIVATE_FILE_MODE)
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        except OSError:
            pass
        return os.fdopen(fd, "a", encoding="utf-8", errors="replace")

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        """Swallow all logging errors (disk full, permissions, ...)."""


_LOCK = threading.Lock()


def get_logger(paths: Paths, max_bytes: int | None = None, backups: int | None = None) -> logging.Logger:
    """Logger for ``paths.log_file`` (one handler per file). Never raises; on setup failure
    records are discarded."""
    name = "quotaribbon.file." + sha256_hex(paths.log_file)[:16]
    logger = logging.getLogger(name)
    with _LOCK:
        if getattr(logger, "_qr_configured", False):
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            ensure_private_dir(paths.logs_dir)
            handler: logging.Handler = _PrivateRotatingFileHandler(
                paths.log_file,
                maxBytes=constants.LOG_MAX_BYTES if max_bytes is None else max_bytes,
                backupCount=constants.LOG_BACKUPS if backups is None else backups,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(_RedactingFormatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
        except Exception:  # noqa: BLE001
            handler = logging.NullHandler()
        logger.addHandler(handler)
        logger._qr_configured = True  # type: ignore[attr-defined]
    return logger


__all__ = ["MAX_MESSAGE_CHARS", "redact", "strip_control", "cap_length", "get_logger"]
