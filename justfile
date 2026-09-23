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
    docker compose exec backend pytest -q

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
