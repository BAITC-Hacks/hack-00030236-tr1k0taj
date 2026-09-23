"""OpenAI-адаптеры STT/TTS (ADR 0008). Включаются через STT_PROVIDER=openai / TTS_PROVIDER=openai.

Внимание: аудио клиента уходит во внешний API OpenAI. Это opt-in через env, по умолчанию mock.
"""

import re
from collections.abc import AsyncIterator

from opentelemetry import trace

from app.config import settings
from app.speech.errors import ProviderUnavailable
from app.speech.ports import AudioChunk, SpeechLanguage, Transcript

_PCM_MIME = "audio/pcm;rate=24000;channels=1"

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

    async def stream(self, text: str, language: SpeechLanguage) -> AsyncIterator[AudioChunk]:
        """Потоковый PCM 24kHz/16-bit/mono: чанки по мере готовности (минимальная задержка)."""
        client = self._c or _client("tts")
        n_chunks, n_bytes, buf = 0, 0, b""
        async with client.audio.speech.with_streaming_response.create(
            model=settings.tts_model, voice=settings.tts_voice, input=text,
            instructions=_INSTR.get(language, _INSTR["ru"]), response_format="pcm",
        ) as response:
            async for raw in response.iter_bytes(chunk_size=4800):  # ~100мс при 24kHz/16-bit
                if not raw:
                    continue
                buf += raw
                even = len(buf) - (len(buf) % 2)  # ровное число байт — целые 16-bit сэмплы
                if even:
                    n_chunks += 1
                    n_bytes += even
                    yield AudioChunk(mime=_PCM_MIME, data=buf[:even])
                    buf = buf[even:]
        if len(buf) >= 2:
            n_chunks += 1
            n_bytes += len(buf) - (len(buf) % 2)
            yield AudioChunk(mime=_PCM_MIME, data=buf[: len(buf) - (len(buf) % 2)])
        trace.get_current_span().set_attributes({
            "gen_ai.provider.name": "openai", "gen_ai.request.model": settings.tts_model,
            "tts.stream_chunks": n_chunks, "tts.stream_bytes": n_bytes,
        })
