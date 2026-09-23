# 0009. Модульный backend с явным публичным API

- Статус: accepted
- Дата: 2026-09-23
- Автор: ye
- Заменяет: правило «все контракты в `backend/app/schemas/`» из AGENTS.md

## Контекст
Три человека пишут kernel параллельно (роутер, исполнитель, контекст, данные, фон). Общая папка схем
становится местом конфликтов и не говорит, кто владеет типом и кто может его менять.

## Решение
- Backend делится на модули `backend/app/<module>/`: `context`, `knowledge`, `router`, `executor`, `background`, `speech`.
- Публичный API модуля — его `__init__.py`. Импорт из внутренних файлов чужого модуля запрещён.
- Типы живут у владельца: `SessionContext`, `BoardEntry`, `Fact`, `ContextPatch` — в `app.context`;
  `Scenario`, `Client`, `Claim` — в `app.knowledge`. Общего `schemas/` нет.
- Модуль скрывает хранилище (таблицы, JSON). FastAPI — только в `api.py` модуля.
- Направление зависимостей: `router`, `executor`, `background` → `context`, `knowledge`;
  `knowledge` → `context` только ради `Fact`; `context` ни от кого из kernel не зависит.

## Альтернативы
- Общий `schemas/` — нет явного владельца, конфликты в одном файле.

## Последствия
- Модуль можно писать и тестировать изолированно (in-memory store, без БД и LLM).
- Смена публичного API = правка спеки модуля + апрув владельца.
