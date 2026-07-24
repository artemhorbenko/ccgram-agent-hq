"""Tests for Agent HQ commands: topic gating, authorization, views, callbacks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.callback_data import (
    CB_HQ_INTERRUPT,
    CB_HQ_INTERRUPT_CONFIRM,
    CB_HQ_REFRESH,
)
from ccgram.handlers.hq.hq_commands import (
    _dispatch,
    agents_command,
    brief_command,
    is_hq_topic,
    needs_you_command,
)
from ccgram.handlers.hq.summary import STATE_NEEDS_YOU, SessionSummary

HQ_THREAD = 42
USER_ID = 100


def _summary(name: str = "wallet", state: str = STATE_NEEDS_YOU) -> SessionSummary:
    return SessionSummary(
        window_id="@1",
        thread_id=7,
        chat_id=-1001234,
        name=name,
        provider="claude",
        cwd="/p",
        state=state,
    )


def _update(thread_id: int | None = HQ_THREAD, user_id: int = USER_ID) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.message_thread_id = thread_id
    update.message.text = "/agents"
    update.callback_query = None
    return update


def _callback_update(data: str, user_id: int = USER_ID) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message = None
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


@pytest.fixture(autouse=True)
def deps():
    with (
        patch("ccgram.handlers.hq.hq_commands.config") as cfg,
        patch(
            "ccgram.handlers.hq.hq_commands.forward_command_handler",
            new_callable=AsyncMock,
        ) as forward,
        patch(
            "ccgram.handlers.hq.hq_commands.safe_reply", new_callable=AsyncMock
        ) as reply,
        patch(
            "ccgram.handlers.hq.hq_commands.safe_edit", new_callable=AsyncMock
        ) as edit,
        patch(
            "ccgram.handlers.hq.hq_commands.collect_session_summaries",
            new_callable=AsyncMock,
        ) as collect,
        patch("ccgram.handlers.hq.hq_commands.log_hq_action") as audit,
        patch("ccgram.handlers.hq.hq_commands.thread_router") as router,
        patch("ccgram.handlers.hq.hq_commands.tmux_manager") as mux,
        patch("ccgram.handlers.hq.hq_commands.user_owns_window") as owns,
    ):
        cfg.hq_topic_id = HQ_THREAD
        cfg.is_user_allowed.return_value = True
        collect.return_value = []
        router.get_display_name.side_effect = lambda wid: "wallet"
        mux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@1"))
        mux.send_keys = AsyncMock(return_value=True)
        owns.return_value = True
        yield SimpleNamespace(
            cfg=cfg,
            forward=forward,
            reply=reply,
            edit=edit,
            collect=collect,
            audit=audit,
            mux=mux,
            owns=owns,
        )


class TestIsHqTopic:
    def test_matches_configured_topic(self, deps) -> None:
        assert is_hq_topic(HQ_THREAD)

    def test_other_topic(self, deps) -> None:
        assert not is_hq_topic(99)

    def test_disabled_feature(self, deps) -> None:
        deps.cfg.hq_topic_id = None
        assert not is_hq_topic(HQ_THREAD)
        assert not is_hq_topic(None)


class TestTopicGating:
    async def test_outside_hq_delegates_to_forward(self, deps) -> None:
        await agents_command(_update(thread_id=99), MagicMock())
        deps.forward.assert_awaited_once()
        deps.reply.assert_not_awaited()
        deps.collect.assert_not_awaited()

    async def test_disabled_feature_delegates(self, deps) -> None:
        deps.cfg.hq_topic_id = None
        await brief_command(_update(), MagicMock())
        deps.forward.assert_awaited_once()
        deps.reply.assert_not_awaited()

    async def test_in_hq_topic_handles(self, deps) -> None:
        await agents_command(_update(), MagicMock())
        deps.forward.assert_not_awaited()
        deps.reply.assert_awaited_once()


class TestAuthorization:
    async def test_unauthorized_user_rejected(self, deps) -> None:
        deps.cfg.is_user_allowed.return_value = False
        await agents_command(_update(), MagicMock())
        deps.collect.assert_not_awaited()
        text = deps.reply.await_args.args[1]
        assert "not authorized" in text

    async def test_unauthorized_never_reaches_audit(self, deps) -> None:
        deps.cfg.is_user_allowed.return_value = False
        await needs_you_command(_update(), MagicMock())
        deps.audit.assert_not_called()


class TestViewCommands:
    async def test_agents_renders_and_audits(self, deps) -> None:
        deps.collect.return_value = [_summary()]
        await agents_command(_update(), MagicMock())
        text = deps.reply.await_args.args[1]
        assert "Agent HQ" in text
        assert "wallet" in text
        assert deps.audit.call_args.kwargs["command"] == "agents"

    async def test_needs_you_renders_and_audits(self, deps) -> None:
        deps.collect.return_value = [_summary()]
        await needs_you_command(_update(), MagicMock())
        text = deps.reply.await_args.args[1]
        assert "Needs you" in text
        assert deps.audit.call_args.kwargs["command"] == "needs_you"

    async def test_brief_renders(self, deps) -> None:
        deps.collect.return_value = [_summary()]
        await brief_command(_update(), MagicMock())
        text = deps.reply.await_args.args[1]
        assert "Agent HQ — 1 session" in text


class TestCallbacks:
    async def test_refresh_rebuilds_view(self, deps) -> None:
        update = _callback_update(f"{CB_HQ_REFRESH}agents")
        await _dispatch(update, MagicMock())
        deps.edit.assert_awaited_once()
        update.callback_query.answer.assert_awaited()

    async def test_interrupt_shows_confirmation(self, deps) -> None:
        update = _callback_update(f"{CB_HQ_INTERRUPT}@1")
        await _dispatch(update, MagicMock())
        deps.mux.send_keys.assert_not_awaited()
        text = deps.edit.await_args.args[1]
        assert "Interrupt" in text

    async def test_interrupt_confirm_sends_escape(self, deps) -> None:
        update = _callback_update(f"{CB_HQ_INTERRUPT_CONFIRM}@1")
        await _dispatch(update, MagicMock())
        deps.mux.send_keys.assert_awaited_once_with(
            "@1", "Escape", enter=False, literal=False
        )
        assert deps.audit.call_args.kwargs["command"] == "interrupt"

    async def test_interrupt_requires_ownership(self, deps) -> None:
        deps.owns.return_value = False
        update = _callback_update(f"{CB_HQ_INTERRUPT_CONFIRM}@1")
        await _dispatch(update, MagicMock())
        deps.mux.send_keys.assert_not_awaited()
        update.callback_query.answer.assert_awaited_with(
            "Not your session", show_alert=True
        )
