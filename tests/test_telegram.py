import io
import unittest
import urllib.error
from unittest import mock

from rssbot.telegram import Telegram, TelegramError


class NetworkErrorTests(unittest.TestCase):
    """URLError가 아닌 타임아웃·연결 오류도 TelegramError로 변환되어야 한다."""

    def call(self, exc):
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            with self.assertRaises(TelegramError) as ctx:
                Telegram("123:SECRET").send_message("1", "hi")
        return str(ctx.exception)

    def test_timeout(self):
        self.assertIn("TimeoutError", self.call(TimeoutError("timed out")))

    def test_connection_reset(self):
        self.call(ConnectionResetError())

    def test_url_error_reason(self):
        self.assertIn("refused", self.call(urllib.error.URLError("refused")))

    def test_invalid_json_body(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b"<html>not json</html>")
        with mock.patch("urllib.request.urlopen", return_value=resp):
            with self.assertRaises(TelegramError):
                Telegram("123:SECRET").send_message("1", "hi")

    def test_token_never_in_error_message(self):
        self.assertNotIn("SECRET", self.call(TimeoutError("https://api.telegram.org/bot123:SECRET/sendMessage timed out")))


class ErrorClassificationTests(unittest.TestCase):
    def call(self, code, description):
        body = ('{"ok": false, "error_code": %d, "description": "%s"}' % (code, description)).encode()
        err = urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(TelegramError) as ctx:
                Telegram("1:T").send_message("1", "hi")
        return ctx.exception

    def test_content_errors_are_permanent(self):
        self.assertTrue(self.call(400, "Bad Request: can't parse entities: Unsupported start tag").permanent)
        self.assertTrue(self.call(400, "Bad Request: message is too long").permanent)
        self.assertTrue(self.call(400, "Bad Request: message text is empty").permanent)

    def test_chat_level_errors_are_not_permanent(self):
        self.assertFalse(self.call(400, "Bad Request: chat not found").permanent)
        self.assertFalse(self.call(400, "Bad Request: have no rights to send a message").permanent)
        self.assertFalse(self.call(400, "Bad Request: PEER_ID_INVALID").permanent)
        self.assertFalse(self.call(400, "Bad Request: chat_id is empty").permanent)

    def test_other_codes_are_not_permanent(self):
        self.assertFalse(self.call(403, "Forbidden: bot was blocked by the user").permanent)
        self.assertFalse(self.call(401, "Unauthorized").permanent)
        self.assertFalse(self.call(500, "Internal Server Error").permanent)


if __name__ == "__main__":
    unittest.main()
