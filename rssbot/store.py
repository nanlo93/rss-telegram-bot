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
    "dropped": {feed_id: 대기열 한도로 버려진 글 수}, "fails": {feed_id: int}, "commands_v": int,
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
        self.state = _load(self.state_path, {"update_offset": 0, "seen": {}, "pending": {}, "dropped": {}, "fails": {}, "commands_v": 0, "outbox": []})
        for key, default in (("seen", {}), ("pending", {}), ("dropped", {}), ("fails", {}), ("outbox", [])):
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
        for key in ("seen", "pending", "dropped", "fails"):
            self.state[key].pop(str(feed["id"]), None)

    def reset_seen(self, feed: dict, seen_ids: list) -> None:
        self.state["seen"][str(feed["id"])] = list(seen_ids)[:SEEN_LIMIT]
        for key in ("pending", "dropped"):  # 주소가 바뀌면 이전 피드의 미전송분은 폐기
            self.state[key].pop(str(feed["id"]), None)
        self.state["fails"].pop(str(feed["id"]), None)

    def save(self) -> None:
        _dump(self.feeds_path, self.feeds)
        _dump(self.state_path, self.state)
