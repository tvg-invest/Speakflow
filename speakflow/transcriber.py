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
        except openai.AuthenticationError:
            logger.error("Invalid OpenAI API key.")
            raise RuntimeError(
                "Authentication failed. Please check your OpenAI API key."
            )
        except openai.RateLimitError as exc:
            logger.error("OpenAI rate limit exceeded.")
            raise RuntimeError(_rate_limit_message(exc))
        except openai.APIConnectionError:
            logger.error("Could not connect to the OpenAI API.")
            raise RuntimeError(
                "Unable to reach the OpenAI API. Check your internet connection."
            )
        except openai.APIError as exc:
            logger.error("OpenAI API error during transcription: %s", exc)
            raise RuntimeError(f"Transcription failed: {exc}") from exc

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
        """Use a GPT model to clean up a raw speech transcription.

        Args:
            raw_text:  The unprocessed transcription text.
            language:  ISO-639-1 language code.
            app_context: Frontmost app name for tone adaptation.

        Returns:
            The cleaned-up text.
        """
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

        # Personal dictionary hint
        dictionary_hint = ""
        if self.personal_dictionary:
            words = ", ".join(self.personal_dictionary)
            dictionary_hint = (
                f"\n\nPERSONAL DICTIONARY — preserve these words/names exactly "
                f"as spelled: {words}"
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
                + context_rules + surrounding_hint + dictionary_hint
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
                + context_rules + surrounding_hint + dictionary_hint
            )

        try:
            logger.debug(
                "Cleaning up transcription with %s (language=%s)",
                self.cleanup_model,
                language,
            )
            response = self.client.chat.completions.create(
                model=self.cleanup_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": raw_text},
                ],
                timeout=30,
            )
            if not response.choices:
                logger.warning("Cleanup returned empty choices, using raw text")
                return raw_text
            content = response.choices[0].message.content
            cleaned: str = content.strip() if content else raw_text
            logger.debug("Cleaned transcription: %s", cleaned)
            return cleaned
        except openai.AuthenticationError:
            logger.error("Invalid OpenAI API key during cleanup.")
            raise RuntimeError(
                "Authentication failed. Please check your OpenAI API key."
            )
        except openai.RateLimitError as exc:
            logger.error("OpenAI rate limit exceeded during cleanup.")
            raise RuntimeError(_rate_limit_message(exc))
        except openai.APIConnectionError:
            logger.error("Could not connect to the OpenAI API during cleanup.")
            raise RuntimeError(
                "Unable to reach the OpenAI API. Check your internet connection."
            )
        except openai.APIError as exc:
            logger.error("OpenAI API error during text cleanup: %s", exc)
            raise RuntimeError(f"Text cleanup failed: {exc}") from exc

    def context_query(
        self,
        selected_text: str,
        voice_instruction: str,
        model: str = "gpt-4o",
        app_context: str = "",
        before_text: str = "",
        after_text: str = "",
    ) -> str:
        """Use GPT to respond based on selected text and a voice instruction.

        Args:
            selected_text: Text the user highlighted on screen.
            voice_instruction: Transcribed voice command from the user.
            model: GPT model to use.
            app_context: Frontmost app name for tone adaptation.
            before_text: Text before cursor in the active text field.
            after_text: Text after cursor in the active text field.

        Returns:
            The generated response text.
        """
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

        dictionary_hint = ""
        if self.personal_dictionary:
            words = ", ".join(self.personal_dictionary)
            dictionary_hint = (
                f"\n\nPERSONAL DICTIONARY — preserve these words/names exactly "
                f"as spelled: {words}"
            )

        system_prompt = (
            "You are a helpful assistant. The user has selected some text on "
            "their screen and is giving you a voice instruction about it. "
            "Follow the instruction precisely. If asked to draft a reply, "
            "write ONLY the reply — no explanations, no labels, no quotes "
            "around it. Match the language of the user's voice instruction."
            + context_hint + surrounding_hint + dictionary_hint
        )

        user_msg = (
            f"Selected text:\n---\n{selected_text}\n---\n\n"
            f"Voice instruction: {voice_instruction}"
        )

        try:
            logger.debug("Context query with model=%s", model)
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                timeout=30,
            )
            if not response.choices:
                logger.warning("Context query returned empty choices")
                return ""
            content = response.choices[0].message.content
            result: str = content.strip() if content else ""
            logger.debug("Context response: %s", result[:200])
            return result
        except openai.AuthenticationError:
            raise RuntimeError("Authentication failed. Check your API key.")
        except openai.RateLimitError as exc:
            raise RuntimeError(_rate_limit_message(exc))
        except openai.APIConnectionError:
            raise RuntimeError("Cannot reach the OpenAI API. Check your connection.")
        except openai.APIError as exc:
            raise RuntimeError(f"Context query failed: {exc}") from exc

    def classify_intent(self, text: str, app_context: str = "",
                        language: str = "") -> str:
        """Classify voice input intent for auto-mode routing.

        Returns one of: ``"dictation"``, ``"ask"``, ``"vision"``, ``"vibecode"``.
        Falls back to ``"dictation"`` on any error.
        """
        app_hint = ""
        if app_context:
            app_hint = f"\nThe user is currently in: {app_context}"
        if language and language != "auto":
            lang_names = {"da": "Danish", "en": "English", "de": "German",
                          "fr": "French", "es": "Spanish", "sv": "Swedish",
                          "nb": "Norwegian", "nl": "Dutch"}
            lang_name = lang_names.get(language, language)
            app_hint += f"\nThe user's language is: {lang_name}"

        system_prompt = (
            "You are classifying a voice transcription to decide how to handle it.\n"
            "The user is using a voice-to-text app. MOST voice input is dictation — "
            "text the user wants typed into their current app.\n\n"
            "CRITICAL RULE: Only classify as something other than \"dictation\" if "
            "the user is clearly talking TO an AI assistant, not composing text to "
            "be typed. Questions that are part of a message (e.g. \"Can you send me "
            "the report?\", \"Hvornår er mødet?\") are DICTATION — the user is "
            "writing a message, not asking the AI.\n\n"
            "Signals that it IS dictation (type as-is):\n"
            "- Conversational text, messages, emails, notes, comments\n"
            "- Sentences addressed to another person (\"Hi John\", \"Hej, kan du...\")\n"
            "- Any text the user would normally type themselves\n"
            "- Short replies, acknowledgements, or casual speech\n\n"
            "Signals that it is NOT dictation:\n"
            "- Explicitly asks the AI for help: \"what is\", \"explain\", \"how do I\", "
            "\"fortæl mig om\", \"hvad betyder\"\n"
            "- References the screen: \"what's on my screen\", \"this error\", "
            "\"hvad ser jeg her\"\n"
            "- Describes code to build: \"create a function that\", \"build a website\", "
            "\"lav en app der\"\n\n"
            "Categories:\n"
            "- \"dictation\" — text to type as-is (DEFAULT when uncertain)\n"
            "- \"ask\" — question directed at the AI for an answer\n"
            "- \"vision\" — asking about something visible on screen\n"
            "- \"vibecode\" — describing software/code to build\n"
            + app_hint
            + "\n\nOutput ONLY the category name. Nothing else."
        )
        try:
            logger.debug("Classifying intent for auto mode")
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                max_tokens=10,
                timeout=10,
            )
            if not response.choices:
                return "dictation"
            result = response.choices[0].message.content.strip().lower().strip('"')
            if result in ("dictation", "ask", "vision", "vibecode"):
                logger.info("Auto mode classified as: %s", result)
                return result
            return "dictation"
        except Exception:
            logger.warning("Intent classification failed, defaulting to dictation",
                           exc_info=True)
            return "dictation"

    def ask_question(self, question: str, model: str = "gpt-4o",
                     app_context: str = "") -> str:
        """Send a voice-transcribed question to GPT and return the answer."""
        context_hint = ""
        if app_context:
            context_hint = f"\nThe user is currently in: {app_context}"

        dictionary_hint = ""
        if self.personal_dictionary:
            words = ", ".join(self.personal_dictionary)
            dictionary_hint = (
                f"\n\nPERSONAL DICTIONARY — preserve these words/names exactly "
                f"as spelled: {words}"
            )

        system_prompt = (
            "You are a helpful AI assistant. The user asked a question via voice "
            "dictation. Answer concisely and directly. Match the language of the "
            "user's question. Output ONLY your answer, no preamble."
            + context_hint + dictionary_hint
        )

        try:
            logger.debug("AI Ask with model=%s", model)
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question},
                ],
                timeout=30,
            )
            if not response.choices:
                return ""
            content = response.choices[0].message.content
            return content.strip() if content else ""
        except openai.AuthenticationError:
            raise RuntimeError("Authentication failed. Check your API key.")
        except openai.RateLimitError as exc:
            raise RuntimeError(_rate_limit_message(exc))
        except openai.APIConnectionError:
            raise RuntimeError("Cannot reach the OpenAI API. Check your connection.")
        except openai.APIError as exc:
            raise RuntimeError(f"AI question failed: {exc}") from exc

    def vision_query(self, screenshot_b64: str, voice_instruction: str,
                     model: str = "gpt-4o", app_context: str = "") -> str:
        """Analyze a screenshot with a voice instruction using GPT-4o vision."""
        context_hint = ""
        if app_context:
            context_hint = f"\nThe user is currently in: {app_context}"

        dictionary_hint = ""
        if self.personal_dictionary:
            words = ", ".join(self.personal_dictionary)
            dictionary_hint = (
                f"\n\nPERSONAL DICTIONARY — preserve these words/names exactly "
                f"as spelled: {words}"
            )

        system_prompt = (
            "You are a helpful AI assistant with vision. The user has shared a "
            "screenshot of their screen and is giving you a voice instruction. "
            "Analyze what you see on screen and respond to their request. Be "
            "concise and helpful. Match the language of the user's instruction. "
            "Output ONLY your response."
            + context_hint + dictionary_hint
        )

        try:
            logger.debug("Vision query with model=%s", model)
            response = self.client.chat.completions.create(
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
            if not response.choices:
                return ""
            content = response.choices[0].message.content
            return content.strip() if content else ""
        except openai.AuthenticationError:
            raise RuntimeError("Authentication failed. Check your API key.")
        except openai.RateLimitError as exc:
            raise RuntimeError(_rate_limit_message(exc))
        except openai.APIConnectionError:
            raise RuntimeError("Cannot reach the OpenAI API. Check your connection.")
        except openai.APIError as exc:
            raise RuntimeError(f"Vision query failed: {exc}") from exc

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

        try:
            logger.debug("VibeCode prompt generation with model=%s", model)
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": description},
                ],
                timeout=30,
            )
            if not response.choices:
                return ""
            content = response.choices[0].message.content
            return content.strip() if content else ""
        except openai.AuthenticationError:
            raise RuntimeError("Authentication failed. Check your API key.")
        except openai.RateLimitError as exc:
            raise RuntimeError(_rate_limit_message(exc))
        except openai.APIConnectionError:
            raise RuntimeError("Cannot reach the OpenAI API. Check your connection.")
        except openai.APIError as exc:
            raise RuntimeError(f"VibeCode generation failed: {exc}") from exc

    def custom_mode_query(self, transcribed_text: str, system_prompt: str,
                          model: str = "gpt-4o") -> str:
        """Apply a custom mode's prompt template to transcribed text."""
        try:
            logger.debug("Custom mode query with model=%s", model)
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": transcribed_text},
                ],
                timeout=30,
            )
            if not response.choices:
                return ""
            content = response.choices[0].message.content
            return content.strip() if content else ""
        except openai.AuthenticationError:
            raise RuntimeError("Authentication failed. Check your API key.")
        except openai.RateLimitError as exc:
            raise RuntimeError(_rate_limit_message(exc))
        except openai.APIConnectionError:
            raise RuntimeError("Cannot reach the OpenAI API. Check your connection.")
        except openai.APIError as exc:
            raise RuntimeError(f"Custom mode failed: {exc}") from exc

    def set_language(self, language: str) -> None:
        """Change the target transcription language.

        Args:
            language: ISO-639-1 language code (e.g. ``"da"``, ``"en"``).
        """
        self.language = language
        logger.info("Transcription language set to '%s'.", language)
