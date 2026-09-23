import asyncio

import pytest

from app.speech import ProviderUnavailable, build_stt
from app.speech.openai_speech import detect_language


def test_openai_stt_without_key_is_unavailable(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "")
    stt = build_stt("openai")
    with pytest.raises(ProviderUnavailable) as e:
        asyncio.run(stt.transcribe(b"x", "audio/webm", "ru"))
    assert e.value.code == "stt_unavailable"


def test_kazakh_letters_detected():
    assert detect_language("Сақтандыру полисі", "ru") == "kk"
    assert detect_language("Мой полис", None) == "ru"
