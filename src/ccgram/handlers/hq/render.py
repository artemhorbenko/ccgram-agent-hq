"""Agent HQ rendering — deterministic text + keyboards for HQ views.

Pure presentation over ``SessionSummary`` lists: no I/O, no state. All
functions are unit-testable without mocks.

Key functions:
  - render_agents / render_brief / render_needs_you: view text
  - build_view_keyboard / build_needs_you_keyboard: inline keyboards
  - format_elapsed: compact "2m" / "3h 05m" elapsed formatting
  - topic_url: deep link to a forum topic (supergroups only)
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..callback_data import (
    CB_HQ_INTERRUPT,
    CB_HQ_READ,
    CB_HQ_REFRESH,
)
from .summary import (
    STATE_DEAD,
    STATE_DONE,
    STATE_IDLE,
    STATE_NEEDS_YOU,
    STATE_WORKING,
    SessionSummary,
)

STATE_EMOJI: dict[str, str] = {
    STATE_NEEDS_YOU: "\U0001f7e1",  # yellow circle
    STATE_DEAD: "\U0001f4a5",  # collision
    STATE_WORKING: "\U0001f7e2",  # green circle
    STATE_DONE: "✅",  # check mark
    STATE_IDLE: "⚪",  # white circle
}

# Grouping order + headers for /brief (needs-attention first).
BRIEF_GROUPS: tuple[tuple[str, str], ...] = (
    (STATE_NEEDS_YOU, "\U0001f7e1 Needs you"),
    (STATE_DEAD, "\U0001f4a5 Dead"),
    (STATE_WORKING, "\U0001f7e2 Working"),
    (STATE_DONE, "✅ Done"),
    (STATE_IDLE, "⚪ Idle"),
)

STATE_LABELS: dict[str, str] = {
    STATE_NEEDS_YOU: "needs you",
    STATE_DEAD: "dead",
    STATE_WORKING: "working",
    STATE_DONE: "done",
    STATE_IDLE: "idle",
}

_STALE_SUFFIX = " ⏳ stale"
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_LINK_BUTTONS_PER_ROW = 2


def format_elapsed(seconds: float | None) -> str:
    """Compact elapsed-time label: 45s / 2m / 3h 05m. Empty for unknown."""
    if seconds is None or seconds < 0:
        return ""
    if seconds < _SECONDS_PER_MINUTE:
        return f"{int(seconds)}s"
    minutes = int(seconds // _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m"
    return f"{minutes // _MINUTES_PER_HOUR}h {minutes % _MINUTES_PER_HOUR:02d}m"


def topic_url(chat_id: int, thread_id: int) -> str | None:
    """Deep link to a forum topic. Only supergroup chat IDs (-100…) link."""
    raw = str(chat_id)
    if not raw.startswith("-100"):
        return None
    return f"https://t.me/c/{raw[4:]}/{thread_id}"


def _activity_suffix(summary: SessionSummary) -> str:
    elapsed = format_elapsed(summary.idle_seconds)
    return f", last activity {elapsed} ago" if elapsed else ""


def render_agents(summaries: list[SessionSummary]) -> str:
    """Full session list: name, provider, project, state, elapsed."""
    if not summaries:
        return "Agent HQ — no sessions.\n\nCreate a new topic (or /new) to start one."

    lines: list[str] = []
    for s in summaries:
        emoji = STATE_EMOJI.get(s.state, "")
        provider = f" [{s.provider}]" if s.provider else ""
        state_label = STATE_LABELS.get(s.state, s.state)
        stale = _STALE_SUFFIX if s.stale else ""
        lines.append(
            f"{emoji} {s.name}{provider} — {state_label}{stale}{_activity_suffix(s)}"
        )
        if s.cwd:
            lines.append(f"    {s.cwd}")
        if s.detail:
            lines.append(f"    {s.detail}")
    return f"Agent HQ — {_count_label(summaries)}\n\n" + "\n".join(lines)


def render_brief(
    summaries: list[SessionSummary], excerpts: dict[str, str] | None = None
) -> str:
    """Compact summary grouped by state, with optional terminal excerpts."""
    if not summaries:
        return "Agent HQ — no sessions.\n\nCreate a new topic (or /new) to start one."

    excerpts = excerpts or {}
    sections: list[str] = [f"Agent HQ — {_count_label(summaries)}"]
    for state, header in BRIEF_GROUPS:
        group = [s for s in summaries if s.state == state]
        if not group:
            continue
        lines = [header]
        for s in group:
            what = s.detail or excerpts.get(s.window_id, "")
            body = f" — {what}" if what else ""
            stale = _STALE_SUFFIX if s.stale else ""
            suffix = _activity_suffix(s) if state == STATE_WORKING else ""
            lines.append(f"• {s.name}{body}{stale}{suffix}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def render_needs_you(summaries: list[SessionSummary]) -> str:
    """Only sessions needing attention: blocked, dead, or stale."""
    attention = [s for s in summaries if s.needs_attention]
    if not attention:
        return "✅ Nobody needs you right now."

    lines = [f"\U0001f7e1 Needs you — {_count_label(attention)}"]
    for s in attention:
        reason = s.detail or STATE_LABELS.get(s.state, s.state)
        if s.stale:
            elapsed = format_elapsed(s.idle_seconds)
            reason = f"stale — no activity for {elapsed}" if elapsed else "stale"
        lines.append(f"• {s.name} — {reason}")
    return "\n".join(lines)


def _count_label(summaries: list[SessionSummary]) -> str:
    n = len(summaries)
    return f"{n} session" if n == 1 else f"{n} sessions"


def _refresh_button(mode: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        "\U0001f504 Refresh", callback_data=f"{CB_HQ_REFRESH}{mode}"[:64]
    )


def build_view_keyboard(
    summaries: list[SessionSummary], mode: str
) -> InlineKeyboardMarkup:
    """Open-topic deep-link buttons (2 per row) plus a Refresh button."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for s in summaries:
        url = topic_url(s.chat_id, s.thread_id)
        if not url:
            continue
        row.append(InlineKeyboardButton(f"↗ {s.name}"[:32], url=url))
        if len(row) == _LINK_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_refresh_button(mode)])
    return InlineKeyboardMarkup(rows)


def build_needs_you_keyboard(
    summaries: list[SessionSummary],
) -> InlineKeyboardMarkup:
    """Per-session action rows (Open / Read / Interrupt) + Refresh."""
    rows: list[list[InlineKeyboardButton]] = []
    for s in summaries:
        if not s.needs_attention:
            continue
        row: list[InlineKeyboardButton] = []
        url = topic_url(s.chat_id, s.thread_id)
        if url:
            row.append(InlineKeyboardButton(f"↗ {s.name}"[:32], url=url))
        if s.state != STATE_DEAD:
            row.append(
                InlineKeyboardButton(
                    "\U0001f4d6 Read",
                    callback_data=f"{CB_HQ_READ}{s.window_id}"[:64],
                )
            )
            row.append(
                InlineKeyboardButton(
                    "⏹ Interrupt",
                    callback_data=f"{CB_HQ_INTERRUPT}{s.window_id}"[:64],
                )
            )
        if row:
            rows.append(row)
    rows.append([_refresh_button("needs")])
    return InlineKeyboardMarkup(rows)
