# OpenCartableBack

API d'**OpenCartable**, plateforme pédagogique libre (AGPLv3) et auto-hébergée (Raspberry Pi ARM64) : un prof compose ses cours par blocs, les partage à ses élèves par liens publics, et dispose d'un assistant IA ; un élève connecté dispose d'un tuteur IA d'exercice.

Stack : **FastAPI** · **SQLAlchemy 2.0 async** + **asyncpg** · **Alembic** · **PostgreSQL** (FTS `french_unaccent`) · **S3** (MinIO en dev) · **Zitadel** (OIDC, validé côté API) · **LangChain / LangGraph** (client IA multi-provider, BYO token).

## Ce que fait l'API

- **Comptes et profil** (`users`) : auto-provisionnés depuis le JWT, onboarding, avatar, credential IA chiffré par utilisateur (`ai_credentials`).
- **Taxonomies** (`subjects`, `education_levels`) : matières et niveaux d'étude seedés, arbres en lecture.
- **Cours** (`courses`) : blocs ordonnés de quatre types (texte markdown, exercice à corrigé, document, module), réglages de lecture, visibilité.
- **Bibliothèques** : ressources S3 par upload présigné direct (`resources`), modules interactifs HTML/CSS/JS (`modules`).
- **Partage** (`share_links`, `public`) : liens à token opaque et régime public **sans JWT** (visibilité + token vérifiés à chaque requête, corrigés jamais servis).
- **Recherche** (`search`) : plein texte Postgres sur les cours publics et les profs opt-in, sans JWT.
- **Export / import** (`course_transfer`) : archive `.zip` d'un cours, réimport en cours neuf.
- **IA** : assistant de cours du prof avec tools de lecture et propositions d'édition validées humainement (`course_assistant`), tuteur d'exercice de l'élève authentifié (`student_exercises`), client générique (`core/ai`).
- **Maintenance** (`maintenance`) : job de purge des données (rétentions, orphelins S3), hors API.

## Architecture

```
app/
├── main.py            # fabrique FastAPI : CORS, lifespan, table ROUTERS
├── core/              # transverse : config, database, auth (IdP), storage (S3), crypto, http, sse, ai/
├── models/            # un module par modèle SQLAlchemy, tous listés dans __init__.py
├── system/            # /health (public), /me (route protégée de référence)
├── users/  ai_credentials/  subjects/  education_levels/
├── courses/  resources/  modules/  share_links/  course_transfer/
├── public/  search/                       # régime élève et recherche, sans JWT
├── course_assistant/  student_exercises/  ai/   # briques IA
└── maintenance/       # job de purge (python -m app.maintenance)
config/                # réglages publics par environnement : development / preprod / production
alembic/               # migrations (env.py async)
tests/                 # pytest, sans réseau, ni Postgres, ni Zitadel (fakes dans tests/fakes.py)
docs/architecture.md   # approfondissements par paquet
```

Chaque domaine est un paquet `app/<feature>/` avec `schemas.py`, `service.py` et `router.py`, monté sous `/api/v1`. Les invariants et les commandes de travail sont dans [CLAUDE.md](CLAUDE.md) ; la spec produit dans [Descriptions.md](Descriptions.md).

## Authentification

Le flow OIDC (Authorization Code + PKCE) est entièrement porté par la SPA Angular. **Cette API n'émet jamais de token** : elle valide le token d'accès Zitadel à chaque requête (signature RS256 via le JWKS découvert depuis l'issuer, vérification `issuer`, `audience` et expiration). Deux réglages : `OIDC_ISSUER` et `OIDC_AUDIENCE`.

Checklist Zitadel (console) :
1. Créer le projet et l'application API.
2. Activer les **JWT access tokens** (Zitadel émet des tokens opaques par défaut — `/me` répond 401 avec un token opaque).
3. Activer « add roles to access token claims » si les rôles sont nécessaires.
4. Renseigner l'URL de l'instance dans `OIDC_ISSUER` et le client/project id dans `OIDC_AUDIENCE`.

Les routes protégées utilisent la dépendance `get_current_user` ([app/core/auth.py](app/core/auth.py)) ; `GET /api/v1/me` en est l'exemple. **Sans JWT** : `/api/v1/health`, et tout le régime public élève (`/api/v1/public/*`, `/api/v1/public/search/*`), dont l'autorisation repose sur la visibilité du cours et le token de partage.

## Configuration

Priorité décroissante : variables d'environnement > `.env` > `config/<APP_ENV>.yaml` (`APP_ENV` = `development` par défaut, `preprod` ou `production`). Les YAML versionnés portent les valeurs publiques (CORS, hôtes, OIDC, endpoint/bucket S3, TTL, plafonds, délais de purge, provider IA par défaut) ; `.env` les secrets (`POSTGRES_PASSWORD`, clés S3, `AI_API_KEY`, `AI_CREDENTIALS_MASTER_KEY`, clés Langfuse) et les overrides locaux. `DATABASE_URL` est assemblée depuis les `POSTGRES_*` ; une URL complète (Postgres managé, `sslmode=…`) peut être collée telle quelle, elle est normalisée pour asyncpg.

## Installation (dev local)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # secrets ; les URL Zitadel vont dans config/development.yaml
```

## Lancer

### Docker (nominal)

```bash
cp .env.example .env          # POSTGRES_PASSWORD (et APP_ENV=production sur le Pi)
docker compose up --build     # db + minio + minio-createbucket + api
docker compose --profile maintenance up purge   # job de purge (optionnel en dev)
```

Les migrations tournent au démarrage de l'`api` (`alembic upgrade head`), puis l'API écoute sur le port 8000. Le reverse proxy nginx (TLS, routage) est fourni par l'infra, hors de ce dépôt.

### À nu (dev)

```bash
uvicorn app.main:app --reload
```

Docs : http://localhost:8000/docs · santé : http://localhost:8000/api/v1/health. Pour tester un upload de bout en bout, lancer uvicorn en local (une URL présignée mintée par l'API en conteneur pointe `minio:9000`, injoignable par le navigateur).

## Migrations

```bash
alembic revision --autogenerate -m "..."   # depuis les modèles (enregistrés dans app/models/__init__.py)
alembic upgrade head
alembic downgrade -1
```

Dans Docker : `docker compose run --rm api alembic upgrade head`.

## Tests

```bash
pytest
ruff check . --exclude venv
```

La suite tourne sans réseau, sans Postgres et sans Zitadel : les tests d'auth signent des JWT avec une clé RSA générée et bouchonnent le cache JWKS ; les services sont testés sur une fausse session FIFO (`tests/fakes.py`) et, pour la FTS et la purge, sur le SQL compilé.

## Licence

GNU AGPL v3 — voir [LICENSE](LICENSE).
