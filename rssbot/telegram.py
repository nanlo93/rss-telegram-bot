"""텔레그램 Bot API 최소 래퍼 (표준 라이브러리만 사용)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class TelegramError(Exception):
    """Bot API가 오류를 돌려줬을 때의 예외."""


class Telegram:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"

    def _call(self, method: str, params: dict, retries: int = 3):
        body = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
        ).encode()
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(self._base + method, data=body, timeout=30) as resp:
                    payload = json.load(resp)
            except urllib.error.HTTPError as e:
                try:
                    payload = json.load(e)
                except Exception:
                    raise TelegramError(f"{method}: HTTP {e.code}") from None
            except urllib.error.URLError as e:
                # 토큰이 URL에 들어 있으므로 예외 메시지를 그대로 노출하지 않는다.
                raise TelegramError(f"{method}: 네트워크 오류 ({e.reason})") from None
            if payload.get("ok"):
                return payload["result"]
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if payload.get("error_code") == 429 and retry_after and attempt < retries - 1:
                time.sleep(min(int(retry_after), 60) + 1)
                continue
            raise TelegramError(f"{method}: {payload.get('description', payload)}")

    def get_updates(self, offset: int) -> list:
        return self._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]})

    def send_message(self, chat_id, text: str, html: bool = False) -> None:
        params = {"chat_id": chat_id, "text": text[:4096]}
        if html:
            params["parse_mode"] = "HTML"
        self._call("sendMessage", params)

    def set_commands(self, commands: list) -> None:
        self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})
