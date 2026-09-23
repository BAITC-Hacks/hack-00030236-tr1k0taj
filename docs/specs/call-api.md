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
1. Фронт генерирует UUID сессии (`crypto.randomUUID()`) и хранит его, пока идёт разговор. Этот UUID —
   ключ потока общения во всех вызовах. `POST /calls {"session_id": "<uuid>"}` открывает звонок и отдаёт
   capabilities; вызов необязателен — первый ход по новому UUID открывает звонок сам.
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

### Подключение agent kernel

`CallService(contexts, providers, kernel=None)` сохраняет UUID фронта, существующий `turn_id`
и этапы STT → роутер всех сценариев → исполнитель. Для обычного `route` без handoff,
системного намерения и действий с побочными эффектами kernel получает transcript,
`ReplyBrief` (instruction/template/decision/facts/language), generation, context_version,
client_id и необязательный `search_query` роутера. Повторный `begin_turn` не вызывается.
Уточнения, ошибки роутера, неподдержанные действия и handoff сохраняют ответ исполнителя.
Без настроенного роутера mock по-прежнему честно сообщает `llm_unavailable`.

Kernel публикует клиенту только текст main: `response.delta` преобразуется в `reply.delta`,
`response.completed` — в `reply.done`, `response.interrupted` — в `turn.cancelled`,
ошибка — в fatal `error` без успешного `turn.done`. Служебные результаты и prompts не выходят
в SSE. Несколько сегментов составляют один response_id; каждый завершённый сегмент получает
отдельную TTS-задачу. `reply.delta` и `audio` имеют дополнительные nullable `response_id`
и `segment_id`; `reply.done` имеет `response_id`. Старые поля и имена событий сохраняются.

`cancel` дополнительно принимает необязательный JSON `{ "played_ms": 123 }`.
Без позиции (включая обрыв fetch) услышанность остаётся unknown; main/TTS отменяются,
актуальные фоновые задачи продолжаются. Reset/смена поколения отменяют весь старый kernel.

`playback` сохраняет старые `eos_to_playback_ms` / `eos_to_reply_text_ms` и принимает
`request_id?`, `response_id?`, `played_ms`, `segments[{segment_id,start_ms,end_ms}]`,
`text_segment_ids[]`. EOS-замеры и позиция аудиодорожки имеют разный смысл: наличие первого
не подтверждает прослушивание. Timeline/text acknowledgement требуют `played_ms`.
Валидация принадлежности ответа, порядка интервалов и монотонного прогресса выполняется kernel.
Без kernel расширенный playback получает 409; прежний замер браузера продолжает работать.
Общий `/capabilities` сохраняет прежние поля и добавляет `kernel_enabled`, `kernel_streaming`,
`kernel_background`, `kernel_playback`; подробный контракт ядра — `/kernel/capabilities`.

## Контракт

### Blackboard v2: вход и повторное подключение

`TextTurn` принимает optional UUID `request_id`, `task_id="default"`,
`interrupt_previous=false`, `updates=[]` (публичный RecordUpdate ядра).
У update без `task_id` scope — выбранная задача; явный `task_id:null` означает общее сведение.
Explicit updates имеют приоритет над одноимёнными слотами, извлечёнными роутером этой реплики;
старые слоты из snapshot повторно не публикуются. Template/system/handoff ответы также
сохраняют выбранную задачу и updates, без запуска генерации kernel.

`AudioTurn` принимает optional `request_id` в multipart form. Идентичность аудиоповтора
проверяется по SHA-256 байтов, MIME и language_hint; имя файла не влияет. Сырые входные
аудиобайты не сохраняются. Если request_id не передан, сервер генерирует его и возвращает
в заголовке `X-Request-ID` и каждом событии. Старые запросы без новых полей работают.

Повтор того же request_id возвращает прежний поток, включая ещё выполняющийся запрос,
без повторного STT/router/executor/main/TTS. Другой payload с тем же ID — 409.
Обычный новый запрос при активном ходе — 409; явный `interrupt_previous=true` сначала
отменяет старый foreground и сохраняет его terminal event, затем принимает новую реплику.
Этот флаг не означает отмену страхового действия. Без playback ACK доставка остаётся unknown.

Каждый public call event сохраняется append-only на общей доске до отправки клиенту.
Старые event names и поля сохранены; добавлены `event_seq`, `request_id`, `generation`,
SSE `id=event_seq`. Поле `audio.seq` остаётся номером аудиоблока 0..N и не является курсором.
`GET /calls/{id}/events?after=N&request_id=<uuid>` читает сохранённые события после курсора
и ждёт завершения выбранного запроса. Без request_id читается публичный поток текущего
поколения до завершения активных запросов. `Last-Event-ID` учитывается вместе с `after`.
Будущий/отрицательный cursor — 422; неизвестный request_id — 404.
GET и повторный POST не запускают провайдеров. Heartbeat не сохраняется.

Disconnect исходного POST отменяет незавершённый ход и сохраняет `turn.cancelled`;
отключение читателя GET/повторного POST не отменяет обработку. После рестарта незавершённый
запрос получает сохранённый fatal `request_interrupted`; генерация с середины не имитируется.
Reset скрывает предыдущие поколения в replay, а старый request_id даёт 409 вместо повтора.
Журнал запросов приватен и не входит в JSON `/context`; public replay не содержит kernel
prompts, tool outputs или фоновые результаты. Сохраняется сгенерированное audio события для
повторной доставки без повторного TTS. Лимиты: 100 запросов на UUID сессии, 4 MiB на событие,
16 MiB на запрос, 64 MiB на поток сессии; превышение завершает запрос понятной ошибкой.
Ограничения v2: один worker, без production authentication и автоматического повтора действий.

| Метод | Путь | Что |
|---|---|---|
| GET | `/capabilities` | провайдеры, режим моков, поддержанные действия |
| POST | `/calls` | `{"session_id": "<uuid>"?}` → `CallStarted`; 201 — новая, 200 — уже была (не сбрасывается) |
| POST | `/calls/{id}/reset` | новый звонок в той же сессии (`generation + 1`) |
| POST | `/calls/{id}/turns/text` | `{"text": "...", "language_hint": "ru"}` → SSE |
| POST | `/calls/{id}/turns/audio` | multipart: `audio` (webm/ogg/wav), `language_hint?` → SSE |
| POST | `/calls/{id}/turns/{turn_id}/cancel` | «стоп», 204 |
| POST | `/calls/{id}/turns/{turn_id}/playback` | `{"eos_to_playback_ms": 1234}` → запись timing на доску, 204 |
| GET | `/calls/{id}/router/last` | реальный промпт и сырой ответ роутера за последний ход (спека 5.4) |
| GET | `/sessions/{id}/context`, `/sessions/{id}/board` | модуль `context`: снимок и доска для панели |

`session_id` — UUID (иначе 422). Ходы по незнакомому UUID открывают звонок сами (первый ход, рестарт
backend). `cancel`, `playback`, `router/last` по незнакомому UUID → 404. Если UUID не передан в `POST /calls`,
его генерирует сервер.

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
| `turn.done` | конец хода | трассировка по формату README кита: `transcript`, `language`, `scenarios`, `alternatives`, `reason`, `slots`, `actions`, `latency_ms{stt, router, reads, response_first_token, response, tts_first_audio, total}`, `context_version`, `trace_id` (32 hex или `null`) |

Ответ роутера — формат README кита (он главнее спеки 4.1): `scenarios` — список объектов
`{scenario_id, confidence, reason}`, первым полем. Неизвестный `scenario_id` → ошибка `router_invalid`, не «похожий».

Политика (разд. 4.2): первый сценарий ≥ 0.75 → `route`; 0.45–0.75 → `clarify` с двумя вариантами;
< 0.45 → `clarify`, при повторе подряд → `handoff`. При `route` сценарии `urgent` идут первыми.

Замеры (`latency_ms`) — время каждого этапа отдельно. `total` — от приёма запроса до первого аудио
(или до `reply.done`, если TTS выключен). Время в браузере (конец речи → начало звука) присылает фронт в `/playback`.

### Трассировка (ADR 0013, [tracer-module.md](tracer-module.md))

Любой запрос может нести W3C `traceparent`; ответ всегда отдаёт `traceparent` и `x-trace-id`.
`turn.done.trace_id` = `x-trace-id` хода. `cancel` и `playback` фронт шлёт с `traceparent` из ответа
хода: их SERVER span попадает в ту же трассу с событием `cancel` / `playback` (замеры браузера).

Атрибуты, которые пишет `call` (все span'ы несут `session.id`, после начала хода — `turn.id`):
- SERVER: `http.request.body.size`, `http.request.header.content_type`, `user_agent.original`,
  `input.source`, `language_hint`, для аудио `audio.mime|bytes|filename`;
- `turn`: `call.generation`, `context.version`, `local.transcript`, `sse.first_event_ms`,
  `sse.events.<type>`, `latency.<stage>_ms` (как `latency_ms`), события `error`, `cancelled`;
- `stt`: `stt.provider`, `stt.language`, `stt.chars`, `local.transcript`;
- `router`: `gen_ai.*`, `router.decision|scenario_id|confidence|alternatives|language|is_continuation`,
  `local.router.slots`, `local.prompt`, `local.raw_response`;
- `executor` (+ дочерние `action`: `action.name|mode|ok|error`): `scenario.id`, `executor.actions`;
- `responder`: `responder.name`, `response.id`, `reply.chars`, событие `response_first_token`;
  под ним kernel `segment` (`segment.id|index|used_source_ids`) → `llm.call` (`gen_ai.usage.*`),
  `rag.search` / `kb.read`; `tts.sentence`: `tts.seq`, `tts.chars`, `audio.mime`, `audio.bytes`.
- Ошибка этапа: статус ERROR + `error.code`, `error.stage`. Фоновые `agent.run` — отдельные трассы
  с link на ход. STT идёт до `begin_turn`, поэтому у `stt` нет `turn.id`.

## Порты этапов

Модуль `call` оркестрирует ход и зависит от портов, а не от провайдеров:
`SpeechToText`, `ScenarioRouter`, `Executor`, `Responder`, `TextToSpeech`, `Background`.
Реализации выбираются через env (`STT_PROVIDER`, `LLM_PROVIDER`, `TTS_PROVIDER`, по умолчанию `mock`).
`LLM_PROVIDER=openai` — `app.router.OpenAIRouter`: Responses API, strict json_schema (enum всех ID), системная часть = правила + карточки всех 40 сценариев + SYS_* + слоты (кэшируется), usage → `gen_ai.usage.*`; без ключа — `llm_unavailable`.
Где живут порты: `SpeechToText`/`TextToSpeech` и моки речи — модуль `speech`
(docs/specs/speech-module.md); `Executor`/`Responder`/`Background` и базовые реализации — модуль `executor`
(docs/specs/executor-module.md); `ScenarioRouter` и `Providers` — `call.ports`. `app.call` реэкспортирует прежние имена.

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
