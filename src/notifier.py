"""알림 — Telegram(옵션). 비활성 시 NullNotifier. 키/시크릿은 절대 로깅하지 않는다(§4)."""

from __future__ import annotations

import logging

log = logging.getLogger("june_bot.notify")


class NullNotifier:
    def notify(self, message: str) -> None:
        log.info("[notify-off] %s", message)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id

    def notify(self, message: str) -> None:
        log.info("[telegram] %s", message)
        try:
            import requests

            requests.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": message},
                timeout=10,
            )
        except Exception as e:  # 알림 실패가 매매를 막아선 안 됨
            log.warning("telegram send failed: %s", e)


def build_notifier(enabled: bool, token: str | None, chat_id: str | None):
    if enabled and token and chat_id:
        return TelegramNotifier(token, chat_id)
    return NullNotifier()
