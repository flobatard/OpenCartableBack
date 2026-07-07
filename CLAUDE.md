# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Backend FastAPI d'une plateforme pédagogique auto-hébergée (Raspberry Pi) : un prof compose des cours par blocs et les partage à ses élèves via des liens publics. Le cahier des charges complet (architecture cible, enjeux, modèle de données, roadmap J0→J5) est dans **Descriptions.md** — le lire avant tout travail de fond ; il fait foi et doit être mis à jour quand une décision d'architecture change. Le projet entame le jalon J1 (contenu) : taxonomie des matières, classification des niveaux d'étude et modèle BDD des cours (blocs, ressources, modules) sont en place ; le CRUD cours, l'upload S3 et l'éditeur de blocs restent à livrer. L'utilisateur échange en français.

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

Sémantique des erreurs à préserver : pas de credentials ou token invalide → **401** + `WWW-Authenticate: Bearer` (jamais 403) ; IdP injoignable ou discovery/JWKS malformé → **503** ; jamais de 500, jamais le token dans les logs. Routes protégées = `Depends(get_current_user)` (`GET /api/v1/me` est l'exemple de référence) ; `/api/v1/health` reste public. Un second régime d'accès (token de partage opaque pour les élèves consultant sans compte) arrivera au jalon J2 : il aura sa **propre dépendance d'autorisation**, distincte de `get_current_user`.

### Comptes applicatifs & onboarding (`users`)
La feature `app/users/` persiste les comptes (profs **et/ou** élèves — rôles cumulables `est_prof`/`est_eleve`) : `GET /api/v1/users/me` **auto-provisionne** la ligne au premier appel (upsert `ON CONFLICT DO NOTHING` sur `sub`, seule donnée IdP persistée avec l'e-mail en snapshot) ; `PUT /api/v1/users/me/onboarding` valide et enregistre le profil (système scolaire ∈ `education_levels.systeme`, niveaux du même système, matières existantes) en **remplacement complet** — il sert aussi d'édition de profil. Les M2M `user_subjects`/`user_education_levels` sont qualifiées par `contexte` (« enseigne »/« apprend ») ; profil complet ⟺ `onboarded_at` non NULL. **Invariant préservé : `get_current_user` ne touche jamais la base** — la résolution `sub → users` vit dans `app/users/service.py` (`get_or_create_by_sub`), pas dans `app/core/auth.py`. L'ordre des `execute` des fonctions du service est un contrat des tests (fausse session FIFO, `tests/test_users_api.py`).

### Configuration en couches
Priorité décroissante : variables d'env > `.env` > `config/<APP_ENV>.yaml` (`APP_ENV` par défaut : `development`). `config/*.yaml` = valeurs publiques versionnées ; `.env` = secrets et overrides locaux uniquement. `DATABASE_URL` est **assemblée** dans `app/core/config.py` depuis les composants `POSTGRES_*` (mot de passe percent-encodé) ; une URL complète collée dans `DATABASE_URL` prend le pas et est normalisée pour asyncpg (`sslmode`/`channel_binding` traduits en `ssl=`). Attention : `settings = get_settings()` s'évalue **à l'import** — dans les tests, les variables d'env sont posées en tête de `tests/conftest.py` avant tout import de `app.*` ; conserver cet ordre.

### Découpage
- Stack 100 % async : SQLAlchemy 2.0 + asyncpg partout, y compris `alembic/env.py`. Ne pas introduire de driver/engine synchrone.
- Package-by-feature : chaque domaine (courses, subjects, resources…) = un package `app/<feature>/` avec `schemas.py`, `service.py` (logique métier), `router.py` (HTTP), monté dans `create_app()` sous `settings.API_V1_PREFIX`.
- Les modèles SQLAlchemy vivent dans **`app/models/<nom>.py`** (un module par modèle) et doivent être importés + listés dans `__all__` de `app/models/__init__.py`, sinon Alembic autogenerate ne les voit pas.

### Taxonomie des matières (`subjects`)
Table unique **auto-référencée** (`app/models/subject.py`) : discipline (profondeur 0) → domaine (1) → sous-domaine (2) → sujet (3), profondeur flexible (une branche peut s'arrêter avant 3, CHECK en base) ; unicité `(parent_id, nom)` en `NULLS NOT DISTINCT` (Postgres 15+), FK `parent_id` en `ondelete=CASCADE`. Chaque nœud porte un `code` = chemin slug complet unique (ex. `mathematiques.algebre.algebre-lineaire`) dont dérive son id **uuid5 déterministe**. La taxonomie pré-remplie (~475 nœuds, lycée → master) vit dans **`app/subjects/seed_data.py` — module de données pur et APPEND-ONLY** : ne jamais changer `SEED_NAMESPACE` ni renommer/supprimer un `code` existant (la migration de seed idempotente `ON CONFLICT DO NOTHING` et les IDs en dépendent) ; tout ajout passe par une nouvelle data migration réutilisant `iter_rows()`. Lecture : `GET /api/v1/subjects/tree` — une seule requête SQL, arbre assemblé en O(n) dans `app/subjects/service.py` (les relations ORM `parent`/`children` ne sont pas chargées : lazy-load async interdit). Le CRUD prof arrivera au J1.

### Classification des niveaux d'étude (`education_levels`)
Même motif que `subjects`, en plus contraint : table auto-référencée (`app/models/education_level.py`) à deux profondeurs — cycle (0, ex. « Collège ») > classe (1, ex. « 6e ») —, un arbre par système scolaire (`systeme`, plusieurs pays occidentaux pour l'instant), unicité `(systeme, parent_id, nom)` en NULLS NOT DISTINCT. Chaque nœud porte les **pivots internationaux** `cite` (CITE/ISCED 2011, NULL si le nœud couvre plusieurs niveaux, ex. « Supérieur ») et `age_min`/`age_max` : c'est par eux que des cours se rapprocheront entre pays — les `nom` sont des noms propres nationaux, jamais traduits. Différence clé avec subjects : les `code` sont **écrits à la main** et préfixés système (ex. `fr.college.6e`), jamais dérivés du nom affiché. Le seed (22 nœuds, primaire → doctorat, voie générale) vit dans **`app/education_levels/seed_data.py`** — module de données pur et APPEND-ONLY, `SEED_NAMESPACE` propre et figé (distinct de celui des subjects) ; tout ajout (maternelle, voie pro, BTS/CPGE, arbre étranger) passe par une nouvelle data migration réutilisant `iter_rows()`. Lecture : `GET /api/v1/education-levels/tree` (même motif flat select + arbre O(n)). Pas de relations ORM ni de CRUD prévu ; le lien cours ↔ niveaux est implémenté (M2M `course_education_levels`, voir ci-dessous) ; le profil utilisateur (niveaux/matières par contexte) est implémenté via `app/users/`.

### Modèle des cours (`courses`, `blocks`, `resources`, `modules`)
**Modèles BDD seuls pour l'instant** (`app/models/{course,block,resource,module}.py`) : pas de package `app/courses/` ni de routes — le CRUD, l'upload S3 presigned et l'éditeur de blocs sont la suite du J1. Un cours appartient à un user (`owner_id`, CASCADE) et est classé par deux M2M **sans qualificatif** `course_subjects`/`course_education_levels` (PK composite `course_id` en tête, index inverse pour les facettes de recherche — motif `user_subjects` sans `contexte`). Son contenu = **blocs ordonnés** : `position` SmallInteger **sans unicité `(course_id, position)`** (voulu : le réordonnancement réécrit les positions côté service ; tri stable `ORDER BY position, id`), quatre types (CHECK) dont le `content` JSONB est un **contrat applicatif documenté dans la docstring de `block.py`** — `texte` (`{"markdown": ...}`, jamais de HTML brut), `exercice` (questions à champ libre ; les `questions[].id` uuid sont générés en service et **stables à vie** : les soumissions élèves (J2) et la review IA référenceront `(block_id, question_id)`, ne jamais les régénérer à l'édition), `ressource`, `lien` (embed externe, rien sur S3). CHECK de cohérence : seuls (et tous) les blocs `ressource` portent `resource_id` (CASCADE). `resources` = fichier S3 d'un cours : `s3_key` **plate** unique (`uuid/nom-original`), type ∈ document/image/audio/video/module, **`statut` `en_attente` → `disponible`** (la ligne est créée avant l'upload direct navigateur→S3 ; confirmation HEAD S3 à venir). `modules` = spécialisation 0..1 d'une ressource `type='module'` (`resource_id` unique, `version` int, `entrypoint`) ; la cohérence cross-table sera validée en service (J4). **Aucune relation ORM** sur ces modèles (lazy-load async interdit). Piège : autogenerate ne détecte pas la modification d'un `CheckConstraint` — élargir `ck_blocks_type`/`ck_resources_type` = migration manuelle. À venir : `share_links` (J2), `search_vector` FTS (J3).

### Tests sans dépendances externes
`tests/conftest.py` fournit : clé RSA de session + `make_token(...)` (signe des JWT RS256 avec `kid` de test, iss/aud/exp valides par défaut et surchargables), `mock_jwks` (monkeypatch de `jwks_cache.get_signing_key` — aucun réseau), `client`. Pour tester une route protégée sans crypto : `app.dependency_overrides[get_current_user]` ; pour une route qui lit la base : `app.dependency_overrides[get_db]` avec une fausse session (motif de référence : `tests/test_subjects_api.py`). Aucun test ne requiert Postgres ni Zitadel.

## Décisions actées (ne pas "corriger")

- **Pas de pgvector** : la vectorisation n'est pas actée ; si elle se fait ce sera probablement ChromaDB (jalon J5). Ne pas proposer d'extension Postgres.
- **Pas de reverse proxy dans ce repo** : nginx est fourni et branché par l'infra ; le compose expose l'API sur le port 8000, c'est voulu.
- **Dépendances IA de `requirements.txt` (langchain*, sentence-transformers, chromadb, redis, psycopg2-binary) volontairement conservées** bien qu'inutilisées au J0 — ne pas les purger.
- **`scripts/` est écrit et maintenu à la main par l'utilisateur** — ne pas le modifier sans demande explicite.
- Contrainte transverse (Descriptions.md §5.8) : cible Raspberry Pi ARM64 — déporter le lourd (transferts via URL S3 présignées, jobs asynchrones), images Docker multi-arch.
- L'utilisateur s'occupe toujours de faire les commande alembic pour créer les migrations et les appliquer
