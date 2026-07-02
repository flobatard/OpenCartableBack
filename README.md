# OpenCartableBack

Backend part of a Web project to share courses, exercises and modules between teachers and with students.

Stack: **FastAPI** + **SQLAlchemy 2.0 (async)** + **Alembic** + **PostgreSQL (asyncpg)** + **Zitadel (OIDC)**.

## Architecture

```
app/
├── main.py            # FastAPI app factory + lifespan + CORS
├── core/              # cross-cutting concerns
│   ├── config.py      # settings (env > .env > config/<APP_ENV>.yaml)
│   ├── database.py    # async engine, session, declarative Base
│   └── auth.py        # Zitadel JWT validation (JWKS) + get_current_user
├── models/            # one module per model, all re-exported in __init__.py
├── health/            # healthcheck (public)
└── auth/              # protected demo route GET /me
config/                # public per-environment settings (development.yaml, production.yaml)
alembic/               # migrations (async env.py)
tests/                 # pytest (no network, no real Zitadel needed)
```

Each new domain (courses, subjects, resources, ...) is a new package under `app/`
with its `schemas.py`, `service.py` and `router.py`; its models go in
`app/models/<name>.py` and are imported (and listed in `__all__`) in
`app/models/__init__.py` so Alembic autogenerate sees them.

## Configuration

Settings resolve with decreasing priority: environment variables > `.env` >
`config/<APP_ENV>.yaml` (`APP_ENV` defaults to `development`). Public,
versioned values (CORS, hosts, OIDC issuer/audience) live in `config/`;
secrets and local overrides (`POSTGRES_PASSWORD`, optional full
`DATABASE_URL`) live in `.env`. The database URL is assembled from the
`POSTGRES_*` components with proper password encoding; a managed-Postgres URL
(e.g. Neon, with `sslmode=...`) can be pasted as-is in `DATABASE_URL`, it is
normalized for asyncpg.

## Authentication

The OIDC login flow (Authorization Code + PKCE) is handled entirely by the
Angular SPA. **This API never issues tokens**: it validates the Zitadel access
token on every request — RS256 signature via the JWKS (discovered from the
issuer), plus `issuer`, `audience` and expiration checks. Only two settings are
needed: `OIDC_ISSUER` and `OIDC_AUDIENCE`.

Zitadel checklist (in the Zitadel console):
1. Create the project and the API application.
2. Enable **JWT access tokens** (Zitadel issues opaque tokens by default —
   `/me` will return 401 with an opaque token).
3. Enable "add roles to access token claims" if roles are needed.
4. Put the instance URL in `OIDC_ISSUER` and the client/project id in
   `OIDC_AUDIENCE`.

Protected routes use the `get_current_user` dependency
([app/core/auth.py](app/core/auth.py)); `GET /api/v1/me` is the reference
example. `/api/v1/health` stays public.

## Setup (local dev)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # secrets ; put your Zitadel URLs in config/development.yaml
```

## Run

### Docker (nominal)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD (and APP_ENV=production on the Pi)
docker compose up --build
```

Migrations run automatically on startup (`alembic upgrade head`), then the API
listens on port 8000. The reverse proxy (nginx) in front of the API is managed
by the infra, outside this repo.

### Bare (dev)

```bash
uvicorn app.main:app --reload
```

Docs at http://localhost:8000/docs · health at http://localhost:8000/api/v1/health

## Database migrations

```bash
alembic revision --autogenerate -m "..."   # create a migration from the models
alembic upgrade head                        # apply migrations
alembic downgrade -1                        # roll back one revision
```

Manual run inside Docker: `docker compose run --rm api alembic upgrade head`.

## Tests

```bash
pytest
```

Auth tests sign tokens with a generated RSA key and stub the JWKS cache — no
network and no real Zitadel instance required.
