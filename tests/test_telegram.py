"""Тесты src/telegram.py: разбивка сообщений, 429/409/401/500, getUpdates/offset."""

from __future__ import annotations

import threading
import time
import unittest

import _support  # noqa: F401
from src import mock_telegram, telegram

TOKEN = "123456:TEST-TOKEN"


class SplitMessageTestCase(unittest.TestCase):
    def test_short_and_empty(self) -> None:
        self.assertEqual(telegram.split_message(""), [])
        self.assertEqual(telegram.split_message("привет"), ["привет"])
        self.assertEqual(telegram.split_message("x" * 4000), ["x" * 4000])

    def test_long_text_split_by_lines(self) -> None:
        text = "\n".join("строка %02d %s" % (i, "y" * 120) for i in range(100))
        parts = telegram.split_message(text)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), telegram.MAX_MESSAGE_LEN)
        # склейка обратно даёт тот же текст (строки не теряются)
        self.assertEqual("\n".join(parts), text)

    def test_single_line_without_breaks_hard_split(self) -> None:
        parts = telegram.split_message("z" * 9000)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(part) <= 4000 for part in parts))
        self.assertEqual("".join(parts), "z" * 9000)

    def test_custom_limit(self) -> None:
        self.assertEqual(telegram.split_message("abcdef", limit=3), ["abc", "def"])


class TelegramClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = mock_telegram.MockTelegramServer(token=TOKEN)
        self.server.start()
        self.tg = telegram.Telegram(TOKEN, api_base=self.server.url)

    def tearDown(self) -> None:
        self.server.stop()

    # --------------------------------------------------------------- база
    def test_empty_token_rejected(self) -> None:
        with self.assertRaises(telegram.TelegramError):
            telegram.Telegram("")
        with self.assertRaises(telegram.TelegramError):
            telegram.Telegram("   ")

    def test_repr_hides_token(self) -> None:
        self.assertNotIn(TOKEN, repr(self.tg))

    def test_call_returns_result(self) -> None:
        me = self.tg.call("getMe")
        self.assertTrue(me["is_bot"])
        self.assertIn("username", me)

    # -------------------------------------------------------------- отправка
    def test_send_records_text(self) -> None:
        self.tg.send(42, "привет")
        self.assertEqual(self.server.sent(), ["привет"])
        self.assertEqual(self.server.sent_messages()[0]["chat_id"], 42)

    def test_send_empty_ignored(self) -> None:
        self.tg.send(42, "")
        self.tg.send(42, "   ")
        self.assertEqual(self.server.sent(), [])

    def test_send_long_message_split(self) -> None:
        self.tg.send(42, "a" * 9000)
        self.assertEqual([len(text) for text in self.server.sent()], [4000, 4000, 1000])

    # ------------------------------------------------------- long polling
    def test_get_updates_and_offset(self) -> None:
        first = self.server.push_update("один", chat_id=7)
        second = self.server.push_update("два", chat_id=7)
        updates = self.tg.get_updates(None, timeout=0)
        self.assertEqual([u["update_id"] for u in updates],
                         [first["update_id"], second["update_id"]])
        self.assertEqual(updates[0]["message"]["text"], "один")
        # offset подтверждает первый
        rest = self.tg.get_updates(first["update_id"] + 1, timeout=0)
        self.assertEqual([u["message"]["text"] for u in rest], ["два"])
        # подтверждение второго → очередь пуста
        self.assertEqual(self.tg.get_updates(second["update_id"] + 1, timeout=0), [])
        self.assertEqual(self.server.pending_updates(), [])

    def test_get_updates_long_poll_wakes_up(self) -> None:
        def later() -> None:
            time.sleep(0.2)
            self.server.push_update("внезапно", chat_id=7)

        thread = threading.Thread(target=later)
        thread.start()
        started = time.monotonic()
        try:
            updates = self.tg.get_updates(None, timeout=20)  # мок ограничивает 1.5с
        finally:
            thread.join()
        elapsed = time.monotonic() - started
        self.assertEqual([u["message"]["text"] for u in updates], ["внезапно"])
        self.assertLess(elapsed, 2.0, "long-poll должен проснуться при push_update")

    # ------------------------------------------------------------- ошибки
    def test_429_retry_after(self) -> None:
        self.server.mode = "429"
        with self.assertRaises(telegram.TelegramError) as ctx:
            self.tg.get_updates(None, timeout=0)
        error = ctx.exception
        self.assertEqual(error.status, 429)
        self.assertEqual(error.retry_after, 2.0)
        self.assertIn("429", str(error))

    def test_409_conflict(self) -> None:
        self.server.mode = "409"
        with self.assertRaises(telegram.TelegramError) as ctx:
            self.tg.get_updates(None, timeout=0)
        self.assertEqual(ctx.exception.status, 409)

    def test_401_unauthorized(self) -> None:
        self.server.mode = "401"
        with self.assertRaises(telegram.TelegramError) as ctx:
            self.tg.send(42, "текст")
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn(TOKEN, str(ctx.exception), "токен не должен утекать в текст ошибки")

    def test_500_generic_error(self) -> None:
        self.server.mode = "500"
        with self.assertRaises(telegram.TelegramError) as ctx:
            self.tg.call("getMe")
        self.assertEqual(ctx.exception.status, 500)

    def test_network_error_wrapped(self) -> None:
        # заведомо закрытый порт: соединение отклонено
        broken = telegram.Telegram(TOKEN, api_base="http://127.0.0.1:1")
        started = time.monotonic()
        with self.assertRaises(telegram.TelegramError):
            broken.get_updates(None, timeout=0)
        self.assertLess(time.monotonic() - started, 5.0)


class TelegramErrorTestCase(unittest.TestCase):
    def test_attributes_and_str(self) -> None:
        error = telegram.TelegramError("беда", status=429, retry_after=3.5)
        self.assertEqual(str(error), "беда")
        self.assertEqual(error.status, 429)
        self.assertEqual(error.retry_after, 3.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
