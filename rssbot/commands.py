"""텔레그램 명령어(/list /add /edit /remove) 처리. 응답 문자열을 돌려준다."""
from __future__ import annotations

from urllib.parse import urlparse

from .feed import FeedError, fetch_feed
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

새 글 확인과 명령 처리는 최대 몇 분 간격으로 이루어져서, 답장이 바로 오지 않을 수 있습니다."""


def _valid_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.netloc)


def _pick(store: Store, token: str):
    """'번호' 인자로 구독 항목을 찾는다. (feed, 오류메시지)"""
    if not token.isdigit():
        return None, "번호는 숫자로 입력해 주세요. /list 로 번호를 확인할 수 있습니다."
    n = int(token)
    if not 1 <= n <= len(store.items):
        return None, f"{n}번 구독이 없습니다. /list 로 확인해 주세요."
    return store.items[n - 1], None


def cmd_list(store: Store, args: list) -> str:
    if not store.items:
        return "구독 중인 RSS가 없습니다. /add <URL> 로 추가해 보세요."
    lines = ["구독 중인 RSS"]
    for i, f in enumerate(store.items, 1):
        lines.append(f"{i}. {f['name']}\n   {f['url']}")
    return "\n".join(lines)


def cmd_add(store: Store, args: list) -> str:
    if not args:
        return "사용법: /add <URL> [이름]"
    url, name = args[0], " ".join(args[1:]).strip()
    if not _valid_url(url):
        return "http:// 또는 https:// 로 시작하는 URL을 입력해 주세요."
    if len(store.items) >= MAX_FEEDS:
        return f"구독은 최대 {MAX_FEEDS}개까지 가능합니다."
    if any(f["url"] == url for f in store.items):
        return "이미 구독 중인 URL입니다."
    try:
        feed = fetch_feed(url)
    except FeedError as e:
        return f"RSS를 확인하지 못했습니다: {e}"
    added = store.add(name or feed.title or url, url, [e.id for e in feed.entries])
    return f"추가했습니다: {len(store.items)}. {added['name']}\n기존 글 {len(feed.entries)}건은 알리지 않고, 이후 새 글부터 알려드립니다."


def cmd_edit(store: Store, args: list) -> str:
    if len(args) < 3 or args[1].lower() not in ("name", "url"):
        return "사용법: /edit <번호> name <새 이름>  또는  /edit <번호> url <새 URL>"
    target, err = _pick(store, args[0])
    if err:
        return err
    field, value = args[1].lower(), " ".join(args[2:]).strip()
    if field == "name":
        target["name"] = value
        return f"이름을 '{value}'(으)로 바꿨습니다."
    if not _valid_url(value):
        return "http:// 또는 https:// 로 시작하는 URL을 입력해 주세요."
    if any(f["url"] == value and f is not target for f in store.items):
        return "이미 구독 중인 URL입니다."
    try:
        feed = fetch_feed(value)
    except FeedError as e:
        return f"RSS를 확인하지 못했습니다: {e}"
    target["url"] = value
    store.reset_seen(target, [e.id for e in feed.entries])
    return f"주소를 바꿨습니다: {target['name']}\n새 주소의 기존 글 {len(feed.entries)}건은 알리지 않습니다."


def cmd_remove(store: Store, args: list) -> str:
    if len(args) != 1:
        return "사용법: /remove <번호>"
    target, err = _pick(store, args[0])
    if err:
        return err
    store.remove(target)
    return f"삭제했습니다: {target['name']}"


HANDLERS = {
    "list": cmd_list,
    "add": cmd_add,
    "edit": cmd_edit,
    "remove": cmd_remove,
    "delete": cmd_remove,
}


def handle(text: str, store: Store) -> str | None:
    """명령 메시지를 처리해 응답을 돌려준다. 명령이 아니면 None."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0][1:].split("@", 1)[0].lower()
    if cmd in ("start", "help"):
        return HELP
    fn = HANDLERS.get(cmd)
    if fn is None:
        return "알 수 없는 명령입니다. /help 를 확인해 주세요."
    return fn(store, parts[1:])
