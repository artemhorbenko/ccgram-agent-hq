"""Tests for voice /tell: deterministic parsing, HQ routing, confirmation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.callback_data import CB_HQ_TELL_CANCEL, CB_HQ_TELL_CONFIRM
from ccgram.handlers.hq.voice_tell import handle_hq_voice, parse_voice_instruction
from ccgram.handlers.user_state import HQ_PENDING_TELL
from ccgram.handlers.voice.voice_handler import handle_voice_message

USER_ID = 100

_NAMES = {"@1": "wallet", "@2": "walrus", "@3": "campaigns wallet"}


@pytest.fixture(autouse=True)
def routers():
    with (
        patch("ccgram.handlers.hq.tell.thread_router") as tell_router,
        patch("ccgram.handlers.hq.voice_tell.thread_router") as voice_router,
    ):
        for router in (tell_router, voice_router):
            router.get_all_thread_windows.return_value = {
                10: "@1",
                20: "@2",
                30: "@3",
            }
            router.get_display_name.side_effect = lambda wid: _NAMES[wid]
        yield tell_router, voice_router


class TestParseVoiceInstruction:
    def test_leading_filler_and_target(self, routers) -> None:
        assert parse_voice_instruction(USER_ID, "tell wallet run the checks") == (
            "@1",
            "wallet",
            "run the checks",
        )

    def test_no_filler(self, routers) -> None:
        assert parse_voice_instruction(USER_ID, "wallet run the checks") == (
            "@1",
            "wallet",
            "run the checks",
        )

    def test_punctuation_after_name(self, routers) -> None:
        assert parse_voice_instruction(USER_ID, "wallet, run the checks") == (
            "@1",
            "wallet",
            "run the checks",
        )

    def test_connector_word_dropped(self, routers) -> None:
        assert parse_voice_instruction(USER_ID, "tell wallet to run tests") == (
            "@1",
            "wallet",
            "run tests",
        )

    def test_multi_word_agent_name(self, routers) -> None:
        window_id, name, instruction = parse_voice_instruction(
            USER_ID, "tell campaigns wallet run template checks"
        )
        assert window_id == "@3"
        assert name == "campaigns wallet"
        assert instruction == "run template checks"

    def test_unresolved_returns_full_text(self, routers) -> None:
        text = "please summarize everything"
        assert parse_voice_instruction(USER_ID, text) == (None, "", text)

    def test_name_without_instruction_unresolved(self, routers) -> None:
        assert parse_voice_instruction(USER_ID, "wallet") == (None, "", "wallet")

    def test_empty_text(self, routers) -> None:
        assert parse_voice_instruction(USER_ID, "") == (None, "", "")


def _voice_update(thread_id: int | None = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.voice = MagicMock()
    update.message.message_thread_id = thread_id
    update.callback_query = None
    return update


def _context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    return context


@pytest.fixture
def hq_voice_deps():
    with (
        patch(
            "ccgram.handlers.voice.voice_handler.transcribe_voice_message",
            new_callable=AsyncMock,
        ) as transcribe,
        patch(
            "ccgram.handlers.hq.voice_tell.safe_reply", new_callable=AsyncMock
        ) as reply,
        patch("ccgram.handlers.hq.voice_tell.log_hq_action") as audit,
    ):
        yield SimpleNamespace(transcribe=transcribe, reply=reply, audit=audit)


class TestHandleHqVoice:
    async def test_resolved_stores_pending_and_previews(
        self, routers, hq_voice_deps
    ) -> None:
        hq_voice_deps.transcribe.return_value = "tell wallet run the checks"
        context = _context()

        await handle_hq_voice(_voice_update(), context)

        assert context.user_data[HQ_PENDING_TELL] == {
            "window_id": "@1",
            "name": "wallet",
            "text": "run the checks",
        }
        text = hq_voice_deps.reply.await_args.args[1]
        assert "Voice instruction" in text
        assert "wallet" in text
        assert "run the checks" in text
        keyboard = hq_voice_deps.reply.await_args.kwargs["reply_markup"]
        data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        assert CB_HQ_TELL_CONFIRM in data
        assert CB_HQ_TELL_CANCEL in data

    async def test_unresolved_shows_transcription_and_hint(
        self, routers, hq_voice_deps
    ) -> None:
        hq_voice_deps.transcribe.return_value = "please summarize everything"
        context = _context()

        await handle_hq_voice(_voice_update(), context)

        assert HQ_PENDING_TELL not in context.user_data
        text = hq_voice_deps.reply.await_args.args[1]
        assert "please summarize everything" in text
        assert "Could not identify" in text
        assert hq_voice_deps.audit.call_args.kwargs["result"] == "unresolved"

    async def test_failed_transcription_stops(self, routers, hq_voice_deps) -> None:
        hq_voice_deps.transcribe.return_value = None
        context = _context()

        await handle_hq_voice(_voice_update(), context)

        hq_voice_deps.reply.assert_not_awaited()
        assert HQ_PENDING_TELL not in context.user_data


class TestVoiceHandlerRouting:
    async def test_hq_topic_routes_to_hq_voice(self, routers) -> None:
        with (
            patch("ccgram.handlers.voice.voice_handler.config") as cfg,
            patch("ccgram.handlers.voice.voice_handler.thread_router") as vh_router,
            patch("ccgram.handlers.hq.is_hq_topic", return_value=True),
            patch(
                "ccgram.handlers.hq.handle_hq_voice", new_callable=AsyncMock
            ) as hq_voice,
        ):
            cfg.is_user_allowed.return_value = True
            await handle_voice_message(_voice_update(), _context())

        hq_voice.assert_awaited_once()
        vh_router.resolve_window_for_thread.assert_not_called()

    async def test_regular_topic_keeps_bound_flow(self, routers) -> None:
        with (
            patch("ccgram.handlers.voice.voice_handler.config") as cfg,
            patch("ccgram.handlers.voice.voice_handler.thread_router") as vh_router,
            patch("ccgram.handlers.hq.is_hq_topic", return_value=False),
            patch(
                "ccgram.handlers.hq.handle_hq_voice", new_callable=AsyncMock
            ) as hq_voice,
            patch(
                "ccgram.handlers.voice.voice_handler.safe_reply",
                new_callable=AsyncMock,
            ) as reply,
        ):
            cfg.is_user_allowed.return_value = True
            vh_router.resolve_window_for_thread.return_value = None

            await handle_voice_message(_voice_update(thread_id=7), _context())

        hq_voice.assert_not_awaited()
        assert reply.await_args is not None
        text = reply.await_args.args[1]
        assert "not bound" in text
