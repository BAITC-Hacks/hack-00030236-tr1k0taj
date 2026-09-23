# Фича: модуль `tracer` — трасса хода в формате OpenTelemetry

- Владелец: зона A
- Связанные ADR: 0013 (OpenTelemetry), 0009 (модули), 0011 (kernel)
- Главная спека: разд. 10 (тайминги); контракт звонка — [call-api.md](call-api.md)

## Цель
Сквозная обзервабилити звонка: одна трасса на ход (от запроса фронта через STT, роутер,
исполнитель, kernel, SQL и TTS до ответа) с таймингами, решением роутера, репликой и токенами
по моделям. Все трассы привязаны к сессии звонка и хранятся в Postgres (`trace_spans`).
Панель читает трассу звонка и историю по HTTP, при желании — Jaeger по OTLP.

## Сценарий
1. Фронт отправляет ход с заголовком `traceparent` (необязательно) и получает в ответе
   `traceparent` и `x-trace-id` серверного span'а.
2. `TraceMiddleware` открывает SERVER span и закрывает его после последнего куска SSE.
3. Call/kernel оборачивают этапы в `span(...)`, SQL даёт span'ы `db.query`; завершённые span'ы
   попадают в буфер памяти, очередь записи в `trace_spans` (и в OTLP, если задан).
4. Панель берёт `GET /traces/sessions/{uuid}` (звонок), `GET /traces/sessions` (история),
   `GET /traces/{x-trace-id}` (дерево хода).

## Устройство

```
backend/app/tracer/
  __init__.py    # публичный API
  setup.py       # setup_tracing, span(), get_tracer, current_*; OTLP-экспорт с маскированием
  middleware.py  # TraceMiddleware (чистый ASGI)
  store.py       # SpanStore: SpanProcessor, ring buffer по trace_id и session.id, sink → writer
  redact.py      # маскирование ПД
  models.py      # таблица trace_spans (Core Table, своя MetaData `trace_metadata`)
  persist.py     # SpanWriter: очередь + asyncio-сбросчик; start/stop_persistence
  sql.py         # instrument_sqlalchemy: span'ы db.query из событий SQLAlchemy
  query.py       # чтение: trace_spans + несброшенный буфер
  views.py       # Pydantic-ответы и агрегация звонка (ходы, этапы, токены)
  api.py         # GET /traces/sessions, /traces/sessions/{id}, /traces/{trace_id}, /traces
```

Модуль не импортирует другие модули приложения (ADR 0012, слой infra): фабрику сессий и engine
передаёт `main`. Настройки — стандартные `OTEL_*` env. Миграция — `0004_trace_spans`
(`migrations/env.py`: `target_metadata = [Base.metadata, trace_metadata]`).

## Публичный API

```python
from app.tracer import (TraceMiddleware, api_router, setup_tracing, span, get_tracer,
                        current_trace_id, current_span_context,
                        start_persistence, stop_persistence, flush_traces, persistence_stats,
                        instrument_sqlalchemy, suppress_sql_spans, trace_metadata, trace_spans,
                        SpanView, TraceView, TraceSummary, SessionTrace, SessionTraceSummary,
                        TurnTrace, ModelUsage, TokenTotals)

setup_tracing(service_name="voice-router-backend", *, otlp_endpoint=None, buffer_spans=5000)
# идемпотентно; otlp_endpoint=None → OTEL_EXPORTER_OTLP_ENDPOINT; пусто → только буфер

await start_persistence(session_factory, *, max_queue=10000, batch_size=200, interval=0.3)
# session_factory — async_sessionmaker; идемпотентно; сам вызывает setup_tracing()
await stop_persistence()        # отключает запись и сбрасывает остаток очереди
await flush_traces() -> int     # сбросить очередь сейчас (тесты)
persistence_stats() -> dict | None   # {queued, flushed, dropped, failed}

instrument_sqlalchemy(engine, *, only_with_parent=True)  # Engine или AsyncEngine, идемпотентно
with suppress_sql_spans(): ...  # SQL внутри блока не трассируется

with span("router", {"gen_ai.request.model": model}, kind=SpanKind.INTERNAL, links=[ctx]) as s:
    s.set_attribute("router.decision", "route")   # s — обычный opentelemetry Span
# исключение → record_exception + ERROR; CancelledError/GeneratorExit → событие cancelled, проброс

current_trace_id() -> str | None           # 32 hex текущей трассы
current_span_context() -> SpanContext | None  # для link из фоновых задач

TraceMiddleware(app, exclude_prefixes=("/traces", "/health"))  # опрос панели не трассируется
```

### Подключение в `main.py` (PR wiring)

```python
from app import tracer
from app.db import SessionLocal, engine

@asynccontextmanager
async def lifespan(application):
    tracer.setup_tracing()
    tracer.instrument_sqlalchemy(engine)          # до первых запросов
    await tracer.start_persistence(SessionLocal)
    ...
    try:
        yield
    finally:
        ...                                        # kernel.shutdown() и прочее
        await tracer.stop_persistence()            # до engine.dispose()
        await engine.dispose()

app.include_router(tracer.api_router)
app.add_middleware(tracer.TraceMiddleware)         # последним add_middleware → самый внешний
```

`TraceMiddleware` должен быть самым внешним (после CORS и прочих `add_middleware`), чтобы
span покрывал весь запрос. В compose нужно пробросить `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_SERVICE_NAME`, `TRACE_EXPORT_CONTENT`.

## Хранение

- Таблица `trace_spans`: `span_id` (pk), `trace_id` (idx), `parent_span_id`, `session_id`,
  `turn_id`, `name`, `kind`, `start_ns`, `end_ns`, `duration_ms`, `status`, `status_message`,
  `attributes`/`events`/`links`/`resource` (JSONB); индекс `(session_id, start_ns)`.
- Запись: `SpanStore.on_end` (любой поток) кладёт замаскированную запись в ограниченную очередь
  (`max_queue`); переполнение — запись выбрасывается и считается в `dropped`, звонок не ждёт.
  Сбросчик — asyncio-задача: пачка раз в `interval` (0.3 с) или при `batch_size` (200) записях,
  `INSERT ... ON CONFLICT DO NOTHING`. Ошибка БД → warning в лог, `failed`, звонок не падает.
- Собственный SQL tracer'а (вставка, backfill, чтение) помечен `execution_options(tracer_skip=True)`
  и выполняется в `suppress_sql_spans()` — петли «запись трасс → новые span'ы» нет.
- Привязка к звонку: `session_id` span'а — свой `session.id`, иначе от любого span'а той же
  трассы. При записи: подсказка trace_id → сессия заполняется уже в `on_start` span'а с
  `session.id` (дети `turn` закрываются раньше него), плюс backfill `UPDATE` span'ам трассы,
  сброшенным раньше. При чтении: сессия = все span'ы трасс, где хоть один span её несёт.
  `turn.id` → `turn_id` так же. SERVER span получает `session.id` из параметра пути `session_id`.
- Чтение: `trace_spans` ∪ буфер памяти (ещё не сброшенное), без дублей по `span_id`.
  Без `start_persistence` (или при ошибке БД) — только буфер, как раньше.
- После рестарта трассы остаются (буфер памяти пуст, но БД — нет).
- `TODO(hack):` нет TTL/очистки `trace_spans`; для демо хватает, в проде — партиции/ретеншн.

## HTTP (`api_router`, тег `trace`)

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/traces/sessions?limit=50&offset=0` | `list[SessionTraceSummary]` — история звонков, новые (по последней активности) первыми |
| GET | `/traces/sessions/{session_id}` | `SessionTrace` — сквозная трасса звонка; 404, если нет |
| GET | `/traces/{trace_id}` | `TraceView`: сводка + `spans` — корни с `children[]`; 404, если нет |
| GET | `/traces?session_id=&limit=20` | `list[TraceSummary]`, новые первыми |

`/traces/sessions*` объявлены раньше `/traces/{trace_id}`.

- `SessionTraceSummary`: `session_id, first_seen, last_seen, duration_ms, traces, turns, errors,
  llm_calls, tokens{input,output,total}, tokens_by_model{model:{input,output,calls}},
  last_transcript, scenarios[]`.
- `SessionTrace`: `summary`, `turns[]`, `traces[]` (все трассы сессии: ходы, cancel, playback,
  фоновые `agent.run`; по времени).
- `TurnTrace`: `turn_id, trace_id, started_at, duration_ms, transcript, input_source,
  router{decision,scenario_id,confidence}, stages{имя: мс}, tokens_by_model, errors[]`.
  `stages` — прямые дети span'а `turn`; повторы одного имени — окно от первого начала до
  последнего конца (параллельные ветки не складываем).
- Токены: usage span'а не считаем, если usage есть у его потомка (`router` + вложенный
  `llm.call`) — без двойного счёта. Модель — `gen_ai.response.model`, иначе `gen_ai.request.model`.
- `SpanView`: `trace_id, span_id, parent_span_id, name, kind, start_time, end_time (ISO), duration_ms,
  status (unset|ok|error), status_message, session_id, attributes, events[{name,time,attributes}],
  links[{trace_id,span_id}], resource, children[]`. `TraceSummary`: `trace_id, root_name,
  start_time, duration_ms, span_count, session_id, turn_id, error`.
- В ответах API есть `local.*` (транскрипт, промпт) — это локальная панель; в OTLP они уходят
  только при `TRACE_EXPORT_CONTENT=true`. Маскирование ПД (ключи и шаблоны телефона, ИИН, email)
  — до буфера и до записи в БД.

## Контракт атрибутов (на них опирается агрегация)

| Атрибут / span | Где | Для чего |
|---|---|---|
| `session.id` | `turn`, `agent.run`, SERVER (из пути) | привязка к звонку |
| `turn.id` | `turn` | номер хода, `turn_id` |
| `input.source` | `turn` | text / audio |
| `local.transcript` | `stt` (или `turn`) | распознанная реплика |
| `router.decision`, `router.scenario_id`, `router.confidence` | `router` | маршрутизация |
| `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.provider.name` | `llm.call` | модель |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | `llm.call` | токены |
| `error.code`, `error.stage` | любой span | ошибки звонка |
| span'ы `turn`, `stt`, `router`, `executor`, `responder`, `segment`, `tts.sentence` | ход | этапы (`stages` — прямые дети `turn`) |
| span'ы `llm.call`, `rag.search`, `kb.read`, `action` | внутри этапов | запросы и действия |
| span `agent.run` | отдельная трасса с link на `turn` | фоновые агенты |
| span `db.query` (CLIENT) | `instrument_sqlalchemy` | SQL: `db.system.name=postgresql`, `db.operation.name`, `db.query.text` (без параметров), `db.collection.name`, `db.response.returned_rows` |

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
- [x] ПД в атрибутах маскируется (и до записи в БД); `local.*` не уходит в OTLP без флага
- [x] span'ы пишутся в `trace_spans`, `GET /traces/sessions/{id}` читает их из БД: ходы, этапы,
      токены по моделям
- [x] span без `session.id` привязан к звонку по трассе (backfill + чтение)
- [x] история звонков — новые первыми
- [x] `db.query` вложен под текущий span; запись трасс не порождает span'ов

## Вне скоупа
Подключение в `main`/`call`/`kernel` (PR wiring), трассы браузера (frontend SDK), метрики и логи OTel,
TTL трасс, auto-instrumentation пакеты OTel (SQL — свои события SQLAlchemy).

## Ключевые тесты
`backend/tests/test_tracer.py`: traceparent + SSE + дерево, ошибка, вытеснение, маскирование ПД, отмена.
`backend/tests/test_tracer_persistence.py`: БД-roundtrip звонка (этапы, токены по двум моделям,
маскирование в БД), наследование сессии, порядок истории, `db.query` + нет петли при сбросе.
