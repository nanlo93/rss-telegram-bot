"""텔레그램 Bot API 최소 래퍼 (표준 라이브러리만 사용)."""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request


# HTTP 400 중 "이 메시지 내용이 문제"임이 분명한 사유만 건너뛰기 대상으로 인정한다. (소문자 비교)
CONTENT_ERRORS = (
    "can't parse entities",
    "can't find end of the entity",
    "message is too long",
    "message text is empty",
    "text must be non-empty",
)


def is_content_error(error_code, description: str) -> bool:
    desc = (description or "").lower()
    return error_code == 400 and any(s in desc for s in CONTENT_ERRORS)


class TelegramError(Exception):
    """Bot API가 오류를 돌려줬을 때의 예외.

    permanent: 메시지 **내용** 때문에 거부된 경우(HTML 해석 실패, 너무 긴 글 등). 재시도해도 소용없으므로 호출자가 건너뛸 수 있다.
    채팅 자체의 문제(chat not found, 봇 차단, 권한 없음 등)는 HTTP 400이어도 permanent가 아니다.
    그런 오류에서 글을 건너뛰면 대기 중인 알림이 전부 버려지므로, 보존하고 재시도해야 한다.
    """

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


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
            except (OSError, http.client.HTTPException) as e:
                # 타임아웃(TimeoutError)·연결 끊김 등 URLError가 아닌 예외도 포함한다.
                # 토큰이 URL에 들어 있으므로 예외 메시지를 그대로 노출하지 않는다.
                reason = getattr(e, "reason", None) or type(e).__name__
                raise TelegramError(f"{method}: 네트워크 오류 ({reason})") from None
            except ValueError:  # JSON 해석 실패
                raise TelegramError(f"{method}: 응답 해석 실패") from None
            if payload.get("ok"):
                return payload["result"]
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if payload.get("error_code") == 429 and retry_after and attempt < retries - 1:
                time.sleep(min(int(retry_after), 60) + 1)
                continue
            raise TelegramError(f"{method}: {payload.get('description', payload)}", permanent=is_content_error(payload.get("error_code"), payload.get("description", "")))

    def get_updates(self, offset: int, limit: int = 100) -> list:
        return self._call("getUpdates", {"offset": offset, "limit": limit, "timeout": 0, "allowed_updates": ["message"]})

    def send_message(self, chat_id, text: str, html: bool = False) -> None:
        params = {"chat_id": chat_id, "text": text[:4096]}
        if html:
            params["parse_mode"] = "HTML"
        self._call("sendMessage", params)

    def set_commands(self, commands: list) -> None:
        self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})
