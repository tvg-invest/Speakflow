"""Speech-to-text transcription module using the OpenAI Whisper API."""

from __future__ import annotations

import io
import logging

import openai

logger = logging.getLogger(__name__)


def _rate_limit_message(exc: openai.RateLimitError) -> str:
    msg = str(exc).lower()
    if "quota" in msg or "billing" in msg or "exceeded" in msg:
        return "OpenAI credits used up. Add credits at platform.openai.com/account/billing."
    return "Rate limit exceeded. Please wait a moment and try again."


def _handle_api_error(exc: Exception, label: str = "API call") -> None:
    if isinstance(exc, openai.AuthenticationError):
        raise RuntimeError("Authentication failed. Check your API key.")
    if isinstance(exc, openai.RateLimitError):
        raise RuntimeError(_rate_limit_message(exc))
    if isinstance(exc, openai.APIConnectionError):
        raise RuntimeError("Cannot reach the OpenAI API. Check your connection.")
    if isinstance(exc, openai.APIError):
        raise RuntimeError(f"{label} failed: {exc}") from exc
    raise


class Transcriber:
    """Handles speech-to-text transcription via OpenAI Whisper and optional
    GPT-based text cleanup."""

    def __init__(
        self,
        api_key: str,
        model: str = "whisper-1",
        language: str = "da",
        auto_detect: bool = True,
        cleanup_model: str = "gpt-4o-mini",
        editing_strength: str = "medium",
        personal_dictionary: list | None = None,
    ) -> None:
        self.client = openai.OpenAI(api_key=api_key, max_retries=5)
        self.model = model
        self.language = language
        self.auto_detect = auto_detect
        self.cleanup_model = cleanup_model
        self.editing_strength = editing_strength
        self.personal_dictionary = personal_dictionary or []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _chat(self, label: str, **kwargs) -> str:
        """Run a chat completion and return the text, with unified error handling."""
        try:
            response = self.client.chat.completions.create(**kwargs)
            if not response.choices:
                return ""
            content = response.choices[0].message.content
            return content.strip() if content else ""
        except (openai.AuthenticationError, openai.RateLimitError,
                openai.APIConnectionError, openai.APIError) as exc:
            _handle_api_error(exc, label)
        return ""

    def _dictionary_hint(self) -> str:
        if not self.personal_dictionary:
            return ""
        words = ", ".join(self.personal_dictionary)
        return (
            f"\n\nPERSONAL DICTIONARY — preserve these words/names exactly "
            f"as spelled: {words}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def transcribe(self, audio_data: bytes, app_context: str = "",
                   before_text: str = "", after_text: str = "",
                   skip_cleanup: bool = False) -> str:
        """Transcribe WAV audio bytes into text.

        Args:
            audio_data: Raw WAV audio content.
            app_context: Name of the frontmost app (for context-aware cleanup).
            before_text: Text before cursor in the active text field.
            after_text: Text after cursor in the active text field.
            skip_cleanup: If True, return raw transcription without AI cleanup.

        Returns:
            The transcribed (and optionally cleaned-up) text.

        Raises:
            RuntimeError: If the OpenAI API call fails.
        """
        buf = io.BytesIO(audio_data)
        buf.name = "recording.wav"

        kwargs: dict = {
            "model": self.model,
            "file": buf,
        }
        if not self.auto_detect:
            kwargs["language"] = self.language
        if self.personal_dictionary:
            kwargs["prompt"] = ", ".join(self.personal_dictionary)

        try:
            logger.debug(
                "Sending audio to Whisper API (model=%s, auto_detect=%s)",
                self.model,
                self.auto_detect,
            )
            response = self.client.audio.transcriptions.create(**kwargs, timeout=30)
            raw_text: str = response.text
            logger.debug("Raw transcription: %s", raw_text)
        except (openai.AuthenticationError, openai.RateLimitError,
                openai.APIConnectionError, openai.APIError) as exc:
            _handle_api_error(exc, "Transcription")

        # Whisper often prepends dashes when it hears a brief pause or noise
        raw_text = raw_text.strip().lstrip("-–—").strip()

        if not skip_cleanup and self.editing_strength != "off" and raw_text:
            try:
                return self.cleanup_text(raw_text, self.language, app_context,
                                         before_text, after_text)
            except Exception:
                logger.warning("Text cleanup failed, returning raw transcription",
                               exc_info=True)
                return raw_text

        return raw_text

    def cleanup_text(self, raw_text: str, language: str, app_context: str = "",
                     before_text: str = "", after_text: str = "") -> str:
        """Use a GPT model to clean up a raw speech transcription."""
        # Context-aware formatting rules
        context_rules = ""
        if app_context:
            al = app_context.lower()
            if any(x in al for x in ("mail", "outlook", "gmail", "spark")):
                context_rules = (
                    "\n\nFORMATTING RULES (user is writing an email):\n"
                    "- Use complete sentences with proper punctuation.\n"
                    "- Use a professional, clear tone.\n"
                    "- Do NOT add greetings or signatures unless the user dictated them.\n"
                    "- Avoid abbreviations — write words out in full.\n"
                    "- Capitalize properly (names, sentence starts)."
                )
            elif any(x in al for x in ("messages", "imessage", "messenger", "telegram",
                                        "whatsapp", "slack", "discord", "teams")):
                context_rules = (
                    "\n\nFORMATTING RULES (user is writing a chat message):\n"
                    "- Keep it casual and natural.\n"
                    "- Use sentence case (capitalize first word only).\n"
                    "- Abbreviations are fine if the user used them.\n"
                    "- Do NOT add a period at the end if it is a single sentence.\n"
                    "- Use periods to separate multiple sentences."
                )
            elif any(x in al for x in ("code", "xcode", "terminal", "intellij",
                                        "pycharm", "cursor", "sublime", "vim", "neovim")):
                context_rules = (
                    "\n\nFORMATTING RULES (user is in a code editor):\n"
                    "- Preserve technical terms, function names, and variable names exactly.\n"
                    "- If it sounds like a code comment, format as: // <comment text>\n"
                    "- If it sounds like a commit message, use imperative mood and keep it short.\n"
                    "- Do NOT alter code-like tokens (e.g. camelCase, snake_case)."
                )
            elif any(x in al for x in ("notes", "notion", "obsidian", "bear", "craft")):
                context_rules = (
                    "\n\nFORMATTING RULES (user is writing notes):\n"
                    "- Use complete sentences with proper punctuation.\n"
                    "- If the user dictates a list, format items with '- ' prefix.\n"
                    "- Keep it concise and well-structured."
                )
            elif any(x in al for x in ("safari", "chrome", "firefox", "arc", "brave", "edge")):
                context_rules = (
                    "\n\nFORMATTING RULES (user is in a browser):\n"
                    "- Use a neutral, clear tone.\n"
                    "- Use complete sentences with proper punctuation."
                )

        # Cursor-aware context for spelling/style consistency
        surrounding_hint = ""
        if before_text or after_text:
            parts = []
            if before_text:
                parts.append(f"TEXT BEFORE CURSOR (already written):\n---\n{before_text}\n---")
            if after_text:
                parts.append(f"TEXT AFTER CURSOR (comes next):\n---\n{after_text}\n---")
            surrounding_hint = (
                "\n\nCONTEXT — the text below is already in the text field around the "
                "user's cursor. Use it ONLY as reference for spelling of names, terms, "
                "and matching style. Do NOT repeat or include any of this text in your "
                "output:\n" + "\n".join(parts)
            )

        if self.editing_strength == "light":
            system_prompt = (
                "You are a text cleanup assistant. Make MINIMAL changes to the "
                "following speech transcription:\n"
                "- Fix punctuation and capitalization.\n"
                "- Remove ALL filler words: um, uh, like, you know, I mean, "
                "basically, so, right, well, øh, altså, ligesom, liksom, "
                "på en måde, jo, ikke også.\n"
                "- Fix self-corrections/backtracking: when the speaker corrects "
                "themselves (e.g. 'Tuesday — no wait, Wednesday' or 'tirsdag, "
                "nej onsdag'), keep ONLY the corrected version.\n"
                "Do NOT reword, rephrase, or restructure. Keep the exact same "
                "language. Output ONLY the cleaned text."
                + context_rules + surrounding_hint + self._dictionary_hint()
            )
        else:
            system_prompt = (
                "You are a text cleanup assistant. Clean up the following speech "
                "transcription:\n"
                "- Remove ALL filler words: um, uh, like, you know, I mean, "
                "basically, so, right, well, actually, øh, altså, ligesom, "
                "liksom, på en måde, jo, ikke også, vel.\n"
                "- Fix self-corrections/backtracking: when the speaker corrects "
                "themselves (e.g. 'Tuesday — no, Wednesday' or 'tirsdag, nej "
                "onsdag'), keep ONLY the corrected version.\n"
                "- Fix punctuation, capitalization, and obvious speech-to-text "
                "errors.\n"
                "Preserve the original meaning and language. Keep the same "
                "language as the input. Output ONLY the cleaned text, nothing else."
                + context_rules + surrounding_hint + self._dictionary_hint()
            )

        logger.debug("Cleaning up transcription with %s (language=%s)",
                      self.cleanup_model, language)
        result = self._chat(
            "Text cleanup",
            model=self.cleanup_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw_text},
            ],
            timeout=30,
        )
        if not result:
            logger.warning("Cleanup returned empty, using raw text")
            return raw_text
        logger.debug("Cleaned transcription: %s", result)
        return result

    def context_query(
        self,
        selected_text: str,
        voice_instruction: str,
        model: str = "gpt-4o",
        app_context: str = "",
        before_text: str = "",
        after_text: str = "",
    ) -> str:
        """Use GPT to respond based on selected text and a voice instruction."""
        context_hint = ""
        if app_context:
            context_hint = f"\nThe user is currently in: {app_context}"

        surrounding_hint = ""
        if before_text or after_text:
            parts = []
            if before_text:
                parts.append(f"Text before cursor:\n---\n{before_text}\n---")
            if after_text:
                parts.append(f"Text after cursor:\n---\n{after_text}\n---")
            surrounding_hint = (
                "\n\nSurrounding text in the text field (for context only, "
                "do NOT repeat it):\n" + "\n".join(parts)
            )

        system_prompt = (
            "You are a helpful assistant. The user has selected some text on "
            "their screen and is giving you a voice instruction about it. "
            "Follow the instruction precisely. If asked to draft a reply, "
            "write ONLY the reply — no explanations, no labels, no quotes "
            "around it. Match the language of the user's voice instruction."
            + context_hint + surrounding_hint + self._dictionary_hint()
        )

        user_msg = (
            f"Selected text:\n---\n{selected_text}\n---\n\n"
            f"Voice instruction: {voice_instruction}"
        )

        logger.debug("Context query with model=%s", model)
        return self._chat(
            "Context query",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            timeout=30,
        )

    _ASK_PREFIXES = (
        "what is", "what's", "what are", "what does", "what do",
        "what time", "what kind",
        "how do", "how does", "how can", "how to", "how is",
        "how many", "how much", "how long", "how old",
        "why is", "why does", "why do", "why are",
        "when is", "when does", "when did", "when was",
        "where is", "where does", "where can", "where do",
        "who is", "who was", "who are",
        "can you explain", "explain", "tell me", "define",
        "is it", "is there", "are there",
        "hvad er", "hvad betyder", "hvad hedder", "hvad gør",
        "hvad tid", "hvad slags",
        "hvordan", "hvorfor", "hvornår",
        "hvor er", "hvor kan", "hvor mange", "hvor meget",
        "hvem er", "hvem var",
        "kan du forklare", "forklar",
        "fortæl mig", "fortæl om",
        "er det", "er der", "findes der",
    )
    _VISION_KEYWORDS = (
        "on my screen", "my screen", "what do you see", "what is this",
        "this error", "this page", "look at",
        "screen",
        "på min skærm", "min skærm", "hvad ser du", "hvad er det her",
        "denne fejl", "denne side", "kig på",
        "skærm",
    )
    _VIBECODE_PREFIXES = (
        "create a", "build a", "make a", "write a function",
        "write a script", "write a program", "write code",
        "generate a", "implement", "code a", "develop",
        "lav en", "byg en", "skriv en funktion", "skriv et script",
        "skriv et program", "skriv kode", "generer en", "implementer",
    )
    _DICTATION_WORDS = frozenset(("said", "asked", "sagde", "spurgte"))
    _SUBORDINATORS = frozenset((
        "that", "because", "fordi", "since", "når",
        "and", "og", "end", "then", "så",
    ))

    def classify_intent(self, text: str, app_context: str = "",
                        language: str = "") -> str:
        """Classify voice input intent using local heuristics.

        Uses word count, word-boundary matching, and structural checks
        to distinguish dictation from questions, vision requests, and
        coding instructions.

        Returns one of: ``"dictation"``, ``"ask"``, ``"vision"``, ``"vibecode"``.
        Defaults to ``"dictation"`` when uncertain.
        """
        t = text.lower().strip()
        words = t.split()
        wc = len(words)

        clean = [w.strip('.,!?;:"\'-()[]') for w in words]
        padded = f" {' '.join(clean)} "

        for prefix in self._VIBECODE_PREFIXES:
            if t.startswith(prefix):
                logger.info("Auto classified → vibecode")
                return "vibecode"

        if wc > 20:
            logger.info("Auto classified → dictation (long text, %d words)", wc)
            return "dictation"

        for kw in self._VISION_KEYWORDS:
            if f" {kw} " in padded:
                logger.info("Auto classified → vision ('%s')", kw)
                return "vision"

        if wc > 15:
            logger.info("Auto classified → dictation (%d words)", wc)
            return "dictation"

        for prefix in self._ASK_PREFIXES:
            if not t.startswith(prefix):
                continue
            rest = t[len(prefix):].strip()
            if rest.startswith(("ikke ", "not ", "don't ", "aldrig ")):
                break
            if prefix in ("explain", "forklar") and rest.startswith("to "):
                break
            if self._DICTATION_WORDS.intersection(clean):
                break
            if prefix == "how to" and " is " in rest:
                break
            if prefix in ("er det", "is it") and any(
                w in clean for w in ("muligt", "possible", "okay", "ok")):
                break
            if any(w in clean for w in ("nogen", "anyone", "somebody")):
                break
            if wc > 8 and self._SUBORDINATORS.intersection(clean):
                break
            logger.info("Auto classified → ask (prefix '%s')", prefix)
            return "ask"

        logger.info("Auto classified → dictation")
        return "dictation"

    def ask_question(self, question: str, model: str = "gpt-4o",
                     app_context: str = "") -> str:
        """Send a voice-transcribed question to GPT and return the answer."""
        context_hint = ""
        if app_context:
            context_hint = f"\nThe user is currently in: {app_context}"

        system_prompt = (
            "You are a helpful AI assistant. The user asked a question via voice "
            "dictation. Answer concisely and directly. Match the language of the "
            "user's question. Output ONLY your answer, no preamble."
            + context_hint + self._dictionary_hint()
        )

        logger.debug("AI Ask with model=%s", model)
        return self._chat(
            "AI question",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            timeout=30,
        )

    def vision_query(self, screenshot_b64: str, voice_instruction: str,
                     model: str = "gpt-4o", app_context: str = "") -> str:
        """Analyze a screenshot with a voice instruction using GPT-4o vision."""
        context_hint = ""
        if app_context:
            context_hint = f"\nThe user is currently in: {app_context}"

        system_prompt = (
            "You are a helpful AI assistant with vision. The user has shared a "
            "screenshot of their screen and is giving you a voice instruction. "
            "Analyze what you see on screen and respond to their request. Be "
            "concise and helpful. Match the language of the user's instruction. "
            "Output ONLY your response."
            + context_hint + self._dictionary_hint()
        )

        logger.debug("Vision query with model=%s", model)
        return self._chat(
            "Vision query",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": voice_instruction},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{screenshot_b64}",
                        "detail": "auto",
                    }},
                ]},
            ],
            timeout=60,
        )

    def vibecode_prompt(self, description: str, model: str = "gpt-4o") -> str:
        """Convert a voice description into an optimized coding prompt."""
        system_prompt = (
            "You are an expert prompt engineer for AI coding assistants "
            "(Claude Code, Cursor, GitHub Copilot, Lovable, etc.). The user will "
            "describe what they want to build or change via voice. Convert their "
            "description into a clear, technically precise prompt optimized for "
            "an AI coding agent.\n\n"
            "The prompt should include:\n"
            "- Clear objective\n"
            "- Technical requirements and constraints\n"
            "- Expected behavior\n"
            "- Edge cases to handle (if relevant)\n\n"
            "Output ONLY the optimized prompt. No explanations, no labels, no "
            "quotes around it. Match the language of the user's description."
        )

        logger.debug("VibeCode prompt generation with model=%s", model)
        return self._chat(
            "VibeCode generation",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": description},
            ],
            timeout=30,
        )

    def custom_mode_query(self, transcribed_text: str, system_prompt: str,
                          model: str = "gpt-4o") -> str:
        """Apply a custom mode's prompt template to transcribed text."""
        logger.debug("Custom mode query with model=%s", model)
        return self._chat(
            "Custom mode",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcribed_text},
            ],
            timeout=30,
        )

    def set_language(self, language: str) -> None:
        """Change the target transcription language."""
        self.language = language
        logger.info("Transcription language set to '%s'.", language)
