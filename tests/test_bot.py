import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
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

    def get_updates(self, offset, limit=100):
        return [u for u in self.updates if u["update_id"] >= offset][:limit]

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

    def test_urls_with_credentials_are_rejected_because_repo_is_public(self):
        for bad in ("https://user:pw@ex.com/rss", "https://ex.com/rss?token=abc", "https://ex.com/rss?api_key=1",
                    "https://ex.com/rss?Key=1", "https://ex.com/rss?x=1&auth=2"):
            self.assertIn("등록할 수 없습니다", commands.handle(f"/add {bad}", self.store), bad)
        self.assertEqual(self.store.items, [])
        for ok in ("https://ex.com/rss?channel_id=UC123", "https://ex.com/feed?keyword=swift", "https://ex.com/rss"):
            self.assertIn("추가했습니다", commands.handle(f"/add {ok}", self.store), ok)
        self.assertIn("등록할 수 없습니다", commands.handle("/edit 1 url https://ex.com/rss?secret=1", self.store))
        for hidden in ("https://ex.com/rss#access_token=1", "https://ex.com/rss;jsessionid=abc", "https://ex.com/rss?a=1;token=x",
                       "https://ex.com/a;jsessionid=abc/rss"):
            self.assertIn("등록할 수 없습니다", commands.handle(f"/add {hidden}", self.store), hidden)

    def test_error_replies_do_not_leak_urls(self):
        err = FeedError("가져오기 실패: HTTP Error 302 -> https://private.example/rss?x=1 (redirect)")
        with mock.patch("rssbot.commands.fetch_feed", side_effect=err):
            r = commands.handle("/add https://ex.com/rss", self.store)
        self.assertIn("확인하지 못했습니다", r)
        self.assertNotIn("private.example", r)
        self.assertIn("<URL>", r)

    def test_redact_handles_any_scheme_and_case(self):
        for s in ("HTTPS://X.example/?a=1", "see ftp://x.example/y now", "Http://x.example"):
            self.assertNotIn("example", commands.redact(s))

    def test_url_change_keeps_unsent_alerts(self):
        """번호가 밀려 엉뚱한 구독에 적용되더라도 미전송 알림이 사라지면 되돌릴 수 없다."""
        commands.handle("/add https://ex.com/rss 내 블로그", self.store)
        fid = str(self.store.items[0]["id"])
        self.store.state["pending"][fid] = [{"id": "x", "title": "미전송", "link": "https://ex.com/x"}]
        self.store.state["dropped"][fid] = 3
        r = commands.handle("/edit 1 url https://ex.com/new", self.store)
        self.assertIn("이전: https://ex.com/rss", r)
        self.assertIn("/edit <번호> url https://ex.com/rss", r)  # 되돌리기 안내는 번호 확인을 먼저 요구
        self.assertEqual([p["id"] for p in self.store.state["pending"][fid]], ["x"])
        self.assertEqual(self.store.state["dropped"][fid], 3)

    def test_edit_to_same_url_is_noop_and_does_not_swallow_new_posts(self):
        commands.handle("/add https://ex.com/rss", self.store)
        fid = str(self.store.items[0]["id"])
        self.store.state["seen"][fid] = ["old"]
        with mock.patch("rssbot.commands.fetch_feed", side_effect=AssertionError("접속하면 안 됨")):
            r = commands.handle("/edit 1 url https://ex.com/rss", self.store)
        self.assertIn("이미 이 주소", r)
        self.assertEqual(self.store.state["seen"][fid], ["old"])
        self.assertNotIn(fid, self.store.state["rebase"])  # 아무 표시도 남기지 않음

    def test_failed_edit_names_the_target_and_asks_to_check_list_before_resending(self):
        commands.handle("/add https://ex.com/rss 첫째", self.store)
        commands.handle("/add https://ex2.com/rss 둘째", self.store)
        r = commands.handle("/edit 2 url https://ex.com/new", self.store, {})  # 사전 검증 실패 상황
        self.assertIn("2번 '둘째'", r)
        self.assertIn("/list", r)
        self.assertEqual([f["url"] for f in self.store.items], ["https://ex.com/rss", "https://ex2.com/rss"])

    def test_replies_echo_target_and_old_values_so_mistakes_are_noticeable(self):
        commands.handle("/add https://ex.com/rss 첫째", self.store)
        self.assertIn("'첫째'에서 '새이름'", commands.handle("/edit 1 name 새이름", self.store))
        r = commands.handle("/remove 1", self.store)
        self.assertIn("새이름", r)
        self.assertIn("/add https://ex.com/rss 새이름", r)  # 번호 없이 바로 복구할 수 있는 명령

    def test_empty_feeds_are_rejected_on_add_and_edit(self):
        with mock.patch("rssbot.commands.fetch_feed", return_value=Feed("빈 피드", [])):
            r = commands.handle("/add https://ex.com/rss", self.store)
            self.assertIn("글이 하나도 없는", r)
            self.assertEqual(self.store.items, [])
        commands.handle("/add https://ex2.com/rss 둘째", self.store)
        with mock.patch("rssbot.commands.fetch_feed", return_value=Feed("빈 피드", [])):
            r2 = commands.handle("/edit 1 url https://ex.com/new", self.store)
        self.assertIn("글이 하나도 없는", r2)
        self.assertEqual(self.store.items[0]["url"], "https://ex2.com/rss")  # 바뀌지 않음
        self.assertNotIn(str(self.store.items[0]["id"]), self.store.state["rebase"])

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

    def test_pending_is_retried_while_feed_is_down(self):
        self.run_bot(FakeTG(), feed_of("1"))
        self.run_bot(FakeTG(fail_sends=99), feed_of("2", "1"))  # 전송 실패 -> pending
        tg = FakeTG()
        with mock.patch("rssbot.main.fetch_feed", side_effect=FeedError("피드 장애")):
            main.run(self.tmp.name, "tok", OWNER, tg)  # 피드는 죽어 있어도
        self.assertEqual(len(tg.sent), 1)  # 저장해 둔 알림은 재전송된다
        self.assertIn("title-2", tg.sent[0][1])
        self.assertNotIn("1", self.state()["pending"])

    def test_write_ahead_checkpoint_precedes_delivery_and_survives_kill(self):
        self.run_bot(FakeTG(), feed_of("1"))
        snaps = []

        def checkpoint():  # 체크포인트 시점의 디스크 상태 = 저장소에 커밋되는 상태
            dest = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, dest, True)
            shutil.copytree(self.tmp.name, dest, dirs_exist_ok=True)
            snaps.append(dest)

        events = []
        tg = FakeTG()
        orig = tg.send_message
        tg.send_message = lambda *a, **k: (events.append(("send", len(snaps))), orig(*a, **k))[1]
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("2", "1")):
            main.run(self.tmp.name, "tok", OWNER, tg, checkpoint=checkpoint)
        self.assertEqual(len(snaps), 2)
        self.assertTrue(all(n == 2 for _, n in events))  # 전송은 두 체크포인트가 끝난 뒤에만

        # 전송 직전에 러너가 죽었다고 가정: 저장소에는 두 번째 체크포인트 상태만 있다.
        with open(os.path.join(snaps[1], "state.json")) as f:
            self.assertEqual([p["id"] for p in json.load(f)["pending"]["1"]], ["2"])
        tg2 = FakeTG()
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("3", "1")):  # 그 사이 2번 글이 피드에서 밀려남
            main.run(snaps[1], "tok", OWNER, tg2)
        texts = [t for _, t, _ in tg2.sent]
        self.assertEqual(len(texts), 2)
        self.assertTrue("title-2" in texts[0] and "title-3" in texts[1])

    def test_command_is_committed_before_reply_is_sent(self):
        events = []

        def checkpoint():
            with open(os.path.join(self.tmp.name, "feeds.json")) as f:
                events.append(("cp", len(json.load(f)["feeds"])))
            with open(os.path.join(self.tmp.name, "state.json")) as f:
                events.append(("offset", json.load(f)["update_offset"]))

        tg = FakeTG(updates=[msg(5, "/remove 1")])
        orig = tg.send_message
        tg.send_message = lambda *a, **k: (events.append(("send",)), orig(*a, **k))[1]
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            main.run(self.tmp.name, "tok", OWNER, tg, checkpoint=checkpoint)
        first_send = events.index(("send",))
        self.assertIn(("cp", 0), events[:first_send])  # 삭제가 반영된 상태가 먼저 커밋됨
        self.assertIn(("offset", 6), events[:first_send])  # 같은 명령이 재실행되지 않도록 offset도 함께

    def test_command_checkpoint_failure_aborts_run_without_replaying(self):
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG(updates=[msg(5, "/remove 1")])
        ok = None
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("2", "1")):
            ok = main.run(self.tmp.name, "tok", OWNER, tg, checkpoint=mock.Mock(side_effect=RuntimeError("push 실패")))
        self.assertFalse(ok)  # 실패로 보고
        self.assertEqual(len(tg.sent), 1)  # 명령 응답은 보낸다
        self.assertIn("삭제했습니다", tg.sent[0][1])
        st = Store(self.tmp.name)
        self.assertEqual(st.items, [])  # 명령 결과와 offset은 디스크에 남아, 마지막 커밋 재시도가 올릴 수 있음
        self.assertEqual(st.state["update_offset"], 6)
        self.assertEqual(st.state["outbox"], [])

    def test_reply_survives_checkpoint_failure_and_send_failure(self):
        """첫 체크포인트도, 응답 전송도 실패해도 응답은 outbox에 남아 다음 실행에서 전달된다."""
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG(updates=[msg(5, "/list")], fail_sends=99)
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            ok = main.run(self.tmp.name, "tok", OWNER, tg, checkpoint=mock.Mock(side_effect=RuntimeError("push 실패")))
        self.assertFalse(ok)
        self.assertEqual(len(self.state()["outbox"]), 1)
        self.assertEqual(self.state()["update_offset"], 6)  # 명령은 다시 실행되지 않도록 offset 보존

        tg2 = FakeTG()  # 같은 update는 더 오지 않음
        self.run_bot(tg2, feed_of("1"))
        self.assertEqual(len(tg2.sent), 1)
        self.assertIn("GN", tg2.sent[0][1])
        self.assertEqual(self.state()["outbox"], [])

    def test_reply_is_retried_when_telegram_is_down(self):
        self.run_bot(FakeTG(), feed_of("1"))
        self.run_bot(FakeTG(updates=[msg(5, "/list")], fail_sends=99), feed_of("1"))
        self.assertEqual(len(self.state()["outbox"]), 1)
        tg = FakeTG()
        self.run_bot(tg, feed_of("1"))
        self.assertEqual(len(tg.sent), 1)
        self.assertEqual(self.state()["outbox"], [])

    def test_bulk_commands_are_batched_and_no_reply_is_dropped(self):
        """한 번에 몰린 명령은 outbox 여유만큼씩 처리해, 어떤 응답도 한도 때문에 버려지지 않는다."""
        self.run_bot(FakeTG(), feed_of("1"))
        ups = [msg(100 + i, "/list") for i in range(70)]
        down = FakeTG(updates=ups, fail_sends=9999)
        for _ in range(3):
            self.run_bot(down, feed_of("1"))
        self.assertEqual(len(self.state()["outbox"]), main.MAX_OUTBOX)
        self.assertEqual(self.state()["update_offset"], 150)  # 가득 차면 더 가져오지 않음 (offset도 그대로)

        tg = FakeTG(updates=ups)
        for _ in range(4):
            self.run_bot(tg, feed_of("1"))
        self.assertEqual(len(tg.sent), 70)  # 70개 명령 모두 응답됨
        self.assertEqual(self.state()["outbox"], [])

    def test_remote_edit_merged_by_checkpoint_is_not_reverted(self):
        """체크포인트(rebase)로 병합돼 온 원격 변경을 이후 save()가 되돌리면 안 된다."""
        feeds_path = os.path.join(self.tmp.name, "feeds.json")

        def hook():  # 원격에서 구독 이름을 고친 변경이 rebase로 병합돼 들어온 상황
            with open(feeds_path) as f:
                d = json.load(f)
            d["feeds"][0]["name"] = "REMOTE-EDIT"
            with open(feeds_path, "w") as f:
                json.dump(d, f)

        self.run_bot(FakeTG(), feed_of("1"))
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("2", "1")):
            main.run(self.tmp.name, "tok", OWNER, FakeTG(), checkpoint=hook)
        self.assertEqual(Store(self.tmp.name).items[0]["name"], "REMOTE-EDIT")

    def test_remote_edit_is_kept_even_if_push_fails_after_rebase(self):
        """rebase는 끝났는데 push만 실패한 체크포인트에서도, 병합돼 들어온 원격 변경을 되돌리지 않는다."""
        feeds_path = os.path.join(self.tmp.name, "feeds.json")

        def hook():
            with open(feeds_path) as f:
                d = json.load(f)
            d["feeds"][0]["name"] = "REMOTE-EDIT"
            with open(feeds_path, "w") as f:
                json.dump(d, f)
            raise RuntimeError("push 실패")

        self.run_bot(FakeTG(), feed_of("1"))
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("2", "1")):
            ok = main.run(self.tmp.name, "tok", OWNER, FakeTG(), checkpoint=hook)
        self.assertFalse(ok)
        self.assertEqual(Store(self.tmp.name).items[0]["name"], "REMOTE-EDIT")

    def test_permanently_rejected_outbox_item_does_not_block_everything(self):
        self.run_bot(FakeTG(), feed_of("1"))
        with open(os.path.join(self.tmp.name, "state.json")) as f:
            s = json.load(f)
        s["outbox"] = [{"text": "거부될 안내"}, {"text": "정상 안내"}]
        with open(os.path.join(self.tmp.name, "state.json"), "w") as f:
            json.dump(s, f)
        tg = FakeTG()
        orig = tg.send_message

        def picky(chat_id, text, html=False):
            if text == "거부될 안내":
                raise TelegramError("message text is empty", permanent=True)
            orig(chat_id, text, html)

        tg.send_message = picky
        self.run_bot(tg, feed_of("2", "1"))
        texts = [t for _, t, _ in tg.sent]
        self.assertEqual(texts[0], "정상 안내")  # 뒤의 안내와
        self.assertIn("title-2", texts[1])  # 피드 알림이 막히지 않음
        self.assertEqual(self.state()["outbox"], [])

    def test_chat_level_error_on_outbox_keeps_items_and_retries(self):
        self.run_bot(FakeTG(), feed_of("1"))
        self.run_bot(FakeTG(updates=[msg(5, "/list")], fail_sends=99), feed_of("1"))  # permanent=False 오류
        self.assertEqual(len(self.state()["outbox"]), 1)

    def test_feed_warning_never_pushes_out_unsent_command_replies(self):
        self.run_bot(FakeTG(), feed_of("1"))
        sp = os.path.join(self.tmp.name, "state.json")
        with open(sp) as f:
            s = json.load(f)
        s["outbox"] = [{"text": f"응답 {i}"} for i in range(main.MAX_OUTBOX)]
        s["fails"]["1"] = main.FAIL_WARN_AT - 1  # 이번 실패로 경고 시점 도달
        with open(sp, "w") as f:
            json.dump(s, f)
        down = FakeTG(fail_sends=9999)
        with mock.patch("rssbot.main.fetch_feed", side_effect=FeedError("down")):
            main.run(self.tmp.name, "tok", OWNER, down)
        box = self.state()["outbox"]
        self.assertEqual(len(box), main.MAX_OUTBOX)
        self.assertEqual(box[0]["text"], "응답 0")  # 가장 오래된 응답이 그대로 남아 있음
        self.assertFalse(any("연속 확인하지 못했습니다" in b["text"] for b in box))

    def test_hanging_feeds_in_front_do_not_starve_later_feeds(self):
        """응답 없는 피드가 앞에 많아도(일꾼 수보다 많아도) 뒤쪽 정상 피드는 매번 확인되어야 한다."""
        hang_n = 12
        feeds = [{"id": i, "name": f"HANG{i}", "url": f"https://hang{i}/rss"} for i in range(1, hang_n + 1)]
        feeds += [{"id": hang_n + 1, "name": "OK1", "url": "https://ok1/rss"},
                  {"id": hang_n + 2, "name": "OK2", "url": "https://ok2/rss"}]
        with open(os.path.join(self.tmp.name, "feeds.json"), "w") as f:
            json.dump({"next_id": hang_n + 3, "feeds": feeds}, f)
        self.run_bot(FakeTG(), feed_of("1"))  # 전체 기준선

        release = threading.Event()
        self.addCleanup(release.set)

        def fetch(url):
            if "hang" in url:
                release.wait(10)
            return feed_of("2", "1")

        for _ in range(2):  # 매 실행 반복돼도 계속 확인되어야 함
            tg = FakeTG()
            with mock.patch("rssbot.main.fetch_feed", side_effect=fetch), mock.patch("rssbot.main.COLLECT_BUDGET", 0.5):
                main.run(self.tmp.name, "tok", OWNER, tg)
            names = sorted(t.split("\n")[0] for _, t, _ in tg.sent if "title-2" in t)
            if _ == 0:
                self.assertEqual(names, ["<b>OK1</b>", "<b>OK2</b>"])
            fails = self.state()["fails"]
            self.assertEqual(len(fails), hang_n)  # 응답 없는 피드만 실패로 집계
        self.assertGreaterEqual(main.COLLECT_WORKERS, len(feeds) - 2)

    def _setup_feeds(self, urls):
        feeds = [{"id": i + 1, "name": f"F{i + 1}", "url": u} for i, u in enumerate(urls)]
        with open(os.path.join(self.tmp.name, "feeds.json"), "w") as f:
            json.dump({"next_id": len(urls) + 1, "feeds": feeds}, f)
        self.run_bot(FakeTG(), feed_of("1"))  # 기준선

    def _run_tracking_concurrency(self, urls, hold=0.15):
        self._setup_feeds(urls)
        lock = threading.Lock()
        cur = {"now": 0, "max": 0, "per_host": {}, "host_max": {}}

        def fetch(url):
            host = url.split("/")[2]
            with lock:
                cur["now"] += 1
                cur["max"] = max(cur["max"], cur["now"])
                cur["per_host"][host] = cur["per_host"].get(host, 0) + 1
                cur["host_max"][host] = max(cur["host_max"].get(host, 0), cur["per_host"][host])
            threading.Event().wait(hold)  # setUp이 time.sleep을 무력화하므로 Event.wait로 실제 대기
            with lock:
                cur["now"] -= 1
                cur["per_host"][host] -= 1
            return feed_of("2", "1")

        tg = FakeTG()
        with mock.patch("rssbot.main.fetch_feed", side_effect=fetch):
            main.run(self.tmp.name, "tok", OWNER, tg)
        return cur, tg

    def test_same_host_requests_are_limited_but_all_feeds_are_fetched(self):
        cur, tg = self._run_tracking_concurrency([f"https://same.example/rss{i}" for i in range(8)])
        self.assertLessEqual(main.PER_HOST_LIMIT, 3)  # 제한이 사실상 풀리지 않았는지
        self.assertLessEqual(cur["host_max"]["same.example"], main.PER_HOST_LIMIT)
        self.assertGreaterEqual(cur["host_max"]["same.example"], 2)  # 제한 안에서는 병렬로 진행
        self.assertEqual(len([1 for _, t, _ in tg.sent if "title-2" in t]), 8)  # 하나도 누락되지 않음

    def test_different_hosts_still_run_concurrently(self):
        cur, tg = self._run_tracking_concurrency([f"https://h{i}.example/rss" for i in range(8)])
        self.assertGreater(cur["max"], 3)  # 호스트가 다르면 호스트당 제한과 무관하게 동시에 진행
        self.assertEqual(len([1 for _, t, _ in tg.sent if "title-2" in t]), 8)

    def test_hanging_host_does_not_block_other_hosts(self):
        urls = [f"https://hang.example/rss{i}" for i in range(5)] + ["https://ok.example/rss"]
        self._setup_feeds(urls)
        release = threading.Event()
        self.addCleanup(release.set)

        def fetch(url):
            if "hang.example" in url:
                release.wait(10)
            return feed_of("2", "1")

        tg = FakeTG()
        started = time.monotonic()
        with mock.patch("rssbot.main.fetch_feed", side_effect=fetch), mock.patch("rssbot.main.COLLECT_BUDGET", 0.5):
            main.run(self.tmp.name, "tok", OWNER, tg)
        self.assertLess(time.monotonic() - started, 5)
        notified = [t for _, t, _ in tg.sent if "title-2" in t]
        self.assertEqual(len(notified), 1)
        self.assertIn("F6", notified[0])  # 다른 호스트의 정상 피드는 영향 없음
        self.assertEqual(len(self.state()["fails"]), 5)  # 응답 없는 호스트의 피드만 실패로 집계

    def test_delivery_stops_at_time_budget_and_keeps_pending(self):
        """전송이 오래 걸려도 최종 커밋 시간을 남기도록 한도에서 멈추고, 남은 글은 pending에 둔다."""
        self.run_bot(FakeTG(), feed_of("1"))
        self.run_bot(FakeTG(fail_sends=99), feed_of("3", "2", "1"))
        tg = FakeTG()
        with mock.patch("rssbot.main.DELIVER_BUDGET", 0):
            self.run_bot(tg, feed_of("3", "2", "1"))
        self.assertEqual(tg.sent, [])
        self.assertEqual(len(self.state()["pending"]["1"]), 2)
        tg2 = FakeTG()
        self.run_bot(tg2, feed_of("3", "2", "1"))
        self.assertEqual(len(tg2.sent), 2)

    def test_command_batch_stops_at_time_budget_without_advancing_offset(self):
        self.run_bot(FakeTG(), feed_of("1"))
        ups = [msg(5, "/list"), msg(6, "/list")]
        tg = FakeTG(updates=ups)
        with mock.patch("rssbot.main.COMMAND_BUDGET", 0):
            self.run_bot(tg, feed_of("1"))
        self.assertEqual(tg.sent, [])
        self.assertEqual(self.state()["update_offset"], 0)  # 처리하지 않은 명령은 다음 실행에서
        tg2 = FakeTG(updates=ups)
        self.run_bot(tg2, feed_of("1"))
        self.assertEqual(len(tg2.sent), 2)

    def test_malformed_feed_url_does_not_break_whole_collection(self):
        with open(os.path.join(self.tmp.name, "feeds.json"), "w") as f:
            json.dump({"next_id": 3, "feeds": [
                {"id": 1, "name": "BAD", "url": "http://[::1/rss"},  # 손으로 고치다 닫는 대괄호를 빠뜨림
                {"id": 2, "name": "OK", "url": "https://ok.example/rss"}]}, f)
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG()
        self.run_bot(tg, feed_of("2", "1"))
        self.assertEqual(len([1 for _, t, _ in tg.sent if "OK" in t and "title-2" in t]), 1)

    def test_checkpoint_timeout_kills_child_processes(self):
        pidfile = os.path.join(self.tmp.name, "child.pid")
        cmd = f"bash -c 'sleep 60 & echo $! > {pidfile}; wait'"
        with mock.patch.dict(os.environ, {"RSSBOT_CHECKPOINT_CMD": cmd}), mock.patch("rssbot.main.CHECKPOINT_TIMEOUT", 0.7):
            hook = main._hook_from_env()
            with self.assertRaises(RuntimeError) as ctx:
                hook()
        self.assertIn("시간 초과", str(ctx.exception))
        with open(pidfile) as f:
            pid = int(f.read().strip())
        alive = True
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            threading.Event().wait(0.05)
        self.assertFalse(alive, "체크포인트의 자식 프로세스가 남아 있음")

    def test_hard_limit_interrupts_blocking_call_and_saves_state(self):
        """단계별 한도를 지나 블로킹된 호출이 있어도 전체 한도에서 끊고, 미전송 글은 상태에 남긴다."""
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG()
        block = threading.Event()
        self.addCleanup(block.set)
        tg.send_message = lambda *a, **k: block.wait(30)  # 응답 없는 텔레그램 호출
        started = time.monotonic()
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("2", "1")):
            ok = main.run(self.tmp.name, "tok", OWNER, tg, hard_limit=1)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual([p["id"] for p in self.state()["pending"]["1"]], ["2"])  # 최종 커밋이 올릴 상태가 저장됨

    def test_timeout_during_command_leaves_offset_command_and_reply_consistent(self):
        """명령 처리 도중 시간 한도가 와도 offset 전진·명령 적용·응답 적재가 함께 끝나야 한다."""
        self.run_bot(FakeTG(), feed_of("1"))
        real_handle = commands.handle
        block = threading.Event()
        self.addCleanup(block.set)

        def slow_handle(text, store, prefetched=None):
            block.wait(1.6)  # 한도(1초)가 이 안에서 도래
            return real_handle(text, store, prefetched)

        tg = FakeTG(updates=[msg(5, "/remove 1")])
        with mock.patch("rssbot.main.commands.handle", side_effect=slow_handle), \
             mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            ok = main.run(self.tmp.name, "tok", OWNER, tg, hard_limit=1)
        self.assertFalse(ok)
        st = Store(self.tmp.name)
        self.assertEqual(st.items, [])  # 명령은 적용됐고
        self.assertEqual(st.state["update_offset"], 6)  # offset도 넘어갔으며
        self.assertEqual(len(st.state["outbox"]), 1)  # 응답도 보관됨 (다음 실행에서 전달)

    def test_timeout_during_collect_never_leaves_seen_without_pending(self):
        """seen에는 기록됐는데 pending은 비는 상태로 저장되면 그 글은 영영 사라진다."""
        self.run_bot(FakeTG(), feed_of("1"))
        real_persist = main._persist_pending
        block = threading.Event()
        self.addCleanup(block.set)

        def slow_persist(store, fid, items):
            block.wait(1.6)  # seen 갱신 직후, pending 기록 도중에 한도가 도래
            return real_persist(store, fid, items)

        with mock.patch("rssbot.main._persist_pending", side_effect=slow_persist), \
             mock.patch("rssbot.main.fetch_feed", return_value=feed_of("2", "1")):
            ok = main.run(self.tmp.name, "tok", OWNER, FakeTG(), hard_limit=1)
        self.assertFalse(ok)
        st = self.state()
        self.assertIn("2", st["seen"]["1"])
        self.assertEqual([p["id"] for p in st["pending"]["1"]], ["2"])  # seen에 있으면 pending에도 있어야 함

        tg = FakeTG()
        self.run_bot(tg, feed_of("2", "1"))  # 다음 실행에서 정상 전달
        self.assertEqual(len([1 for _, t, _ in tg.sent if "title-2" in t]), 1)

    def test_hard_limit_interrupts_feed_validation_and_command_is_retried(self):
        """/add 검증의 네트워크 호출은 시간 한도로 끊을 수 있어야 하고, 끊겨도 명령은 다음 실행에서 처리된다."""
        self.run_bot(FakeTG(), feed_of("1"))
        block = threading.Event()
        self.addCleanup(block.set)
        slow = mock.Mock(side_effect=lambda url: (block.wait(30), feed_of("9"))[1])
        tg = FakeTG(updates=[msg(5, "/add https://slow.example/rss 새피드")])
        started = time.monotonic()
        with mock.patch("rssbot.commands.fetch_feed", slow), mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            ok = main.run(self.tmp.name, "tok", OWNER, tg, hard_limit=1)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - started, 6)  # 30초짜리 검증이 한도에서 끊김
        st = Store(self.tmp.name)
        self.assertEqual([f["name"] for f in st.items], ["GN"])  # 아무것도 바뀌지 않았고
        self.assertEqual(st.state["update_offset"], 0)  # offset도 그대로라 명령은 다음 실행에서 다시 처리됨

        tg2 = FakeTG(updates=[msg(5, "/add https://slow.example/rss 새피드")])
        with mock.patch("rssbot.commands.fetch_feed", return_value=feed_of("9")), mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            main.run(self.tmp.name, "tok", OWNER, tg2)
        self.assertEqual([f["name"] for f in Store(self.tmp.name).items], ["GN", "새피드"])
        self.assertEqual(len([1 for _, t, _ in tg2.sent if "추가했습니다" in t]), 1)

    def test_malformed_url_command_does_not_block_following_commands(self):
        """해석할 수 없는 URL 하나가 예외로 offset 전진을 막으면, 같은 명령이 매 실행 실패하며 뒤의 명령이 영영 막힌다."""
        self.run_bot(FakeTG(), feed_of("1"))
        ups = [msg(5, "/add http://[::1/rss"), msg(6, "/edit 1 url http://[bad"), msg(7, "/list")]
        tg = FakeTG(updates=ups)
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            main.run(self.tmp.name, "tok", OWNER, tg)
        texts = [t for _, t, _ in tg.sent]
        self.assertEqual(len(texts), 3)
        self.assertIn("http://", texts[0])  # 잘못된 URL 안내
        self.assertIn("구독 중인 RSS", texts[2])  # 뒤의 /list도 처리됨
        self.assertEqual(self.state()["update_offset"], 8)  # 세 명령 모두 넘어감

    def test_failed_prefetch_never_causes_network_access_inside_deferred_section(self):
        """사전 검증이 실패해도 handle()이 보류 구간(시간 한도 신호 차단) 안에서 피드를 다시 요청하면 안 된다."""
        self.run_bot(FakeTG(), feed_of("1"))
        net = mock.Mock(side_effect=AssertionError("보류 구간에서 네트워크 접속"))
        tg = FakeTG(updates=[msg(5, "/add https://ex.com/rss"), msg(6, "/edit 1 url https://ex.com/new"), msg(7, "/list")])
        with mock.patch("rssbot.commands.prefetch", side_effect=RuntimeError("사전 검증 실패")), \
             mock.patch("rssbot.commands.fetch_feed", net), mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            main.run(self.tmp.name, "tok", OWNER, tg)
        net.assert_not_called()
        texts = [t for _, t, _ in tg.sent]
        self.assertEqual(len(texts), 3)
        self.assertIn("사전 검증", texts[0])
        self.assertIn("사전 검증", texts[1])
        self.assertIn("구독 중인 RSS", texts[2])  # 뒤의 명령은 정상 처리
        st = Store(self.tmp.name)
        self.assertEqual([f["url"] for f in st.items], ["https://gn/rss"])  # 검증 못 한 add/edit은 반영되지 않음
        self.assertEqual(st.state["update_offset"], 8)

    def test_handle_with_prefetch_dict_never_fetches_and_none_fetches(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            with mock.patch("rssbot.commands.fetch_feed", side_effect=AssertionError("접속하면 안 됨")):
                self.assertIn("사전 검증", commands.handle("/add https://ex.com/rss", store, {}))  # 빈 dict도 접속 금지
            with mock.patch("rssbot.commands.fetch_feed", return_value=feed_of("a")) as ff:
                self.assertIn("추가했습니다", commands.handle("/add https://ex.com/rss", store))  # None이면 직접 가져옴
                ff.assert_called_once()

    def test_prefetch_failure_is_swallowed_and_never_leaks_out(self):
        with mock.patch("rssbot.commands.fetch_feed", side_effect=RuntimeError("예기치 못한 오류")):
            self.assertEqual(commands.prefetch("/add https://ex.com/rss"), {})
        with mock.patch("rssbot.commands._target_url", side_effect=ValueError("boom")):
            self.assertEqual(commands.prefetch("/add https://ex.com/rss"), {})

    def test_prefetched_result_is_used_and_rejected_urls_are_never_contacted(self):
        feed = feed_of("a", "b")
        with mock.patch("rssbot.commands.fetch_feed", side_effect=AssertionError("다시 가져오면 안 됨")):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            store = Store(tmp.name)
            r = commands.handle("/add https://ex.com/rss 이름", store, {"https://ex.com/rss": (feed, None)})
            self.assertIn("추가했습니다", r)
            r2 = commands.handle("/add https://ex2.com/rss", store, {"https://ex2.com/rss": (None, FeedError("HTTP 404 응답"))})
            self.assertIn("HTTP 404", r2)
            self.assertEqual(len(store.items), 1)
        with mock.patch("rssbot.commands.fetch_feed", side_effect=AssertionError("접속하면 안 됨")):
            for bad in ("/add ftp://x", "/add https://ex.com/rss?token=1", "/add", "/list", "그냥 글", "/edit 1 name 새이름"):
                self.assertEqual(commands.prefetch(bad), {}, bad)

    def test_hard_limit_is_not_swallowed_by_except_exception(self):
        self.assertFalse(issubclass(main.RunTimeout, Exception))

    def test_hard_limit_kills_checkpoint_child_processes(self):
        pidfile = os.path.join(self.tmp.name, "hook.pid")
        cmd = f"bash -c 'sleep 60 & echo $! > {pidfile}; wait'"
        self.run_bot(FakeTG(), feed_of("1"))
        with mock.patch.dict(os.environ, {"RSSBOT_CHECKPOINT_CMD": cmd}), mock.patch("rssbot.main.CHECKPOINT_TIMEOUT", 60):
            hook = main._hook_from_env()
            with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
                ok = main.run(self.tmp.name, "tok", OWNER, FakeTG(), checkpoint=hook, hard_limit=1)
        self.assertFalse(ok)
        with open(pidfile) as f:
            pid = int(f.read().strip())
        alive = True
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            threading.Event().wait(0.05)
        self.assertFalse(alive)

    def _edit_url_scenario(self, new_feed, edit_to="https://mirror.example/rss"):
        """GN 구독에 [2,1]까지 본 상태에서 주소를 바꾼 뒤, 새 주소에서 new_feed를 받는 실행."""
        self.run_bot(FakeTG(), feed_of("2", "1"))
        tg = FakeTG(updates=[msg(5, f"/edit 1 url {edit_to}")])
        with mock.patch("rssbot.commands.fetch_feed", return_value=new_feed), mock.patch("rssbot.main.fetch_feed", return_value=new_feed):
            main.run(self.tmp.name, "tok", OWNER, tg)
        return tg

    def test_url_change_to_same_feed_does_not_swallow_new_posts(self):
        """http→https, 끝 슬래시, 미러처럼 같은 피드의 주소만 바뀐 경우 마지막 수집 이후의 새 글도 알려야 한다."""
        tg = self._edit_url_scenario(feed_of("4", "3", "2", "1"))
        texts = [t for _, t, _ in tg.sent if "title-" in t]
        self.assertEqual(len(texts), 2)  # 3, 4번 글
        self.assertTrue("title-3" in texts[0] and "title-4" in texts[1])
        self.assertNotIn("1", self.state()["rebase"])  # 판단이 끝나면 표시를 지움

    def test_url_change_to_different_feed_sets_silent_baseline(self):
        tg = self._edit_url_scenario(feed_of("y", "x"))  # 기존 기록과 겹치는 글이 없음 = 다른 피드
        self.assertEqual([t for _, t, _ in tg.sent if "title-" in t], [])  # 기존 글을 쏟아내지 않음
        self.assertEqual(self.state()["seen"]["1"], ["y", "x"])
        self.assertNotIn("1", self.state()["rebase"])

    def test_url_change_judgement_waits_for_a_successful_fetch(self):
        self.run_bot(FakeTG(), feed_of("2", "1"))
        with mock.patch("rssbot.commands.fetch_feed", return_value=feed_of("3", "2", "1")), \
             mock.patch("rssbot.main.fetch_feed", side_effect=FeedError("일시 장애")):
            main.run(self.tmp.name, "tok", OWNER, FakeTG(updates=[msg(5, "/edit 1 url https://mirror.example/rss")]))
        self.assertTrue(self.state()["rebase"]["1"])  # 가져오기에 실패해도 표시는 남음
        tg = FakeTG()
        self.run_bot(tg, feed_of("3", "2", "1"))  # 같은 피드 -> 새 글(3) 정상 알림
        self.assertEqual(len([1 for _, t, _ in tg.sent if "title-3" in t]), 1)
        self.assertNotIn("1", self.state()["rebase"])

    def test_url_change_keeps_pending_and_remove_clears_rebase_flag(self):
        self.run_bot(FakeTG(), feed_of("2", "1"))
        self.run_bot(FakeTG(fail_sends=99), feed_of("3", "2", "1"))  # 3번 글 전송 실패 -> pending
        tg = FakeTG(updates=[msg(5, "/edit 1 url https://mirror.example/rss")])
        with mock.patch("rssbot.commands.fetch_feed", return_value=feed_of("y", "x")), mock.patch("rssbot.main.fetch_feed", return_value=feed_of("y", "x")):
            main.run(self.tmp.name, "tok", OWNER, tg)
        self.assertEqual(len([1 for _, t, _ in tg.sent if "title-3" in t]), 1)  # 다른 피드로 바뀌어도 미전송 알림은 전달
        st = Store(self.tmp.name)
        st.state["rebase"]["1"] = True
        st.remove(st.items[0])
        self.assertNotIn("1", st.state["rebase"])

    def test_empty_response_never_creates_empty_baseline_and_recovery_does_not_flood(self):
        """비어 있는 응답을 기준선으로 삼으면, 복구된 뒤 기존 글이 전부 새 글로 쏟아진다."""
        tg = FakeTG()
        self.run_bot(tg, Feed("T", []))  # 처음 보는 피드가 글 없이 응답 (장애로 비어 보임)
        self.assertNotIn("1", self.state()["seen"])  # 빈 기준선을 만들지 않음
        self.assertEqual(tg.sent, [])
        self.run_bot(tg, feed_of("5", "4", "3", "2", "1"))  # 복구
        self.assertEqual(tg.sent, [])  # 기존 글은 조용히 기준선
        self.assertEqual(len(self.state()["seen"]["1"]), 5)
        self.run_bot(tg, feed_of("6", "5", "4", "3", "2", "1"))
        self.assertEqual(len([1 for _, t, _ in tg.sent if "title-6" in t]), 1)  # 이후 새 글만 알림

    def test_transient_empty_response_keeps_existing_record(self):
        self.run_bot(FakeTG(), feed_of("2", "1"))
        self.run_bot(FakeTG(), Feed("T", []))  # 일시적으로 빈 응답
        self.assertEqual(self.state()["seen"]["1"], ["2", "1"])
        tg = FakeTG()
        self.run_bot(tg, feed_of("3", "2", "1"))  # 복구
        self.assertEqual(len(tg.sent), 1)  # 새 글(3)만, 기존 글 중복 없음
        self.assertIn("title-3", tg.sent[0][1])

    def test_url_change_judgement_is_deferred_while_new_feed_is_empty(self):
        self.run_bot(FakeTG(), feed_of("2", "1"))
        with mock.patch("rssbot.commands.fetch_feed", return_value=feed_of("y", "x")), \
             mock.patch("rssbot.main.fetch_feed", return_value=Feed("T", [])):
            main.run(self.tmp.name, "tok", OWNER, FakeTG(updates=[msg(5, "/edit 1 url https://other.example/rss")]))
        st = self.state()
        self.assertTrue(st["rebase"]["1"])  # 판단을 미루고 표시 유지
        self.assertEqual(st["seen"]["1"], ["2", "1"])  # 기존 기록도 그대로
        tg = FakeTG()
        self.run_bot(tg, feed_of("y", "x"))  # 글이 보이면 그때 판단: 겹치지 않음 = 다른 피드
        self.assertEqual(tg.sent, [])
        self.assertEqual(self.state()["seen"]["1"], ["y", "x"])
        self.assertNotIn("1", self.state()["rebase"])

    def test_http_to_https_mirror_is_recognized_as_same_feed_and_only_new_posts_are_sent(self):
        """ID에 스킴·www가 들어 있는 피드는 주소를 바꾸면 ID가 하나도 겹치지 않아 다른 피드로 오판되기 쉽다."""
        old = Feed("T", [Entry(id=f"http://www.ex.com/p/{i}/", title=f"글{i}", link=f"http://www.ex.com/p/{i}") for i in (2, 1)])
        new = Feed("T", [Entry(id=f"https://ex.com/p/{i}", title=f"글{i}", link=f"https://ex.com/p/{i}") for i in (3, 2, 1)])
        self.run_bot(FakeTG(), old)
        tg = FakeTG(updates=[msg(5, "/edit 1 url https://ex.com/rss")])
        with mock.patch("rssbot.commands.fetch_feed", return_value=new), mock.patch("rssbot.main.fetch_feed", return_value=new):
            main.run(self.tmp.name, "tok", OWNER, tg)
        texts = [t for _, t, is_html in tg.sent if is_html]  # 글 알림만 (명령 응답은 평문)
        self.assertEqual(len(texts), 1)  # 3번 글만. 1·2번은 이미 본 글
        self.assertIn("글3", texts[0])
        self.assertEqual(len(self.state()["seen"]["1"]), 5)  # 이후 비교는 새 ID로 이어지도록 기록은 합침

    def test_norm_id_ignores_scheme_www_slash_and_host_case_but_keeps_path_case(self):
        n = main._norm_id
        self.assertEqual(n("http://WWW.Ex.com/P/AbC/"), n("https://ex.com/P/AbC"))  # 스킴·www·호스트 대소문자·끝 슬래시
        self.assertEqual(n("//EX.com/p"), n("https://ex.com/p"))
        self.assertNotEqual(n("https://ex.com/p/abc"), n("https://ex.com/p/ABC"))  # 경로 대소문자는 다른 글
        self.assertNotEqual(n("https://ex.com/p?v=aBc"), n("https://ex.com/p?v=abc"))  # 쿼리도 마찬가지
        self.assertNotEqual(n("yt:video:AbCdEf"), n("yt:video:abcdef"))  # 비 URL ID도 구별
        self.assertNotEqual(n("tag:ex.com,2026:Post/1"), n("tag:ex.com,2026:post/1"))

    def test_norm_id_never_merges_distinct_ids(self):
        n = main._norm_id
        # 쿼리·fragment 값의 끝 슬래시는 ID의 일부다
        self.assertNotEqual(n("https://ex.com/f?cat=/"), n("https://ex.com/f?cat="))
        self.assertNotEqual(n("https://ex.com/p#/"), n("https://ex.com/p#"))
        self.assertNotEqual(n("https://ex.com/p?a=1/#x/"), n("https://ex.com/p?a=1#x"))
        # 비 URL ID와 상대 경로는 전혀 정규화하지 않는다
        self.assertNotEqual(n("tag:ex.com,2026:post/1/"), n("tag:ex.com,2026:post/1"))
        self.assertNotEqual(n("/p/1/"), n("/p/1"))
        self.assertNotEqual(n("urn:uuid:abc/"), n("urn:uuid:abc"))
        # 합쳐도 되는 것: 같은 글의 주소 표기 차이 (스킴·www·호스트 대소문자·경로 끝 슬래시)
        self.assertEqual(n("https://ex.com/p/"), n("http://www.EX.com/p"))
        self.assertEqual(n("https://ex.com/"), n("https://ex.com"))
        self.assertEqual(n("https://ex.com/p/?id=1"), n("https://ex.com/p?id=1"))

    def test_url_change_notifies_new_posts_whose_ids_differ_only_in_query_slash_or_opaque_slash(self):
        old = Feed("T", [Entry(id="https://ex.com/f?cat=", title="기존1", link="https://ex.com/a"),
                         Entry(id="tag:ex.com,2026:post/1", title="기존2", link="https://ex.com/b")])
        new = Feed("T", [Entry(id="https://ex.com/f?cat=/", title="새글-쿼리", link="https://ex.com/c"),
                         Entry(id="tag:ex.com,2026:post/1/", title="새글-태그", link="https://ex.com/d"),
                         Entry(id="https://ex.com/f?cat=", title="기존1", link="https://ex.com/a"),
                         Entry(id="tag:ex.com,2026:post/1", title="기존2", link="https://ex.com/b")])
        self.run_bot(FakeTG(), old)
        tg = FakeTG(updates=[msg(5, "/edit 1 url https://mirror.example/rss")])
        with mock.patch("rssbot.commands.fetch_feed", return_value=new), mock.patch("rssbot.main.fetch_feed", return_value=new):
            main.run(self.tmp.name, "tok", OWNER, tg)
        texts = sorted(t for _, t, is_html in tg.sent if is_html)
        self.assertEqual(len(texts), 2)  # 서로 다른 ID의 새 글 둘 다 알림, 기존 글은 중복 없음
        self.assertTrue(any("새글-쿼리" in t for t in texts) and any("새글-태그" in t for t in texts))

    def test_url_change_notifies_new_post_whose_id_differs_only_by_path_case(self):
        old = Feed("T", [Entry(id="https://ex.com/p/abc", title="소문자 글", link="https://ex.com/p/abc")])
        new = Feed("T", [Entry(id="https://ex.com/p/ABC", title="대문자 새 글", link="https://ex.com/p/ABC"),
                         Entry(id="https://ex.com/p/abc", title="소문자 글", link="https://ex.com/p/abc")])
        self.run_bot(FakeTG(), old)
        tg = FakeTG(updates=[msg(5, "/edit 1 url https://mirror.example/rss")])
        with mock.patch("rssbot.commands.fetch_feed", return_value=new), mock.patch("rssbot.main.fetch_feed", return_value=new):
            main.run(self.tmp.name, "tok", OWNER, tg)
        texts = [t for _, t, is_html in tg.sent if is_html]
        self.assertEqual(len(texts), 1)
        self.assertIn("대문자 새 글", texts[0])  # 경로 대소문자만 다른 새 글도 알림

    def test_slow_feeds_do_not_consume_delivery_time(self):
        with open(os.path.join(self.tmp.name, "feeds.json"), "w") as f:
            json.dump({"next_id": 3, "feeds": [
                {"id": 1, "name": "SLOW", "url": "https://slow/rss"},
                {"id": 2, "name": "FAST", "url": "https://fast/rss"}]}, f)
        self.run_bot(FakeTG(), feed_of("1"))  # 두 피드 모두 기준선

        release = threading.Event()
        self.addCleanup(release.set)

        def fetch(url):
            if "slow" in url:
                release.wait(10)  # 응답하지 않는 피드
            return feed_of("2", "1")

        tg = FakeTG()
        started = time.monotonic()
        with mock.patch("rssbot.main.fetch_feed", side_effect=fetch), mock.patch("rssbot.main.COLLECT_BUDGET", 0.3):
            main.run(self.tmp.name, "tok", OWNER, tg)
        self.assertLess(time.monotonic() - started, 5)  # 느린 피드를 기다리지 않고 진행
        self.assertEqual([t for _, t, _ in tg.sent if "FAST" in t].__len__(), 1)  # 정상 피드 알림은 전달
        self.assertEqual(self.state()["fails"]["1"], 1)  # 느린 피드는 실패로 집계

    def test_collect_checkpoint_failure_still_delivers_but_reports_failure(self):
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG()
        hook = mock.Mock(side_effect=[None, RuntimeError("push 실패")])  # 두 번째(write-ahead)만 실패
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("2", "1")):
            ok = main.run(self.tmp.name, "tok", OWNER, tg, checkpoint=hook)
        self.assertFalse(ok)
        self.assertEqual(len(tg.sent), 1)  # 알림은 보낸다 (중복은 감수)

    def test_successful_checkpoints_report_ok(self):
        self.run_bot(FakeTG(), feed_of("1"))
        with mock.patch("rssbot.main.fetch_feed", return_value=feed_of("1")):
            self.assertTrue(main.run(self.tmp.name, "tok", OWNER, FakeTG(), checkpoint=lambda: None))

    def test_chat_level_400_keeps_every_pending_item(self):
        """chat not found 같은 채팅 오류는 HTTP 400이어도 글을 건너뛰면 안 된다. 전부 보존해야 한다."""
        self.run_bot(FakeTG(), feed_of("1"))
        body = json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}).encode()

        def fake_urlopen(req, data=None, timeout=None):
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(body))

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("rssbot.main.fetch_feed", return_value=feed_of("3", "2", "1")):
            main.run(self.tmp.name, "tok", OWNER)  # 실제 Telegram 클래스 사용
        self.assertEqual([p["id"] for p in self.state()["pending"]["1"]], ["2", "3"])

    def test_telegram_outage_stops_after_first_failure(self):
        with open(os.path.join(self.tmp.name, "feeds.json"), "w") as f:
            json.dump({"next_id": 3, "feeds": [
                {"id": 1, "name": "A", "url": "https://a/rss"}, {"id": 2, "name": "B", "url": "https://b/rss"}]}, f)
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG()
        tg.send_message = mock.Mock(side_effect=TelegramError("down"))
        self.run_bot(tg, feed_of("2", "1"))
        self.assertEqual(tg.send_message.call_count, 1)  # 피드 수만큼 타임아웃을 기다리지 않음
        self.assertEqual(sorted(self.state()["pending"]), ["1", "2"])  # 둘 다 보존

    def test_rejected_html_falls_back_to_plain_and_poison_item_is_skipped(self):
        self.run_bot(FakeTG(), feed_of("1"))
        tg = FakeTG()
        orig = tg.send_message

        def picky(chat_id, text, html=False):
            if html and "title-2" in text:
                raise TelegramError("can't parse entities", permanent=True)
            if "title-3" in text:
                raise TelegramError("Bad Request", permanent=True)  # 평문도 거부되는 글
            orig(chat_id, text, html)

        tg.send_message = picky
        self.run_bot(tg, feed_of("4", "3", "2", "1"))
        texts = [t for _, t, _ in tg.sent]
        self.assertEqual(len(texts), 2)
        self.assertIn("title-2", texts[0])  # 평문으로 재시도되어 전달
        self.assertIn("title-4", texts[1])  # 3번 글을 건너뛰어도 뒤 글은 막히지 않음
        self.assertNotIn("1", self.state()["pending"])

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
