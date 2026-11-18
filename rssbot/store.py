"""구독 목록(feeds.json)과 처리 상태(state.json)의 JSON 저장소."""
from __future__ import annotations

import json
import os

SEEN_LIMIT = 300  # 피드당 기억할 글 ID 수


def _load(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


class Store:
    """feeds: {"next_id": int, "feeds": [{"id","name","url"}]}
    state: {"update_offset": int, "seen": {feed_id: [글 ID]}, "pending": {feed_id: [{"id","title","link"}]},
    "dropped": {feed_id: 대기열 한도로 버려진 글 수}, "fails": {feed_id: int}, "rebase": {feed_id: True}, "deferred": {feed_id: [대기열이 가득 차 미룬 글 ID]},
    "lost": {feed_id: 미뤄 둔 사이 피드에서 밀려나 알리지 못한 글 수 (안내를 보낼 때까지 보관)}, "commands_v": int,
    "outbox": [{"text": 보내야 할 명령 응답·경고 메시지}]}
    """

    def __init__(self, data_dir: str):
        self.feeds_path = os.path.join(data_dir, "feeds.json")
        self.state_path = os.path.join(data_dir, "state.json")
        os.makedirs(data_dir, exist_ok=True)
        self.reload()

    def reload(self) -> None:
        """디스크의 feeds.json·state.json을 다시 읽는다.

        체크포인트의 `git pull --rebase`가 원격에서 바뀐 파일을 병합해 오는 경우가 있다.
        메모리 상태를 그대로 두면 다음 save()가 그 변경을 되돌리므로, 체크포인트 직후 병합된 내용을 다시 읽는다.
        """
        self.feeds = _load(self.feeds_path, {"next_id": 1, "feeds": []})
        self.state = _load(self.state_path, {"update_offset": 0, "seen": {}, "pending": {}, "dropped": {}, "fails": {}, "rebase": {}, "deferred": {}, "lost": {}, "commands_v": 0, "outbox": []})
        for key, default in (("seen", {}), ("pending", {}), ("dropped", {}), ("fails", {}), ("rebase", {}), ("deferred", {}), ("lost", {}), ("outbox", [])):
            self.state.setdefault(key, default)

    @property
    def items(self) -> list:
        return self.feeds["feeds"]

    def add(self, name: str, url: str, seen_ids: list) -> dict:
        feed = {"id": self.feeds["next_id"], "name": name, "url": url}
        self.feeds["next_id"] += 1
        self.items.append(feed)
        self.state["seen"][str(feed["id"])] = list(seen_ids)[:SEEN_LIMIT]
        return feed

    def remove(self, feed: dict) -> None:
        self.items.remove(feed)
        for key in ("seen", "pending", "dropped", "fails", "rebase", "deferred", "lost"):
            self.state[key].pop(str(feed["id"]), None)

    def mark_url_changed(self, feed: dict) -> None:
        """구독의 주소가 바뀌었음을 표시한다. 기준선은 여기서 잡지 않고 다음 수집 때 판단한다.

        바로 새 주소의 현재 글을 전부 '본 글'로 덮으면, 같은 피드를 주소만 바꾼 경우(http→https, 끝 슬래시, 미러)나 이전
        주소로 되돌리는 경우에 마지막 수집 이후 올라온 새 글이 알림 없이 사라진다. 다음 수집 때 새 주소의 글이 기존
        기록과 겹치는지 보고 같은 피드인지 다른 피드인지 정한다. (main._collect_one 참고)
        pending·dropped(미전송 알림)도 지우지 않는다. 번호가 밀려 엉뚱한 구독에 적용돼도 알림이 사라지지 않게 하기 위함.
        """
        self.state["rebase"][str(feed["id"])] = True
        self.state["fails"].pop(str(feed["id"]), None)
        # 미뤄 둔 글의 ID는 이전 주소 기준이라, 새 주소에서 보이지 않는다고 '밀려나 놓쳤다'고 오판하지 않도록 지운다.
        # (그 글이 새 주소에도 있으면 어차피 다시 새 글로 잡힌다)
        self.state["deferred"].pop(str(feed["id"]), None)

    def save(self) -> None:
        _dump(self.feeds_path, self.feeds)
        _dump(self.state_path, self.state)
