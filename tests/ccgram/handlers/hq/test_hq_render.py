"""Tests for Agent HQ rendering (pure text + keyboard builders)."""

from ccgram.handlers.callback_data import (
    CB_HQ_INTERRUPT,
    CB_HQ_READ,
    CB_HQ_REFRESH,
)
from ccgram.handlers.hq.render import (
    build_needs_you_keyboard,
    build_view_keyboard,
    format_elapsed,
    render_agents,
    render_brief,
    render_needs_you,
    topic_url,
)
from ccgram.handlers.hq.summary import (
    STATE_DEAD,
    STATE_DONE,
    STATE_IDLE,
    STATE_NEEDS_YOU,
    STATE_WORKING,
    SessionSummary,
)


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
        thread_id=7,
        chat_id=-1009876543210,
        name=name,
        provider="claude",
        cwd="/home/u/proj",
        state=state,
        idle_seconds=idle_seconds,
        detail=detail,
        stale=stale,
    )


def _callback_data(keyboard) -> list[str]:
    return [
        btn.callback_data
        for row in keyboard.inline_keyboard
        for btn in row
        if isinstance(btn.callback_data, str)
    ]


class TestFormatElapsed:
    def test_none_and_negative(self) -> None:
        assert format_elapsed(None) == ""
        assert format_elapsed(-5) == ""

    def test_seconds(self) -> None:
        assert format_elapsed(45) == "45s"

    def test_minutes(self) -> None:
        assert format_elapsed(150) == "2m"

    def test_hours(self) -> None:
        assert format_elapsed(3 * 3600 + 5 * 60) == "3h 05m"


class TestTopicUrl:
    def test_supergroup(self) -> None:
        assert topic_url(-1009876543210, 42) == "https://t.me/c/9876543210/42"

    def test_non_supergroup_has_no_link(self) -> None:
        assert topic_url(12345, 42) is None


class TestRenderAgents:
    def test_empty(self) -> None:
        assert "no sessions" in render_agents([])

    def test_shows_name_provider_state_cwd(self) -> None:
        text = render_agents([_summary("wallet", STATE_WORKING)])
        assert "wallet [claude] — working" in text
        assert "/home/u/proj" in text

    def test_shows_detail_and_elapsed(self) -> None:
        text = render_agents(
            [_summary("w", STATE_WORKING, detail="editing template", idle_seconds=120)]
        )
        assert "editing template" in text
        assert "last activity 2m ago" in text

    def test_stale_marker(self) -> None:
        text = render_agents([_summary("w", STATE_WORKING, stale=True)])
        assert "stale" in text


class TestRenderBrief:
    def test_groups_by_state_in_triage_order(self) -> None:
        text = render_brief(
            [
                _summary("done1", STATE_DONE),
                _summary("blocked1", STATE_NEEDS_YOU, detail="US-only or all?"),
                _summary("work1", STATE_WORKING),
                _summary("idle1", STATE_IDLE),
            ]
        )
        assert (
            text.index("Needs you")
            < text.index("Working")
            < text.index("Done")
            < text.index("Idle")
        )
        assert "• blocked1 — US-only or all?" in text

    def test_excerpt_used_when_no_detail(self) -> None:
        text = render_brief([_summary("w", STATE_WORKING)], {"@w": "running pytest…"})
        assert "• w — running pytest…" in text

    def test_empty_groups_omitted(self) -> None:
        text = render_brief([_summary("w", STATE_WORKING)])
        assert "Needs you" not in text
        assert "Dead" not in text


class TestRenderNeedsYou:
    def test_nothing_needs_attention(self) -> None:
        text = render_needs_you([_summary("w", STATE_WORKING)])
        assert "Nobody needs you" in text

    def test_lists_only_attention_sessions(self) -> None:
        text = render_needs_you(
            [
                _summary("blocked1", STATE_NEEDS_YOU, detail="approve plan?"),
                _summary("fine", STATE_WORKING),
                _summary("gone", STATE_DEAD),
            ]
        )
        assert "blocked1 — approve plan?" in text
        assert "gone — dead" in text
        assert "fine" not in text

    def test_stale_shows_inactivity(self) -> None:
        text = render_needs_you(
            [_summary("s", STATE_WORKING, stale=True, idle_seconds=2400)]
        )
        assert "stale" in text
        assert "40m" in text


class TestKeyboards:
    def test_view_keyboard_has_links_and_refresh(self) -> None:
        kb = build_view_keyboard([_summary("wallet", STATE_WORKING)], "agents")
        urls = [
            btn.url for row in kb.inline_keyboard for btn in row if btn.url is not None
        ]
        assert urls == ["https://t.me/c/9876543210/7"]
        assert f"{CB_HQ_REFRESH}agents" in _callback_data(kb)

    def test_needs_you_keyboard_actions(self) -> None:
        kb = build_needs_you_keyboard(
            [_summary("blocked1", STATE_NEEDS_YOU), _summary("ok", STATE_WORKING)]
        )
        data = _callback_data(kb)
        assert f"{CB_HQ_READ}@blocked1" in data
        assert f"{CB_HQ_INTERRUPT}@blocked1" in data
        assert not any("@ok" in d for d in data)

    def test_dead_session_has_no_interrupt(self) -> None:
        kb = build_needs_you_keyboard([_summary("gone", STATE_DEAD)])
        data = _callback_data(kb)
        assert not any(d.startswith(CB_HQ_INTERRUPT) for d in data)
        assert not any(d.startswith(CB_HQ_READ) for d in data)
