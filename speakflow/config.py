from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
}


class Config:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            merged = {**DEFAULTS, **self._data}
            if merged != self._data:
                self._data = merged
                self.save()
        else:
            self._data = dict(DEFAULTS)
            self.save()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

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

    def __repr__(self) -> str:
        return f"Config({self._data!r})"
