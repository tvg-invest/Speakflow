from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".speakflow"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    "openai_api_key": "",
    "hotkey": "ctrl+shift+space",
    "language": "auto",
    "auto_language_detect": True,
    "model": "whisper-1",
    "max_recording_seconds": 7200,
    "silence_timeout": 2.0,
    "sound_feedback": True,
    "text_insertion_method": "clipboard",
    "ai_cleanup": True,
    "ai_cleanup_model": "gpt-4o-mini",
    "auto_start": False,
    "context_cleanup": True,
    "sound_volume": 0.3,
    "context_hotkey": "alt",
    "context_model": "gpt-4o",
    "microphone": None,  # None = system default, or device index
    "first_run": True,
    "editing_strength": "medium",  # "off", "light", "medium"
    "personal_dictionary": [],     # custom words/names for transcription
}


class Config:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
            # Detect if this is an old config without editing_strength
            had_editing_strength = "editing_strength" in self._data
            merged = {**DEFAULTS, **self._data}
            if merged != self._data:
                self._data = merged
                self.save()
            # Migrate old ai_cleanup=false → editing_strength="off"
            if not had_editing_strength and not self._data.get("ai_cleanup", True):
                self._data["editing_strength"] = "off"
                self.save()
        else:
            self._data = dict(DEFAULTS)
            self.save()

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except OSError:
            logger.warning("Could not save config to %s", CONFIG_FILE, exc_info=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    @property
    def openai_api_key(self) -> str:
        return self._data["openai_api_key"]

    @openai_api_key.setter
    def openai_api_key(self, value: str) -> None:
        self.set("openai_api_key", value)

    @property
    def hotkey(self) -> str:
        return self._data["hotkey"]

    @hotkey.setter
    def hotkey(self, value: str) -> None:
        self.set("hotkey", value)

    @property
    def language(self) -> str:
        return self._data["language"]

    @language.setter
    def language(self, value: str) -> None:
        self.set("language", value)

    @property
    def auto_language_detect(self) -> bool:
        return self._data["auto_language_detect"]

    @auto_language_detect.setter
    def auto_language_detect(self, value: bool) -> None:
        self.set("auto_language_detect", value)

    @property
    def model(self) -> str:
        return self._data["model"]

    @model.setter
    def model(self, value: str) -> None:
        self.set("model", value)

    @property
    def max_recording_seconds(self) -> int:
        return self._data["max_recording_seconds"]

    @max_recording_seconds.setter
    def max_recording_seconds(self, value: int) -> None:
        self.set("max_recording_seconds", value)

    @property
    def silence_timeout(self) -> float:
        return self._data["silence_timeout"]

    @silence_timeout.setter
    def silence_timeout(self, value: float) -> None:
        self.set("silence_timeout", value)

    @property
    def sound_feedback(self) -> bool:
        return self._data["sound_feedback"]

    @sound_feedback.setter
    def sound_feedback(self, value: bool) -> None:
        self.set("sound_feedback", value)

    @property
    def text_insertion_method(self) -> str:
        return self._data["text_insertion_method"]

    @text_insertion_method.setter
    def text_insertion_method(self, value: str) -> None:
        if value not in ("clipboard", "keyboard"):
            raise ValueError(f"Invalid text_insertion_method: {value!r}. Must be 'clipboard' or 'keyboard'.")
        self.set("text_insertion_method", value)

    @property
    def ai_cleanup(self) -> bool:
        return self._data["ai_cleanup"]

    @ai_cleanup.setter
    def ai_cleanup(self, value: bool) -> None:
        self.set("ai_cleanup", value)

    @property
    def ai_cleanup_model(self) -> str:
        return self._data["ai_cleanup_model"]

    @ai_cleanup_model.setter
    def ai_cleanup_model(self, value: str) -> None:
        self.set("ai_cleanup_model", value)

    @property
    def auto_start(self) -> bool:
        return self._data["auto_start"]

    @auto_start.setter
    def auto_start(self, value: bool) -> None:
        self.set("auto_start", value)

    @property
    def context_cleanup(self) -> bool:
        return self._data["context_cleanup"]

    @context_cleanup.setter
    def context_cleanup(self, value: bool) -> None:
        self.set("context_cleanup", value)

    @property
    def sound_volume(self) -> float:
        return self._data["sound_volume"]

    @sound_volume.setter
    def sound_volume(self, value: float) -> None:
        self.set("sound_volume", max(0.0, min(1.0, value)))

    @property
    def context_hotkey(self) -> str:
        return self._data["context_hotkey"]

    @context_hotkey.setter
    def context_hotkey(self, value: str) -> None:
        self.set("context_hotkey", value)

    @property
    def context_model(self) -> str:
        return self._data["context_model"]

    @context_model.setter
    def context_model(self, value: str) -> None:
        self.set("context_model", value)

    @property
    def microphone(self):
        return self._data.get("microphone")

    @microphone.setter
    def microphone(self, value) -> None:
        self.set("microphone", value)

    @property
    def editing_strength(self) -> str:
        return self._data.get("editing_strength", "medium")

    @editing_strength.setter
    def editing_strength(self, value: str) -> None:
        self.set("editing_strength", value)

    @property
    def personal_dictionary(self) -> list:
        return self._data.get("personal_dictionary", [])

    @personal_dictionary.setter
    def personal_dictionary(self, value: list) -> None:
        self.set("personal_dictionary", value)

    def __repr__(self) -> str:
        safe = {k: ("***" if k == "openai_api_key" else v) for k, v in self._data.items()}
        return f"Config({safe!r})"
