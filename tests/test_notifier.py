"""알림 단위테스트 — requests.post 를 monkeypatch 해 네트워크 없이 검증."""

import requests

from src.notifier import NullNotifier, TelegramNotifier, build_notifier


def test_telegram_posts_expected_payload(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured.update(url=url, json=json, timeout=timeout)
        return object()

    monkeypatch.setattr(requests, "post", fake_post)
    TelegramNotifier("tok", "123").notify("hi")
    assert captured["url"] == "https://api.telegram.org/bottok/sendMessage"
    assert captured["json"] == {"chat_id": "123", "text": "hi"}
    assert captured["timeout"] == 10


def test_telegram_failsafe_on_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "post", boom)
    TelegramNotifier("tok", "123").notify("hi")  # 알림 실패가 예외로 새지 않아야 함


def test_build_notifier_disabled_paths():
    assert isinstance(build_notifier(False, "t", "c"), NullNotifier)  # 비활성
    assert isinstance(build_notifier(True, None, "c"), NullNotifier)  # 토큰 없음
    assert isinstance(build_notifier(True, "t", None), NullNotifier)  # 챗ID 없음
    assert isinstance(build_notifier(True, "t", "c"), TelegramNotifier)  # 완비
