"""Тексты OpenAPI: общее описание и теги. Показываются в /docs и /redoc."""

DESCRIPTION = """
Голосовой бот страховой **Saqta Insurance** (HackAlem AI, кейс 2). Клиент говорит на русском,
казахском или вперемешку. LLM-роутер выбирает один из 40 сценариев кита, исполнитель читает данные,
и бот отвечает голосом. Каждое решение видно на доске (трассировка).

### Как пройти звонок

1. Фронт генерирует UUID сессии (`crypto.randomUUID()`): это ключ потока общения во всех вызовах.
   `POST /calls {"session_id": "<uuid>"}` открывает звонок и отдаёт `capabilities` (что живое, что мок).
   Вызов необязателен: первый ход по новому UUID открывает звонок сам.
2. `POST /calls/{session_id}/turns/audio` (push-to-talk) или `/turns/text` (резервный ввод).
   Ответ приходит потоком **Server-Sent Events**: транскрипт → решение роутера → действия и факты →
   текст ответа кусками → аудио по предложениям → `turn.done` с трассировкой и таймингами.
3. «Стоп» — оборвать fetch и/или `POST /calls/{id}/turns/{turn_id}/cancel`.
4. Когда звук начал играть, отправьте замер: `POST /calls/{id}/turns/{turn_id}/playback`.
5. Панель трассировки: `GET /sessions/{id}/board` (доска) и `GET /sessions/{id}/context` (контекст).
6. «Новый звонок»: `POST /calls/{id}/reset` (история прошлого звонка не протекает).

### Чтение потока в браузере

`EventSource` умеет только GET, поэтому читаем `fetch` + `ReadableStream`:

```js
const res = await fetch(`/api/calls/${sid}/turns/text`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text: "Что с моим заявлением?" }),
  signal: abort.signal, // abort.abort() = «стоп»
});
const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
let buf = "";
for (;;) {
  const { value, done } = await reader.read();
  if (done) break;
  buf += value;
  let i;
  while ((i = buf.indexOf("\\n\\n")) >= 0) {
    const block = buf.slice(0, i); buf = buf.slice(i + 2);
    const data = block.split("\\n").find((l) => l.startsWith("data: "));
    if (data) handle(JSON.parse(data.slice(6))); // по полю type
  }
}
```

Строки `: ping` — keepalive, их пропускаем. Схемы всех событий смотрите в разделе **Schemas**
(`TranscriptEvent`, `RoutingEvent`, …, `TurnDoneEvent`).

### Трассировка (OpenTelemetry)

Фронт может прислать W3C-заголовок `traceparent` — сервер продолжит его трассу. Каждый ответ
возвращает `traceparent` и `x-trace-id`; `turn.done` несёт тот же `trace_id`. Дерево хода
(сервер → `turn` → `stt` → `router` → `executor` → `responder` → `segment` → `tts.sentence`) —
`GET /traces/{trace_id}`, трассы звонка — `GET /traces?session_id=<uuid>`. Замеры `playback`
и `cancel` фронт шлёт с `traceparent` хода, чтобы они легли в ту же трассу.

### Режим без ключей

По умолчанию `STT_PROVIDER`, `LLM_PROVIDER` и `TTS_PROVIDER` равны `mock`. Звонок не падает:
в потоке приходит `error` с кодом `llm_unavailable` или `stt_unavailable` и честный ответ, без
выдуманного сценария. Что именно включено, видно в `GET /capabilities`.

### Правила данных

«Сегодня» — `2026-10-01` (`dataset_today`). Факты берутся только из кита и несут `source`/`source_id`.
Через фронт все пути доступны с префиксом `/api` (Next rewrites).
"""

TAGS = [
    {
        "name": "call",
        "description": "Звонок: сессия, реплика с потоковым ответом (SSE), стоп, замеры, "
        "устройство слоя роутера. Спека: `docs/specs/call-api.md`.",
    },
    {
        "name": "context",
        "description": "Рабочая память звонка для панели трассировки: снимок `SessionContext` и "
        "append-only доска `BoardEntry`. Спека: `docs/specs/context-module.md`.",
    },
    {
        "name": "kit",
        "description": "Стартовый кит в БД: сценарии, действия, слоты, клиенты, KB. Поиск, "
        "небольшой CRUD (правки только в БД) и перезагрузка чистого кита. "
        "`kind`: `scenario`, `system_intent`, `action`, `slot`, `client`, `policy`, `claim`, "
        "`payment`, `kb`, `office`, `clinic`, `inspection_point`, `queue`. "
        "Спека: `docs/specs/knowledge-module.md`.",
    },
    {
        "name": "trace",
        "description": "Трассы хода в формате OpenTelemetry для панели: дерево span'ов с "
        "таймингами, решением роутера, чтениями и ошибками. Хранятся в памяти процесса "
        "(после рестарта пусто), при `OTEL_EXPORTER_OTLP_ENDPOINT` уходят и по OTLP. "
        "Спека: `docs/specs/tracer-module.md`.",
    },
    {"name": "health", "description": "Проверка, что backend и БД живы."},
]

SSE_EXAMPLE = """event: transcript
data: {"turn_id":1,"type":"transcript","text":"Что с моим заявлением по затоплению?","language":"ru","source":"stt"}

event: turn.started
data: {"turn_id":1,"type":"turn.started","session_id":"9f2c…","generation":1,"context_version":0}

event: routing
data: {"turn_id":1,"type":"routing","decision":"route","scenarios":[{"scenario_id":"SC17","confidence":0.91,"reason":"спрашивает статус, а не документы (SC18)"}],"alternatives":[{"scenario_id":"SC18","confidence":0.3}],"language":"ru","slots":{"claim_number":null},"is_continuation":false,"clarify_options":[]}

event: reply.delta
data: {"turn_id":1,"type":"reply.delta","text":"Сейчас проверю."}

event: audio
data: {"turn_id":1,"type":"audio","seq":0,"mime":"audio/mpeg","data":"SUQzBAAAAA…","text":"Сейчас проверю."}

event: reply.delta
data: {"turn_id":1,"type":"reply.delta","text":" Назовите, пожалуйста, номер телефона."}

event: reply.done
data: {"turn_id":1,"type":"reply.done","text":"Сейчас проверю. Назовите, пожалуйста, номер телефона.","language":"ru"}

event: turn.done
data: {"turn_id":1,"type":"turn.done","transcript":"Что с моим заявлением по затоплению?","language":"ru","scenarios":[{"scenario_id":"SC17","confidence":0.91,"reason":"…"}],"alternatives":[{"scenario_id":"SC18","confidence":0.3}],"reason":"…","slots":{"claim_number":null},"actions":[],"reply":"Сейчас проверю. Назовите, пожалуйста, номер телефона.","latency_ms":{"stt":410,"router":620,"reads":35,"response_first_token":180,"response":540,"tts_first_audio":390,"total":1640},"context_version":2}
"""
