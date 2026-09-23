# Фича: модуль `tracer` — трасса хода в формате OpenTelemetry

- Владелец: зона A
- Связанные ADR: 0013 (OpenTelemetry), 0009 (модули), 0011 (kernel)
- Главная спека: разд. 10 (тайминги); контракт звонка — [call-api.md](call-api.md)

## Цель
Одна трасса на ход: от запроса фронта через STT, роутер, исполнитель, kernel и TTS до ответа,
с таймингами и метаданными. Панель читает её по HTTP, при желании — Jaeger по OTLP.

## Сценарий
1. Фронт отправляет ход с заголовком `traceparent` (необязательно) и получает в ответе
   `traceparent` и `x-trace-id` серверного span'а.
2. `TraceMiddleware` открывает SERVER span и закрывает его после последнего куска SSE.
3. Call/kernel оборачивают этапы в `span(...)`; завершённые span'ы попадают в буфер (и в OTLP).
4. Панель берёт `GET /traces/{x-trace-id}` или `GET /traces?session_id=<uuid>`.

## Устройство

```
backend/app/tracer/
  __init__.py    # публичный API
  setup.py       # setup_tracing, span(), get_tracer, current_*; OTLP-экспорт с маскированием
  middleware.py  # TraceMiddleware (чистый ASGI)
  store.py       # SpanStore: SpanProcessor, ring buffer по trace_id и session.id
  redact.py      # маскирование ПД
  api.py         # GET /traces, /traces/{trace_id}; SpanView, TraceView, TraceSummary
```

Модуль не импортирует другие модули приложения; настройки — стандартные `OTEL_*` env.

## Публичный API

```python
from app.tracer import (TraceMiddleware, api_router, setup_tracing, span, get_tracer,
                        current_trace_id, current_span_context, SpanView, TraceView, TraceSummary)

setup_tracing(service_name="voice-router-backend", *, otlp_endpoint=None, buffer_spans=5000)
# идемпотентно; otlp_endpoint=None → OTEL_EXPORTER_OTLP_ENDPOINT; пусто → только буфер

with span("router", {"gen_ai.request.model": model}, kind=SpanKind.INTERNAL, links=[ctx]) as s:
    s.set_attribute("router.decision", "route")   # s — обычный opentelemetry Span
# исключение → record_exception + ERROR; CancelledError/GeneratorExit → событие cancelled, проброс

current_trace_id() -> str | None           # 32 hex текущей трассы
current_span_context() -> SpanContext | None  # для link из фоновых задач
```

`api_router` (тег `trace`):

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/traces/{trace_id}` | `TraceView`: сводка + `spans` — корни с `children[]`; 404, если нет |
| GET | `/traces?session_id=&limit=20` | `list[TraceSummary]`, новые первыми |

`SpanView`: `trace_id, span_id, parent_span_id, name, kind, start_time, end_time (ISO), duration_ms,
status (unset|ok|error), status_message, attributes, events[{name,time,attributes}],
links[{trace_id,span_id}], children[]`. `TraceSummary`: `trace_id, root_name, start_time,
duration_ms, span_count, session_id, turn_id, error`. Сессия индексируется, если **любой** span
трассы несёт `session.id`. При превышении `buffer_spans` удаляются трассы, дольше всех не получавшие span'ов.

## Подключение (следующий PR)

```python
# main.py
setup_tracing()                         # в начале lifespan (или до создания app)
app.add_middleware(TraceMiddleware)     # последним add_middleware → самый внешний
app.include_router(tracer.api_router)
```

`TraceMiddleware` должен быть самым внешним (после CORS и прочих `add_middleware`), чтобы
span покрывал весь запрос. В compose нужно пробросить `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_SERVICE_NAME`, `TRACE_EXPORT_CONTENT`.

## Контракт span'ов одного хода

```
POST /calls/{session_id}/turns/text        SERVER, из middleware
└─ turn          session.id, turn.id, call.generation, context.version
   ├─ stt        stt.provider, audio.mime, audio.bytes, language
   ├─ router     gen_ai.operation.name=chat, gen_ai.request.model,
   │             gen_ai.usage.input_tokens, gen_ai.usage.output_tokens,
   │             router.decision, router.scenario_id, router.confidence
   ├─ executor   scenario.id, executor.action
   │  ├─ kb.read     kind, key | source_id
   │  └─ rag.search  kind, search_mode (vector|lexical), hits
   └─ responder  gen_ai.* как у router, response.id
      └─ segment      response.id, segment.id, index, used_source_ids
         └─ tts.sentence  seq, chars, mime
```

- `/turns/audio` — тот же корень с шаблоном пути аудио.
- Фоновые агенты kernel: отдельный span `agent.run` (`agent.name`, `session.id`, `call.generation`)
  с **link** на `turn` (`links=[current_span_context()]`, захваченный при запуске) — они переживают запрос.
- Playback: фон сообщает замер после конца SSE, span `turn` уже закрыт. Фронт шлёт
  `POST .../playback` (и `cancel`) с `traceparent` из ответа хода — запрос попадает в ту же трассу,
  обработчик добавляет в текущий span событие `playback` (`eos_to_playback_ms`, `played_ms`, `response.id`).
- ПД в атрибуты не пишем (`client.id` можно, телефон/ИИН/email/ФИО — нет). Промпт/транскрипт —
  только `local.gen_ai.prompt`, `local.transcript`: видны в панели, в OTLP — при `TRACE_EXPORT_CONTENT=true`.
- `latency_ms` в `turn.done` и `timing` на доске остаются ради совместимости; позже считаем их из span'ов.

## Критерии приёмки
- [x] входящий `traceparent` продолжается: тот же trace_id, родитель — span браузера
- [x] SERVER span закрывается после потокового тела, дочерние span'ы вложены под ним
- [x] `GET /traces?session_id=` находит трассу
- [x] буфер вытесняет старые трассы
- [x] ПД в атрибутах маскируется; `local.*` не уходит в OTLP без флага

## Вне скоупа
Подключение в `main`/`call`/`kernel`, трассы браузера (frontend SDK), метрики и логи OTel,
хранение трасс в БД, auto-instrumentation библиотек.

## Ключевые тесты
`backend/tests/test_tracer.py`: traceparent + SSE + дерево, ошибка, вытеснение, маскирование ПД, отмена.
