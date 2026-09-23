# 0001. Стек и архитектура

- Статус: accepted
- Дата: 2026-09-23
- Автор: ye

## Контекст
Хакатон 5 часов, 3 человека. Нужен стек, который все знают и который поднимается одной командой.

## Решение
- Backend: Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2 (async, asyncpg), Alembic. Зависимости через uv.
- БД: PostgreSQL 17.
- Frontend: Next.js (App Router), TypeScript. Браузер ходит на `/api/*`, Next проксирует на backend (rewrites), поэтому CORS не нужен.
- Локально всё поднимается через `docker compose` в dev-режиме с hot reload (код примонтирован volume'ом).
- Команды: `justfile` (`just up`, `just test`, `just reset` и т.д.).
- Миграции применяются автоматически при старте backend.

## Альтернативы
- Django — тяжелее для API-only сервиса.
- Отдельный SPA (Vite) — Next даёт роутинг и прокси из коробки.

## Последствия
- Один origin для фронта и API, простая локальная разработка.
- Dev-режим в compose не подходит для прода. Для демо этого достаточно.
