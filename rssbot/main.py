"""한 번 실행: 명령 처리 -> [체크포인트] -> 피드 수집 -> [체크포인트] -> 알림 전송.

체크포인트는 상태를 저장소에 먼저 커밋하는 write-ahead 단계다. 새 글을 pending에 기록해 커밋한 뒤에야 전송하므로,
전송 도중 러너가 강제 종료돼도 글은 저장소의 pending에 남아 다음 실행에서 (중복 가능성은 있어도) 누락 없이 재전송된다.
"""
from __future__ import annotations

import html
import os
import shlex
import subprocess
import sys
import queue
import signal
import threading
import time

from urllib.parse import urlparse

from . import commands
from .feed import Entry, FeedError, fetch_feed
from .store import SEEN_LIMIT, Store
from .telegram import Telegram, TelegramError

COMMANDS_VERSION = 1  # commands.COMMANDS를 바꾸면 올려서 메뉴를 다시 등록
MAX_PER_FEED = 10  # 한 번에 알리는 최대 글 수 (초과분은 요약)
MAX_PENDING = 100  # 전송하지 못해 쌓아 두는 글의 최대 개수 (피드당)
FAIL_WARN_AT = 12  # 연속 실패 횟수가 이 값이 되면 한 번 경고 (5분 주기 기준 약 1시간)
SEND_INTERVAL = 0.4
MAX_OUTBOX = 50  # 보내지 못해 쌓아 두는 명령 응답·경고의 최대 개수
MAX_UPDATES_PER_RUN = 25  # 한 번에 가져와 처리하는 텔레그램 명령 수 (나머지는 다음 실행에서)
COLLECT_WORKERS = commands.MAX_FEEDS  # 허용된 구독 수만큼 일꾼을 둬, 응답 없는 피드가 앞에 몰려도 뒤 피드가 굶지 않게 함 (피드마다 전용 스레드)
PER_HOST_LIMIT = 2  # 같은 호스트로 동시에 보내는 요청 수 (몰리면 429·차단을 맞아 정상 피드가 반복 누락됨)
COMMAND_BUDGET = 120  # 텔레그램 명령 처리 단계의 시간 한도(초). 넘기면 남은 명령은 다음 실행에서 (offset이 안 넘어감)
DELIVER_BUDGET = 300  # 전송 단계의 시간 한도(초). 넘기면 남은 글은 pending에 두고 끝낸다 (최종 커밋 시간 확보)
COLLECT_BUDGET = 150  # 수집 단계 전체 시간 한도(초). 넘기면 못 가져온 피드는 이번 실행에서 건너뜀
CHECKPOINT_TIMEOUT = 120


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- 1단계: 텔레그램 명령 적용 ----------

def _enqueue(store: Store, texts: list, droppable: bool = False) -> None:
    """보낼 메시지를 상태의 outbox에 기록한다. 전송에 성공해야 지워지므로, 체크포인트·재시작을 넘어 보존된다.

    droppable=True(피드 경고 같은 부가 안내)는 자리가 없으면 새 메시지를 버린다. 이미 쌓인 명령 응답을 밀어내지 않는다.
    (명령의 offset은 이미 확정돼 응답을 다시 만들 수 없지만, 경고는 의미가 크지 않다.)
    """
    box = store.state["outbox"]
    for t in texts:
        if not t:
            continue
        if len(box) >= MAX_OUTBOX:
            if droppable:
                log("outbox가 가득 차 부가 안내를 생략합니다.")
                continue
            del box[0]  # 명령 응답은 process_updates가 여유만큼만 가져오므로 보통 도달하지 않는다
        box.append({"text": t})


def process_updates(store: Store, tg: Telegram, owner_chat_id: str) -> None:
    """getUpdates로 받은 명령을 상태에 반영하고, 응답은 outbox에 적재한다. (전송은 하지 않음)

    지정된 채팅 외의 메시지는 무시한다. 응답은 체크포인트 이후에 보내므로, 강제 종료돼도 명령이 다시 적용되지 않는다.
    """
    offset = store.state.get("update_offset", 0)
    # 명령 하나는 응답 하나를 만든다. outbox 여유만큼만 가져와, 응답이 한도 때문에 버려지는 일이 없게 한다.
    # 가져오지 않은 명령은 offset이 넘어가지 않으므로 다음 실행에서 처리된다.
    room = MAX_OUTBOX - len(store.state["outbox"])
    if room <= 0:
        log("outbox가 가득 차 이번 실행에서는 새 명령을 가져오지 않습니다.")
        return
    deadline = time.monotonic() + COMMAND_BUDGET
    for upd in tg.get_updates(offset, limit=min(room, MAX_UPDATES_PER_RUN)):
        if time.monotonic() >= deadline:  # 명령(/add 검증 등)이 오래 걸려도 체크포인트·전송 시간을 남긴다
            log("명령 처리 시간 한도에 도달해 나머지 명령은 다음 실행에서 처리합니다.")
            break
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
            _enqueue(store, [reply])


# ---------- 2단계: 피드 수집 (전송 없음) ----------

def _queue_item(e: Entry) -> dict:
    return {"id": e.id, "title": e.title, "link": e.link}


def _persist_pending(store: Store, fid: str, items: list) -> None:
    if items:
        store.state["pending"][fid] = items
    else:
        store.state["pending"].pop(fid, None)


def _fetch_safely(url: str):
    """스레드에서 호출: (Feed, None) 또는 (None, 오류). 어떤 예외도 밖으로 내보내지 않는다."""
    try:
        return fetch_feed(url), None
    except Exception as e:
        return None, e


def _collect_one(store: Store, f: dict, notices: list, fetched) -> None:
    fid = str(f["id"])
    parsed, error = fetched
    if error is not None:
        msg = str(error) if isinstance(error, FeedError) else f"예기치 못한 오류 ({type(error).__name__})"
        fails = store.state["fails"].get(fid, 0) + 1
        store.state["fails"][fid] = fails
        log(f"[{f['name']}] 확인 실패 ({fails}회 연속): {msg}")
        if fails == FAIL_WARN_AT:
            notices.append(f"'{f['name']}' 피드를 {fails}회 연속 확인하지 못했습니다.\n{f['url']}\n마지막 오류: {commands.redact(msg)}")
        return
    store.state["fails"].pop(fid, None)

    current_ids = [e.id for e in parsed.entries]
    old = store.state["seen"].get(fid)
    if old is None:  # 처음 보는 피드는 기준선만 잡고 알리지 않음
        store.state["seen"][fid] = current_ids[:SEEN_LIMIT]
        log(f"[{f['name']}] 기준선 등록: {len(current_ids)}건")
        return

    pending = store.state["pending"].get(fid, [])
    known = set(old) | {p["id"] for p in pending}
    new = [_queue_item(e) for e in reversed(parsed.entries) if e.id not in known]  # 오래된 글부터

    # 관측한 글은 seen에 기록하고, 아직 알리지 못한 글은 내용째 pending에 보관한다.
    # (이후 피드에서 그 글이 사라져도, 전송이 늦어져도 누락되지 않음)
    merged = current_ids + [i for i in old if i not in set(current_ids)]
    store.state["seen"][fid] = merged[: max(SEEN_LIMIT, len(current_ids))]

    queue = pending + new
    overflow = len(queue) - MAX_PENDING
    if overflow > 0:  # 보관 한도를 넘긴 오래된 글은 버리되, 건수를 기록해 다음 요약에서 반드시 알린다.
        store.state["dropped"][fid] = store.state["dropped"].get(fid, 0) + overflow
        queue = queue[overflow:]
        log(f"[{f['name']}] 대기열 한도 초과로 {overflow}건 보류 해제 (누적 {store.state['dropped'][fid]}건, 다음 요약에서 안내)")
    _persist_pending(store, fid, queue)
    if new:
        log(f"[{f['name']}] 새 글 {len(new)}건 (전송 대기 {len(queue)}건)")


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:  # 손으로 고친 feeds.json의 잘못된 URL(예: 닫는 대괄호 누락)이 수집 전체를 막지 않게 함
        return ""


def _fetch_all(feeds: list) -> dict:
    """피드를 동시에 가져온다. {feed id: (Feed|None, 오류|None)} — 시간 한도 안에 끝나지 않은 피드는 빠진다.

    - 서로 다른 호스트는 모두 동시에 가져온다. 일꾼 수가 피드 수 이상이라(COLLECT_WORKERS = 구독 상한) 응답 없는
      피드가 일꾼을 붙잡아 뒤쪽 정상 피드가 굶는 일이 없다.
    - 같은 호스트는 PER_HOST_LIMIT개까지만 동시에 요청한다. 한 사이트의 피드 여러 개나 feedburner 같은 공용 호스트로
      요청이 몰려 429·차단을 맞으면 정상 피드가 반복해서 누락되기 때문이다. 응답 없는 호스트의 피드끼리만 서로 기다리고
      다른 호스트에는 영향이 없다.
    - 일꾼 스레드는 daemon이라 응답 없는 서버가 남아 있어도 프로세스 종료를 막지 않는다.
    """
    todo: queue.Queue = queue.Queue()
    for f in feeds:
        todo.put(f)
    results: dict = {}
    host_slots = {h: threading.Semaphore(PER_HOST_LIMIT) for h in {_host_of(f["url"]) for f in feeds}}
    deadline = time.monotonic() + COLLECT_BUDGET

    def worker() -> None:
        while time.monotonic() < deadline:
            try:
                f = todo.get_nowait()
            except queue.Empty:
                return
            slot = host_slots[_host_of(f["url"])]
            if not slot.acquire(timeout=max(0.0, deadline - time.monotonic())):
                continue  # 같은 호스트의 다른 요청이 한도 시간까지 끝나지 않음 -> 이번 실행에서는 건너뜀
            try:
                results[f["id"]] = _fetch_safely(f["url"])
            finally:
                slot.release()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(COLLECT_WORKERS, len(feeds)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return dict(results)  # 시점 고정 복사본: 이후 늦게 끝나는 스레드의 결과는 쓰지 않는다


def collect_feeds(store: Store) -> list:
    """모든 구독 피드를 동시에 확인해 새 글을 pending에 기록한다. 보낼 안내 메시지 목록을 돌려준다.

    장애 중인 피드가 많아도 체크포인트와 전송 시간을 잡아먹지 않도록, 가져오기는 병렬로 하고 전체 시간 한도를 둔다.
    상태 갱신은 모두 이 스레드에서 순차로 처리한다.
    """
    notices: list = []
    feeds = list(store.items)
    if not feeds:
        return notices
    fetched = _fetch_all(feeds)
    for f in feeds:
        result = fetched.get(f["id"], (None, FeedError("시간 초과 (이번 실행에서는 건너뜀)")))
        try:
            _collect_one(store, f, notices, result)
        except Exception as e:  # 한 피드의 예기치 못한 오류가 다른 피드 확인을 막지 않도록
            log(f"[{f['name']}] 처리 중 오류: {type(e).__name__}: {e}")
    return notices


# ---------- 3단계: 전송 ----------

def format_entry(feed_name: str, entry: Entry) -> str:
    title = html.escape(entry.title)
    head = f"<b>{html.escape(feed_name)}</b>\n"
    if entry.link:
        return head + f'<a href="{html.escape(entry.link, quote=True)}">{title}</a>'
    return head + title


def _send_item(tg: Telegram, owner_chat_id: str, feed_name: str, entry: Entry) -> bool:
    """글 하나를 보낸다. 텔레그램이 메시지 자체를 거부(HTTP 400)하면 평문으로 한 번 더 시도하고,
    그래도 거부하면 건너뛴다(False). 일시 오류는 예외로 올려 재시도 경로를 탄다."""
    try:
        tg.send_message(owner_chat_id, format_entry(feed_name, entry), html=True)
        return True
    except TelegramError as e:
        if not e.permanent:
            raise
        log(f"[{feed_name}] HTML 전송 거부, 평문으로 재시도: {e}")
    try:
        tg.send_message(owner_chat_id, f"{feed_name}\n{entry.title}\n{entry.link}".strip())
        return True
    except TelegramError as e:
        if not e.permanent:
            raise
        log(f"[{feed_name}] 전송할 수 없는 글이라 건너뜀: {entry.id} ({e})")
        return False


def _deliver_feed(store: Store, tg: Telegram, owner_chat_id: str, f: dict, deadline: float | None = None) -> bool:
    """피드의 pending을 전송한다. 일시 오류로 중단되면 False (남은 글은 pending에 유지)."""
    fid = str(f["id"])
    queue = store.state["pending"].get(fid, [])
    if not queue:
        return True
    skipped = max(0, len(queue) - MAX_PER_FEED)
    to_send = queue[skipped:]
    omitted = skipped + store.state["dropped"].get(fid, 0)
    try:
        if omitted:
            tg.send_message(owner_chat_id, f"'{f['name']}' 새 글이 많아 오래된 {omitted}건은 알림을 생략하고 최신 {len(to_send)}건만 알립니다.")
            store.state["dropped"].pop(fid, None)
            _persist_pending(store, fid, to_send)
        for idx, item in enumerate(to_send):
            if deadline is not None and time.monotonic() >= deadline:
                log(f"[{f['name']}] 전송 시간 한도에 도달해 남은 {len(to_send) - idx}건은 다음 실행에서 보냅니다.")
                return False
            _send_item(tg, owner_chat_id, f["name"], Entry(**item))
            _persist_pending(store, fid, to_send[idx + 1 :])
            time.sleep(SEND_INTERVAL)
    except Exception as err:  # 타임아웃 등 어떤 실패든 pending에 남은 글을 다음 실행에서 재시도
        left = len(store.state["pending"].get(fid, []))
        log(f"[{f['name']}] 전송 실패, 남은 {left}건은 다음 실행에서 재시도: {type(err).__name__}: {err}")
        return False
    return True


def flush_outbox(store: Store, tg: Telegram, owner_chat_id: str, deadline: float | None = None) -> bool:
    """outbox의 명령 응답·경고를 순서대로 보낸다. 성공한 것만 지우며, 실패하거나 시간 한도를 넘기면 남은 것을 두고 False."""
    box = store.state["outbox"]
    while box:
        if deadline is not None and time.monotonic() >= deadline:
            log(f"전송 시간 한도에 도달해 남은 안내 {len(box)}건은 다음 실행에서 보냅니다.")
            return False
        try:
            tg.send_message(owner_chat_id, box[0]["text"])
        except TelegramError as e:
            if e.permanent:  # 이 메시지 내용 자체가 거부됨: 재시도해도 소용없고 뒤의 전송까지 막으므로 건너뜀
                log(f"전송할 수 없는 안내라 건너뜀: {e}")
                box.pop(0)
                continue
            log(f"안내 전송 실패, 남은 {len(box)}건은 다음 실행에서 재시도: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            log(f"안내 전송 실패, 남은 {len(box)}건은 다음 실행에서 재시도: {type(e).__name__}: {e}")
            return False
        box.pop(0)
    return True


def deliver(store: Store, tg: Telegram, owner_chat_id: str) -> None:
    """outbox를 비운 뒤 모든 피드의 pending을 전송한다. 피드 확인 성공 여부와 무관하게 재시도한다.

    텔레그램 자체가 불통이면(첫 실패) 나머지는 시도하지 않고 다음 실행으로 넘긴다.
    """
    deadline = time.monotonic() + DELIVER_BUDGET  # 최종 커밋(실행 제한 10분 안)에 도달할 시간을 남기기 위함
    if not flush_outbox(store, tg, owner_chat_id, deadline):
        return
    for f in list(store.items):
        if not _deliver_feed(store, tg, owner_chat_id, f, deadline):
            return


# ---------- 실행 ----------

def _checkpoint(store: Store, hook) -> bool:
    """상태를 디스크에 저장하고, 설정된 훅(저장소 커밋·푸시)을 호출한다. 훅이 실패하면 False."""
    store.save()
    if hook is None:
        return True
    ok = True
    try:
        hook()
    except Exception as e:
        log(f"체크포인트 실패: {type(e).__name__}: {e}")
        ok = False
    # 성공 여부와 무관하게 다시 읽는다: rebase로 병합된 뒤 push만 실패했어도 작업 트리에는 원격 변경(예: 손으로 고친
    # 구독 이름)이 들어와 있고, 메모리 상태를 그대로 두면 이후 save()가 그것을 되돌린다.
    store.reload()
    return ok


def run(data_dir: str, token: str, owner_chat_id: str, tg: Telegram | None = None, checkpoint=None) -> bool:
    """한 번 실행한다. 상태를 저장소에 기록하지 못한 구간이 있었다면 False (호출자가 실패로 알리도록)."""
    store = Store(data_dir)
    tg = tg or Telegram(token)
    ok = True
    try:
        if store.state.get("commands_v") != COMMANDS_VERSION:
            try:
                tg.set_commands(commands.COMMANDS)
                store.state["commands_v"] = COMMANDS_VERSION
            except TelegramError as e:
                log(f"명령 메뉴 등록 실패: {e}")

        try:
            process_updates(store, tg, owner_chat_id)
        except Exception as e:  # 명령 수신 실패와 무관하게 새 글 확인은 계속 진행
            log(f"명령 수신 처리 실패: {type(e).__name__}: {e}")
        if not _checkpoint(store, checkpoint):  # 명령 반영 + update offset을 먼저 확정 (재시작해도 명령이 재실행되지 않음)
            # 명령 결과를 저장소에 못 남긴 채 진행하면 다음 실행에서 같은 명령이 다시 적용될 수 있다(/remove 등).
            # 이번 실행은 여기서 멈추고, 워크플로 마지막 단계의 커밋 재시도에 맡긴다.
            log("명령 반영 상태를 저장소에 기록하지 못해 이번 실행을 중단합니다.")
            flush_outbox(store, tg, owner_chat_id)  # 명령은 이미 반영됐으니 응답은 보낸다 (실패해도 outbox에 남아 재시도)
            return False

        _enqueue(store, collect_feeds(store), droppable=True)
        if not _checkpoint(store, checkpoint):  # 새 글을 pending으로 확정한 뒤에야 전송 (write-ahead)
            # 알림 자체는 보낸다(중복은 감수). 대신 실행을 실패로 표시해 저장 실패를 눈에 띄게 한다.
            ok = False
            log("새 글 상태를 저장소에 기록하지 못했습니다. 알림은 계속 전송합니다.")

        deliver(store, tg, owner_chat_id)
    finally:
        store.save()
    return ok


def _hook_from_env():
    cmd = os.environ.get("RSSBOT_CHECKPOINT_CMD", "").strip()
    if not cmd:
        return None
    argv = shlex.split(cmd)

    def hook() -> None:
        # 별도 프로세스 그룹으로 실행해, 시간 초과 시 git pull/push 같은 자식까지 함께 종료한다.
        # (직접 실행한 셸만 죽으면 남은 git이 작업 트리를 계속 바꿔, 이후 상태 읽기·저장과 충돌한다)
        proc = subprocess.Popen(argv, start_new_session=True)
        try:
            code = proc.wait(timeout=CHECKPOINT_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise RuntimeError(f"{argv[0]} 시간 초과({CHECKPOINT_TIMEOUT}초): 자식 프로세스까지 종료함") from None
        if code != 0:
            raise RuntimeError(f"{argv[0]} 종료 코드 {code}")

    return hook


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log("TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경 변수가 필요합니다.")
        return 2
    ok = run(os.environ.get("DATA_DIR", "data"), token, chat_id, checkpoint=_hook_from_env())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
