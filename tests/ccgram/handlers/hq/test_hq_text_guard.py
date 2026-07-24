"""Tests for the HQ-topic guard in the unbound-topic text flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.text.text_handler import _handle_unbound_topic


@pytest.fixture
def guards():
    with (
        patch("ccgram.handlers.hq.is_hq_topic") as is_hq,
        patch("ccgram.handlers.hq.handle_hq_text", new_callable=AsyncMock) as hq_help,
        patch("ccgram.handlers.text.text_handler.thread_router") as router,
        patch("ccgram.handlers.text.text_handler.tmux_manager") as mux,
    ):
        yield is_hq, hq_help, router, mux


class TestHqTopicTextGuard:
    async def test_hq_topic_shows_help_and_never_binds(self, guards) -> None:
        is_hq, hq_help, router, mux = guards
        is_hq.return_value = True

        handled = await _handle_unbound_topic(1, 42, "hello", {}, MagicMock())

        assert handled is True
        hq_help.assert_awaited_once()
        # The binding flow (window picker / directory browser) never starts.
        router.get_window_for_thread.assert_not_called()
        mux.list_windows.assert_not_called()

    async def test_regular_bound_topic_unaffected(self, guards) -> None:
        is_hq, hq_help, router, _mux = guards
        is_hq.return_value = False
        router.get_window_for_thread.return_value = "@1"

        handled = await _handle_unbound_topic(1, 7, "hello", {}, MagicMock())

        assert handled is False
        hq_help.assert_not_awaited()
