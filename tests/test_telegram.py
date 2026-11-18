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


if __name__ == "__main__":
    unittest.main()
