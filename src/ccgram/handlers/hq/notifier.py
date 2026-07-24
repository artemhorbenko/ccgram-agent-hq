"""Agent HQ notifier — state-change notifications + optional daily digest.

A background loop (started from bootstrap when Agent HQ is enabled and
``CCGRAM_HQ_NOTIFY_INTERVAL`` > 0) periodically aggregates session
summaries and posts a message into the HQ topic when a session
transitions into a notify-worthy state: needs input (blocked), done,
dead, or stale. Calm states (working/idle) never notify — per the PRD,
the notifier emits only for states that need the owner.

Dedup and noise control:
  - one notification per window per state transition (``_last_notified``)
  - the first tick after startup primes the cache silently, so a restart
    does not re-announce every already-done/blocked session
  - posts only for users whose HQ-topic chat_id is known (recorded when
    they use the HQ topic); silently skips otherwise

The optional daily digest (``CCGRAM_HQ_DIGEST_TIME``, local HH:MM) rides
the same loop: once per day after the configured time, the /brief view
is posted into the HQ topic.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import TYPE_CHECKING

import structlog

from ...config import config
from ...thread_router import thread_router
from ..messaging_pipeline.message_sender import rate_limit_send_message
from .audit import log_hq_action
from .render import STATE_EMOJI, STATE_LABELS, build_view_keyboard, format_elapsed
from .summary import (
    STATE_DEAD,
    STATE_DONE,
    STATE_NEEDS_YOU,
    SessionSummary,
    collect_session_summaries,
)

if TYPE_CHECKING:
    from ...telegram_client import TelegramClient

logger = structlog.get_logger()

# window_id -> last notified state token ("" = calm). Global across users:
# window IDs are unique within a multiplexer server.
_last_notified: dict[str, str] = {}
_primed = False
_last_digest_date: date | None = None


def reset_for_testing() -> None:
    """Clear notifier module state (test isolation)."""
    global _primed, _last_digest_date
    _last_notified.clear()
    _primed = False
    _last_digest_date = None


def notify_state(summary: SessionSummary) -> str:
    """Map a summary to its notify-worthy state token ("" = don't notify)."""
    if summary.state in (STATE_NEEDS_YOU, STATE_DEAD, STATE_DONE):
        return summary.state
    if summary.stale:
        return "stale"
    return ""


def _notification_text(summary: SessionSummary, token: str) -> str:
    """Render one state-transition notification line."""
    if token == "stale":
        elapsed = format_elapsed(summary.idle_seconds)
        reason = f"no activity for {elapsed}" if elapsed else "no recent activity"
        return f"⏳ *{summary.name}* looks stale — {reason}"
    emoji = STATE_EMOJI.get(token, "")
    label = STATE_LABELS.get(token, token)
    detail = f" — {summary.detail}" if summary.detail else ""
    return f"{emoji} *{summary.name}* is {label}{detail}"


def _hq_chat_id(user_id: int) -> int | None:
    """Chat ID of the HQ topic for *user_id*, or None if never recorded."""
    if config.hq_topic_id is None:
        return None
    return thread_router.group_chat_ids.get(f"{user_id}:{config.hq_topic_id}")


async def tick_hq_notifications(client: "TelegramClient") -> None:
    """One notifier pass: diff session states, post transitions to HQ."""
    global _primed
    if config.hq_topic_id is None:
        return

    seen: set[str] = set()
    for user_id in list(thread_router.thread_bindings):
        chat_id = _hq_chat_id(user_id)
        summaries = await collect_session_summaries(user_id)
        for summary in summaries:
            seen.add(summary.window_id)
            token = notify_state(summary)
            if token == _last_notified.get(summary.window_id, ""):
                continue
            _last_notified[summary.window_id] = token
            if not token or not _primed or chat_id is None:
                continue
            await rate_limit_send_message(
                client,
                chat_id,
                _notification_text(summary, token),
                message_thread_id=config.hq_topic_id,
                reply_markup=build_view_keyboard([summary], "needs"),
            )
            log_hq_action(
                user_id=user_id,
                command="notify",
                target=summary.window_id,
                detail=token,
            )

    # Drop tracking for windows that no longer have any binding.
    for window_id in list(_last_notified):
        if window_id not in seen:
            del _last_notified[window_id]

    _primed = True


async def maybe_send_daily_digest(
    client: "TelegramClient", now: datetime | None = None
) -> None:
    """Post the /brief view into HQ once per day after the configured time."""
    global _last_digest_date
    if config.hq_topic_id is None or config.hq_digest_time is None:
        return
    now = now or datetime.now()
    if now.time() < config.hq_digest_time or _last_digest_date == now.date():
        return
    _last_digest_date = now.date()

    # Lazy: hq_commands imports notifier-adjacent modules; call-time import
    # keeps notifier importable without the command/forward handler graph.
    from .hq_commands import _build_view

    for user_id in list(thread_router.thread_bindings):
        chat_id = _hq_chat_id(user_id)
        if chat_id is None:
            continue
        text, keyboard = await _build_view(user_id, "brief")
        await rate_limit_send_message(
            client,
            chat_id,
            f"🗞 *Daily digest*\n\n{text}",
            message_thread_id=config.hq_topic_id,
            reply_markup=keyboard,
        )
        log_hq_action(user_id=user_id, command="digest")


async def hq_notify_loop(client: "TelegramClient") -> None:
    """Background loop: notifications + daily digest until cancelled."""
    logger.info(
        "HQ notifier started (interval=%.0fs, digest=%s)",
        config.hq_notify_interval,
        config.hq_digest_time or "off",
    )
    while True:
        try:
            await tick_hq_notifications(client)
            await maybe_send_daily_digest(client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive any tick error
            logger.exception("HQ notifier tick failed")
        await asyncio.sleep(config.hq_notify_interval)
