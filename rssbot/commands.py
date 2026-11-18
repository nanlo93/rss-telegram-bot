"""텔레그램 명령어(/list /add /edit /remove) 처리. 응답 문자열을 돌려준다."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .feed import FeedError, fetch_feed, find_secret_param
from .store import Store

MAX_FEEDS = 50

COMMANDS = [
    ("list", "구독 중인 RSS 목록 보기"),
    ("add", "RSS 추가: /add <URL> [이름]"),
    ("edit", "RSS 수정: /edit <번호> name|url <값>"),
    ("remove", "RSS 삭제: /remove <번호>"),
    ("help", "사용법 보기"),
]

HELP = """RSS Pinger 사용법

/list — 구독 중인 RSS 목록
/add <URL> [이름] — RSS 추가 (이름 생략 시 피드 제목 사용)
/edit <번호> name <새 이름> — 이름 변경
/edit <번호> url <새 URL> — 주소 변경
/remove <번호> — 구독 삭제

번호는 /list 기준이며 구독을 삭제하면 뒤의 번호가 당겨집니다. /edit·/remove 는 보내기 전에 /list 로 번호를 확인하세요.

새 글 확인과 명령 처리는 최대 몇 분 간격으로 이루어져서, 답장이 바로 오지 않을 수 있습니다."""


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:  # 'http://[::1/x' 처럼 해석할 수 없는 URL
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


_URL_RE = re.compile(r"[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)  # 스킴·대소문자와 무관하게


def redact(text) -> str:
    """오류 문구에서 URL을 지운다. 응답은 상태 파일(outbox)에 기록되어 공개 저장소에 커밋되기 때문."""
    return _URL_RE.sub("<URL>", str(text))


def unsafe_url_reason(url: str):
    """URL에 인증 정보가 들어 있어 보이면 이유를, 아니면 None을 돌려준다.

    구독 목록(data/feeds.json)은 공개 저장소에 그대로 커밋되므로, 비밀 토큰이 든 URL은 등록하지 않는다.
    """
    try:
        p = urlparse(url)
        has_userinfo = bool(p.username or p.password)
    except ValueError:
        return "URL을 해석할 수 없습니다"
    if has_userinfo:
        return "URL에 아이디·비밀번호가 들어 있습니다"
    key = find_secret_param(url)  # 쿼리뿐 아니라 ';' 구분 파라미터, 경로 파라미터, fragment까지 검사
    if key:
        return f"URL의 '{key}' 값이 인증 정보로 보입니다"
    return None


_EMPTY_FEED_MESSAGE = ("글이 하나도 없는 피드는 등록할 수 없습니다. 새 글은 '이미 본 글'과 비교해서 판단하는데, 비어 있는 상태를 기준으로 삼으면 "
                       "나중에 기존 글이 전부 새 글로 발송됩니다. (일시적으로 비어 보이는 것일 수 있으니 잠시 뒤 다시 시도해 주세요)")


def _reject_message(reason: str) -> str:
    return f"{reason}. 이 저장소는 공개라 구독 URL이 그대로 노출되므로, 인증 정보가 든 URL은 등록할 수 없습니다."


def _target_url(text: str):
    """/add <URL> 또는 /edit <번호> url <URL> 명령이 검증을 위해 가져와야 할 URL. 해당 없으면 None."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd, args = parts[0][1:].split("@", 1)[0].lower(), parts[1:]
    if cmd == "add" and args:
        return args[0]
    if cmd == "edit" and len(args) >= 3 and args[1].lower() == "url":
        return " ".join(args[2:]).strip()
    return None


def prefetch(text: str) -> dict:
    """명령 검증에 필요한 피드를 미리 가져온다. 상태는 바꾸지 않는다. {url: (Feed|None, 오류|None)}

    네트워크 호출은 오래 걸릴 수 있어서, 상태를 바꾸는 구간(시간 한도 신호를 보류하는 구간) 밖에서 먼저 해 두어야
    실행 전체 시간 한도가 이 호출을 끊을 수 있다. handle()은 여기서 받은 결과를 쓴다.
    """
    try:
        url = _target_url(text)
        if not url or not _valid_url(url) or unsafe_url_reason(url):
            return {}  # handle()이 어차피 거부할 URL은 접속하지 않는다
        try:
            return {url: (fetch_feed(url), None)}
        except FeedError as e:
            return {url: (None, e)}
    except Exception:
        # 어떤 이유로든 미리 가져오지 못하면 빈 결과를 돌려준다. handle()이 직접 검증해 오류 응답을 만든다.
        # (여기서 예외가 밖으로 나가면 offset이 넘어가지 않아 같은 명령이 매번 실패하며 뒤의 명령을 막는다)
        return {}


def _fetch(prefetched, url: str):
    """검증용 피드를 얻는다.

    prefetched가 None이면(단독 호출) 직접 가져온다. dict이면(봇의 명령 처리 경로) **절대 네트워크에 접속하지 않는다**:
    이 경로의 handle()은 시간 한도 신호를 보류한 구간 안에서 실행되므로, 여기서 접속하면 그동안 전체 시간 한도가 무력화된다.
    사전 검증(prefetch)이 실패했거나 결과가 없으면 오류로 알리고, 사용자가 명령을 다시 보내게 한다.
    """
    if prefetched is None:
        return fetch_feed(url)
    if url in prefetched:
        feed, err = prefetched[url]
        if err is not None:
            raise err
        return feed
    raise FeedError("사전 검증을 하지 못했습니다. 잠시 후 명령을 다시 보내 주세요")


def _pick(store: Store, token: str):
    """'번호' 인자로 구독 항목을 찾는다. (feed, 오류메시지)"""
    if not token.isdigit():
        return None, "번호는 숫자로 입력해 주세요. /list 로 번호를 확인할 수 있습니다."
    n = int(token)
    if not 1 <= n <= len(store.items):
        return None, f"{n}번 구독이 없습니다. /list 로 확인해 주세요."
    return store.items[n - 1], None


def cmd_list(store: Store, args: list, prefetched=None) -> str:
    if not store.items:
        return "구독 중인 RSS가 없습니다. /add <URL> 로 추가해 보세요."
    lines = ["구독 중인 RSS"]
    for i, f in enumerate(store.items, 1):
        lines.append(f"{i}. {f['name']}\n   {f['url']}")
    return "\n".join(lines)


def cmd_add(store: Store, args: list, prefetched=None) -> str:
    if not args:
        return "사용법: /add <URL> [이름]"
    url, name = args[0], " ".join(args[1:]).strip()
    if not _valid_url(url):
        return "http:// 또는 https:// 로 시작하는 URL을 입력해 주세요."
    reason = unsafe_url_reason(url)
    if reason:
        return _reject_message(reason)
    if len(store.items) >= MAX_FEEDS:
        return f"구독은 최대 {MAX_FEEDS}개까지 가능합니다."
    if any(f["url"] == url for f in store.items):
        return "이미 구독 중인 URL입니다."
    try:
        feed = _fetch(prefetched, url)
    except FeedError as e:
        return f"RSS를 확인하지 못했습니다: {redact(e)}"
    if not feed.entries:
        return _EMPTY_FEED_MESSAGE
    added = store.add(name or feed.title or url, url, [e.id for e in feed.entries])
    return f"추가했습니다: {len(store.items)}. {added['name']}\n기존 글 {len(feed.entries)}건은 알리지 않고, 이후 새 글부터 알려드립니다."


def cmd_edit(store: Store, args: list, prefetched=None) -> str:
    if len(args) < 3 or args[1].lower() not in ("name", "url"):
        return "사용법: /edit <번호> name <새 이름>  또는  /edit <번호> url <새 URL>"
    target, err = _pick(store, args[0])
    if err:
        return err
    field, value = args[1].lower(), " ".join(args[2:]).strip()
    number = store.items.index(target) + 1
    if field == "name":
        old_name, target["name"] = target["name"], value
        return f"{number}번 이름을 '{old_name}'에서 '{value}'(으)로 바꿨습니다."
    if not _valid_url(value):
        return "http:// 또는 https:// 로 시작하는 URL을 입력해 주세요."
    reason = unsafe_url_reason(value)
    if reason:
        return _reject_message(reason)
    if any(f["url"] == value and f is not target for f in store.items):
        return "이미 구독 중인 URL입니다."
    if target["url"] == value:
        # 같은 주소로의 변경은 아무것도 하지 않는다. 기준선을 다시 잡으면 아직 알리지 못한 새 글을 삼킨다.
        return f"{number}번 '{target['name']}'은(는) 이미 이 주소를 구독 중입니다."
    try:
        feed = _fetch(prefetched, value)
    except FeedError as e:
        # 구독 번호는 목록이 바뀌면 달라진다. 안내대로 다시 보냈을 때 다른 구독에 적용되지 않도록 대상을 보여주고 확인을 권한다.
        return (f"{number}번 '{target['name']}'의 주소를 바꾸지 못했습니다: {redact(e)}\n"
                "구독 번호는 목록이 바뀌면 달라질 수 있으니, 다시 보내기 전에 /list 로 번호를 확인해 주세요.")
    if not feed.entries:
        return _EMPTY_FEED_MESSAGE
    old_url, target["url"] = target["url"], value
    store.mark_url_changed(target)
    return (f"{number}번 '{target['name']}'의 주소를 바꿨습니다.\n이전: {old_url}\n"
            "새 주소가 같은 피드(주소만 다름)면 새 글부터 그대로 알리고, 다른 피드면 그 피드의 기존 글은 알리지 않고 이후 글부터 알립니다. "
            "아직 알리지 못한 글은 그대로 전달합니다.\n"
            f"되돌리려면 /list 로 번호를 확인한 뒤 '/edit <번호> url {old_url}' 를 보내세요. "
            "다른 피드로 바꿨다가 돌아오면 그 사이 올라온 글은 알리지 않습니다.")


def cmd_remove(store: Store, args: list, prefetched=None) -> str:
    if len(args) != 1:
        return "사용법: /remove <번호>"
    target, err = _pick(store, args[0])
    if err:
        return err
    store.remove(target)
    return (f"삭제했습니다: {target['name']}\n주소: {target['url']}\n"
            f"잘못 지웠다면 '/add {target['url']} {target['name']}' 로 다시 추가할 수 있습니다. "
            "(다시 추가하면 그 사이 올라온 글은 알리지 않고, 삭제 시점의 미전송 알림은 사라집니다)")


HANDLERS = {
    "list": cmd_list,
    "add": cmd_add,
    "edit": cmd_edit,
    "remove": cmd_remove,
    "delete": cmd_remove,
}


def handle(text: str, store: Store, prefetched: dict | None = None) -> str | None:
    """명령 메시지를 처리해 응답을 돌려준다. 명령이 아니면 None.

    prefetched: prefetch()의 결과. None이면 필요한 피드를 직접 가져오고, dict이면(빈 dict 포함) 네트워크에 접속하지 않고
    그 결과만 쓴다.
    """
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0][1:].split("@", 1)[0].lower()
    if cmd in ("start", "help"):
        return HELP
    fn = HANDLERS.get(cmd)
    if fn is None:
        return "알 수 없는 명령입니다. /help 를 확인해 주세요."
    return fn(store, parts[1:], prefetched)
