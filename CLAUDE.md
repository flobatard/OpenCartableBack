# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Backend FastAPI d'une plateforme pédagogique auto-hébergée (Raspberry Pi) : un prof compose des cours par blocs et les partage à ses élèves via des liens publics. Le cahier des charges complet (architecture cible, enjeux, modèle de données, roadmap J0→J5) est dans **Descriptions.md** — le lire avant tout travail de fond ; il fait foi et doit être mis à jour quand une décision d'architecture change. Le projet en est au jalon J0 (socle) ; la taxonomie des matières et la classification des niveaux d'étude (livrables J1) sont en place. L'utilisateur échange en français.

## Commands

```bash
source venv/bin/activate
pip install -r requirements.txt

pytest                                    # all tests (fast, no network, no DB)
pytest tests/test_auth.py::test_me_with_valid_token   # single test
ruff check . --exclude venv               # lint (config in pyproject.toml)

uvicorn app.main:app --reload             # dev server (uses config/development.yaml)
docker compose up --build                 # api + postgres (needs .env: cp .env.example .env)

alembic revision --autogenerate -m "..."  # new migration (register models first, see below)
alembic upgrade head
```

## Architecture

### Auth : resource server pur (décision structurante)
Le flow OIDC (Authorization Code + PKCE) est **entièrement géré par la SPA Angular**. L'API n'émet jamais de token et ne stocke aucune identité/mot de passe : elle valide le JWT Zitadel de chaque requête (signature RS256 via JWKS découvert depuis l'issuer, vérif issuer/audience/exp). Toute la logique IdP est **confinée dans `app/core/auth.py`** (exigence du cahier des charges : pouvoir changer d'IdP en ne touchant que ce module). Seuls réglages : `OIDC_ISSUER` et `OIDC_AUDIENCE`.

Sémantique des erreurs à préserver : pas de credentials ou token invalide → **401** + `WWW-Authenticate: Bearer` (jamais 403) ; IdP injoignable ou discovery/JWKS malformé → **503** ; jamais de 500, jamais le token dans les logs. Routes protégées = `Depends(get_current_user)` (`GET /api/v1/me` est l'exemple de référence) ; `/api/v1/health` reste public. Un second régime d'accès (token de partage opaque pour les élèves, sans compte) arrivera au jalon J2 : il aura sa **propre dépendance d'autorisation**, distincte de `get_current_user`.

### Configuration en couches
Priorité décroissante : variables d'env > `.env` > `config/<APP_ENV>.yaml` (`APP_ENV` par défaut : `development`). `config/*.yaml` = valeurs publiques versionnées ; `.env` = secrets et overrides locaux uniquement. `DATABASE_URL` est **assemblée** dans `app/core/config.py` depuis les composants `POSTGRES_*` (mot de passe percent-encodé) ; une URL complète collée dans `DATABASE_URL` prend le pas et est normalisée pour asyncpg (`sslmode`/`channel_binding` traduits en `ssl=`). Attention : `settings = get_settings()` s'évalue **à l'import** — dans les tests, les variables d'env sont posées en tête de `tests/conftest.py` avant tout import de `app.*` ; conserver cet ordre.

### Découpage
- Stack 100 % async : SQLAlchemy 2.0 + asyncpg partout, y compris `alembic/env.py`. Ne pas introduire de driver/engine synchrone.
- Package-by-feature : chaque domaine (courses, subjects, resources…) = un package `app/<feature>/` avec `schemas.py`, `service.py` (logique métier), `router.py` (HTTP), monté dans `create_app()` sous `settings.API_V1_PREFIX`.
- Les modèles SQLAlchemy vivent dans **`app/models/<nom>.py`** (un module par modèle) et doivent être importés + listés dans `__all__` de `app/models/__init__.py`, sinon Alembic autogenerate ne les voit pas.

### Taxonomie des matières (`subjects`)
Table unique **auto-référencée** (`app/models/subject.py`) : discipline (profondeur 0) → domaine (1) → sous-domaine (2) → sujet (3), profondeur flexible (une branche peut s'arrêter avant 3, CHECK en base) ; unicité `(parent_id, nom)` en `NULLS NOT DISTINCT` (Postgres 15+), FK `parent_id` en `ondelete=CASCADE`. Chaque nœud porte un `code` = chemin slug complet unique (ex. `mathematiques.algebre.algebre-lineaire`) dont dérive son id **uuid5 déterministe**. La taxonomie pré-remplie (~475 nœuds, lycée → master) vit dans **`app/subjects/seed_data.py` — module de données pur et APPEND-ONLY** : ne jamais changer `SEED_NAMESPACE` ni renommer/supprimer un `code` existant (la migration de seed idempotente `ON CONFLICT DO NOTHING` et les IDs en dépendent) ; tout ajout passe par une nouvelle data migration réutilisant `iter_rows()`. Lecture : `GET /api/v1/subjects/tree` — une seule requête SQL, arbre assemblé en O(n) dans `app/subjects/service.py` (les relations ORM `parent`/`children` ne sont pas chargées : lazy-load async interdit). Le CRUD prof arrivera au J1.

### Classification des niveaux d'étude (`education_levels`)
Même motif que `subjects`, en plus contraint : table auto-référencée (`app/models/education_level.py`) à deux profondeurs — cycle (0, ex. « Collège ») > classe (1, ex. « 6e ») —, un arbre par système scolaire (`systeme`, plusieurs pays occidentaux pour l'instant), unicité `(systeme, parent_id, nom)` en NULLS NOT DISTINCT. Chaque nœud porte les **pivots internationaux** `cite` (CITE/ISCED 2011, NULL si le nœud couvre plusieurs niveaux, ex. « Supérieur ») et `age_min`/`age_max` : c'est par eux que des cours se rapprocheront entre pays — les `nom` sont des noms propres nationaux, jamais traduits. Différence clé avec subjects : les `code` sont **écrits à la main** et préfixés système (ex. `fr.college.6e`), jamais dérivés du nom affiché. Le seed (22 nœuds, primaire → doctorat, voie générale) vit dans **`app/education_levels/seed_data.py`** — module de données pur et APPEND-ONLY, `SEED_NAMESPACE` propre et figé (distinct de celui des subjects) ; tout ajout (maternelle, voie pro, BTS/CPGE, arbre étranger) passe par une nouvelle data migration réutilisant `iter_rows()`. Lecture : `GET /api/v1/education-levels/tree` (même motif flat select + arbre O(n)). Pas de relations ORM ni de CRUD prévu ; le lien cours ↔ niveaux (M2M) et le profil prof sont documentés dans Descriptions.md §6, non implémentés.

### Tests sans dépendances externes
`tests/conftest.py` fournit : clé RSA de session + `make_token(...)` (signe des JWT RS256 avec `kid` de test, iss/aud/exp valides par défaut et surchargables), `mock_jwks` (monkeypatch de `jwks_cache.get_signing_key` — aucun réseau), `client`. Pour tester une route protégée sans crypto : `app.dependency_overrides[get_current_user]` ; pour une route qui lit la base : `app.dependency_overrides[get_db]` avec une fausse session (motif de référence : `tests/test_subjects_api.py`). Aucun test ne requiert Postgres ni Zitadel.

## Décisions actées (ne pas "corriger")

- **Pas de pgvector** : la vectorisation n'est pas actée ; si elle se fait ce sera probablement ChromaDB (jalon J5). Ne pas proposer d'extension Postgres.
- **Pas de reverse proxy dans ce repo** : nginx est fourni et branché par l'infra ; le compose expose l'API sur le port 8000, c'est voulu.
- **Dépendances IA de `requirements.txt` (langchain*, sentence-transformers, chromadb, redis, psycopg2-binary) volontairement conservées** bien qu'inutilisées au J0 — ne pas les purger.
- **`scripts/` est écrit et maintenu à la main par l'utilisateur** — ne pas le modifier sans demande explicite.
- Contrainte transverse (Descriptions.md §5.8) : cible Raspberry Pi ARM64 — déporter le lourd (transferts via URL S3 présignées, jobs asynchrones), images Docker multi-arch.
- L'utilisateur s'occupe toujours de faire les commande alembic pour créer les migrations et les appliquer
