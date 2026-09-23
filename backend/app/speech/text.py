"""Разбиение текста ответа на предложения для TTS по мере стриминга."""

import re

SENTENCE_END = re.compile(r"(?<=[.!?…:;])\s+")  # «:»/«;» — раньше первый звук


def split_sentences(text: str) -> list[str]:
    """Последний элемент — незаконченный хвост (может быть пустым)."""
    return SENTENCE_END.split(text)
