"""RSS 2.0 / Atom 피드 가져오기와 파싱."""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.parsers import expat
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

USER_AGENT = "rss-telegram-bot/0.1 (+https://github.com/nanlo93/rss-telegram-bot)"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 20


class FeedError(Exception):
    """피드를 가져오거나 해석하지 못했을 때의 오류."""


@dataclass
class Entry:
    id: str
    title: str
    link: str


@dataclass
class Feed:
    title: str
    entries: list  # 피드에 나온 순서 그대로 (보통 최신순)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def clean_link(url: str) -> str:
    """추적용 utm_* 쿼리 파라미터를 제거한다."""
    p = urlparse(url)
    if not p.query:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunparse(p._replace(query=urlencode(q)))


def _atom_link(entry) -> str:
    fallback = ""
    for child in entry:
        if _local(child.tag) != "link":
            continue
        href = child.get("href", "").strip()
        rel = child.get("rel", "alternate")
        if rel == "alternate" and href:
            return href
        if href and not fallback:
            fallback = href
    return fallback


def _forbid_entities(data: bytes) -> None:
    """엔티티 선언(billion laughs 등 확장 공격의 전제)이 있으면 거부한다.

    바이트 검색은 UTF-16 같은 다른 인코딩으로 우회되므로, expat가 디코딩한 뒤 호출하는 핸들러로 검사한다.
    """

    def reject(*_args):
        raise FeedError("엔티티 선언이 포함된 피드는 지원하지 않음")

    parser = expat.ParserCreate()
    parser.EntityDeclHandler = reject
    try:
        parser.Parse(data, True)
    except expat.ExpatError as e:
        raise FeedError(f"XML 해석 실패: {e}") from e


def parse_feed(data: bytes) -> Feed:
    """RSS 2.0 또는 Atom XML 바이트를 Feed로 변환한다."""
    _forbid_entities(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"XML 해석 실패: {e}") from e

    kind = _local(root.tag)
    if kind == "feed":  # Atom
        title = next((_text(c) for c in root if _local(c.tag) == "title"), "")
        items = [c for c in root if _local(c.tag) == "entry"]
    elif kind in ("rss", "RDF"):  # RSS 2.0 / RSS 1.0
        channel = root if kind == "RDF" else next((c for c in root if _local(c.tag) == "channel"), None)
        if channel is None:
            raise FeedError("RSS channel 요소가 없음")
        title = next((_text(c) for c in channel if _local(c.tag) == "title"), "")
        items = [c for c in root.iter() if _local(c.tag) == "item"]
    else:
        raise FeedError("RSS/Atom 피드가 아님")

    entries = []
    for it in items:
        fields = {}
        for c in it:
            fields.setdefault(_local(c.tag), c)
        etitle = html.unescape(_text(fields.get("title"))) or "(제목 없음)"
        if kind == "feed":
            link = _atom_link(it)
            eid = _text(fields.get("id"))
        else:
            link = _text(fields.get("link"))
            eid = _text(fields.get("guid"))
        eid = eid or link or etitle
        entries.append(Entry(id=eid, title=re.sub(r"\s+", " ", etitle), link=clean_link(link)))
    return Feed(title=html.unescape(title).strip(), entries=entries)


def fetch_feed(url: str) -> Feed:
    """URL에서 피드를 내려받아 파싱한다. 실패하면 FeedError."""
    if urlparse(url).scheme not in ("http", "https"):
        raise FeedError("http/https URL만 지원")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*"})
    # 오류 문구는 예외 원문이 아니라 종류별로 정해 둔 문구로 만든다. urllib의 예외 문자열에는 리디렉션 대상 URL 등
    # 민감할 수 있는 값이 섞여 들어가고, 이 문구는 공개 저장소의 상태 파일에 기록될 수 있기 때문이다.
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FeedError(f"HTTP {e.code} 응답") from None
    except urllib.error.URLError as e:
        reason = e.reason
        detail = reason.strerror if isinstance(reason, OSError) and reason.strerror else type(reason).__name__
        raise FeedError(f"연결 실패 ({detail})") from None
    except TimeoutError:
        raise FeedError("응답 시간 초과") from None
    except Exception as e:
        raise FeedError(f"가져오기 실패 ({type(e).__name__})") from None
    if len(data) > MAX_BYTES:
        raise FeedError("피드가 너무 큼 (5MB 초과)")
    try:
        return parse_feed(data)
    except FeedError:
        raise
    except Exception as e:  # 깊게 중첩된 XML 등 예기치 못한 해석 오류
        raise FeedError(f"피드 해석 중 오류: {type(e).__name__}") from e
