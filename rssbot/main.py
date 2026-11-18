"""한 번 실행: 명령 처리 -> [체크포인트] -> 피드 수집 -> [체크포인트] -> 알림 전송.

체크포인트는 상태를 저장소에 먼저 커밋하는 write-ahead 단계다. 새 글을 pending에 기록해 커밋한 뒤에야 전송하므로,
전송 도중 러너가 강제 종료돼도 글은 저장소의 pending에 남아 다음 실행에서 (중복 가능성은 있어도) 누락 없이 재전송된다.
"""
from __future__ import annotations

import contextlib
import html
import os
import shlex
import subprocess
import sys
import queue
import re
import signal
import threading
import time

from urllib.parse import urlparse

from . import commands
from .feed import Entry, FeedError, fetch_feed
from .store import SEEN_LIMIT, Store
from .telegram import Telegram, TelegramError

COMMANDS_VERSION = 1  # commands.COMMANDS를 바꾸면 올려서 메뉴를 다시 등록
MAX_PER_FEED = 10  # 한 번의 실행에서 피드 하나에 보내는 최대 글 수 (나머지는 다음 실행에서 이어서 보냄)
MAX_DEFERRED_IDS = 500  # 대기열이 가득 차 미룬 글 중 밀려남을 추적할 ID의 최대 개수 (피드당)
MAX_PENDING = 100  # 전송하지 못해 쌓아 두는 글의 최대 개수 (피드당). 넘는 새 글은 버리지 않고 다음 실행으로 미룬다
FAIL_WARN_AT = 12  # 연속 실패 횟수가 이 값이 되면 한 번 경고 (5분 주기 기준 약 1시간)
SEND_INTERVAL = 0.4
MAX_OUTBOX = 50  # 보내지 못해 쌓아 두는 명령 응답·경고의 최대 개수
NOTICE_QUOTA = 5  # 그중 부가 안내(누락 안내·경고)가 차지할 수 있는 자리. 명령 응답에는 나머지(MAX_OUTBOX - NOTICE_QUOTA)가 보장된다
MAX_UPDATES_PER_RUN = 25  # 한 번에 가져와 처리하는 텔레그램 명령 수 (나머지는 다음 실행에서)
COLLECT_WORKERS = commands.MAX_FEEDS  # 허용된 구독 수만큼 일꾼을 둬, 응답 없는 피드가 앞에 몰려도 뒤 피드가 굶지 않게 함 (피드마다 전용 스레드)
PER_HOST_LIMIT = 2  # 같은 호스트로 동시에 보내는 요청 수 (몰리면 429·차단을 맞아 정상 피드가 반복 누락됨)
RUN_HARD_LIMIT = 420  # 한 번 실행의 하드 한도(초). 실행 제한 10분 중 체크아웃·최종 커밋(상태 파일 저장) 시간을 남긴다
COMMAND_BUDGET = 120  # 텔레그램 명령 처리 단계의 시간 한도(초). 넘기면 남은 명령은 다음 실행에서 (offset이 안 넘어감)
DELIVER_BUDGET = 300  # 전송 단계의 시간 한도(초). 넘기면 남은 글은 pending에 두고 끝낸다 (최종 커밋 시간 확보)
COLLECT_BUDGET = 150  # 수집 단계 전체 시간 한도(초). 넘기면 못 가져온 피드는 이번 실행에서 건너뜀
CHECKPOINT_TIMEOUT = 120


def log(msg: str) -> None:
    print(msg, flush=True)


class RunTimeout(BaseException):
    """실행 전체 시간 한도 초과. Exception이 아니라서 곳곳의 'except Exception'에 삼켜지지 않고, finally(상태 저장)는 실행된다."""


@contextlib.contextmanager
def _defer_timeout():
    """이 블록이 끝날 때까지 시간 한도 신호(SIGALRM)를 보류했다가, 끝난 뒤에 전달한다.

    SIGALRM은 어느 줄에서든 실행을 끊을 수 있어서, 상태를 여러 군데 고치는 도중에 끊기면 (예: seen에는 기록됐는데
    pending은 아직) 반쯤 바뀐 상태가 저장돼 글이나 명령이 영영 사라진다. 상태를 바꾸는 구간은 이 블록으로 감싸 원자적으로 만든다.
    네트워크 호출처럼 오래 걸릴 수 있는 구간에는 쓰지 않는다. (그 호출이 전체 시간 한도로 끊기지 않게 되므로)
    """
    if not hasattr(signal, "pthread_sigmask") or threading.current_thread() is not threading.main_thread():
        yield
        return
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)  # 보류된 신호는 여기서 전달된다 (블록 안의 변경은 모두 끝난 뒤)


# ---------- 1단계: 텔레그램 명령 적용 ----------

def _notice_count(store: Store) -> int:
    return sum(1 for e in store.state["outbox"] if e.get("kind") == "notice")


def _enqueue_notice(store: Store, text: str) -> bool:
    """부가 안내(누락 안내·경고)를 발송함에 적재한다. 안내 몫(NOTICE_QUOTA)이나 전체 자리가 없으면 적재하지 않고 False.

    발송함의 자리는 순서가 아니라 몫으로 나눈다. 안내에 먼저 자리를 주면 새 누락이 계속 생기는 동안 명령이 굶고, 명령 응답에
    먼저 주면 안내가 굶는다. 안내는 NOTICE_QUOTA개까지만, 명령 응답은 나머지가 보장된다.
    """
    box = store.state["outbox"]
    if len(box) >= MAX_OUTBOX or _notice_count(store) >= NOTICE_QUOTA:
        return False
    box.append({"text": text, "kind": "notice"})
    return True


def _enqueue(store: Store, texts: list, droppable: bool = False) -> None:
    """보낼 메시지를 상태의 outbox에 기록한다. 전송에 성공해야 지워지므로, 체크포인트·재시작을 넘어 보존된다.

    droppable=True(피드 경고 같은 부가 안내)는 안내 몫이나 자리가 없으면 새 메시지를 버린다.
    droppable=False(명령 응답)는 process_updates가 안내 몫을 뺀 여유만큼만 가져오므로 보통 자리가 있다.
    """
    box = store.state["outbox"]
    for t in texts:
        if not t:
            continue
        if droppable:
            if not _enqueue_notice(store, t):
                log("발송함의 안내 자리가 없어 부가 안내를 생략합니다.")
            continue
        if len(box) >= MAX_OUTBOX:
            del box[0]  # 명령 응답은 process_updates가 여유만큼만 가져오므로 보통 도달하지 않는다
        box.append({"text": t})


def process_updates(store: Store, tg: Telegram, owner_chat_id: str) -> None:
    """getUpdates로 받은 명령을 상태에 반영하고, 응답은 outbox에 적재한다. (전송은 하지 않음)

    지정된 채팅 외의 메시지는 무시한다. 응답은 체크포인트 이후에 보내므로, 강제 종료돼도 명령이 다시 적용되지 않는다.
    """
    offset = store.state.get("update_offset", 0)
    # 명령 하나는 응답 하나를 만든다. outbox 여유만큼만 가져와, 응답이 한도 때문에 버려지는 일이 없게 한다.
    # 가져오지 않은 명령은 offset이 넘어가지 않으므로 다음 실행에서 처리된다.
    # 안내가 쓸 몫(아직 안 쓴 만큼)은 남겨 둔다. 그래야 명령이 쏟아져도 누락 안내가 굶지 않는다.
    reserved = max(0, NOTICE_QUOTA - _notice_count(store))
    room = MAX_OUTBOX - len(store.state["outbox"]) - reserved
    if room <= 0:
        log("outbox가 가득 차 이번 실행에서는 새 명령을 가져오지 않습니다.")
        return
    deadline = time.monotonic() + COMMAND_BUDGET
    for upd in tg.get_updates(offset, limit=min(room, MAX_UPDATES_PER_RUN)):
        if time.monotonic() >= deadline:  # 명령(/add 검증 등)이 오래 걸려도 체크포인트·전송 시간을 남긴다
            log("명령 처리 시간 한도에 도달해 나머지 명령은 다음 실행에서 처리합니다.")
            break
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = msg.get("text") or ""
        is_owner = chat_id == owner_chat_id
        # 1) 네트워크가 필요한 검증(/add·/edit url의 피드 가져오기)은 상태를 바꾸는 구간 밖에서 먼저 한다.
        #    여기는 시간 한도 신호로 끊을 수 있다. 끊겨도 아직 아무것도 바꾸지 않았으니 명령은 다음 실행에서 다시 처리된다.
        try:
            prefetched = commands.prefetch(text) if is_owner else {}
        except Exception as e:  # 한 명령의 사전 검증 실패가 뒤의 명령을 막지 않게 한다 (offset은 아래에서 어떤 경우든 넘어감)
            log(f"명령 사전 검증 오류: {type(e).__name__}: {e}")
            prefetched = {}
        # 2) offset 전진·명령 적용·응답 적재는 한 덩어리로 끝나야 한다. 중간에 끊기면 offset만 넘어가 명령이 사라지거나,
        #    명령은 적용됐는데 offset이 안 넘어가 /remove 같은 명령이 재실행된다. 네트워크가 없어 보류해도 순간이다.
        with _defer_timeout():
            store.state["update_offset"] = upd["update_id"] + 1
            if not is_owner:
                log(f"무시: 허용되지 않은 채팅의 메시지 (update {upd['update_id']})")
                continue
            try:
                reply = commands.handle(text, store, prefetched)
            except Exception as e:  # 한 명령의 오류가 전체 실행을 막지 않도록
                log(f"명령 처리 오류: {type(e).__name__}: {e}")
                reply = "명령을 처리하다 오류가 발생했습니다."
            if reply:
                _enqueue(store, [reply])


# ---------- 2단계: 피드 수집 (전송 없음) ----------

def _queue_item(e: Entry) -> dict:
    return {"id": e.id, "title": e.title, "link": e.link}


_URL_ID_RE = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:)?//([^/?#]*)([^?#]*)(.*)$", re.IGNORECASE | re.DOTALL)


def _norm_id(raw: str) -> str:
    """글 ID를 '같은 글인가' 비교용으로 정규화한다. 절대 URL(스킴 있음 또는 //로 시작)에만 적용하고, 바꾸는 것은 딱 넷이다:
    스킴, 앞의 www., **호스트**의 대소문자, **경로**의 끝 슬래시.

    그 밖에는 손대지 않는다. 서로 다른 글의 ID가 합쳐지면 새 글이 이미 본 글로 오인돼 알림이 사라지기 때문이다.
    - 경로·쿼리·fragment의 대소문자는 구별한다 (짧은 링크, base64 계열 ID, 영상 ID).
    - 쿼리·fragment 값의 끝 슬래시는 그대로 둔다 ('?cat=/' 와 '?cat=' 은 다른 ID).
    - tag:, urn:, yt:video: 같은 비 URL ID와 상대 경로는 전혀 바꾸지 않는다.
    """
    s = raw.strip()
    m = _URL_ID_RE.match(s)
    if not m:
        return s
    host = re.sub(r"^www\.", "", m.group(1).lower())  # 호스트 이름만 대소문자 무관 (RFC 3986)
    return host + m.group(2).rstrip("/") + m.group(3)


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
    url_changed = store.state["rebase"].pop(fid, None)  # /edit url 직후의 첫 수집 (가져오기에 실패했다면 아직 남아 있음)

    if not current_ids and (not old or url_changed):
        # 글이 하나도 없는 응답으로는 기준선도, '같은 피드인가' 판단도 할 수 없다. 장애로 잠깐 비어 있는 것일 수 있는데
        # 여기서 빈 기준선을 잡으면 복구된 뒤 기존 글이 전부 새 글로 쏟아진다. 판단을 글이 보일 때까지 미룬다.
        if url_changed:
            store.state["rebase"][fid] = True
        log(f"[{f['name']}] 글이 없는 응답이라 기준선·주소 변경 판단을 미룹니다")
        return

    pending = store.state["pending"].get(fid, [])
    known = set(old or ()) | {p["id"] for p in pending}

    if not old:  # 처음 보는 피드(또는 기록이 비어 있는 피드)는 기준선만 잡고 알리지 않음
        store.state["seen"][fid] = current_ids[:SEEN_LIMIT]
        log(f"[{f['name']}] 기준선 등록: {len(current_ids)}건")
        return

    if url_changed:
        # 새 주소의 글이 기존 기록(·미전송 글)과 겹치면 같은 피드(주소만 다름)라서 새 글을 알리고, 하나도 겹치지 않으면 다른
        # 피드라서 기존 글을 알리지 않고 기준선만 잡는다. http→https·www·끝 슬래시 차이로 글 ID가 달라지는 피드도
        # 같은 피드로 알아보도록, 이 첫 비교에서는 그 차이를 무시한다.
        old_by_norm: dict = {}
        for i in known:
            old_by_norm.setdefault(_norm_id(i), set()).add(i)
        new_by_norm: dict = {}
        for e in parsed.entries:
            new_by_norm.setdefault(_norm_id(e.id), []).append(e.id)
        if not (set(new_by_norm) & set(old_by_norm)):
            store.state["seen"][fid] = current_ids[:SEEN_LIMIT]
            log(f"[{f['name']}] 주소가 바뀐 뒤 다른 피드로 판단해 기준선 등록: {len(current_ids)}건")
            return

        def already_seen(entry) -> bool:
            if entry.id in known:  # 정확히 같은 ID: 확실히 본 글
                return True
            # 근사 일치(스킴·www·끝 슬래시 차이)는 어떤 정규화 규칙으로도 서로 다른 글을 합칠 위험이 남는다. 알림을 억제하는
            # 쪽(= 누락)이 중복 알림보다 나쁘므로, 기존 기록과 새 피드 양쪽에서 그 형태가 하나뿐인 1:1 짝일 때만 인정한다.
            # 모호하면(같은 형태의 ID가 여럿) 합치지 않고 알린다.
            key = _norm_id(entry.id)
            return len(old_by_norm.get(key, ())) == 1 and len(new_by_norm.get(key, ())) == 1

        new = [_queue_item(e) for e in reversed(parsed.entries) if not already_seen(e)]  # 오래된 글부터
    else:
        new = [_queue_item(e) for e in reversed(parsed.entries) if e.id not in known]  # 오래된 글부터

    # 대기열(pending)에는 보관 한도가 있다. 한도를 넘는 새 글을 대기열 앞쪽의 오래된 글을 잘라 내며 밀어 넣으면, 주소 변경 뒤의
    # 중복 알림처럼 새로 쌓인 글이 먼저 대기하던 진짜 미전송 알림을 밀어낸다. 그래서 먼저 대기하던 글은 그대로 두고, 자리가 모자란
    # 새 글(오래된 것부터 채움)은 대기열에도 넣지 않고 seen에도 기록하지 않는다. 다음 실행에서 자리가 나면(= 전송이 진행되면)
    # 피드에 남아 있는 한 다시 새 글로 잡힌다.
    room = max(0, MAX_PENDING - len(pending))
    accepted, deferred = new[:room], new[room:]
    deferred_ids = {d["id"] for d in deferred}
    if deferred:
        log(f"[{f['name']}] 대기열이 가득 차 새 글 {len(deferred)}건은 다음 실행으로 미룹니다 (대기 {len(pending)}건)")

    # 미룬 글은 피드에 남아 있는 동안만 다시 잡힌다. 그 사이 피드에서 밀려나면 알릴 방법이 없으므로, 조용히 사라지지 않도록
    # 미룬 글의 ID를 기억해 두었다가 사라진 건수를 안내한다. 미루기 시작할 때도 한 번 경고한다.
    prev_deferred = store.state["deferred"].get(fid, [])
    current_set = set(current_ids)
    lost = [i for i in prev_deferred if i not in current_set]
    if deferred:
        store.state["deferred"][fid] = [d["id"] for d in deferred][:MAX_DEFERRED_IDS]
    else:
        store.state["deferred"].pop(fid, None)
    if lost:
        # 안내 메시지를 바로 발송함에 넣지 않고 건수를 상태에 먼저 기록한다. 발송함이 가득 차 있으면 새 안내가 버려지는데,
        # 위에서 추적(deferred)은 이미 지웠으므로 그러면 누락 사실이 영구히 사라진다. 기록은 안내를 실제로 적재할 때 지운다.
        store.state["lost"][fid] = store.state["lost"].get(fid, 0) + len(lost)
    elif deferred and not prev_deferred:
        notices.append(f"'{f['name']}' 대기열이 가득 차 새 글 {len(deferred)}건의 알림을 미룹니다. 전송이 정상화되기 전에 피드에서 밀려나면 알리지 못할 수 있습니다.")

    # 관측한 글은 seen에 기록하고(미룬 글 제외), 아직 알리지 못한 글은 내용째 pending에 보관한다.
    # (이후 피드에서 그 글이 사라져도, 전송이 늦어져도 누락되지 않음)
    merged = [i for i in current_ids if i not in deferred_ids] + [i for i in old if i not in set(current_ids)]
    store.state["seen"][fid] = merged[: max(SEEN_LIMIT, len(current_ids))]

    queue = pending + accepted
    _persist_pending(store, fid, queue)
    if accepted:
        log(f"[{f['name']}] 새 글 {len(accepted)}건 (전송 대기 {len(queue)}건)")


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
        if hasattr(signal, "pthread_sigmask"):  # 시간 한도 신호는 항상 메인 스레드로 전달되게 한다
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
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


def _enqueue_lost(store: Store) -> None:
    """피드에서 밀려나 알리지 못한 글의 안내를 발송함에 적재한다. 안내 몫에 자리가 있을 때만 적재하고 기록을 지운다.

    자리가 없으면 기록을 그대로 두어 다음 실행에서 다시 시도한다. (명령 응답을 밀어내지도, 이 안내를 버리지도 않는다.
    반대로 안내가 몫을 넘어 자리를 차지하지도 않으므로 명령이 굶지 않는다)
    """
    names = {str(f["id"]): f["name"] for f in store.items}
    for fid in list(store.state["lost"]):
        count = store.state["lost"][fid]
        if fid not in names or not count:  # 구독이 삭제됐거나 의미 없는 값
            store.state["lost"].pop(fid, None)
            continue
        if not _enqueue_notice(store, f"'{names[fid]}' 대기열이 가득 차 미뤄 둔 새 글 {count}건이 피드에서 밀려나 알리지 못했습니다."):
            log(f"[{names[fid]}] 발송함의 안내 자리가 없어 밀려난 글 {count}건의 안내를 다음 실행으로 미룹니다")
            continue
        store.state["lost"].pop(fid, None)


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
            with _defer_timeout():  # seen·pending·dropped·fails를 함께 고치므로 중간에 끊기면 글이 사라진다
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
    """피드의 pending을 오래된 글부터 한 번에 MAX_PER_FEED건씩 전송한다. 나머지는 pending에 두고 다음 실행에서 이어 보낸다.

    오래된 글을 건너뛰고 최신 글만 보내는 방식은 쓰지 않는다. 주소 변경 뒤의 중복 알림처럼 새로 쌓인 글이 발송 자리를 채우면
    먼저 대기하던 진짜 미전송 알림이 영구히 사라지기 때문이다. 대신 쌓인 순서대로 나눠 보내 몇 번의 실행에 걸쳐 모두 전달한다.
    (dropped는 예전 방식이 대기열 한도로 버린 건수를 남긴 값이다. 지금은 한도를 넘는 새 글을 버리지 않고 다음 실행으로 미루므로
    새로 늘지 않지만, 이미 기록된 값이 있으면 한 번 안내한다.)
    일시 오류로 중단되면 False (남은 글은 pending에 유지).
    """
    fid = str(f["id"])
    queue = store.state["pending"].get(fid, [])
    if not queue:
        return True
    to_send = queue[:MAX_PER_FEED]
    dropped = store.state["dropped"].get(fid, 0)
    try:
        if dropped:
            tg.send_message(owner_chat_id, f"'{f['name']}' 알림이 예전 대기열 한도를 넘어 오래된 {dropped}건은 알리지 못했습니다.")
            store.state["dropped"].pop(fid, None)
        for idx, item in enumerate(to_send):
            if deadline is not None and time.monotonic() >= deadline:
                log(f"[{f['name']}] 전송 시간 한도에 도달해 남은 {len(queue) - idx}건은 다음 실행에서 보냅니다.")
                return False
            _send_item(tg, owner_chat_id, f["name"], Entry(**item))
            _persist_pending(store, fid, queue[idx + 1 :])  # 보낸 글만 지우고 나머지는 그대로 둔다
            time.sleep(SEND_INTERVAL)
    except Exception as err:  # 타임아웃 등 어떤 실패든 pending에 남은 글을 다음 실행에서 재시도
        left = len(store.state["pending"].get(fid, []))
        log(f"[{f['name']}] 전송 실패, 남은 {left}건은 다음 실행에서 재시도: {type(err).__name__}: {err}")
        return False
    left = len(store.state["pending"].get(fid, []))
    if left:
        log(f"[{f['name']}] {len(to_send)}건 전송, 남은 {left}건은 다음 실행에서 이어서 보냅니다.")
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


def run(data_dir: str, token: str, owner_chat_id: str, tg: Telegram | None = None, checkpoint=None,
        hard_limit: float | None = None) -> bool:
    """한 번 실행한다. 상태를 저장소에 기록하지 못한 구간이 있었다면 False (호출자가 실패로 알리도록)."""
    store = Store(data_dir)
    tg = tg or Telegram(token)
    ok = True
    # 단계별 한도는 "대체로" 지켜질 뿐이라(진행 중인 요청·재시도 대기 등) 더하면 실행 제한을 넘길 수 있다.
    # 그래서 전체 한도를 SIGALRM으로 강제한다: 블로킹 중인 호출도 끊고, finally에서 상태를 저장한 뒤 실패로 끝낸다.
    # (최종 커밋은 별도 워크플로 단계에서 하므로, 그 시간을 남기려고 실행 제한보다 짧게 잡는다)
    use_alarm = bool(hard_limit) and hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    old_handler = None
    if use_alarm:
        def on_alarm(signum, frame):
            raise RunTimeout()

        old_handler = signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(int(hard_limit))
    try:
        if store.state.get("commands_v") != COMMANDS_VERSION:
            try:
                tg.set_commands(commands.COMMANDS)
                store.state["commands_v"] = COMMANDS_VERSION
            except TelegramError as e:
                log(f"명령 메뉴 등록 실패: {e}")

        # 이미 기록해 둔 누락 안내는 새 명령 응답보다 먼저 발송함 자리를 얻어야 한다. 순서가 반대면 자리가 날 때마다 새 응답이
        # 먼저 채워, 명령이 계속 들어오는 동안 이 안내가 끝없이 뒤로 밀린다. (명령은 outbox 여유만큼만 가져오므로 안내가 우선권을 갖는다)
        _enqueue_lost(store)
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
        _enqueue_lost(store)  # 부가 안내와 달리 버리지 않고, 자리가 날 때까지 상태에 보관
        if not _checkpoint(store, checkpoint):  # 새 글을 pending으로 확정한 뒤에야 전송 (write-ahead)
            # 알림 자체는 보낸다(중복은 감수). 대신 실행을 실패로 표시해 저장 실패를 눈에 띄게 한다.
            ok = False
            log("새 글 상태를 저장소에 기록하지 못했습니다. 알림은 계속 전송합니다.")

        deliver(store, tg, owner_chat_id)
    except RunTimeout:
        log(f"실행 전체 시간 한도({hard_limit}초)에 도달해 중단합니다. 못 보낸 알림은 상태에 남아 다음 실행에서 이어집니다.")
        ok = False
    finally:
        if use_alarm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
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

        def kill_group() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

        try:
            code = proc.wait(timeout=CHECKPOINT_TIMEOUT)
        except subprocess.TimeoutExpired:
            kill_group()
            raise RuntimeError(f"{argv[0]} 시간 초과({CHECKPOINT_TIMEOUT}초): 자식 프로세스까지 종료함") from None
        except BaseException:  # 전체 시간 한도(RunTimeout)·Ctrl+C로 끊길 때도 git 자식 프로세스를 남기지 않는다
            kill_group()
            raise
        if code != 0:
            raise RuntimeError(f"{argv[0]} 종료 코드 {code}")

    return hook


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log("TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 환경 변수가 필요합니다.")
        return 2
    ok = run(os.environ.get("DATA_DIR", "data"), token, chat_id, checkpoint=_hook_from_env(), hard_limit=RUN_HARD_LIMIT)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
