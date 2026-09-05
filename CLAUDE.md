# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projet

API FastAPI d'**OpenCartable**, plateforme pédagogique auto-hébergée (Raspberry Pi) : un prof compose des cours par blocs et les partage à ses élèves par liens publics ; un élève connecté dispose d'un tuteur IA. La spec (architecture cible, modèle de données) est `Descriptions.md` — elle fait foi et se met à jour quand l'architecture change. Historique des jalons : `../docs/milestones.md` ; décisions encore contraignantes : `../docs/decisions.md` ; dettes : `../TODO.md`. L'utilisateur échange en **français**.

## Commandes

```bash
source venv/bin/activate
pip install -r requirements.txt

pytest                                    # toute la suite (rapide, sans réseau ni Postgres)
pytest tests/test_auth.py::test_me_with_valid_token   # un seul test
ruff check . --exclude venv               # lint (config dans pyproject.toml : E, F, I, UP, B)

uvicorn app.main:app --reload             # serveur de dev (config/development.yaml)
docker compose up --build                 # db + minio + minio-createbucket + api (.env requis : cp .env.example .env)
docker compose --profile maintenance up purge   # job de purge (une passe au démarrage, puis toutes les PURGE_INTERVAL_SECONDS)
python -m app.maintenance                 # une passe de purge, à la main

alembic revision --autogenerate -m "..."  # nouvelle migration (modèle enregistré dans app/models/__init__.py d'abord)
alembic upgrade head
```

**L'utilisateur lance lui-même les commandes alembic** (création et application des migrations) ; ne pas les exécuter à sa place. `scripts/` est maintenu à la main par l'utilisateur : ne pas y toucher sans demande.

Dev local nominal : uvicorn local + Postgres docker (port 5429) + MinIO docker (console `:9001`). Une URL présignée mintée par l'API en conteneur pointe `minio:9000`, injoignable par le navigateur : pour tester un upload de bout en bout, lancer uvicorn en local.

## Carte de `app/`

Package-by-feature : chaque domaine = `schemas.py` (Pydantic), `service.py` (métier, lève les `HTTPException`), `router.py` (HTTP), monté par la table `ROUTERS` de `app/main.py` sous `settings.API_V1_PREFIX` (`/api/v1`). Les modèles SQLAlchemy vivent dans `app/models/<nom>.py`, un module par modèle, listés dans `__all__` de `app/models/__init__.py` (sinon Alembic ne les voit pas).

| Paquet | Rôle | Routes |
|---|---|---|
| `core/config.py` | Réglages en couches (env > `.env` > `config/<APP_ENV>.yaml`), `DATABASE_URL` assemblée | — |
| `core/database.py` | Engine/session async, `Base`, `touch(*rows)` (bump `updated_at` côté Python) | — |
| `core/auth.py` | Validation du JWT Zitadel, `get_current_user` — **seul module IdP** | — |
| `core/storage.py` | Client S3 (presign, HEAD, delete, listing, bytes pour l'export/import) — **seul module boto3** | — |
| `core/crypto.py` | Chiffrement des clés API IA — **seul module `cryptography`** | — |
| `core/http.py`, `core/sse.py` | Constructeurs d'erreurs HTTP partagés ; contrat SSE de référence + `sse_event`/`sse_response` | — |
| `core/ai/` | Client IA multi-provider (LangChain/LangGraph) — **seul paquet langchain/langgraph/langfuse** : `client.py` façade, `agent.py` graphe + HITL, `messages.py`, `providers.py`, `errors.py`, `model_catalog.py`, `observability.py` | — |
| `system/` | Sonde `/health` (publique) et `/me` (route protégée de référence) | `/health`, `/me` |
| `users/` | Comptes auto-provisionnés au premier appel, profil d'onboarding, avatar | `/users/me…` |
| `ai_credentials/` | Credential IA chiffré par utilisateur, cascade `effective_config`, quota quotidien de l'IA par défaut | `/users/me/ai-credentials…` |
| `subjects/`, `education_levels/` | Taxonomies seedées (`seed_data.py` APPEND-ONLY), arbres en une requête | `/subjects/tree`, `/education-levels/tree` |
| `courses/` | Cours (`service.py`), blocs (`blocks.py`), lectures partagées (`queries.py` : `get_owned_course`, lectures batchées) | `/courses…` |
| `resources/` | Bibliothèque S3 d'un cours, flow presigned en trois temps | `/courses/{id}/resources…` |
| `modules/` | Bibliothèque de modules interactifs (code HTML/CSS/JS en base) | `/courses/{id}/modules…` |
| `share_links/` | Liens de partage élèves (token opaque, expiration, révocation) | `/courses/{id}/share-links…` |
| `course_transfer/` | Export (`export.py`) / import (`importer.py`) d'un cours en `.zip`, `archive.py` sécurité de lecture | `/courses/{id}/export`, `/courses/import` |
| `public/` | Régime élève **sans JWT** : `access.py` (autorisation visibilité + token, 404 uniforme), `service.py` (lectures filtrées) | `/public…` |
| `search/` | Recherche FTS publique : `queries.py` builders purs, `service.py` | `/public/search…` |
| `course_assistant/` | Assistant IA du prof : `service.py` CRUD conversations, `streaming.py` flux + reprise HITL, `turn_encoder.py` boucle SSE partagée, `context.py`/`render.py`/`replay.py`/`refs.py` helpers purs, `tools.py`, `hitl.py` registre, `editing/` descripteurs des contextes d'édition | `/courses/{id}/assistant…` |
| `student_exercises/` | Tuteur d'exercice de l'élève **authentifié** (JWT + accès au cours par le régime public) et routes prof d'effacement | `/student/courses/{id}/blocks/{id}…`, `/courses/{id}/blocks/{id}/submissions…` |
| `ai/` | Routes de smoke-test du client IA, banc d'essai de la cascade config × quota (supprimable, cf. TODO.md) | `/ai/chat…` |
| `maintenance/` | Job de purge hors API : `service.py` sept tâches, `schema.py` garde de schéma, `__main__.py` | — |

Tests : `tests/fakes.py` (fausse session FIFO, faux S3, `make_client`, `parse_sse`) et `tests/course_assistant_fakes.py` (lignes de données et faux client IA de l'assistant/tuteur), `tests/conftest.py` (JWT de test, JWKS mocké, neutralisation des `AI_*`).

## Invariants

**Auth et régimes d'accès**
- Resource server pur : l'API n'émet jamais de token ; token absent/invalide → **401** + `WWW-Authenticate: Bearer` (jamais 403), IdP injoignable → **503**, jamais 500, jamais le token dans les logs. `get_current_user` **ne touche jamais la base** (la résolution `sub → users` vit dans `app/users/service.py`).
- Régime public (`/public/*`, `/public/search/*`) : **aucune dépendance JWT** ; l'autorisation vit dans `app/public/access.py` ; **404 uniforme « Cours introuvable »**, jamais 401/403/410 (aucun oracle) ; le token de partage voyage en `?token=`.
- Le tuteur élève (`/student/*`) exige le JWT **et** passe par `get_public_course` : l'appel IA est imputé à la config de l'élève.
- Un cours d'autrui est **introuvable (404), jamais interdit (403)** — `get_owned_course`. 401 est réservé au JWT : une clé IA refusée par le provider est un **400**.

**Confinement des dépendances** (remplaçabilité) : IdP → `core/auth.py` ; boto3 → `core/storage.py` ; `cryptography` → `core/crypto.py` ; langchain/langgraph/langfuse → `core/ai/` (imports **paresseux** dans les fonctions, rien n'est chargé au boot ; pas `init_chat_model` — son backend huggingface charge un modèle local, mortel sur Pi). Les consommateurs n'importent que les ré-exports de `app.core.ai`.

**Données**
- 100 % async (SQLAlchemy 2.0 + asyncpg, y compris `alembic/env.py`) ; **aucune relation ORM chargée** (lazy-load async interdit : arbres assemblés en Python).
- Un JSONB se remplace par un **nouveau dict** (une mutation in-place n'est pas détectée) ; `touch(course)` à toute mutation d'un contenu du cours (le cours remonte dans la liste) ; un objet de réponse se construit **avant** le commit (piège `MissingGreenlet` sur `updated_at` généré côté SQL).
- Autogenerate ne voit **ni** la modification d'un `CheckConstraint` **ni** les données : élargir un CHECK ou seeder = migration manuelle. `seed_data.py` des taxonomies est APPEND-ONLY, `SEED_NAMESPACE` figé (ids uuid5 déterministes).
- `search_vector` de `courses`/`blocks` : maintenu par **triggers**, jamais écrit par l'ORM (`deferred=True`), et n'indexe **jamais** `expected_answer` (un test scanne les migrations).
- `questions[].id` des exercices sont **stables à vie** (tentatives des élèves et propositions de l'assistant les référencent) : ne jamais les régénérer.
- `expected_answer` n'atteint jamais un élève : `public_content` **reconstruit** le content (jamais un `exclude`), y compris pour l'instantané vu par le tuteur IA (hors la question cible).

**S3** : bucket privé, l'API ne sert que des URL présignées ; `presign_*` = calcul local (sync OK), HEAD/delete/listing = réseau sous threadpool. Purge S3 **après** commit (un échec laisse un orphelin ramassé par la réconciliation, jamais une référence vers un objet absent) ; l'import fait l'inverse (put S3 avant commit) pour la même raison. Seuls `read_object_into`/`put_object` transportent des octets (export/import).

**IA**
- La config voyage à chaque appel ; la cascade (explicite > credential chiffré > fallback `AI_*` sous quota) vit dans `ai_credentials.service.effective_config`, **jamais** dans `AIClient` (singleton stateless) ; `refund_on_error` rembourse un ticket sur échec eager, l'encodeur de tour avant le premier token.
- `stream()`/`stream_agent()` valident **eager** (vrai 4xx avant le flux) ; les erreurs provider sont traduites au bord (`core/ai/errors.py`) ; contrat SSE dans `core/sse.py`, extension agent dans `course_assistant/streaming.py` — **additif**, le front tolère les événements inconnus.
- HITL : `hitl_gate` (`editing/base.py`) est le **seul appelant** d'`agent_interrupt` ; le tool est ré-exécuté à la reprise (validation idempotente) ; checkpointer en mémoire + registre in-process ⇒ **mono-worker** (TODO.md).
- Le modèle ne manipule que des **références courtes** (`B1`/`R1`/`M1`/`Q1`), réécrites en UUID en flux ; aucun accès DB pendant l'exécution des tools (instantané du tour).
- Le prompt `MODULE_RUNTIME` (`course_assistant/prompts.py`) est le miroir du bac à sable front (`shared/module-runner/module-document.ts`) : les faire évoluer ensemble.

**Configuration** : `settings = get_settings()` s'évalue **à l'import** — dans les tests, les variables d'env sont posées en tête de `tests/conftest.py` avant tout import de `app.*`. Tous les délais de purge vivent dans `config/*.yaml`, jamais dans `.env`.

**Tests** : sans réseau, Postgres ni Zitadel. **L'ordre des `execute` d'une fonction de service est un contrat** rejoué par la fausse session FIFO — documenté dans chaque docstring, à préserver à tout refactor. FTS et purge sont testées sur le **SQL compilé** (`stmt.compile(dialect=postgresql.dialect())`). `GenericFakeChatModel` ne sait pas streamer des tool calls : les tests agent utilisent `SeqToolModel` (`tests/test_ai_agent.py`).

## Décisions à ne pas « corriger »

Détails et contexte dans `../docs/decisions.md`.

- Pas d'extension Postgres, sauf `unaccent` + config `french_unaccent` ; pas de pgvector.
- Pas de binaire par le backend, sauf l'export/import de cours.
- Modules interactifs : code en base, jamais un bundle S3.
- Token de partage stocké en clair ; 404 uniforme partout côté public.
- Dépendances IA inutilisées de `requirements.txt` conservées ; `langchain*` épinglé en 1.x.
- Pas de reverse proxy dans ce repo (nginx d'infra) ; l'API écoute sur 8000.
- Purge en job compose séparé, jamais dans le process uvicorn.

## Approfondissements

`docs/architecture.md` — Auth et comptes · Configuration · Taxonomies · Modèle des cours et contrats JSONB · Ressources S3 · Modules · Export/import · Régime public · Recherche FTS · Client IA · Credentials et quota · Assistant de cours · Tuteur d'exercice · Purge.
