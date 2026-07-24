"""Voice message handler — download OGG audio, transcribe via Whisper, and present confirm keyboard.

Handles Telegram voice messages by downloading the audio, transcribing it using
the configured Whisper provider, and showing the transcription with a confirm/discard
inline keyboard so the user can review before sending to the agent.

Key handler:
  - handle_voice_message: main entry point for filters.VOICE
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from ...config import config
from ...thread_router import thread_router
from ...whisper import get_transcriber
from ...whisper.base import TranscriptionResult, WhisperTranscriber
from ..callback_helpers import get_thread_id
from ..messaging_pipeline.message_sender import safe_reply
from ..user_state import VOICE_PENDING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# Max voice file size: 25 MB (Telegram Bot API getFile limit)
_MAX_VOICE_SIZE = 25 * 1024 * 1024


def _build_voice_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Build the confirm/discard inline keyboard for a transcribed voice message."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✓ Send to agent",
                    callback_data=f"vc:send:{message_id}",
                ),
                InlineKeyboardButton(
                    "✗ Discard",
                    callback_data=f"vc:drop:{message_id}",
                ),
            ]
        ]
    )


async def _download_voice(message: Message, file_id: str) -> bytes | None:
    """Download voice audio from Telegram. Returns bytes or None on error."""
    try:
        file = await message.get_bot().get_file(file_id)
        audio_bytearray = await file.download_as_bytearray()
        return bytes(audio_bytearray)
    except TelegramError as e:
        logger.warning("Failed to download voice message: %s", e)
        await safe_reply(message, "❌ Failed to download voice message.")
        return None


async def _get_transcriber_or_reply(message: Message) -> WhisperTranscriber | None:
    """Resolve the configured transcriber and surface user-facing errors."""
    try:
        transcriber = get_transcriber()
    except (ValueError, RuntimeError) as e:
        await safe_reply(message, f"❌ {e}")
        return None

    if transcriber is None:
        await safe_reply(
            message,
            "⚠️ Voice transcription is not configured. Set CCGRAM_WHISPER_PROVIDER to enable it.\n\nSupported providers: openai, groq",
        )
        return None

    return transcriber


async def _transcribe_audio(
    message: Message, transcriber: WhisperTranscriber, audio_bytes: bytes
) -> TranscriptionResult | None:
    """Transcribe audio bytes. Returns TranscriptionResult or None on error."""
    try:
        return await transcriber.transcribe(audio_bytes, "voice.ogg")
    except (ValueError, RuntimeError) as e:
        await safe_reply(message, f"❌ {e}")
        return None


async def _send_confirm_message(
    message: Message, text: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send transcription with confirm/discard keyboard in a single message.

    Uses the original voice message_id as the callback reference so the keyboard
    is included on first send (no edit_reply_markup needed).
    """
    keyboard = _build_voice_keyboard(message.message_id)
    confirm_msg = await safe_reply(
        message, f"🎤 Transcribed:\n\n{text}", reply_markup=keyboard
    )
    if confirm_msg is None:
        return

    if context.user_data is not None:
        key = (confirm_msg.chat.id, message.message_id)
        context.user_data.setdefault(VOICE_PENDING, {})[key] = text


async def transcribe_voice_message(message: Message) -> str | None:
    """Download and transcribe a voice message, replying on any failure.

    Shared by the per-agent voice flow and the Agent HQ voice-/tell flow.
    Returns the transcribed text, or None after a user-facing error reply
    (size limit, missing transcriber, download or transcription failure,
    empty result).
    """
    voice = message.voice
    if voice is None:
        return None
    if voice.file_size is not None and voice.file_size > _MAX_VOICE_SIZE:
        size_mb = voice.file_size / (1024 * 1024)
        await safe_reply(
            message,
            f"❌ Voice message too large ({size_mb:.1f} MB). Maximum 25 MB.",
        )
        return None

    transcriber = await _get_transcriber_or_reply(message)
    if transcriber is None:
        return None

    audio_bytes = await _download_voice(message, voice.file_id)
    if audio_bytes is None:
        return None

    await message.get_bot().send_chat_action(
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
        action=ChatAction.TYPING,
    )

    result = await _transcribe_audio(message, transcriber, audio_bytes)
    if result is None:
        return None

    if not result.text.strip():
        await safe_reply(message, "⚠️ Could not transcribe audio (empty result).")
        return None

    return result.text


async def handle_voice_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle incoming voice messages: transcribe and present confirm keyboard."""
    user = update.effective_user
    message = update.message
    if not user or not message or not message.voice:
        return

    if not config.is_user_allowed(user.id):
        await safe_reply(message, "You are not authorized to use this bot.")
        return

    thread_id = get_thread_id(update)

    # Agent HQ control-plane topic: voice becomes a /tell with a structured
    # target + instruction confirmation instead of the bound-window flow.
    # Lazy: hq imports handlers.commands (forward); keep it off this module's load path.
    from ..hq import handle_hq_voice, is_hq_topic

    if is_hq_topic(thread_id):
        await handle_hq_voice(update, context)
        return

    window_id = thread_router.resolve_window_for_thread(user.id, thread_id)
    if not window_id:
        await safe_reply(
            message,
            "⚠ Topic not bound — send a text message first to pick a "
            "directory, then re-record.\n"
            "\U0001f4ac Voice messages aren't queued.",
        )
        return

    text = await transcribe_voice_message(message)
    if text is None:
        return

    await _send_confirm_message(message, text, context)
