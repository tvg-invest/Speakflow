"""Transcription history — persists recent dictations to disk."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HISTORY_FILE = Path.home() / ".speakflow" / "history.json"
MAX_ENTRIES = 30
_lock = threading.Lock()


def load() -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read history file; starting fresh.")
        return []


def add(text: str, app_name: str = "", language: str = "") -> dict[str, Any]:
    with _lock:
        entries = load()
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "text": text,
            "app": app_name,
            "language": language,
        }
        entries.insert(0, entry)
        entries = entries[:MAX_ENTRIES]
        _save(entries)
    return entry


def _save(entries: list[dict[str, Any]]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
