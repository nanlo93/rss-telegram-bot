import json
import os
import tempfile
import unittest
from unittest import mock

from rssbot import commands, main
from rssbot.feed import Entry, Feed, FeedError
from rssbot.store import Store
from rssbot.telegram import TelegramError

OWNER = "111"


def feed_of(*ids, title="T"):
    return Feed(title=title, entries=[Entry(id=i, title=f"title-{i}", link=f"https://x/{i}") for i in ids])


class FakeTG:
    def __init__(self, updates=None, fail_sends=0):
        self.updates, self.sent, self.fail_sends, self.commands = updates or [], [], fail_sends, None

    def get_updates(self, offset):
        return [u for u in self.updates if u["update_id"] >= offset]

    def send_message(self, chat_id, text, html=False):
        if self.fail_sends:
            self.fail_sends -= 1
            raise TelegramError("boom")
        self.sent.append((chat_id, text, html))

    def set_commands(self, cmds):
        self.commands = cmds


def msg(uid, text, chat=OWNER):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        p = mock.patch("rssbot.commands.fetch_feed", side_effect=lambda url: feed_of("a", "b", title="Fetched"))
        p.start()
        self.addCleanup(p.stop)

    def test_add_list_edit_remove_flow(self):
        r = commands.handle("/add https://ex.com/rss 내 블로그", self.store)
        self.assertIn("추가했습니다", r)
        self.assertEqual(self.store.items[0]["name"], "내 블로그")
        self.assertEqual(self.store.state["seen"]["1"], ["a", "b"])  # 기존 글은 기준선

        self.assertIn("1. 내 블로그", commands.handle("/list", self.store))
        self.assertIn("이미 구독", commands.handle("/add https://ex.com/rss", self.store))

        commands.handle("/edit 1 name 새 이름", self.store)
        self.assertEqual(self.store.items[0]["name"], "새 이름")

        commands.handle("/edit 1 url https://ex.com/new", self.store)
        self.assertEqual(self.store.items[0]["url"], "https://ex.com/new")

        self.assertIn("삭제했습니다", commands.handle("/remove 1", self.store))
        self.assertEqual(self.store.items, [])
        self.assertNotIn("1", self.store.state["seen"])
        self.assertIn("없습니다", commands.handle("/list", self.store))

    def test_default_name_is_feed_title(self):
        commands.handle("/add https://ex.com/rss", self.store)
        self.assertEqual(self.store.items[0]["name"], "Fetched")

    def test_invalid_inputs(self):
        self.assertIn("URL", commands.handle("/add ftp://x", self.store))
        self.assertIn("사용법", commands.handle("/add", self.store))
        self.assertIn("숫자", commands.handle("/remove abc", self.store))
        self.assertIn("없습니다", commands.handle("/remove 9", self.store))
        self.assertIn("사용법", commands.handle("/edit 1 color red", self.store))
        self.assertIsNone(commands.handle("그냥 메시지", self.store))
        self.assertIn("알 수 없는", commands.handle("/nope", self.store))
        self.assertIn("사용법", commands.handle("/help@rss_pinger_bot", self.store))

    def test_add_reports_fetch_failure_without_saving(self):
        with mock.patch("rssbot.commands.fetch_feed", side_effect=FeedError("404")):
            r = commands.handle("/add https://ex.com/bad", self.store)
        self.assertIn("확인하지 못했습니다", r)
        self.assertEqual(self.store.items, [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "feeds.json"), "w") as f:
            json.dump({"next_id": 2, "feeds": [{"id": 1, "name": "GN", "url": "https://gn/rss"}]}, f)
        p = mock.patch("rssbot.main.time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def run_bot(self, tg, feed):
        with mock.patch("rssbot.main.fetch_feed", return_value=feed):
            main.run(self.tmp.name, "tok", OWNER, tg)

    def state(self):
        return Store(self.tmp.name).state

    def test_first_run_sets_baseline_then_notifies_only_new_oldest_first(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("3", "2", "1"))
        self.assertEqual(tg.sent, [])  # 기준선만
        self.assertIsNotNone(tg.commands)

        self.run_bot(tg, feed_of("5", "4", "3", "2", "1"))
        texts = [t for _, t, _ in tg.sent]
        self.assertEqual(len(texts), 2)
        self.assertIn("title-4", texts[0])  # 오래된 글(4)부터
        self.assertIn("title-5", texts[1])
        self.assertTrue(all(h for _, _, h in tg.sent))

        self.run_bot(tg, feed_of("5", "4", "3", "2", "1"))
        self.assertEqual(len(tg.sent), 2)  # 중복 알림 없음

    def test_html_is_escaped(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("1"))
        evil = Feed("T", [Entry("2", "<b>x</b> & y", 'https://x/?a="1"'), Entry("1", "t", "https://x/1")])
        self.run_bot(tg, evil)
        text = tg.sent[0][1]
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; y", text)
        self.assertIn("&quot;", text)

    def test_failed_send_is_retried_next_run(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("1"))
        tg2 = FakeTG(fail_sends=1)
        self.run_bot(tg2, feed_of("2", "1"))
        self.assertEqual(tg2.sent, [])
        tg3 = FakeTG()
        self.run_bot(tg3, feed_of("2", "1"))
        self.assertEqual(len(tg3.sent), 1)
        self.assertNotIn("1", self.state()["pending"])  # 전송 성공 후 대기열 비움

    def test_failed_send_survives_entry_dropping_out_of_feed(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("1"))
        self.run_bot(FakeTG(fail_sends=99), feed_of("2", "1"))  # 2번 글 전송 실패
        tg3 = FakeTG()
        self.run_bot(tg3, feed_of("3", "1"))  # 다음 응답에서 2번 글이 빠졌더라도
        texts = [t for _, t, _ in tg3.sent]
        self.assertEqual(len(texts), 2)
        self.assertIn("title-2", texts[0])  # 누락 없이 오래된 순으로
        self.assertIn("title-3", texts[1])

    def test_partial_failure_keeps_only_unsent(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("0"))
        tg2 = FakeTG()
        orig = tg2.send_message
        calls = {"n": 0}

        def flaky(chat_id, text, html=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise TelegramError("boom")
            orig(chat_id, text, html)

        tg2.send_message = flaky
        self.run_bot(tg2, feed_of("3", "2", "1", "0"))
        self.assertEqual(len(tg2.sent), 1)  # 1번만 성공
        self.assertEqual([p["id"] for p in self.state()["pending"]["1"]], ["2", "3"])
        tg3 = FakeTG()
        self.run_bot(tg3, feed_of("3", "2", "1", "0"))
        self.assertEqual([t for _, t, _ in tg3.sent if "title-" in t].__len__(), 2)

    def test_summary_failure_loses_nothing(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("0"))
        ids = [str(i) for i in range(15, 0, -1)] + ["0"]
        self.run_bot(FakeTG(fail_sends=99), feed_of(*ids))
        self.assertEqual(len(self.state()["pending"]["1"]), 15)

    def test_flood_is_capped_with_summary(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("0"))
        ids = [str(i) for i in range(15, 0, -1)] + ["0"]
        self.run_bot(tg, feed_of(*ids))
        self.assertEqual(len(tg.sent), main.MAX_PER_FEED + 1)
        self.assertIn("생략", tg.sent[0][1])  # 요약 안내가 먼저
        self.assertIn("title-6", tg.sent[1][1])  # 이어서 최신 10건을 오래된 순으로
        self.assertIn("title-15", tg.sent[-1][1])
        self.assertNotIn("1", self.state()["pending"])

    def test_pending_overflow_is_counted_and_announced_not_silently_lost(self):
        self.run_bot(FakeTG(), feed_of("0"))
        with mock.patch("rssbot.main.MAX_PENDING", 5):
            ids = [str(i) for i in range(12, 0, -1)] + ["0"]
            self.run_bot(FakeTG(fail_sends=99), feed_of(*ids))  # 신규 12건, 전부 전송 실패
            st = self.state()
            self.assertEqual(len(st["pending"]["1"]), 5)
            self.assertEqual(st["dropped"]["1"], 7)

            more = [str(i) for i in range(15, 12, -1)] + ids
            self.run_bot(FakeTG(fail_sends=99), feed_of(*more))  # 3건 더, 또 실패
            st = self.state()
            self.assertEqual(len(st["pending"]["1"]), 5)
            self.assertEqual(st["dropped"]["1"], 10)

            tg = FakeTG()
            self.run_bot(tg, feed_of(*more))  # 복구
        texts = [t for _, t, _ in tg.sent]
        self.assertIn("10건은 알림을 생략", texts[0])
        self.assertEqual(len(texts), 6)  # 요약 1 + 대기열 5건
        st = self.state()
        self.assertNotIn("1", st["pending"])
        self.assertNotIn("1", st["dropped"])

    def test_non_telegram_error_during_send_loses_nothing(self):
        """TimeoutError처럼 TelegramError가 아닌 예외가 나도 미전송 글은 상태에 남고 실행은 계속된다."""
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG()
        tg.send_message = mock.Mock(side_effect=TimeoutError("timed out"))
        self.run_bot(tg, feed_of("3", "2", "1"))  # 예외가 밖으로 새지 않아야 함
        self.assertEqual([p["id"] for p in self.state()["pending"]["1"]], ["2", "3"])

        tg2 = FakeTG()
        self.run_bot(tg2, feed_of("4", "1"))  # 2, 3번 글이 피드에서 빠졌어도
        texts = [t for _, t, _ in tg2.sent]
        self.assertEqual(len(texts), 3)
        self.assertTrue("title-2" in texts[0] and "title-3" in texts[1] and "title-4" in texts[2])

    def test_pending_is_saved_before_sending_even_if_process_is_interrupted(self):
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG()
        tg.send_message = mock.Mock(side_effect=KeyboardInterrupt())  # Exception이 아닌 중단
        with self.assertRaises(KeyboardInterrupt):
            self.run_bot(tg, feed_of("3", "2", "1"))
        self.assertEqual([p["id"] for p in self.state()["pending"]["1"]], ["2", "3"])

    def test_summary_timeout_keeps_queue_and_overflow_count(self):
        self.run_bot(FakeTG(), feed_of("0"))
        ids = [str(i) for i in range(12, 0, -1)] + ["0"]
        tg = FakeTG()
        tg.send_message = mock.Mock(side_effect=TimeoutError("timed out"))
        with mock.patch("rssbot.main.MAX_PENDING", 5):
            self.run_bot(tg, feed_of(*ids))  # 요약 전송이 타임아웃
        st = self.state()
        self.assertEqual(len(st["pending"]["1"]), 5)
        self.assertEqual(st["dropped"]["1"], 7)  # 잘려 나간 건수가 저장됨

    def test_one_broken_feed_does_not_block_others(self):
        feeds_path = os.path.join(self.tmp.name, "feeds.json")
        with open(feeds_path, "w") as f:
            json.dump({"next_id": 3, "feeds": [
                {"id": 1, "name": "BAD", "url": "https://bad/rss"},
                {"id": 2, "name": "OK", "url": "https://ok/rss"}]}, f)

        def fetch(url):
            if "bad" in url:
                raise ValueError("unexpected")
            return feed_of("2", "1") if self.second else feed_of("1")

        self.second = False
        tg = FakeTG()
        with mock.patch("rssbot.main.fetch_feed", side_effect=fetch):
            main.run(self.tmp.name, "tok", OWNER, tg)
            self.second = True
            main.run(self.tmp.name, "tok", OWNER, tg)
        self.assertEqual(len([t for _, t, _ in tg.sent if "title-2" in t]), 1)

    def test_commands_processed_only_from_owner_and_offset_advances(self):
        tg = FakeTG(updates=[msg(10, "/list"), msg(11, "/list", chat="999")])
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            main.run(self.tmp.name, "tok", OWNER, tg)
        replies = [t for _, t, _ in tg.sent]
        self.assertEqual(len(replies), 1)
        self.assertIn("GN", replies[0])
        self.assertEqual(self.state()["update_offset"], 12)

    def test_add_via_command_does_not_notify_existing_posts(self):
        tg = FakeTG(updates=[msg(1, "/add https://new/rss 새피드")])
        with mock.patch("rssbot.commands.fetch_feed", return_value=feed_of("x", "y")), \
             mock.patch("rssbot.main.fetch_feed", return_value=feed_of("x", "y")):
            main.run(self.tmp.name, "tok", OWNER, tg)
        self.assertEqual(len(tg.sent), 1)  # 추가 확인 응답만
        self.assertEqual([f["name"] for f in Store(self.tmp.name).items], ["GN", "새피드"])

    def test_repeated_failure_warns_once(self):
        tg = FakeTG()
        self.run_bot(tg, feed_of("1"))
        with mock.patch("rssbot.main.fetch_feed", side_effect=FeedError("down")):
            for _ in range(main.FAIL_WARN_AT + 3):
                main.run(self.tmp.name, "tok", OWNER, tg)
        warns = [t for _, t, _ in tg.sent if "연속 확인하지 못했습니다" in t]
        self.assertEqual(len(warns), 1)


if __name__ == "__main__":
    unittest.main()
