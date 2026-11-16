"""Тесты src/mock_telegram.py: API мока, очередь апдейтов, режимы ошибок."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request

import _support  # noqa: F401
from src import mock_telegram


class MockServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = mock_telegram.MockTelegramServer()
        self.port = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def post(self, method: str, payload: dict) -> dict:
        url = "%s/bot123:TEST/%s" % (self.server.url, method)
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_start_returns_port_and_url(self) -> None:
        self.assertIsInstance(self.port, int)
        self.assertGreater(self.port, 0)
        self.assertEqual(self.server.url, "http://127.0.0.1:%d" % self.port)
        self.assertEqual(self.server.start(), self.port, "повторный start идемпотентен")

    def test_get_me_and_delete_webhook(self) -> None:
        me = self.post("getMe", {})
        self.assertTrue(me["ok"])
        self.assertTrue(me["result"]["is_bot"])
        self.assertTrue(self.post("deleteWebhook", {})["ok"])

    def test_push_and_get_updates_offset(self) -> None:
        self.server.push_update("один", chat_id=1)
        self.server.push_update("два", chat_id=2)
        result = self.post("getUpdates", {"timeout": 0})["result"]
        self.assertEqual([u["message"]["text"] for u in result], ["один", "два"])
        self.assertEqual(result[0]["message"]["chat"]["id"], 1)
        second_id = result[1]["update_id"]
        result = self.post("getUpdates", {"offset": second_id + 1, "timeout": 0})["result"]
        self.assertEqual(result, [])
        self.assertEqual(self.server.pending_updates(), [], "offset подтверждает апдейты")

    def test_push_update_with_explicit_id(self) -> None:
        update = self.server.push_update("x", chat_id=5, update_id=100)
        self.assertEqual(update["update_id"], 100)
        result = self.post("getUpdates", {"timeout": 0})["result"]
        self.assertEqual(result[0]["update_id"], 100)

    def test_send_message_accumulates(self) -> None:
        self.post("sendMessage", {"chat_id": 42, "text": "привет"})
        self.post("sendMessage", {"chat_id": 42, "text": "ещё"})
        self.assertEqual(self.server.sent(), ["привет", "ещё"])
        self.assertEqual(len(self.server.sent_messages()), 2)

    def test_form_encoded_payload_accepted(self) -> None:
        url = "%s/bot123:TEST/sendMessage" % self.server.url
        body = "chat_id=42&text=%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82".encode("ascii")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.loads(response.read())["ok"])
        self.assertEqual(self.server.sent(), ["привет"])

    def test_long_poll_wakes_on_push(self) -> None:
        result_holder: list = []

        def poll() -> None:
            result_holder.extend(
                self.post("getUpdates", {"timeout": 20})["result"]
            )

        thread = threading.Thread(target=poll)
        thread.start()
        try:
            self.server.push_update("бум", chat_id=1)
        finally:
            thread.join(timeout=5)
        self.assertEqual([u["message"]["text"] for u in result_holder], ["бум"])

    def test_error_modes(self) -> None:
        for mode, status in (("429", 429), ("409", 409), ("500", 500)):
            with self.subTest(mode=mode):
                self.server.mode = mode
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.post("getUpdates", {"timeout": 0})
                self.assertEqual(ctx.exception.code, status)
                body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertFalse(body["ok"])
                self.assertEqual(body["error_code"], status)
                if status == 429:
                    self.assertEqual(body["parameters"]["retry_after"], 2)
                self.server.mode = None

    def test_wait_sent_timeout_returns_partial(self) -> None:
        started = self.server.wait_sent(timeout=0.2, min_count=1)
        self.assertEqual(started, [])
        self.post("sendMessage", {"chat_id": 1, "text": "раз"})
        self.assertEqual(self.server.wait_sent(timeout=1, min_count=1), ["раз"])

    def test_stop_idempotent(self) -> None:
        self.server.stop()
        self.server.stop()
        self.assertIsNotNone(self.port)


if __name__ == "__main__":
    unittest.main(verbosity=2)
