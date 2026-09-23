# Фича: модуль `executor` — исполнитель сценариев, ответ, фон

- Владелец: зона A
- Связанные ADR: 0005 (исполнитель), 0006 (фон), 0008 (моки), 0009 (модули)
- Связанные спеки: call-api.md (разд. «Порты этапов»), context-module.md, knowledge-module.md

## Цель
Агентная сторона хода после роутера: выполнить сценарий (действия, факты, контекст) и собрать
`ReplyBrief` для генератора ответа. Может импортировать `router`, `context`, `knowledge` через их пакеты.

## Сценарий
1. `call` передаёт `TurnInput` (решение роутера, снимок, `Contexts`, `Knowledge`) в `Executor.execute`.
2. Исполнитель возвращает `Execution` (действия, факты, brief); `Responder.stream(brief)` отдаёт текст кусками.

## Контракт
Публичный API — только `from app.executor import ...`:
- типы: `ActionCall`, `ActionMode`, `ReplyBrief`, `ReplyLanguage`, `Execution`, `TurnInput`;
- порты: `Executor`, `Responder`, `Background`;
- реализации: `BaselineExecutor` (тема, стек, слоты, handoff без чтений), `TemplateResponder`
  (ориентир кита без LLM), `NoopBackground`;
- `reply_language(language, fallback)`.

Все имена реэкспортируются из `app.call` для совместимости.

## Критерии приёмки
- [ ] поведение хода не изменилось (перенос кода)

## Вне скоупа
Боевой исполнитель с чтениями и подтверждениями (отдельные PR).

## Ключевые тесты
Покрываются `tests/test_call_api.py`, `tests/test_call_kernel.py`.
