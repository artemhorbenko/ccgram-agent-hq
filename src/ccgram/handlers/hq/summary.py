"""Agent HQ session summaries — cross-session status aggregation.

Builds read-only ``SessionSummary`` projections for every topic-bound
window of a user by composing the existing query layers: thread bindings
(``thread_router``), window identity (``window_query``), volatile session
state (``session_state_ports``), and the push-updated native agent-status
cache (``multiplexer.agent_status_cache``). No new source of truth — the
aggregator only reads.

Key functions:
  - collect_session_summaries(): sorted summaries for a user's sessions
  - classify_state(): pure lifecycle-state classifier (unit-testable)
  - capture_excerpt(): redacted one-line tail of a window's terminal
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ... import window_query
from ...multiplexer import agent_status_cache
from ...multiplexer import multiplexer as tmux_manager
from ...session_state_ports.live_session_state import get_live_session_snapshot
from ...thread_router import thread_router
from ..shell.shell_context import redact_for_llm
from ..status.topic_emoji import strip_emoji_prefix

if TYPE_CHECKING:
    from ...claude_task_state import ClaudeTaskSnapshot
    from ...multiplexer.base import AgentStatus

# Lifecycle states, ordered by triage priority (needs attention first).
STATE_NEEDS_YOU = "needs_you"
STATE_DEAD = "dead"
STATE_WORKING = "working"
STATE_DONE = "done"
STATE_IDLE = "idle"

STATE_ORDER: dict[str, int] = {
    STATE_NEEDS_YOU: 0,
    STATE_DEAD: 1,
    STATE_WORKING: 2,
    STATE_DONE: 3,
    STATE_IDLE: 4,
}

# Transcript activity within this window counts as "working".
WORKING_THRESHOLD_SECONDS = 120.0
# A "working" session with no activity for this long is flagged stale.
STALE_THRESHOLD_SECONDS = 1800.0

EXCERPT_MAX_CHARS = 160


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Read-only cross-session dashboard row for one topic-bound window."""

    window_id: str
    thread_id: int
    chat_id: int
    name: str
    provider: str
    cwd: str
    state: str
    idle_seconds: float | None = None
    detail: str = ""
    stale: bool = False

    @property
    def needs_attention(self) -> bool:
        """True when the session should surface in /needs_you."""
        return self.state in (STATE_NEEDS_YOU, STATE_DEAD) or self.stale


def classify_state(
    *,
    alive: bool,
    wait_header: str | None,
    native_state: str | None,
    has_active_task: bool,
    open_count: int | None,
    done_count: int | None,
    idle_seconds: float | None,
) -> str:
    """Classify a session's lifecycle state from independent signals.

    Priority: window death, then explicit wait/blocked signals (hook
    Notification header, native ``blocked``), then the native backend
    status, then task/transcript activity heuristics.
    """
    if not alive:
        return STATE_DEAD
    if wait_header or native_state == "blocked":
        return STATE_NEEDS_YOU
    native_map = {"working": STATE_WORKING, "done": STATE_DONE}
    if native_state in native_map:
        return native_map[native_state]
    recently_active = (
        idle_seconds is not None and idle_seconds < WORKING_THRESHOLD_SECONDS
    )
    if has_active_task or recently_active:
        return STATE_WORKING
    if open_count == 0 and (done_count or 0) > 0:
        return STATE_DONE
    return STATE_IDLE


def sort_summaries(summaries: list[SessionSummary]) -> list[SessionSummary]:
    """Order by triage priority (needs attention → working → done → idle)."""
    return sorted(
        summaries,
        key=lambda s: (STATE_ORDER.get(s.state, len(STATE_ORDER)), s.name.casefold()),
    )


def _summarize_window(
    user_id: int,
    thread_id: int,
    window_id: str,
    live_ids: set[str],
    now: float,
) -> SessionSummary:
    """Build one SessionSummary from the read-only query layers."""
    name = strip_emoji_prefix(thread_router.get_display_name(window_id))
    chat_id = thread_router.resolve_chat_id(user_id, thread_id)
    view = window_query.view_window(window_id)
    snap = get_live_session_snapshot(window_id)
    native = agent_status_cache.get_status(window_id)

    task = snap.task_snapshot
    has_active_task = bool(task and task.active_task_id)
    idle_seconds = (
        now - snap.last_activity_ts if snap.last_activity_ts is not None else None
    )

    state = classify_state(
        alive=window_id in live_ids,
        wait_header=snap.wait_header,
        native_state=native.state if native else None,
        has_active_task=has_active_task,
        open_count=task.open_count if task else None,
        done_count=task.done_count if task else None,
        idle_seconds=idle_seconds,
    )
    stale = (
        state == STATE_WORKING
        and idle_seconds is not None
        and idle_seconds >= STALE_THRESHOLD_SECONDS
    )

    return SessionSummary(
        window_id=window_id,
        thread_id=thread_id,
        chat_id=chat_id,
        name=name,
        provider=(view.provider_name if view and view.provider_name else ""),
        cwd=(view.cwd if view else ""),
        state=state,
        idle_seconds=idle_seconds,
        detail=_resolve_detail(snap.wait_header, native, task),
        stale=stale,
    )


def _resolve_detail(
    wait_header: str | None,
    native: "AgentStatus | None",
    task: "ClaudeTaskSnapshot | None",
) -> str:
    """Best available one-line 'what is it doing / what does it need'."""
    if wait_header:
        return wait_header
    if native and native.custom_status:
        return native.custom_status
    if task:
        if task.active_task_id:
            for item in task.items:
                if item.task_id == task.active_task_id:
                    return item.active_form or item.subject
        if task.total_count:
            return f"{task.done_count}/{task.total_count} tasks done"
    return ""


async def collect_session_summaries(user_id: int) -> list[SessionSummary]:
    """Aggregate summaries for all of a user's topic-bound sessions, sorted."""
    bindings = thread_router.get_all_thread_windows(user_id)
    if not bindings:
        return []

    all_windows = await tmux_manager.list_windows()
    live_ids = {w.window_id for w in all_windows}
    now = time.monotonic()

    summaries = [
        _summarize_window(user_id, thread_id, window_id, live_ids, now)
        for thread_id, window_id in sorted(bindings.items())
    ]
    return sort_summaries(summaries)


async def capture_excerpt(window_id: str, max_chars: int = EXCERPT_MAX_CHARS) -> str:
    """Deterministic redacted excerpt: last meaningful terminal line.

    Never raises — capture failures degrade to an empty string. Secret
    patterns are redacted with the same regex the shell provider uses
    before terminal text leaves the host.
    """
    try:
        text = await tmux_manager.capture_pane(window_id)
    except Exception:  # noqa: BLE001 — summary must survive backend hiccups
        return ""
    if not text:
        return ""
    # Last line with real content (skips TUI borders / separator glyphs).
    meaningful = [
        line.strip() for line in text.splitlines() if any(ch.isalnum() for ch in line)
    ]
    if not meaningful:
        return ""
    excerpt = redact_for_llm(meaningful[-1])
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 1] + "…"
    return excerpt
