# Миграции PostgreSQL

Alembic использует корневой `alembic.ini`, `env.py`, асинхронный DatabaseHelper
и реестр моделей `src.database.models.Base.metadata`. Настройки DB_* берутся
из типизированного `src/core/settings.py`; пароль не хранится в alembic.ini.

Команды выполняются из корня проекта. Для локального Python нужны зависимости
из uv.lock и доступный PostgreSQL:

```powershell
uv run --locked alembic upgrade head
uv run --locked alembic current
uv run --locked alembic check
```

Текущий head — `0002_imports`:

- `0001_auth`: пользователи, назначения объектов и серверные сессии.
- `0002_imports`: журнал попыток импорта и указатель опубликованного снимка.

Для подготовленного Docker-образа и работающих БД:

```powershell
docker compose run --rm --no-deps app uv run --locked --no-dev alembic upgrade head
docker compose run --rm --no-deps app uv run --locked --no-dev alembic current
docker compose run --rm --no-deps app uv run --locked --no-dev alembic check
```

Повтор `upgrade head` не создаёт повторных таблиц. `current` показывает
применённую ревизию, `check` проверяет расхождение ORM-моделей и схемы.
Миграции не создают учётную запись приложения и не импортируют CSV.
Запуск приложения не выполняет миграции автоматически.

ClickHouse имеет отдельный механизм SQL-миграций в `src/analytics/migrations/`
с проверкой SHA-256 применённых файлов:

```powershell
uv run --locked python -m src.analytics.migrations
docker compose run --rm --no-deps app uv run --locked --no-dev python -m src.analytics.migrations
```

Выберите локальный или Docker-вариант под своё окружение. Alembic не управляет
таблицами ClickHouse. Полный порядок подготовки обеих БД, создания пользователя,
импорта и запуска UI приведён в [корневом README](../../../README.md).
