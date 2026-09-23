# Фича: API звонка и потоковый ответ (модуль `call`)

- Владелец: зона A (контракты)
- Связанные ADR: 0003 (доска), 0004 (роутер), 0005 (исполнитель), 0006 (фон), 0008 (голос, провайдеры), 0009 (модули)
- Главная спека: разд. 3.2, 4.1–4.2, 5.4, 7.5, 8.1, 9, 10.2
- Зависит от модулей: `context` (docs/specs/context-module.md), `knowledge` (docs/specs/knowledge-module.md)

## Цель
Один HTTP-контракт, через который фронт ведёт звонок: создаёт сессию, отправляет реплику (аудио или текст)
и получает по ходу обработки потоком события: транскрипт, решение роутера, действия, факты,
текст ответа по кускам, аудио по предложениям и итоговую трассировку с таймингами.

## Сценарий
1. Фронт вызывает `POST /calls` и получает `session_id` и capabilities (какие провайдеры живые, а какие моки).
2. Клиент жмёт push-to-talk. Фронт отправляет запись в `POST /calls/{id}/turns/audio` (или текст в `/turns/text`).
3. Ответ — `text/event-stream`. Сервер по очереди выполняет STT → роутер → валидацию и политику → исполнитель
   (чтения) → генерацию ответа → TTS и отдаёт событие после каждого этапа.
4. Текст ответа приходит кусками (`reply.delta`). Аудио приходит по предложениям (`audio`) и может начать играть
   раньше, чем закончится текст.
5. «Стоп»: фронт обрывает fetch и/или вызывает `POST /calls/{id}/turns/{turn_id}/cancel`. Сервер прекращает ход,
   поздние события этого `turn_id` не отправляются (спека 7.5).
6. Когда звук начал играть, фронт отправляет замер `POST /calls/{id}/turns/{turn_id}/playback` (спека 10.2).
7. «Новый звонок»: `POST /calls/{id}/reset` повышает `generation`, история не протекает.

Streaming здесь означает поток **событий ответа** сервер → браузер. Распознавание речи остаётся push-to-talk
(ADR 0008), партиалов ASR нет.

## Контракт

| Метод | Путь | Что |
|---|---|---|
| GET | `/capabilities` | провайдеры, режим моков, поддержанные действия |
| POST | `/calls` | новый звонок → `CallStarted` |
| POST | `/calls/{id}/reset` | новый звонок в той же сессии (`generation + 1`) |
| POST | `/calls/{id}/turns/text` | `{"text": "...", "language_hint": "ru"}` → SSE |
| POST | `/calls/{id}/turns/audio` | multipart: `audio` (webm/ogg/wav), `language_hint?` → SSE |
| POST | `/calls/{id}/turns/{turn_id}/cancel` | «стоп», 204 |
| POST | `/calls/{id}/turns/{turn_id}/playback` | `{"eos_to_playback_ms": 1234}` → запись timing на доску, 204 |
| GET | `/calls/{id}/router/last` | реальный промпт и сырой ответ роутера за последний ход (спека 5.4) |
| GET | `/sessions/{id}/context`, `/sessions/{id}/board` | модуль `context`: снимок и доска для панели |

Ошибки: 404 — сессии нет; 409 `turn_in_progress` — предыдущий ход ещё идёт (один foreground на сессию);
422 — пустой текст или аудио.

### События SSE

Каждое событие имеет вид `event: <type>\ndata: <json>\n\n`. В `data` всегда есть `type` и `turn_id`.

| event | когда | data (кроме `type`, `turn_id`) |
|---|---|---|
| `transcript` | после STT (для текста сразу) | `text`, `language`, `source: stt\|text` |
| `turn.started` | ход зарегистрирован на доске | `session_id`, `generation`, `context_version` |
| `routing` | роутер ответил, прошли валидацию и политику | `decision: route\|clarify\|handoff`, `scenarios[{scenario_id, confidence, reason}]`, `alternatives`, `language`, `slots`, `is_continuation`, `clarify_options` |
| `action` | исполнитель вызвал действие | `name`, `mode: read\|preview\|execute\|handoff\|unsupported`, `params`, `ok`, `result\|error` |
| `facts` | зафиксированы факты хода | `facts[{key, value, source, source_id, origin}]` |
| `reply.delta` | кусок текста ответа | `text` |
| `reply.done` | ответ целиком | `text`, `language` |
| `audio` | озвучено очередное предложение | `seq`, `mime`, `data` (base64), `text` |
| `error` | ошибка этапа | `stage`, `code`, `message`, `fatal` |
| `turn.cancelled` | ход остановлен | — |
| `turn.done` | конец хода | трассировка по формату README кита: `transcript`, `language`, `scenarios`, `alternatives`, `reason`, `slots`, `actions`, `latency_ms{stt, router, reads, response_first_token, response, tts_first_audio, total}`, `context_version` |

Ответ роутера — формат README кита (он главнее спеки 4.1): `scenarios` — список объектов
`{scenario_id, confidence, reason}`, первым полем. Неизвестный `scenario_id` → ошибка `router_invalid`, не «похожий».

Политика (разд. 4.2): первый сценарий ≥ 0.75 → `route`; 0.45–0.75 → `clarify` с двумя вариантами;
< 0.45 → `clarify`, при повторе подряд → `handoff`. При `route` сценарии `urgent` идут первыми.

Замеры (`latency_ms`) — время каждого этапа отдельно. `total` — от приёма запроса до первого аудио
(или до `reply.done`, если TTS выключен). Время в браузере (конец речи → начало звука) присылает фронт в `/playback`.

## Порты этапов

Модуль `call` оркестрирует ход и зависит от портов, а не от провайдеров:
`SpeechToText`, `ScenarioRouter`, `Executor`, `Responder`, `TextToSpeech`, `Background`.
Реализации выбираются через env (`STT_PROVIDER`, `LLM_PROVIDER`, `TTS_PROVIDER`, по умолчанию `mock`).

Моки ведут себя честно (ADR 0008): mock-STT отдаёт `error stt_unavailable` и просит текстовый ввод;
mock-роутер отдаёт `error llm_unavailable` без выдуманного сценария; mock-TTS аудио не отдаёт.
Базовый исполнитель в моке двигает тему и слоты в контексте, чтения не делает. Ответ собирается из шаблонов кита.
Боевые роутер, исполнитель и адаптеры речи подключаются в этих портах отдельными PR.

## Критерии приёмки
- [ ] `POST /calls` → `session_id`; `POST /turns/text` отдаёт `transcript → turn.started → … → turn.done`
- [ ] без ключей ход не падает: видно `error` с понятным кодом и `turn.done`
- [ ] второй ход во время первого → 409
- [ ] cancel: после него событий хода нет, на доске `turn cancelled`
- [ ] неизвестный `scenario_id` от роутера → `error router_invalid`
- [ ] политика порогов: route / clarify / handoff, `urgent` первым
- [ ] `/docs` показывает все эндпоинты, схемы событий SSE и примеры

## Вне скоупа
Боевые LLM, STT и TTS (отдельные PR в портах), WebSocket, streaming ASR, барж-ин, авторизация.

## Ключевые тесты
1. Ход текстом с фейковым роутером: порядок событий, `turn.done`, `context_version` вырос.
2. Mock-режим: `llm_unavailable` не роняет ход; 409 при параллельном ходе.
3. Политика порогов и валидация неизвестного ID.
