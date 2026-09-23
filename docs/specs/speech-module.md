# Фича: модуль `speech` — порты и адаптеры STT/TTS

- Владелец: зона C
- Связанные ADR: 0008 (голос, провайдеры, режим без ключей), 0009 (модули)
- Связанные спеки: call-api.md (разд. «Порты этапов»)

## Цель
Слой предобработки речи: распознавание, синтез и разбиение ответа на предложения для TTS.
Не зависит от агентного слоя (`kernel`, `router`, `executor`), `context` и `knowledge`.

## Сценарий
1. `call` получает аудио хода и вызывает `SpeechToText.transcribe`.
2. Текст ответа режется `split_sentences`, каждое предложение уходит в `TextToSpeech.synthesize`.

## Контракт
Публичный API — только `from app.speech import ...`:
- типы: `Transcript`, `AudioChunk`, `SpeechLanguage` (`ru|kk`);
- порты: `SpeechToText`, `TextToSpeech`;
- ошибки: `ProviderUnavailable(stage, code, message)` — общая ошибка провайдера (реэкспорт в `app.call`);
- моки: `MockSpeechToText` (`stt_unavailable`), `MockTextToSpeech` (аудио нет);
- реестры `STT`, `TTS` и `build_stt(name)`, `build_tts(name)` по `STT_PROVIDER`/`TTS_PROVIDER`;
- `split_sentences(text)`, `SENTENCE_END`.

## Критерии приёмки
- [ ] `speech` не импортирует `kernel`, `router`, `executor`, `context`, `knowledge`, `call`
- [ ] без ключей mock-STT отдаёт понятную ошибку, mock-TTS не роняет ход

## Вне скоупа
Боевые адаптеры (отдельные PR), streaming ASR.

## Ключевые тесты
Покрываются `tests/test_call_api.py` (mock-режим).
