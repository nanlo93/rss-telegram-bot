"""한 번 실행: 텔레그램 명령 처리 -> 구독 피드 확인 -> 새 글 알림 -> 상태 저장."""
from __future__ import annotations

import html
import os
import sys
import time

from . import commands
from .feed import Entry, FeedError, fetch_feed
from .store import SEEN_LIMIT, Store
from .telegram import Telegram, TelegramError

COMMANDS_VERSION = 1  # commands.COMMANDS를 바꾸면 올려서 메뉴를 다시 등록
MAX_PER_FEED = 10  # 한 번에 알리는 최대 글 수 (초과분은 요약)
MAX_PENDING = 100  # 전송하지 못해 쌓아 두는 글의 최대 개수 (피드당)
FAIL_WARN_AT = 12  # 연속 실패 횟수가 이 값이 되면 한 번 경고 (5분 주기 기준 약 1시간)
SEND_INTERVAL = 0.4


def log(msg: str) -> None:
    print(msg, flush=True)


def process_updates(store: Store, tg: Telegram, owner_chat_id: str) -> None:
    """getUpdates로 받은 명령을 처리한다. 지정된 채팅 외의 메시지는 무시."""
    offset = store.state.get("update_offset", 0)
    updates = tg.get_updates(offset)
    for upd in updates:
        store.state["update_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = msg.get("text") or ""
        if chat_id != owner_chat_id:
            log(f"무시: 허용되지 않은 채팅의 메시지 (update {upd['update_id']})")
            continue
        try:
            reply = commands.handle(text, store)
        except Exception as e:  # 한 명령의 오류가 전체 실행을 막지 않도록
            log(f"명령 처리 오류: {type(e).__name__}: {e}")
            reply = "명령을 처리하다 오류가 발생했습니다."
        if reply:
            tg.send_message(owner_chat_id, reply)


def format_entry(feed_name: str, entry) -> str:
    title = html.escape(entry.title)
    head = f"<b>{html.escape(feed_name)}</b>\n"
    if entry.link:
        return head + f'<a href="{html.escape(entry.link, quote=True)}">{title}</a>'
    return head + title


def _queue_item(e: Entry) -> dict:
    return {"id": e.id, "title": e.title, "link": e.link}


def poll_feeds(store: Store, tg: Telegram, owner_chat_id: str) -> None:
    for f in store.items:
        fid = str(f["id"])
        try:
            parsed = fetch_feed(f["url"])
        except FeedError as e:
            fails = store.state["fails"].get(fid, 0) + 1
            store.state["fails"][fid] = fails
            log(f"[{f['name']}] 확인 실패 ({fails}회 연속): {e}")
            if fails == FAIL_WARN_AT:
                try:
                    tg.send_message(owner_chat_id, f"'{f['name']}' 피드를 {fails}회 연속 확인하지 못했습니다.\n{f['url']}\n마지막 오류: {e}")
                except TelegramError as err:
                    log(f"[{f['name']}] 경고 전송 실패: {err}")
            continue
        store.state["fails"].pop(fid, None)

        current_ids = [e.id for e in parsed.entries]
        old = store.state["seen"].get(fid)
        if old is None:  # 처음 보는 피드는 기준선만 잡고 알리지 않음
            store.state["seen"][fid] = current_ids[:SEEN_LIMIT]
            log(f"[{f['name']}] 기준선 등록: {len(current_ids)}건")
            continue

        # 전송 실패분은 pending에 글 내용째 보관하므로, 다음 응답에서 피드가 그 글을 빼도 누락되지 않는다.
        pending = store.state["pending"].get(fid, [])
        known = set(old) | {p["id"] for p in pending}
        new = [_queue_item(e) for e in reversed(parsed.entries) if e.id not in known]  # 오래된 글부터

        # 관측한 글은 전송 여부와 무관하게 seen에 기록하고, 미전송분의 책임은 pending이 진다.
        merged = current_ids + [i for i in old if i not in set(current_ids)]
        store.state["seen"][fid] = merged[: max(SEEN_LIMIT, len(current_ids))]

        queue = pending + new
        if not queue:
            continue
        log(f"[{f['name']}] 전송 대기 {len(queue)}건 (신규 {len(new)}건)")

        skipped = max(0, len(queue) - MAX_PER_FEED)
        to_send = queue[skipped:]
        dropped = store.state["dropped"].get(fid, 0)  # 대기열 한도로 이미 버려진 글 수
        omitted = skipped + dropped
        remaining = queue  # 전송 도중 실패하면 아직 안 보낸 글부터 다시 시도
        try:
            if omitted:
                tg.send_message(owner_chat_id, f"'{f['name']}' 새 글이 많아 오래된 {omitted}건은 알림을 생략하고 최신 {len(to_send)}건만 알립니다.")
                store.state["dropped"].pop(fid, None)
                remaining = to_send
            for idx, item in enumerate(to_send):
                tg.send_message(owner_chat_id, format_entry(f["name"], Entry(**item)), html=True)
                remaining = to_send[idx + 1 :]
                time.sleep(SEND_INTERVAL)
        except TelegramError as err:
            log(f"[{f['name']}] 전송 실패, 남은 {len(remaining)}건은 다음 실행에서 재시도: {err}")

        if remaining:
            overflow = len(remaining) - MAX_PENDING
            if overflow > 0:  # 한도를 넘긴 오래된 글은 버리되, 건수를 기록해 다음 요약에서 반드시 알린다.
                store.state["dropped"][fid] = store.state["dropped"].get(fid, 0) + overflow
                log(f"[{f['name']}] 대기열 한도 초과로 {overflow}건 보류 해제 (누적 {store.state['dropped'][fid]}건, 다음 요약에서 안내)")
            store.state["pending"][fid] = remaining[-MAX_PENDING:]
        else:
            store.state["pending"].pop(fid, None)


def run(data_dir: str, token: str, owner_chat_id: str, tg: Telegram | None = None) -> None:
    store = Store(data_dir)
    tg = tg or Telegram(token)
    try:
        if store.state.get("commands_v") != COMMANDS_VERSION:
            try:
                tg.set_commands(commands.COMMANDS)
                store.state["commands_v"] = COMMANDS_VERSION
            except TelegramError as e:
                log(f"명령 메뉴 등록 실패: {e}")
        process_updates(store, tg, owner_chat_id)
        poll_feeds(store, tg, owner_chat_id)
    finally:
        store.save()


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log("TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경 변수가 필요합니다.")
        return 2
    run(os.environ.get("DATA_DIR", "data"), token, chat_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
