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
