"""OpenAI-адаптеры STT/TTS (ADR 0008). Включаются через STT_PROVIDER=openai / TTS_PROVIDER=openai.

Внимание: аудио клиента уходит во внешний API OpenAI. Это opt-in через env, по умолчанию mock.
"""

import re

from opentelemetry import trace

from app.config import settings
from app.speech.errors import ProviderUnavailable
from app.speech.ports import AudioChunk, RealtimeSession, SpeechLanguage, Transcript

_KK = re.compile(r"[әғқңөұүһіӘҒҚҢӨҰҮҺІ]")
_EXT = {"webm": "webm", "ogg": "ogg", "wav": "wav", "x-wav": "wav", "mpeg": "mp3", "mp4": "mp4"}
_INSTR = {
    "ru": "Говори спокойно и доброжелательно, как оператор страховой компании, по-русски.",
    "kk": "Сақтандыру компаниясының операторы сияқты сабырлы, мейірімді қазақ тілінде сөйле.",
}


def _client(stage: str):
    if not settings.openai_api_key:
        raise ProviderUnavailable(
            stage, f"{stage}_unavailable",
            f"{stage.upper()}_PROVIDER=openai, но OPENAI_API_KEY не задан (см. README).",
        )
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds)


def detect_language(text: str, hint: str | None) -> str | None:
    """Казахские буквы → kk, иначе подсказка клиента или ru для кириллицы."""
    if _KK.search(text):
        return "kk"
    if hint in ("ru", "kk"):
        return hint
    return "ru" if re.search(r"[а-яё]", text, re.IGNORECASE) else None


class OpenAISpeechToText:
    name = "openai"

    def __init__(self, client=None):
        self._c = client

    async def transcribe(self, audio: bytes, mime: str, language_hint: str | None) -> Transcript:
        client = self._c or _client("stt")
        sub = mime.split(";")[0].split("/")[-1]
        kwargs = {"model": settings.stt_model, "file": (f"turn.{_EXT.get(sub, 'webm')}", audio, mime)}
        if language_hint in ("ru", "kk"):
            kwargs["language"] = language_hint
        res = await client.audio.transcriptions.create(**kwargs)
        attrs = {"gen_ai.provider.name": "openai", "gen_ai.request.model": settings.stt_model}
        usage = getattr(res, "usage", None)
        for k in ("input_tokens", "output_tokens"):
            if isinstance(getattr(usage, k, None), int):
                attrs[f"gen_ai.usage.{k}"] = getattr(usage, k)
        trace.get_current_span().set_attributes(attrs)
        text = (res.text or "").strip()
        if not text:
            raise ProviderUnavailable("stt", "stt_empty", "Речь не распознана. Повторите, пожалуйста.")
        return Transcript(text=text, language=detect_language(text, language_hint))


async def create_realtime_session(language_hint: str | None) -> RealtimeSession:
    """Эфемерный client_secret для WebRTC realtime-транскрипции (voice-router-spec.md, ADR 0008).

    Аудио клиента при этом уходит браузер → OpenAI напрямую, минуя backend (быстрее push-to-talk
    upload). Ход остаётся: финальный текст возвращается через POST /turns/text (source=stt).
    """
    client = _client("stt")
    transcription: dict = {"model": settings.stt_model}
    if language_hint in ("ru", "kk"):
        transcription["language"] = language_hint
    secret = await client.realtime.client_secrets.create(
        session={
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "noise_reduction": {"type": "near_field"},
                    "transcription": transcription,
                    "turn_detection": {"type": "server_vad"},
                }
            },
        }
    )
    return RealtimeSession(client_secret=secret.value, expires_at=secret.expires_at, model=settings.stt_model)


class OpenAITextToSpeech:
    name = "openai"

    def __init__(self, client=None):
        self._c = client

    async def synthesize(self, text: str, language: SpeechLanguage) -> AudioChunk | None:
        client = self._c or _client("tts")
        res = await client.audio.speech.create(
            model=settings.tts_model, voice=settings.tts_voice, input=text,
            instructions=_INSTR.get(language, _INSTR["ru"]), response_format="mp3",
        )
        trace.get_current_span().set_attributes(
            {"gen_ai.provider.name": "openai", "gen_ai.request.model": settings.tts_model}
        )
        return AudioChunk(mime="audio/mpeg", data=res.content)
