"""Tests for the HQ notifier: transitions, dedup, priming, digest, wiring."""

import asyncio
import contextlib
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.hq import notifier
from ccgram.handlers.hq.notifier import (
    maybe_send_daily_digest,
    notify_state,
    tick_hq_notifications,
)
from ccgram.handlers.hq.summary import (
    STATE_DEAD,
    STATE_DONE,
    STATE_IDLE,
    STATE_NEEDS_YOU,
    STATE_WORKING,
    SessionSummary,
)

USER_ID = 100
HQ_THREAD = 42
CHAT_ID = -1001234567890


def _summary(
    name: str = "wallet",
    state: str = STATE_NEEDS_YOU,
    *,
    stale: bool = False,
    detail: str = "",
) -> SessionSummary:
    return SessionSummary(
        window_id=f"@{name}",
        thread_id=7,
        chat_id=CHAT_ID,
        name=name,
        provider="claude",
        cwd="/p",
        state=state,
        detail=detail,
        stale=stale,
    )


@pytest.fixture(autouse=True)
def deps():
    notifier.reset_for_testing()
    with (
        patch("ccgram.handlers.hq.notifier.config") as cfg,
        patch("ccgram.handlers.hq.notifier.thread_router") as router,
        patch(
            "ccgram.handlers.hq.notifier.collect_session_summaries",
            new_callable=AsyncMock,
        ) as collect,
        patch(
            "ccgram.handlers.hq.notifier.rate_limit_send_message",
            new_callable=AsyncMock,
        ) as send,
        patch("ccgram.handlers.hq.notifier.log_hq_action") as audit,
    ):
        cfg.hq_topic_id = HQ_THREAD
        cfg.hq_digest_time = None
        router.thread_bindings = {USER_ID: {7: "@wallet"}}
        router.group_chat_ids = {f"{USER_ID}:{HQ_THREAD}": CHAT_ID}
        collect.return_value = []
        yield SimpleNamespace(
            cfg=cfg, router=router, collect=collect, send=send, audit=audit
        )
    notifier.reset_for_testing()


class TestNotifyState:
    def test_attention_states_notify(self) -> None:
        assert notify_state(_summary(state=STATE_NEEDS_YOU)) == STATE_NEEDS_YOU
        assert notify_state(_summary(state=STATE_DEAD)) == STATE_DEAD
        assert notify_state(_summary(state=STATE_DONE)) == STATE_DONE
        assert notify_state(_summary(state=STATE_WORKING, stale=True)) == "stale"

    def test_calm_states_do_not(self) -> None:
        assert notify_state(_summary(state=STATE_WORKING)) == ""
        assert notify_state(_summary(state=STATE_IDLE)) == ""


class TestTickNotifications:
    async def test_first_tick_primes_silently(self, deps) -> None:
        deps.collect.return_value = [_summary(state=STATE_NEEDS_YOU)]
        await tick_hq_notifications(MagicMock())
        deps.send.assert_not_awaited()

    async def test_transition_notifies_once(self, deps) -> None:
        client = MagicMock()
        deps.collect.return_value = [_summary(state=STATE_WORKING)]
        await tick_hq_notifications(client)  # prime

        deps.collect.return_value = [
            _summary(state=STATE_NEEDS_YOU, detail="approve plan?")
        ]
        await tick_hq_notifications(client)
        deps.send.assert_awaited_once()
        text = deps.send.await_args.args[2]
        assert "wallet" in text
        assert "approve plan?" in text
        assert deps.send.await_args.kwargs["message_thread_id"] == HQ_THREAD
        assert deps.audit.call_args.kwargs["command"] == "notify"

        # Same state again → no repeat notification.
        await tick_hq_notifications(client)
        deps.send.assert_awaited_once()

    async def test_recovery_then_reblock_notifies_again(self, deps) -> None:
        client = MagicMock()
        deps.collect.return_value = [_summary(state=STATE_WORKING)]
        await tick_hq_notifications(client)  # prime

        deps.collect.return_value = [_summary(state=STATE_NEEDS_YOU)]
        await tick_hq_notifications(client)
        deps.collect.return_value = [_summary(state=STATE_WORKING)]
        await tick_hq_notifications(client)  # calm — no message
        deps.collect.return_value = [_summary(state=STATE_NEEDS_YOU)]
        await tick_hq_notifications(client)
        assert deps.send.await_count == 2

    async def test_stale_notification_text(self, deps) -> None:
        client = MagicMock()
        deps.collect.return_value = [_summary(state=STATE_WORKING)]
        await tick_hq_notifications(client)  # prime
        deps.collect.return_value = [
            SessionSummary(
                window_id="@wallet",
                thread_id=7,
                chat_id=CHAT_ID,
                name="wallet",
                provider="claude",
                cwd="/p",
                state=STATE_WORKING,
                idle_seconds=2400.0,
                stale=True,
            )
        ]
        await tick_hq_notifications(client)
        text = deps.send.await_args.args[2]
        assert "stale" in text
        assert "40m" in text

    async def test_no_recorded_chat_id_skips_send(self, deps) -> None:
        deps.router.group_chat_ids = {}
        client = MagicMock()
        deps.collect.return_value = [_summary(state=STATE_WORKING)]
        await tick_hq_notifications(client)  # prime
        deps.collect.return_value = [_summary(state=STATE_NEEDS_YOU)]
        await tick_hq_notifications(client)
        deps.send.assert_not_awaited()

    async def test_feature_disabled_is_noop(self, deps) -> None:
        deps.cfg.hq_topic_id = None
        deps.collect.return_value = [_summary(state=STATE_NEEDS_YOU)]
        await tick_hq_notifications(MagicMock())
        deps.collect.assert_not_awaited()

    async def test_unbound_windows_pruned_from_tracking(self, deps) -> None:
        client = MagicMock()
        deps.collect.return_value = [_summary(state=STATE_NEEDS_YOU)]
        await tick_hq_notifications(client)
        assert "@wallet" in notifier._last_notified

        deps.collect.return_value = []
        await tick_hq_notifications(client)
        assert "@wallet" not in notifier._last_notified


class TestDailyDigest:
    @pytest.fixture(autouse=True)
    def digest_deps(self, deps):
        deps.cfg.hq_digest_time = time(9, 0)
        with patch(
            "ccgram.handlers.hq.hq_commands._build_view", new_callable=AsyncMock
        ) as build:
            build.return_value = ("brief text", MagicMock())
            yield SimpleNamespace(build=build)

    async def test_not_before_configured_time(self, deps, digest_deps) -> None:
        await maybe_send_daily_digest(MagicMock(), now=datetime(2026, 7, 24, 8, 0))
        deps.send.assert_not_awaited()

    async def test_sends_once_after_time(self, deps, digest_deps) -> None:
        client = MagicMock()
        await maybe_send_daily_digest(client, now=datetime(2026, 7, 24, 9, 30))
        deps.send.assert_awaited_once()
        text = deps.send.await_args.args[2]
        assert "Daily digest" in text
        assert "brief text" in text

        # Second call the same day: already sent.
        await maybe_send_daily_digest(client, now=datetime(2026, 7, 24, 18, 0))
        deps.send.assert_awaited_once()

    async def test_sends_again_next_day(self, deps, digest_deps) -> None:
        client = MagicMock()
        await maybe_send_daily_digest(client, now=datetime(2026, 7, 24, 9, 30))
        await maybe_send_daily_digest(client, now=datetime(2026, 7, 25, 9, 30))
        assert deps.send.await_count == 2

    async def test_disabled_when_no_time_configured(self, deps, digest_deps) -> None:
        deps.cfg.hq_digest_time = None
        await maybe_send_daily_digest(MagicMock(), now=datetime(2026, 7, 24, 9, 30))
        deps.send.assert_not_awaited()

    async def test_skips_users_without_recorded_chat(self, deps, digest_deps) -> None:
        deps.router.group_chat_ids = {}
        await maybe_send_daily_digest(MagicMock(), now=datetime(2026, 7, 24, 9, 30))
        deps.send.assert_not_awaited()


class TestBootstrapWiring:
    async def test_disabled_feature_returns_none(self) -> None:
        from ccgram import bootstrap

        with patch("ccgram.bootstrap.config") as cfg:
            cfg.hq_topic_id = None
            cfg.hq_notify_interval = 30.0
            assert bootstrap.start_hq_notifier(MagicMock()) is None

    async def test_zero_interval_returns_none(self) -> None:
        from ccgram import bootstrap

        with patch("ccgram.bootstrap.config") as cfg:
            cfg.hq_topic_id = HQ_THREAD
            cfg.hq_notify_interval = 0.0
            assert bootstrap.start_hq_notifier(MagicMock()) is None

    async def test_enabled_spawns_task(self) -> None:
        from ccgram import bootstrap

        async def _forever(_client) -> None:
            await asyncio.sleep(3600)

        with (
            patch("ccgram.bootstrap.config") as cfg,
            patch("ccgram.handlers.hq.notifier.hq_notify_loop", new=_forever),
        ):
            cfg.hq_topic_id = HQ_THREAD
            cfg.hq_notify_interval = 30.0
            task = bootstrap.start_hq_notifier(MagicMock())
        try:
            assert task is not None
            assert not task.done()
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            bootstrap._hq_notify_task = None
