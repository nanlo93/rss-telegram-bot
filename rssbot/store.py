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
    state: {"update_offset": int, "seen": {feed_id: [글 ID]}, "fails": {feed_id: int}, "commands_v": int}
    """

    def __init__(self, data_dir: str):
        self.feeds_path = os.path.join(data_dir, "feeds.json")
        self.state_path = os.path.join(data_dir, "state.json")
        os.makedirs(data_dir, exist_ok=True)
        self.feeds = _load(self.feeds_path, {"next_id": 1, "feeds": []})
        self.state = _load(self.state_path, {"update_offset": 0, "seen": {}, "fails": {}, "commands_v": 0})
        for key, default in (("seen", {}), ("fails", {})):
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
        self.state["seen"].pop(str(feed["id"]), None)
        self.state["fails"].pop(str(feed["id"]), None)

    def reset_seen(self, feed: dict, seen_ids: list) -> None:
        self.state["seen"][str(feed["id"])] = list(seen_ids)[:SEEN_LIMIT]
        self.state["fails"].pop(str(feed["id"]), None)

    def save(self) -> None:
        _dump(self.feeds_path, self.feeds)
        _dump(self.state_path, self.state)
