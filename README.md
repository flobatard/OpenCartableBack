# OpenCartableBack

Backend part of a Web project to share courses, exercises and modules between teachers and with students.

Stack: **FastAPI** + **SQLAlchemy 2.0 (async)** + **Alembic** + **PostgreSQL (asyncpg)**.

## Architecture

```
app/
├── main.py            # FastAPI app factory + lifespan
├── core/              # cross-cutting concerns
│   ├── config.py      # settings (pydantic-settings, reads .env)
│   ├── database.py    # async engine, session, declarative Base
│   └── security.py    # password hashing + JWT
├── models.py          # model registry imported by Alembic
├── health/            # example feature: healthcheck
└── users/             # example feature (package-by-feature)
    ├── models.py      # SQLAlchemy models
    ├── schemas.py     # Pydantic schemas
    ├── service.py     # business logic
    └── router.py      # HTTP endpoints
alembic/               # migrations (async env.py)
tests/
```

Each new domain (courses, exercises, modules, ...) is a new package under `app/`
following the same layout. Remember to register its models in `app/models.py`.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit DATABASE_URL and SECRET_KEY
```

Generate a secret: `openssl rand -hex 32`.

## Database migrations

```bash
alembic revision --autogenerate -m "init"   # create a migration from the models
alembic upgrade head                         # apply migrations
alembic downgrade -1                         # roll back one revision
```

## Run

```bash
uvicorn app.main:app --reload
```

Docs at http://localhost:8000/docs · health at http://localhost:8000/api/v1/health

## Tests

```bash
pytest
```
