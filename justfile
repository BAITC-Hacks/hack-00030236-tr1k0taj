set dotenv-load

# список команд
default:
    @just --list

# поднять всё (сборка + запуск)
up:
    docker compose up -d --build

# остановить
down:
    docker compose down

# снести всё вместе с данными БД и поднять заново
reset:
    docker compose down -v --remove-orphans
    docker compose up -d --build

# снести всё: контейнеры, данные, собранные образы
nuke:
    docker compose down -v --remove-orphans --rmi local

# пересобрать образы без кэша
build:
    docker compose build --no-cache

# статус сервисов
ps:
    docker compose ps

# логи (все или конкретного сервиса: just logs backend)
logs *service:
    docker compose logs -f --tail=100 {{service}}

# ключевые тесты
test:
    docker compose exec -e MOCK_MODE=true -e EMBEDDINGS_ENABLED=false -e OPENAI_API_KEY= backend pytest -q

# линтеры
lint:
    docker compose exec backend ruff check .
    docker compose exec frontend npm run lint

# применить миграции
migrate:
    docker compose exec backend alembic upgrade head

# создать миграцию из моделей: just migration "add users"
migration message:
    docker compose exec backend alembic revision --autogenerate -m "{{message}}"

# перезагрузить стартовый кит из datasets/ в БД (сбрасывает правки через CRUD)
load-data:
    docker compose exec backend python -m app.knowledge --reset

# psql в базу
psql:
    docker compose exec db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB'

# shell в контейнер: just sh backend
sh service="backend":
    docker compose exec {{service}} sh

# локальные проверки без Docker (uv + Python 3.13, PostgreSQL на localhost:55432)
[positional-arguments]
test-local *args:
    MOCK_MODE=true EMBEDDINGS_ENABLED=false OPENAI_API_KEY= ./infra/backend-local.sh pytest -q "$@"

[positional-arguments]
lint-backend-local *args:
    ./infra/backend-local.sh ruff check . "$@"

migrate-local:
    ./infra/backend-local.sh alembic upgrade head

run-local:
    ./infra/backend-local.sh uvicorn app.main:app --host 127.0.0.1 --port 8000

# HTTP SSE + Postgres smoke; live prompts for a key without saving it
smoke-local:
    ./infra/backend-local.sh python -m scripts.kernel_smoke

smoke-live-local:
    ./infra/backend-local.sh python -m scripts.kernel_smoke --live

index-local:
    ./infra/backend-local.sh python -m scripts.index_knowledge

# отдельный локальный PostgreSQL 17; PG_BIN можно переопределить
db-local:
    ./infra/postgres-local.sh start

db-stop-local:
    ./infra/postgres-local.sh stop

[positional-arguments]
psql-local *args:
    ./infra/postgres-local.sh psql "$@"
