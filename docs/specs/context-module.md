# Фича: модуль `context` — состояние звонка, доска, версии, патчи, подтверждения

- Владелец: зона A
- Связанные ADR: 0003 (доска, `SessionContext`), 0005 (подтверждения, темы), 0006 (патчи, версии), 0009 (модули)
- Главная спека: разд. 3.1, 4.3–4.6, 6.3–6.4, 7.1, 7.5, 15.3–15.4

## Цель
Один модуль владеет рабочей памятью звонка: `SessionContext` + append-only доска `BoardEntry`.
Kernel (роутер, исполнитель, фоновый помощник, API звонка) меняет состояние только через него.
Модуль гарантирует версии, атомарность, подтверждения и отсутствие протечки между звонками.

## Устройство

```
backend/app/context/
  __init__.py   # публичный API — остальные модули импортируют только отсюда
  types.py      # SessionContext, Fact, PendingConfirmation, Turn, BoardEntry, ContextPatch, PatchResult
  mutation.py   # Mutation: транзакция над рабочей копией, решает, растёт ли версия
  service.py    # Contexts: единственный писатель, asyncio.Lock на сессию
  store.py      # ContextStore: PgStore (прод) и MemoryStore (тесты)
  models.py     # таблицы sessions, board_entries (JSONB)
  views.py      # router_view(), handoff_summary() — чистые функции над снимком
  api.py        # GET /sessions/{id}/context, /sessions/{id}/board
```

Рабочий кеш — память процесса (один процесс, ADR 0003), сохранённый снимок читается через `load`.
Каждое изменение под lock'ом сессии
пишется в Postgres одной транзакцией: upsert `sessions` + insert `board_entries`.
Перезапуск завершает активную генерацию; сохранённые история и журнал доступны для чтения.

Agent kernel использует те же `sessions`/`board_entries`, без отдельных таблиц сессий:
`SessionContext.kernel` хранит его runtime projection и исключён из публичной сериализации
`/context`. `Contexts.kernel_create/get/change/events/open_sessions` — единственный путь
его чтения и атомарных изменений; `Repository` ядра является адаптером этих методов.
События хранятся как `BoardEntry(type="kernel", payload=envelope)`; envelope содержит
монотонный `seq`, generation и visibility. `/board` скрывает внутренние kernel-события,
а SSE использует отдельный отфильтрованный replay. Runtime-изменения не повышают
доменный `context_version`; актуальность фона проверяется по generation/input_revision.
Reset очищает projection, повышает generation и сохраняет монотонность курсора.
История bot сама по себе не подтверждает доставку: playback определяется только ACK ядра.

Типы `Fact`, `BoardEntry`, `ContextPatch` принадлежат `context`. `add_facts` и `ContextPatch` принимают и `knowledge.Fact` (те же поля).

## Публичный API

```python
from app.context import Contexts, ContextPatch, Fact, router_view, handoff_summary

ctx: Contexts = app.state.contexts          # в хендлерах: Depends(get_contexts)

# жизненный цикл
snap = await ctx.start_call(session_id=None)          # новый звонок; для существующей сессии generation+1
turn = await ctx.begin_turn(sid, transcript, "kk")    # новый turn_id + utterance; версию не меняет
await ctx.add_reply(sid, turn, text, "kk")            # ответ бота; версию не меняет
snap = await ctx.snapshot(sid)                        # копия, её правка ничего не меняет

# значимые изменения: одна транзакция — версия +1 (если что-то реально поменялось)
async with ctx.mutate(sid, turn_id=turn, author="executor") as m:
    m.record_routing(router_json, confidence)         # запись routing + low_confidence_streak (без версии)
    m.set_client("C004")                              # None→C004: идентификация; C004→C005: новое поколение
    m.switch_topic("SC33")                            # текущая тема уходит в стек
    m.finish_topic()  -> "SC17" | None                # закрыть, вернуть тему, к которой предложить вернуться
    m.resume_topic()  -> "SC17" | None                # снять вершину стека
    m.set_slots("SC17", {"claim_number": "CL-500311"})# None не затирает; смена params → сброс подтверждения
    m.add_facts([Fact(key, value, source, source_id)])
    m.request_confirmation("update_contact", params)
    m.cancel_confirmation()
    m.ctx                                             # рабочая копия для чтения
# исключение внутри блока → ничего не записано. Внутри блока LLM не ждём: держится lock.

ok = await ctx.consume_confirmation(sid, "update_contact", params)  # True ровно один раз
res = await ctx.apply_patch(patch)                    # PatchResult: applied | stale | rejected
await ctx.log(sid, turn, "timing", "router", {...}, ts_start_ms=..., ts_end_ms=...)  # без версии
await ctx.cancel_turn(sid, turn); ctx.is_current_turn(sid, turn)                    # «стоп» (7.5)
ctx.on_reset(lambda sid, generation: ...)             # фон отменяет свои задачи
await ctx.board(sid, since_turn=None)                 # list[BoardEntry] для панели

router_view(snap, last_turns=4)   # dict для переменной части промпта роутера
handoff_summary(snap)             # dict с контекстом для оператора
```

## Правила
- Версия растёт: клиент, активная тема/стек, слоты, факты, подтверждение, применённый патч.
  Не растёт: utterance, response, routing, timing, trace, action, отмена хода, отклонённый патч.
- `context_version` монотонна через поколения. `generation` растёт при `start_call` для существующей
  сессии и при смене одного клиента на другого; тогда темы, слоты, факты, подтверждение и история сбрасываются.
- `apply_patch`: чужой `generation` или `client_id` → `rejected`; `base_context_version` ≠ текущей → `stale`.
  Любой исход пишется на доску (`type=patch`, `author=background`). Применённые факты — `origin=background`.
- `pending_confirmation` привязан к `action` + `params` + теме. Смена параметра в слотах, отмена,
  смена или закрытие темы → сброс. `consume_confirmation` атомарно сбрасывает его, повтор → `False`.
- Типы записей доски: `call, utterance, response, routing, client, topic_stack, slots, facts,
  confirmation, patch, action, timing, turn, trace, error`. У записи есть `generation`.
- `ts_*_ms` — epoch ms. Точные тайминги этапов передаёт вызывающий в `log(...)`.

## HTTP (для панели, зона B)

| Метод | Путь | Что |
|---|---|---|
| GET | `/sessions/{id}/context` | текущий `SessionContext`, 404 если нет |
| GET | `/sessions/{id}/board?since_turn=` | записи доски по порядку |

## Критерии приёмки
- [x] `just reset` → миграция создаёт `sessions`, `board_entries`
- [x] timing/utterance/routing не меняют `context_version`; слоты и факты меняют (одна транзакция — +1)
- [x] патч: stale / rejected / applied, все исходы на доске
- [x] смена параметров или отмена инвалидирует подтверждение; `consume_confirmation` истинно ровно один раз
- [x] новый звонок и смена клиента не протекают
- [x] FastAPI только в `api.py`

## Вне скоупа
Реестр `asyncio.Task` фонового помощника (зона C, через `on_reset` и `generation`),
возобновление активной генерации после рестарта, несколько процессов, редактирование доски.

## Ключевые тесты
`backend/tests/test_context.py` на `MemoryStore`, без БД и LLM: версии, откат, патчи, подтверждения,
изоляция звонков, стек тем и отмена хода.
