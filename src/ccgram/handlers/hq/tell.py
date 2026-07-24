"""``/tell <agent> <instruction>`` — directed routing with confirmation.

Resolves the target agent by topic name (exact → prefix → substring,
case-insensitive, must be unambiguous), shows a confirmation preview,
and only on explicit confirmation sends the instruction through the
established ``send_to_window`` input path. Every delivery is audited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from ...multiplexer.window_ops import send_to_window
from ...thread_router import thread_router
from ..callback_data import CB_HQ_TELL_CANCEL, CB_HQ_TELL_CONFIRM
from ..callback_helpers import user_owns_window
from ..callback_registry import register
from ..messaging_pipeline.message_sender import safe_edit, safe_reply
from ..status.topic_emoji import strip_emoji_prefix
from ..user_state import HQ_PENDING_TELL
from .audit import log_hq_action
from .render import topic_url

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

_USAGE = (
    "Usage: /tell <agent> <instruction>\n\n"
    "Example: /tell wallet Run the template checks, then show the diff."
)

# "/tell", "<agent>", "<instruction…>"
_MIN_TELL_PARTS = 3


def _agent_names(user_id: int) -> dict[str, str]:
    """Map window_id → clean topic name for the user's bound sessions."""
    return {
        window_id: strip_emoji_prefix(thread_router.get_display_name(window_id))
        for window_id in thread_router.get_all_thread_windows(user_id).values()
    }


def resolve_agent(user_id: int, token: str) -> tuple[str | None, list[str]]:
    """Resolve an agent token to a window_id.

    Match precedence: exact name → unique prefix → unique substring
    (case-insensitive). Returns ``(window_id, [])`` on a unique match, or
    ``(None, candidate_names)`` when ambiguous / not found.
    """
    names = _agent_names(user_id)
    tok = token.casefold()

    for predicate in (
        lambda nm: nm == tok,
        lambda nm: nm.startswith(tok),
        lambda nm: tok in nm,
    ):
        matches = [wid for wid, nm in names.items() if predicate(nm.casefold())]
        if len(matches) == 1:
            return matches[0], []
        if matches:
            return None, sorted({names[wid] for wid in matches})
    return None, []


def _names_hint(user_id: int) -> str:
    names = sorted(set(_agent_names(user_id).values()), key=str.casefold)
    if not names:
        return "No sessions yet — create a topic (or /new) to start one."
    return "Agents: " + ", ".join(names)


async def tell_command(update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
    """``/tell`` command — HQ-topic only; delegates elsewhere."""
    # Lazy: hq_commands imports commands.forward; keep tell importable alone.
    from .hq_commands import _authorized, _delegate_outside_hq

    if await _delegate_outside_hq(update, context):
        return
    if not await _authorized(update):
        return
    user = update.effective_user
    message = update.message
    if not user or not message or not message.text:
        return

    # Split off "/tell" (or "/tell@bot"), the target token, and the rest.
    parts = message.text.split(maxsplit=2)
    if len(parts) < _MIN_TELL_PARTS or not parts[2].strip():
        await safe_reply(message, f"{_USAGE}\n\n{_names_hint(user.id)}")
        return
    token, instruction = parts[1], parts[2].strip()

    window_id, candidates = resolve_agent(user.id, token)
    if window_id is None:
        if candidates:
            listing = "\n".join(f"• {name}" for name in candidates)
            await safe_reply(
                message,
                f"❓ '{token}' matches several agents:\n{listing}\n\nBe more specific.",
            )
        else:
            await safe_reply(
                message,
                f"❓ No agent matches '{token}'.\n\n{_names_hint(user.id)}",
            )
        log_hq_action(
            user_id=user.id,
            command="tell",
            target=token,
            result="unresolved",
        )
        return

    name = strip_emoji_prefix(thread_router.get_display_name(window_id))
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
        f"Send to *{name}*?\n\n{instruction}",
        reply_markup=keyboard,
    )


def _open_topic_keyboard(
    user_id: int, window_id: str, name: str
) -> InlineKeyboardMarkup | None:
    thread_id = thread_router.get_thread_for_window(user_id, window_id)
    if thread_id is None:
        return None
    url = topic_url(thread_router.resolve_chat_id(user_id, thread_id), thread_id)
    if not url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"↗ {name}"[:32], url=url)]])


@register(CB_HQ_TELL_CONFIRM, CB_HQ_TELL_CANCEL)
async def _dispatch(update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not user:
        return

    pending = (
        context.user_data.pop(HQ_PENDING_TELL, None)
        if context.user_data is not None
        else None
    )

    if query.data == CB_HQ_TELL_CANCEL:
        await safe_edit(query, "✖ Cancelled — nothing was sent.")
        await query.answer()
        return

    if not isinstance(pending, dict):
        await query.answer("Nothing pending — send /tell again.", show_alert=True)
        return

    window_id = str(pending.get("window_id", ""))
    name = str(pending.get("name", window_id))
    instruction = str(pending.get("text", ""))
    if not user_owns_window(user.id, window_id):
        await query.answer("Not your session", show_alert=True)
        return

    ok, result_msg = await send_to_window(window_id, instruction)
    log_hq_action(
        user_id=user.id,
        command="tell",
        target=window_id,
        result="ok" if ok else "error",
        detail=result_msg,
    )
    if ok:
        await safe_edit(
            query,
            f"✅ Sent to *{name}*.",
            reply_markup=_open_topic_keyboard(user.id, window_id, name),
        )
        await query.answer("Sent")
    else:
        await safe_edit(query, f"❌ {result_msg}")
        await query.answer(result_msg, show_alert=True)
