"""Tests for Agent HQ summary aggregation (classification, ordering, excerpts)."""

from unittest.mock import AsyncMock, patch

import pytest

from ccgram.handlers.hq.summary import (
    STATE_DEAD,
    STATE_DONE,
    STATE_IDLE,
    STATE_NEEDS_YOU,
    STATE_WORKING,
    SessionSummary,
    capture_excerpt,
    classify_state,
    sort_summaries,
)


def _classify(
    *,
    alive: bool = True,
    wait_header: str | None = None,
    native_state: str | None = None,
    has_active_task: bool = False,
    open_count: int | None = None,
    done_count: int | None = None,
    idle_seconds: float | None = None,
) -> str:
    return classify_state(
        alive=alive,
        wait_header=wait_header,
        native_state=native_state,
        has_active_task=has_active_task,
        open_count=open_count,
        done_count=done_count,
        idle_seconds=idle_seconds,
    )


class TestClassifyState:
    def test_dead_window_wins_over_everything(self) -> None:
        assert (
            _classify(alive=False, wait_header="Waiting", native_state="working")
            == STATE_DEAD
        )

    def test_wait_header_means_needs_you(self) -> None:
        assert _classify(wait_header="Plan approval needed") == STATE_NEEDS_YOU

    def test_native_blocked_means_needs_you(self) -> None:
        assert _classify(native_state="blocked") == STATE_NEEDS_YOU

    def test_wait_header_beats_native_working(self) -> None:
        assert (
            _classify(wait_header="Waiting for input", native_state="working")
            == STATE_NEEDS_YOU
        )

    def test_native_working(self) -> None:
        assert _classify(native_state="working") == STATE_WORKING

    def test_native_done(self) -> None:
        assert _classify(native_state="done") == STATE_DONE

    def test_active_task_means_working(self) -> None:
        assert _classify(has_active_task=True) == STATE_WORKING

    def test_recent_activity_means_working(self) -> None:
        assert _classify(idle_seconds=30.0) == STATE_WORKING

    def test_old_activity_is_idle(self) -> None:
        assert _classify(idle_seconds=600.0) == STATE_IDLE

    def test_all_tasks_done_means_done(self) -> None:
        assert _classify(open_count=0, done_count=3, idle_seconds=600.0) == STATE_DONE

    def test_no_signals_is_idle(self) -> None:
        assert _classify() == STATE_IDLE

    def test_native_idle_is_idle(self) -> None:
        assert _classify(native_state="idle") == STATE_IDLE


def _summary(
    name: str,
    state: str,
    *,
    idle_seconds: float | None = None,
    detail: str = "",
    stale: bool = False,
) -> SessionSummary:
    return SessionSummary(
        window_id=f"@{name}",
        thread_id=1,
        chat_id=-1001,
        name=name,
        provider="claude",
        cwd="/p",
        state=state,
        idle_seconds=idle_seconds,
        detail=detail,
        stale=stale,
    )


class TestSortSummaries:
    def test_triage_order(self) -> None:
        summaries = [
            _summary("idle1", STATE_IDLE),
            _summary("done1", STATE_DONE),
            _summary("work1", STATE_WORKING),
            _summary("dead1", STATE_DEAD),
            _summary("blocked1", STATE_NEEDS_YOU),
        ]
        ordered = [s.state for s in sort_summaries(summaries)]
        assert ordered == [
            STATE_NEEDS_YOU,
            STATE_DEAD,
            STATE_WORKING,
            STATE_DONE,
            STATE_IDLE,
        ]

    def test_alphabetical_within_state(self) -> None:
        summaries = [
            _summary("zeta", STATE_WORKING),
            _summary("Alpha", STATE_WORKING),
        ]
        assert [s.name for s in sort_summaries(summaries)] == ["Alpha", "zeta"]


class TestNeedsAttention:
    def test_needs_you_and_dead_need_attention(self) -> None:
        assert _summary("a", STATE_NEEDS_YOU).needs_attention
        assert _summary("a", STATE_DEAD).needs_attention

    def test_stale_working_needs_attention(self) -> None:
        assert _summary("a", STATE_WORKING, stale=True).needs_attention

    def test_healthy_states_do_not(self) -> None:
        assert not _summary("a", STATE_WORKING).needs_attention
        assert not _summary("a", STATE_DONE).needs_attention
        assert not _summary("a", STATE_IDLE).needs_attention


class TestCaptureExcerpt:
    @pytest.fixture(autouse=True)
    def _patch_mux(self):
        with patch("ccgram.handlers.hq.summary.tmux_manager") as mux:
            yield mux

    async def test_takes_last_meaningful_line(self, _patch_mux) -> None:
        _patch_mux.capture_pane = AsyncMock(
            return_value="running tests\nAll 12 passed\n└──────────┘\n"
        )
        assert await capture_excerpt("@1") == "All 12 passed"

    async def test_redacts_secrets(self, _patch_mux) -> None:
        _patch_mux.capture_pane = AsyncMock(
            return_value="export API_KEY=super-secret-value-123\n"
        )
        excerpt = await capture_excerpt("@1")
        assert "super-secret-value-123" not in excerpt
        assert "[REDACTED]" in excerpt

    async def test_empty_capture(self, _patch_mux) -> None:
        _patch_mux.capture_pane = AsyncMock(return_value=None)
        assert await capture_excerpt("@1") == ""

    async def test_capture_error_degrades_to_empty(self, _patch_mux) -> None:
        _patch_mux.capture_pane = AsyncMock(side_effect=RuntimeError("boom"))
        assert await capture_excerpt("@1") == ""

    async def test_truncates_long_lines(self, _patch_mux) -> None:
        _patch_mux.capture_pane = AsyncMock(return_value="x" * 500)
        excerpt = await capture_excerpt("@1", max_chars=50)
        assert len(excerpt) == 50
        assert excerpt.endswith("…")
