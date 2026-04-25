"""Voice-triggered computer actions for SpeakFlow.

Matches spoken commands like "åbn Gmail", "hvad er klokken", "open Spotify"
against known patterns and executes them via macOS system commands.
"""

from __future__ import annotations

import datetime
import logging
import subprocess

logger = logging.getLogger(__name__)

# ── App / URL aliases ──────────────────────────────────────────

_APP_ALIASES: dict[str, tuple[str, str]] = {
    # Web services
    "gmail": ("url", "https://mail.google.com"),
    "google mail": ("url", "https://mail.google.com"),
    "youtube": ("url", "https://youtube.com"),
    "google": ("url", "https://google.com"),
    "facebook": ("url", "https://facebook.com"),
    "instagram": ("url", "https://instagram.com"),
    "twitter": ("url", "https://x.com"),
    "x": ("url", "https://x.com"),
    "linkedin": ("url", "https://linkedin.com"),
    "github": ("url", "https://github.com"),
    "chatgpt": ("url", "https://chat.openai.com"),
    "chat gpt": ("url", "https://chat.openai.com"),
    "claude": ("url", "https://claude.ai"),
    "netflix": ("url", "https://netflix.com"),
    "reddit": ("url", "https://reddit.com"),
    "amazon": ("url", "https://amazon.com"),
    "google docs": ("url", "https://docs.google.com"),
    "google sheets": ("url", "https://sheets.google.com"),
    "google drive": ("url", "https://drive.google.com"),
    # macOS apps — English
    "safari": ("app", "Safari"),
    "chrome": ("app", "Google Chrome"),
    "google chrome": ("app", "Google Chrome"),
    "firefox": ("app", "Firefox"),
    "mail": ("app", "Mail"),
    "weather": ("app", "Weather"),
    "calendar": ("app", "Calendar"),
    "notes": ("app", "Notes"),
    "music": ("app", "Music"),
    "settings": ("app", "System Settings"),
    "system settings": ("app", "System Settings"),
    "system preferences": ("app", "System Settings"),
    "finder": ("app", "Finder"),
    "terminal": ("app", "Terminal"),
    "messages": ("app", "Messages"),
    "imessage": ("app", "Messages"),
    "facetime": ("app", "FaceTime"),
    "slack": ("app", "Slack"),
    "discord": ("app", "Discord"),
    "spotify": ("app", "Spotify"),
    "maps": ("app", "Maps"),
    "photos": ("app", "Photos"),
    "reminders": ("app", "Reminders"),
    "calculator": ("app", "Calculator"),
    "preview": ("app", "Preview"),
    "app store": ("app", "App Store"),
    "xcode": ("app", "Xcode"),
    "cursor": ("app", "Cursor"),
    "whatsapp": ("app", "WhatsApp"),
    "telegram": ("app", "Telegram"),
    "zoom": ("app", "zoom.us"),
    "teams": ("app", "Microsoft Teams"),
    "word": ("app", "Microsoft Word"),
    "excel": ("app", "Microsoft Excel"),
    "powerpoint": ("app", "Microsoft PowerPoint"),
    "notion": ("app", "Notion"),
    "obsidian": ("app", "Obsidian"),
    "figma": ("app", "Figma"),
    "arc": ("app", "Arc"),
    "brave": ("app", "Brave Browser"),
    # macOS apps — Danish
    "vejr": ("app", "Weather"),
    "kalender": ("app", "Calendar"),
    "noter": ("app", "Notes"),
    "musik": ("app", "Music"),
    "indstillinger": ("app", "System Settings"),
    "beskeder": ("app", "Messages"),
    "kort": ("app", "Maps"),
    "fotos": ("app", "Photos"),
    "påmindelser": ("app", "Reminders"),
    "lommeregner": ("app", "Calculator"),
    "ur": ("app", "Clock"),
}

# ── Prefix patterns for "open/close" commands ─────────────────

_OPEN_PREFIXES = (
    "åbn ", "åben ", "open ", "start ", "launch ",
    "luk op for ", "vis ", "show ", "go to ", "gå til ",
)

_CLOSE_PREFIXES = (
    "luk ", "close ", "quit ", "afslut ",
)

# ── Time / date patterns ──────────────────────────────────────

_TIME_PHRASES = (
    "hvad er klokken", "hvad klokken er", "hvornår er det",
    "what time is it", "what's the time", "whats the time",
    "vis klokken", "show the time", "check the time",
    "tjek klokken", "tell me the time",
)

_DATE_PHRASES = (
    "hvad er datoen", "hvad dato er det", "hvilken dato",
    "hvad dag er det", "hvilken dag er det",
    "what's the date", "whats the date", "what date is it",
    "which day is it", "what day is it", "today's date",
)

_WEEKDAYS_DA = [
    "mandag", "tirsdag", "onsdag", "torsdag",
    "fredag", "lørdag", "søndag",
]
_MONTHS_DA = [
    "januar", "februar", "marts", "april", "maj", "juni",
    "juli", "august", "september", "oktober", "november", "december",
]

# ── Weather patterns ──────────────────────────────────────────

_WEATHER_PHRASES = (
    "tjek vejret", "check the weather", "vis vejret",
    "show the weather", "how's the weather", "hows the weather",
    "åbn vejr", "open weather",
)

# ── System commands ───────────────────────────────────────────

_SYSTEM_ACTIONS: dict[str, list[str]] = {
    "empty trash":       ["osascript", "-e", 'tell application "Finder" to empty trash'],
    "tøm papirkurv":     ["osascript", "-e", 'tell application "Finder" to empty trash'],
    "tøm papirkurven":   ["osascript", "-e", 'tell application "Finder" to empty trash'],
    "dark mode":         ["osascript", "-e", 'tell app "System Events" to tell appearance preferences to set dark mode to true'],
    "light mode":        ["osascript", "-e", 'tell app "System Events" to tell appearance preferences to set dark mode to false'],
    "mute":              ["osascript", "-e", "set volume with output muted"],
    "unmute":            ["osascript", "-e", "set volume without output muted"],
    "lyd fra":           ["osascript", "-e", "set volume with output muted"],
    "lyd til":           ["osascript", "-e", "set volume without output muted"],
    "sleep":             ["pmset", "sleepnow"],
    "lock screen":       ["osascript", "-e", 'tell application "System Events" to keystroke "q" using {command down, control down}'],
    "lås skærmen":       ["osascript", "-e", 'tell application "System Events" to keystroke "q" using {command down, control down}'],
    "screenshot":        ["screencapture", "-i", "-c"],
    "skærmbillede":      ["screencapture", "-i", "-c"],
    "do not disturb on": ["shortcuts", "run", "Do Not Disturb On"],
    "do not disturb off": ["shortcuts", "run", "Do Not Disturb Off"],
}


# ── Execution helpers ─────────────────────────────────────────

def _open_app(name: str) -> str:
    try:
        result = subprocess.run(
            ["open", "-a", name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return f"Åbnede {name}"
        return f"Kunne ikke finde: {name}"
    except Exception as exc:
        return f"Fejl: {exc}"


def _close_app(name: str) -> str:
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{name}" to quit'],
            capture_output=True, text=True, timeout=5,
        )
        return f"Lukkede {name}"
    except Exception as exc:
        return f"Fejl: {exc}"


def _open_url(url: str) -> str:
    try:
        subprocess.run(["open", url], capture_output=True, timeout=5)
        domain = url.split("//")[-1].split("/")[0]
        return f"Åbnede {domain}"
    except Exception as exc:
        return f"Fejl: {exc}"


def _resolve_and_open(name: str) -> str:
    key = name.lower().strip()
    alias = _APP_ALIASES.get(key)
    if alias:
        kind, target = alias
        if kind == "url":
            return _open_url(target)
        return _open_app(target)
    return _open_app(name.title())


def _resolve_and_close(name: str) -> str:
    key = name.lower().strip()
    alias = _APP_ALIASES.get(key)
    if alias:
        _, target = alias
        if alias[0] == "app":
            return _close_app(target)
    return _close_app(name.title())


# ── Pattern matchers ──────────────────────────────────────────

def _try_open(t: str) -> str | None:
    for prefix in _OPEN_PREFIXES:
        if t.startswith(prefix):
            target = t[len(prefix):].strip().rstrip(".")
            if target:
                return _resolve_and_open(target)
    return None


def _try_close(t: str) -> str | None:
    for prefix in _CLOSE_PREFIXES:
        if t.startswith(prefix):
            target = t[len(prefix):].strip().rstrip(".")
            if target:
                return _resolve_and_close(target)
    return None


def _try_time(t: str) -> str | None:
    for phrase in _TIME_PHRASES:
        if phrase in t:
            now = datetime.datetime.now()
            return f"Klokken er {now.strftime('%H:%M')}"
    return None


def _try_date(t: str) -> str | None:
    for phrase in _DATE_PHRASES:
        if phrase in t:
            now = datetime.datetime.now()
            wd = _WEEKDAYS_DA[now.weekday()]
            m = _MONTHS_DA[now.month - 1]
            return f"I dag er {wd} den {now.day}. {m} {now.year}"
    return None


def _try_weather(t: str) -> str | None:
    for phrase in _WEATHER_PHRASES:
        if phrase in t:
            return _open_app("Weather")
    return None


def _try_system(t: str) -> str | None:
    cmd = _SYSTEM_ACTIONS.get(t)
    if cmd is None:
        return None
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
        return f"Udført: {t}"
    except Exception as exc:
        return f"Fejl: {exc}"


# ── Public API ────────────────────────────────────────────────

def try_action(text: str) -> str | None:
    """Try to match text against known action patterns.

    Returns the action result string if matched, None otherwise.
    """
    t = text.lower().strip().rstrip(".")
    logger.debug("Action check: %r", t)

    for handler in (
        _try_time, _try_date, _try_weather,
        _try_open, _try_close, _try_system,
    ):
        result = handler(t)
        if result is not None:
            logger.info("Action matched: %s → %s", t, result)
            return result
    return None
