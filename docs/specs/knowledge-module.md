# Фича: модуль `knowledge` — загрузка кита в БД, каталог, данные, поиск, CRUD

- Владелец: зона C (данные)
- Связанные ADR: 0002, 0003, 0004 (ограничения на retrieval), 0005 (действия)
- Главная спека: разд. 1.2, 1.3, 2.2 («добавление сценария без разработчика»), 5.5, 6.2

## Цель
Один модуль, через который agent kernel (роутер, исполнитель, фоновый помощник) получает всё, что лежит в ките: каталог сценариев/действий/слотов, факты о клиентах и KB, поиск по фактам. Kernel не знает про JSON-файлы, таблицы и SQL.

## Находки по данным

- Кит — 7 JSON: `scenarios` (40 + 3 системных), `actions` (31 + 6 очередей + коды ошибок), `slots` (43), `mock_backend` (11 клиентов, 11 полисов, 4 обращения, 2 платежа), `knowledge_base` (≈140 листовых фактов: офисы, клиники, продукты, цены, документы, правила), `dialogs_sample`, `dev_utterances`.
- Всё маленькое (≈260 КБ). Каталог из 43 карточек целиком помещается в промпт роутера.
- **Ограничение из README кита:** encoder-классификаторы для выбора сценария запрещены. Спека 5.5 разрешает retrieval-шортлист сценариев только с пометкой в README и логированием расхождений. Инвариант: сужать каталог нельзя.

- **KB, клиенты и справочники — на английском**, клиенты говорят на ru/kk. Значения слотов в `slots.json` — английские enum (`city: Almaty`, `product_type: ogpo`, `document_type: policy_duplicate`), `topic` — свободная строка. Примеры сценариев — на ru/kk.

**Вывод 1:** нормализацию «ru/kk → словарь кита» делает LLM-роутер (он и так извлекает слоты). Модуль принимает нормализованные значения и ищет точно или лексически. Для `kb_lookup` модуль отдаёт **индекс тем KB** (`kb.search.topics()`), из которого LLM выбирает тему. Лексический поиск по сырым ru/kk-фразам в английской KB не работает — это задача эмбеддингов (фаза 2).

**Вывод 2:** поиск по сценариям — только подсказка для трассировки/отладки, никогда не фильтр для роутера. Основная ценность поиска — **KB и справочные данные** (`kb_lookup`, офисы, клиники) для исполнителя и фонового помощника.

## Устройство

```
backend/app/knowledge/
  __init__.py     # публичный API модуля — kernel импортирует только отсюда
  types.py        # Pydantic-типы: Scenario, Action, Slot, Client, Policy, Claim, Payment, Fact, Hit
  models.py       # таблица kit_records (SQLAlchemy)
  loader.py       # датасеты -> БД (идемпотентно), KB режется на темы по путям
  __main__.py     # python -m app.knowledge [--reset]  (just load-data)
  store.py        # низкоуровневый доступ к kit_records (kernel его не трогает)
  catalog.py      # Catalog: сценарии, системные намерения, действия, очереди, слоты
  records.py      # Records: клиенты, полисы, обращения, платежи, update_contact
  search.py       # Search: гибридный поиск (Postgres FTS + trigram), kb_lookup
  service.py      # Knowledge — фасад: Knowledge(session).catalog / .records / .search
  api.py          # FastAPI-роутер /kit: небольшой CRUD + поиск + reload
```

### Хранение

Одна таблица `kit_records` (JSONB):

| колонка | смысл |
|---|---|
| `kind` + `key` (PK) | `scenario/SC17`, `client/C004`, `kb/claims.submission`, `office/almaty-0` … |
| `payload` JSONB | исходный объект из кита без изменений |
| `search_text` | текст для поиска (ru/kk/en поля объекта) |
| `tsv` | generated `to_tsvector('simple', search_text)` + GIN |
| `origin` | `kit` (из файлов) или `user` (создано/изменено через CRUD) |

Файлы кита не меняются. Правки через CRUD живут только в БД, `reload --reset` возвращает чистый кит (eval всегда на чистом наборе).

### Поиск (фаза 1 — сейчас)

Гибрид без внешних API и ключей: `ts_rank` по `simple`-конфигурации (не стеммит, одинаково для ru/kk/en) + `similarity()` из `pg_trgm` (ловит казахскую и русскую морфологию: «өтінішім» ≈ «өтініш»). Скор = взвешенная сумма, фильтр по `kind`.

### Поиск (фаза 2 — если будет время и ключ провайдера)

Интерфейс `Search.query()` не меняется. Добавляется колонка `pgvector` и эмбеддер за адаптером (ADR 0008), скор смешивается с лексическим. Для сценариев — всё равно только подсказка.

## Внутренний API для kernel

```python
from app.knowledge import Knowledge

kb = Knowledge(session)                        # AsyncSession

await kb.catalog.scenarios()                   # list[Scenario], все 40, порядок кита
await kb.catalog.system_intents()              # 3 SYS_*
await kb.catalog.scenario("SC17")              # Scenario | None
await kb.catalog.scenario_ids()                # set[str] — для валидации ответа роутера
await kb.catalog.action("get_claim")           # Action | None
await kb.catalog.slot("phone")                 # Slot | None
await kb.catalog.queues()                      # list[str]

await kb.records.find_client(phone="+77010000004")   # Client | None (или iin=)
await kb.records.client("C004")
await kb.records.policies("C004")              # list[Policy]
await kb.records.claims(client_id="C004")      # list[Claim]
await kb.records.claim("CL-500311")
await kb.records.payments("C003", date="2026-09-30")
await kb.records.update_contact("C004", "email", "x@y.kz")   # только после подтверждения (решает executor)

await kb.search.query("фото акта", kinds=["kb"], limit=5)   # list[Hit(kind, key, score, snippet, payload)]
await kb.search.topics()                                    # list[str] — индекс тем KB для промпта
await kb.search.kb_lookup("claims.submission")              # Fact | None; точный путь или лексический поиск
                                                            # source="kb_lookup", source_id=<kb path>
await kb.search.offices("Almaty")              # list[Fact]
await kb.search.clinics("Almaty", specialty=None)
```

Все факты возвращаются как `Fact(key, value, source, source_id)` — формат доски (ADR 0003).
Ошибки уровня данных: `None` / пустой список; kernel сам маппит в коды `not_found` из `actions.json`.

## HTTP (для UI и отладки)

| Метод | Путь | Что |
|---|---|---|
| GET | `/kit/{kind}` | список записей |
| GET | `/kit/{kind}/{key}` | одна запись |
| PUT | `/kit/{kind}/{key}` | создать/заменить (`origin=user`); для `scenario/slot/action` — валидация Pydantic |
| DELETE | `/kit/{kind}/{key}` | удалить |
| GET | `/kit/search?q=&kind=&limit=` | поиск |
| POST | `/kit/reload?reset=true` | перезагрузить кит из файлов |

## Загрузка

- При старте backend: если `kit_records` пустая — загрузить кит из `DATASETS_DIR` (по умолчанию `/datasets`).
- `just load-data` — принудительная перезагрузка с `reset`.
- `dev_utterances.json` и `dialogs_sample.json` **не** загружаются (только evaluator).

## Критерии приёмки
- [ ] `just reset` → в БД 40 сценариев, 3 системных намерения, 31 действие, 43 слота, 11 клиентов, 4 обращения, KB-факты
- [ ] `kb.records.find_client(phone="+77010000004")` → C004, `claims` → CL-500311
- [ ] `kb.search.kb_lookup("claims.submission")` и `kb_lookup("claim document submission")` возвращают `claims.submission` с источником
- [ ] `kb.search.query("Өтінішім қандай күйде", kinds=["scenario"])` — подсказка SC17 в топе (ru/kk-примеры сценариев)
- [ ] PUT нового сценария через `/kit/scenario/SC41` → он есть в `catalog.scenarios()`; `reload?reset=true` его убирает
- [ ] Модуль не зависит от FastAPI вне `api.py`

## Вне скоупа
Эмбеддинги (фаза 2), шортлист сценариев для роутера, UI редактирования каталога, авторизация CRUD.

## Ключевые тесты
1. Loader: счётчики записей по видам после загрузки.
2. Records + Search: C004 → CL-500311; kb_lookup про отправку документов → `claims.submission`.
3. CRUD: PUT сценария невалидной формы → 422; валидный — появляется в каталоге.
