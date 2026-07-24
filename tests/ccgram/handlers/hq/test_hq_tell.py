"""Tests for /tell: target resolution, confirmation flow, delivery, audit."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.callback_data import CB_HQ_TELL_CANCEL, CB_HQ_TELL_CONFIRM
from ccgram.handlers.hq.tell import _dispatch, resolve_agent, tell_command
from ccgram.handlers.user_state import HQ_PENDING_TELL

USER_ID = 100


@pytest.fixture(autouse=True)
def deps():
    with (
        patch("ccgram.handlers.hq.tell.thread_router") as router,
        patch("ccgram.handlers.hq.tell.send_to_window", new_callable=AsyncMock) as send,
        patch("ccgram.handlers.hq.tell.safe_reply", new_callable=AsyncMock) as reply,
        patch("ccgram.handlers.hq.tell.safe_edit", new_callable=AsyncMock) as edit,
        patch("ccgram.handlers.hq.tell.log_hq_action") as audit,
        patch("ccgram.handlers.hq.tell.user_owns_window") as owns,
        patch(
            "ccgram.handlers.hq.hq_commands._delegate_outside_hq",
            new_callable=AsyncMock,
        ) as delegate,
        patch(
            "ccgram.handlers.hq.hq_commands._authorized", new_callable=AsyncMock
        ) as authorized,
    ):
        names = {"@1": "wallet", "@2": "walrus", "@3": "pulsetto-fix"}
        router.get_all_thread_windows.return_value = {10: "@1", 20: "@2", 30: "@3"}
        router.get_display_name.side_effect = lambda wid: names[wid]
        router.get_thread_for_window.return_value = 10
        router.resolve_chat_id.return_value = -1001234567890
        send.return_value = (True, "Sent to wallet")
        owns.return_value = True
        delegate.return_value = False
        authorized.return_value = True
        yield SimpleNamespace(
            router=router,
            send=send,
            reply=reply,
            edit=edit,
            audit=audit,
            owns=owns,
            delegate=delegate,
        )


class TestResolveAgent:
    def test_exact_match(self, deps) -> None:
        assert resolve_agent(USER_ID, "wallet") == ("@1", [])

    def test_exact_match_case_insensitive(self, deps) -> None:
        assert resolve_agent(USER_ID, "WALLET") == ("@1", [])

    def test_unique_prefix(self, deps) -> None:
        assert resolve_agent(USER_ID, "pul") == ("@3", [])

    def test_ambiguous_prefix_returns_candidates(self, deps) -> None:
        window_id, candidates = resolve_agent(USER_ID, "wal")
        assert window_id is None
        assert candidates == ["wallet", "walrus"]

    def test_unique_substring(self, deps) -> None:
        assert resolve_agent(USER_ID, "fix") == ("@3", [])

    def test_no_match(self, deps) -> None:
        assert resolve_agent(USER_ID, "nothing") == (None, [])


def _update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.message.text = text
    return update


def _context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    return context


def _callback_update(data: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = USER_ID
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


class TestTellCommand:
    async def test_outside_hq_delegates(self, deps) -> None:
        deps.delegate.return_value = True
        context = _context()
        await tell_command(_update("/tell wallet do it"), context)
        deps.reply.assert_not_awaited()
        assert HQ_PENDING_TELL not in context.user_data

    async def test_usage_when_no_instruction(self, deps) -> None:
        await tell_command(_update("/tell wallet"), _context())
        text = deps.reply.await_args.args[1]
        assert "Usage" in text
        assert "wallet" in text  # available agents listed

    async def test_unresolved_target_lists_agents(self, deps) -> None:
        await tell_command(_update("/tell nothing do it"), _context())
        text = deps.reply.await_args.args[1]
        assert "No agent matches" in text
        deps.audit.assert_called_once()
        assert deps.audit.call_args.kwargs["result"] == "unresolved"

    async def test_ambiguous_target_lists_candidates(self, deps) -> None:
        await tell_command(_update("/tell wal do it"), _context())
        text = deps.reply.await_args.args[1]
        assert "matches several" in text
        assert "wallet" in text
        assert "walrus" in text

    async def test_preview_stores_pending_and_does_not_send(self, deps) -> None:
        context = _context()
        await tell_command(_update("/tell wallet run the checks"), context)
        deps.send.assert_not_awaited()
        pending = context.user_data[HQ_PENDING_TELL]
        assert pending == {
            "window_id": "@1",
            "name": "wallet",
            "text": "run the checks",
        }
        text = deps.reply.await_args.args[1]
        assert "Send to" in text
        assert "run the checks" in text
        keyboard = deps.reply.await_args.kwargs["reply_markup"]
        data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
        assert CB_HQ_TELL_CONFIRM in data
        assert CB_HQ_TELL_CANCEL in data


class TestTellConfirmation:
    def _pending_context(self) -> MagicMock:
        context = _context()
        context.user_data[HQ_PENDING_TELL] = {
            "window_id": "@1",
            "name": "wallet",
            "text": "run the checks",
        }
        return context

    async def test_confirm_sends_and_audits(self, deps) -> None:
        context = self._pending_context()
        await _dispatch(_callback_update(CB_HQ_TELL_CONFIRM), context)
        deps.send.assert_awaited_once_with("@1", "run the checks")
        assert HQ_PENDING_TELL not in context.user_data
        assert deps.audit.call_args.kwargs["result"] == "ok"
        text = deps.edit.await_args.args[1]
        assert "Sent to" in text

    async def test_cancel_never_sends(self, deps) -> None:
        context = self._pending_context()
        await _dispatch(_callback_update(CB_HQ_TELL_CANCEL), context)
        deps.send.assert_not_awaited()
        assert HQ_PENDING_TELL not in context.user_data
        text = deps.edit.await_args.args[1]
        assert "Cancelled" in text

    async def test_confirm_without_pending_alerts(self, deps) -> None:
        update = _callback_update(CB_HQ_TELL_CONFIRM)
        await _dispatch(update, _context())
        deps.send.assert_not_awaited()
        update.callback_query.answer.assert_awaited_with(
            "Nothing pending — send /tell again.", show_alert=True
        )

    async def test_confirm_requires_ownership(self, deps) -> None:
        deps.owns.return_value = False
        await _dispatch(_callback_update(CB_HQ_TELL_CONFIRM), self._pending_context())
        deps.send.assert_not_awaited()

    async def test_send_failure_reports_error(self, deps) -> None:
        deps.send.return_value = (False, "Window not found")
        await _dispatch(_callback_update(CB_HQ_TELL_CONFIRM), self._pending_context())
        assert deps.audit.call_args.kwargs["result"] == "error"
        text = deps.edit.await_args.args[1]
        assert "Window not found" in text
