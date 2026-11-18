import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

CHILD = textwrap.dedent("""
    import json, sys, threading, time
    from unittest import mock
    from rssbot import main
    from rssbot.feed import Entry, Feed

    class TG:
        sent = []
        def get_updates(self, offset, limit=100): return []
        def send_message(self, *a, **k): pass
        def set_commands(self, c): pass

    def fetch(url):
        if "hang" in url:
            time.sleep(60)  # 응답 없는 서버
        return Feed("T", [Entry("1", "t", "https://x/1")])

    with mock.patch("rssbot.main.fetch_feed", side_effect=fetch), mock.patch("rssbot.main.COLLECT_BUDGET", 0.5):
        main.run(sys.argv[1], "tok", "1", TG())
""")


class ProcessExitTests(unittest.TestCase):
    def test_hanging_feed_does_not_keep_process_alive(self):
        """수집 시간 한도가 지난 뒤에도 응답 없는 피드 스레드가 CLI 종료를 막으면 안 된다."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "feeds.json"), "w") as f:
                json.dump({"next_id": 3, "feeds": [
                    {"id": 1, "name": "HANG", "url": "https://hang/rss"},
                    {"id": 2, "name": "OK", "url": "https://ok/rss"}]}, f)
            started = time.monotonic()
            r = subprocess.run([sys.executable, "-c", CHILD, d], cwd=ROOT, capture_output=True, timeout=30)
            elapsed = time.monotonic() - started
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertLess(elapsed, 10, f"종료까지 {elapsed:.1f}초")


if __name__ == "__main__":
    unittest.main()
