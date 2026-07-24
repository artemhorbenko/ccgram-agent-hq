"""Agent HQ commands — cross-session control plane in one forum topic.

The topic configured via ``CCGRAM_HQ_TOPIC_ID`` becomes the control
plane: ``/agents``, ``/brief``, ``/needs_you``, ``/hq_status`` and
``/new`` activate there. Outside the HQ topic (or when the feature is
disabled) every command delegates to ``forward_command_handler``, so
per-agent topics keep today's behavior byte-for-byte.

Also owns the HQ callbacks: view refresh, Read-output, and the two-step
Interrupt confirmation.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError

from ...config import config
from ...multiplexer import multiplexer as tmux_manager
from ...telegram_client import PTBTelegramClient
from ...thread_router import thread_router
from ..callback_data import (
    CB_HQ_INTERRUPT,
    CB_HQ_INTERRUPT_CONFIRM,
    CB_HQ_READ,
    CB_HQ_REFRESH,
)
from ..callback_helpers import get_thread_id, user_owns_window
from ..callback_registry import register
from ..commands import forward_command_handler
from ..messaging_pipeline.message_sender import (
    rate_limit_send_message,
    safe_edit,
    safe_reply,
)
from ..shell.shell_context import redact_for_llm
from .audit import audit_log_path, log_hq_action
from .render import (
    STATE_LABELS,
    build_needs_you_keyboard,
    build_view_keyboard,
    render_agents,
    render_brief,
    render_needs_you,
    topic_url,
)
from .summary import (
    STATE_NEEDS_YOU,
    STATE_WORKING,
    capture_excerpt,
    collect_session_summaries,
)

if TYPE_CHECKING:
    from telegram import CallbackQuery, Message
    from telegram.ext import ContextTypes

logger = structlog.get_logger()

# Max characters of terminal tail posted by the Read-output button.
_READ_OUTPUT_LIMIT = 3500

_HELP_TEXT = (
    "\U0001f39b *Agent HQ*\n\n"
    "This topic is the control plane — it never binds to an agent.\n\n"
    "/agents — list all sessions\n"
    "/brief — compact summary by state\n"
    "/needs\\_you — only agents waiting on you\n"
    "/tell <agent> <instruction> — send work to an agent\n"
    "/new \\[name] — create a topic for a new agent\n"
    "/hq\\_status — registry health"
)


def is_hq_topic(thread_id: int | None) -> bool:
    """True when Agent HQ is enabled and *thread_id* is the HQ topic."""
    return config.hq_topic_id is not None and thread_id == config.hq_topic_id


async def _delegate_outside_hq(
    update: Update, context: "ContextTypes.DEFAULT_TYPE"
) -> bool:
    """Preserve pre-HQ behavior outside the HQ topic.

    Returns True when the command was delegated to the provider-forward
    path (feature disabled, or invoked in a regular topic) — exactly what
    happened to these command names before Agent HQ existed.
    """
    if is_hq_topic(get_thread_id(update)):
        return False
    await forward_command_handler(update, context)
    return True


async def _authorized(update: Update) -> bool:
    """Allowlist check with the standard rejection reply."""
    user = update.effective_user
    if user and config.is_user_allowed(user.id):
        return True
    if update.message:
        await safe_reply(update.message, "You are not authorized to use this bot.")
    return False


async def _build_view(user_id: int, mode: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build text + keyboard for an HQ view: agents | brief | needs."""
    summaries = await collect_session_summaries(user_id)
    if mode == "brief":
        # Excerpts only where they add signal (active or blocked sessions).
        targets = [
            s
            for s in summaries
            if not s.detail and s.state in (STATE_WORKING, STATE_NEEDS_YOU)
        ]
        captured = await asyncio.gather(
            *(capture_excerpt(s.window_id) for s in targets)
        )
        excerpts = {s.window_id: text for s, text in zip(targets, captured) if text}
        return render_brief(summaries, excerpts), build_view_keyboard(
            summaries, "brief"
        )
    if mode == "needs":
        return render_needs_you(summaries), build_needs_you_keyboard(summaries)
    return render_agents(summaries), build_view_keyboard(summaries, "agents")


async def _run_view_command(
    update: Update,
    context: "ContextTypes.DEFAULT_TYPE",
    mode: str,
    command: str,
) -> None:
    if await _delegate_outside_hq(update, context):
        return
    if not await _authorized(update):
        return
    user = update.effective_user
    if not user or not update.message:
        return
    text, keyboard = await _build_view(user.id, mode)
    await safe_reply(update.message, text, reply_markup=keyboard)
    log_hq_action(user_id=user.id, command=command)


async def agents_command(update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
    """``/agents`` — list all registered sessions, triage-sorted."""
    await _run_view_command(update, context, "agents", "agents")


async def brief_command(update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
    """``/brief`` — compact summary grouped by state with excerpts."""
    await _run_view_command(update, context, "brief", "brief")


async def needs_you_command(
    update: Update, context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """``/needs_you`` — only blocked / dead / stale sessions."""
    await _run_view_command(update, context, "needs", "needs_you")


async def hq_status_command(
    update: Update, context: "ContextTypes.DEFAULT_TYPE"
) -> None:
    """``/hq_status`` — registry health for the HQ control plane."""
    if await _delegate_outside_hq(update, context):
        return
    if not await _authorized(update):
        return
    user = update.effective_user
    if not user or not update.message:
        return

    summaries = await collect_session_summaries(user.id)
    counts = {state: 0 for state in STATE_LABELS}
    for s in summaries:
        counts[s.state] = counts.get(s.state, 0) + 1
    stale = sum(1 for s in summaries if s.stale)
    try:
        backend = tmux_manager.capabilities.name
    except Exception:  # noqa: BLE001 — status must render even if unwired
        backend = "unknown"

    state_line = ", ".join(
        f"{STATE_LABELS[state]} {count}" for state, count in counts.items() if count
    )
    await safe_reply(
        update.message,
        "\U0001f39b *Agent HQ status*\n"
        f"• HQ topic: {config.hq_topic_id}\n"
        f"• Multiplexer: {backend}\n"
        f"• Sessions: {len(summaries)}"
        + (f" ({state_line})" if state_line else "")
        + "\n"
        f"• Stale: {stale}\n"
        f"• Audit log: {audit_log_path()}",
    )
    log_hq_action(user_id=user.id, command="hq_status")


async def hq_new_command(update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
    """``/new [name]`` in HQ — create a topic that runs the normal flow.

    The new topic is a plain unbound topic: its first message triggers
    CCGram's existing directory → worktree → provider creation flow, and
    the session registers in ``/agents`` through the normal binding path.
    """
    if await _delegate_outside_hq(update, context):
        return
    if not await _authorized(update):
        return
    user = update.effective_user
    message = update.message
    chat = update.effective_chat
    if not user or not message or not chat:
        return

    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        name = f"agent-{len(thread_router.get_all_thread_windows(user.id)) + 1}"

    client = PTBTelegramClient(context.bot)
    try:
        topic = await client.create_forum_topic(chat.id, name)
    except TelegramError as exc:
        logger.info("HQ /new: create_forum_topic failed: %s", exc)
        log_hq_action(user_id=user.id, command="new", result="error", detail=str(exc))
        await safe_reply(
            message,
            "❌ Could not create a topic (the bot needs the *Manage topics* "
            "right). Create a topic manually — its first message starts the "
            "session flow.",
        )
        return

    await rate_limit_send_message(
        client,
        chat.id,
        "\U0001f195 Send your task here to start a session "
        "(directory → worktree → provider).",
        message_thread_id=topic.message_thread_id,
    )
    url = topic_url(chat.id, topic.message_thread_id)
    keyboard = (
        InlineKeyboardMarkup([[InlineKeyboardButton(f"↗ {name}"[:32], url=url)]])
        if url
        else None
    )
    await safe_reply(
        message,
        f"✅ Topic *{name}* created. Send the first task there to launch the agent.",
        reply_markup=keyboard,
    )
    log_hq_action(user_id=user.id, command="new", target=name)


async def handle_hq_text(message: "Message") -> None:
    """Plain text in the HQ topic: show help instead of the binding flow."""
    await safe_reply(message, _HELP_TEXT)


# ---------------------------------------------------------------------------
# Callbacks: refresh, read output, interrupt (two-step)
# ---------------------------------------------------------------------------


async def _handle_refresh(query: "CallbackQuery", user_id: int, mode: str) -> None:
    text, keyboard = await _build_view(user_id, mode)
    await safe_edit(query, text, reply_markup=keyboard)
    await query.answer("Refreshed")


async def _handle_read_output(
    query: "CallbackQuery",
    user_id: int,
    window_id: str,
    context: "ContextTypes.DEFAULT_TYPE",
) -> None:
    """Post a redacted terminal tail for *window_id* into the HQ topic."""
    if not user_owns_window(user_id, window_id):
        await query.answer("Not your session", show_alert=True)
        return
    msg = query.message
    if msg is None or not hasattr(msg, "chat"):
        await query.answer("Cannot post here", show_alert=True)
        return

    raw = await tmux_manager.capture_pane(window_id)
    if not raw:
        await query.answer("No output available", show_alert=True)
        return
    tail = redact_for_llm(raw.strip())[-_READ_OUTPUT_LIMIT:]
    name = thread_router.get_display_name(window_id)
    await rate_limit_send_message(
        PTBTelegramClient(context.bot),
        msg.chat.id,
        f"\U0001f4d6 *{name}*\n```\n{tail}\n```",
        message_thread_id=config.hq_topic_id,
    )
    log_hq_action(user_id=user_id, command="read_output", target=window_id)
    await query.answer()


async def _handle_interrupt(
    query: "CallbackQuery", user_id: int, window_id: str
) -> None:
    """First Interrupt tap — show confirmation (PRD: confirm before interrupt)."""
    if not user_owns_window(user_id, window_id):
        await query.answer("Not your session", show_alert=True)
        return
    name = thread_router.get_display_name(window_id)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"⚠ Confirm interrupt {name}"[:60],
                    callback_data=f"{CB_HQ_INTERRUPT_CONFIRM}{window_id}"[:64],
                )
            ],
            [InlineKeyboardButton("↩ Back", callback_data=f"{CB_HQ_REFRESH}needs")],
        ]
    )
    await safe_edit(
        query,
        f"Interrupt *{name}*?\n\nThis sends Escape to the agent.",
        reply_markup=keyboard,
    )
    await query.answer()


async def _handle_interrupt_confirm(
    query: "CallbackQuery", user_id: int, window_id: str
) -> None:
    """Second tap — send Escape, then re-render the needs-you view."""
    if not user_owns_window(user_id, window_id):
        await query.answer("Not your session", show_alert=True)
        return
    name = thread_router.get_display_name(window_id)
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        log_hq_action(
            user_id=user_id,
            command="interrupt",
            target=window_id,
            result="error",
            detail="window not found",
        )
        await query.answer("Window not found", show_alert=True)
        return
    await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)
    log_hq_action(user_id=user_id, command="interrupt", target=window_id)

    text, keyboard = await _build_view(user_id, "needs")
    await safe_edit(query, f"⎋ Sent Escape to {name}\n\n{text}", reply_markup=keyboard)
    await query.answer("⎋ Sent Escape")


@register(CB_HQ_REFRESH, CB_HQ_READ, CB_HQ_INTERRUPT, CB_HQ_INTERRUPT_CONFIRM)
async def _dispatch(update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not user:
        return
    data = query.data

    if data.startswith(CB_HQ_REFRESH):
        await _handle_refresh(query, user.id, data[len(CB_HQ_REFRESH) :])
    elif data.startswith(CB_HQ_INTERRUPT_CONFIRM):
        await _handle_interrupt_confirm(
            query, user.id, data[len(CB_HQ_INTERRUPT_CONFIRM) :]
        )
    elif data.startswith(CB_HQ_INTERRUPT):
        await _handle_interrupt(query, user.id, data[len(CB_HQ_INTERRUPT) :])
    elif data.startswith(CB_HQ_READ):
        await _handle_read_output(query, user.id, data[len(CB_HQ_READ) :], context)
