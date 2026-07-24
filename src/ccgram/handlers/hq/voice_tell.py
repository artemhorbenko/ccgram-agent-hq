"""Voice /tell — voice notes in the HQ topic become directed instructions.

A voice note sent in the Agent HQ topic is transcribed with the existing
Whisper pipeline, deterministically parsed into a target agent + an
instruction (no LLM), and presented as a structured preview. Delivery
happens only through the same explicit confirmation callbacks as text
``/tell`` — a voice-derived instruction is never sent without a tap.

Key functions:
  - handle_hq_voice(): voice branch entry point (called by voice_handler)
  - parse_voice_instruction(): pure-ish deterministic target extraction
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from ...thread_router import thread_router
from ..callback_data import CB_HQ_TELL_CANCEL, CB_HQ_TELL_CONFIRM
from ..messaging_pipeline.message_sender import safe_reply
from ..status.topic_emoji import strip_emoji_prefix
from ..user_state import HQ_PENDING_TELL
from .audit import log_hq_action
from .tell import _names_hint, resolve_agent

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# Leading filler words dropped before target matching ("Tell wallet …",
# "Скажи wallet …"). Lowercase, punctuation-stripped comparison.
_FILLER_PREFIXES = frozenset({"tell", "ask", "скажи", "скажите", "передай", "попроси"})

# Connector words dropped between the target and the instruction
# ("tell wallet TO run the checks").
_CONNECTOR_WORDS = frozenset({"to", "что", "чтобы", "пусть"})

# Try the first 1..3 words as the agent name (topic names may contain spaces).
_MAX_NAME_SPAN = 3

_PUNCT = ",.:;!?"


def parse_voice_instruction(user_id: int, text: str) -> tuple[str | None, str, str]:
    """Deterministically split a transcription into (window_id, name, instruction).

    Drops one leading filler word ("tell", "скажи", …), then tries the
    first 1–3 words as an agent name via the same exact → prefix →
    substring resolution as text ``/tell`` (longest span first). A match
    requires a non-empty remaining instruction. Returns
    ``(None, "", text)`` when no unambiguous target is found.
    """
    tokens = text.split()
    if tokens and tokens[0].lower().strip(_PUNCT) in _FILLER_PREFIXES:
        tokens = tokens[1:]

    for span in range(min(_MAX_NAME_SPAN, len(tokens) - 1), 0, -1):
        candidate = " ".join(tokens[:span]).strip(_PUNCT)
        if not candidate:
            continue
        window_id, _candidates = resolve_agent(user_id, candidate)
        if window_id is None:
            continue
        rest = tokens[span:]
        if rest and rest[0].lower().strip(_PUNCT) in _CONNECTOR_WORDS:
            rest = rest[1:]
        instruction = " ".join(rest).strip()
        if not instruction:
            continue
        name = strip_emoji_prefix(thread_router.get_display_name(window_id))
        return window_id, name, instruction

    return None, "", text


async def handle_hq_voice(update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Voice note in the HQ topic → transcribe → structured /tell preview.

    Authorization has already been checked by ``handle_voice_message``.
    """
    user = update.effective_user
    message = update.message
    if not user or not message or not message.voice:
        return

    # Lazy: voice_handler lazily imports hq for the topic branch; importing it
    # eagerly here would close that cycle at module load.
    from ..voice.voice_handler import transcribe_voice_message

    text = await transcribe_voice_message(message)
    if text is None:
        return

    window_id, name, instruction = parse_voice_instruction(user.id, text)
    if window_id is None:
        await safe_reply(
            message,
            f"🎤 Transcribed:\n\n{text}\n\n"
            "❓ Could not identify a target agent. Start with the agent's "
            "name (e.g. “tell wallet run the checks”), or use "
            f"/tell <agent> <instruction>.\n\n{_names_hint(user.id)}",
        )
        log_hq_action(
            user_id=user.id,
            command="voice_tell",
            result="unresolved",
            detail=text[:200],
        )
        return

    if context.user_data is not None:
        context.user_data[HQ_PENDING_TELL] = {
            "window_id": window_id,
            "name": name,
            "text": instruction,
        }
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Send", callback_data=CB_HQ_TELL_CONFIRM),
                InlineKeyboardButton("✖ Cancel", callback_data=CB_HQ_TELL_CANCEL),
            ]
        ]
    )
    await safe_reply(
        message,
        f"🎤 *Voice instruction*\n\nAgent: *{name}*\nInstruction: {instruction}\n\n"
        "Send it? (Cancel and use /tell to edit.)",
        reply_markup=keyboard,
    )
