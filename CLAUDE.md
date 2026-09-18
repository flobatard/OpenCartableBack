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
docker compose up --build                 # db + minio + minio-createbucket + redis + api (.env requis : cp .env.example .env)
docker compose --profile maintenance up scheduler  # scheduler de maintenance (un cron par job)
python -m app.maintenance                 # une passe complète, à la main
python -m app.maintenance share_links s3_orphans   # un ou plusieurs jobs nommés
python -m app.users.roles grant prof@example.org   # rôle de plateforme super_admin (revoke, list) ; compte déjà connecté une fois

alembic revision --autogenerate -m "..."  # nouvelle migration (modèle enregistré dans app/models/__init__.py d'abord)
alembic upgrade head
```

**L'utilisateur lance lui-même les commandes alembic** (création et application des migrations) ; ne pas les exécuter à sa place. `scripts/` est maintenu à la main par l'utilisateur : ne pas y toucher sans demande.

Dev local nominal : uvicorn local + Postgres docker (port 5429) + MinIO docker (console `:9001`) + Redis docker (port 6389, canal de contrôle du backoffice). Une URL présignée mintée par l'API en conteneur pointe `minio:9000`, injoignable par le navigateur : pour tester un upload de bout en bout, lancer uvicorn en local.

## Carte de `app/`

Package-by-feature : chaque domaine = `schemas.py` (Pydantic), `service.py` (métier, lève les `HTTPException`), `router.py` (HTTP), monté par la table `ROUTERS` de `app/main.py` sous `settings.API_V1_PREFIX` (`/api/v1`). Les modèles SQLAlchemy vivent dans `app/models/<nom>.py`, un module par modèle, listés dans `__all__` de `app/models/__init__.py` (sinon Alembic ne les voit pas).

| Paquet | Rôle | Routes |
|---|---|---|
| `core/config.py` | Réglages en couches (env > `.env` > `config/<APP_ENV>.yaml`), `DATABASE_URL` assemblée | — |
| `core/database.py` | Engine/session async, `Base`, `touch(*rows)` (bump `updated_at` côté Python) | — |
| `core/auth.py` | Validation du JWT Zitadel, `get_current_user` — **seul module IdP** | — |
| `core/storage.py` | Client S3 (presign, HEAD, delete, listing, bytes pour l'export/import) — **seul module boto3** | — |
| `core/kv.py` | Magasin clé-valeur partagé (Redis, sans persistance) : interface étroite, une seule exception `KVUnavailable` — **seul module redis** | — |
| `core/crypto.py` | Chiffrement des clés API IA — **seul module `cryptography`** | — |
| `core/http.py`, `core/sse.py` | Constructeurs d'erreurs HTTP partagés (`forbidden` = le 403 du seul rôle de plateforme) ; contrat SSE de référence + `sse_event`/`sse_response` | — |
| `core/ai/` | Client IA multi-provider (LangChain/LangGraph) — **seul paquet langchain/langgraph/langfuse** : `client.py` façade, `agent.py` graphe + HITL, `messages.py`, `providers.py`, `errors.py`, `model_catalog.py`, `reasoning.py` (catalogue des options de raisonnement par couple provider/modèle), `profiles.py` (profil embarqué langchain), `observability.py` | — |
| `system/` | Sonde `/health` (publique) et `/me` (route protégée de référence) | `/health`, `/me` |
| `users/` | Comptes auto-provisionnés au premier appel, profil d'onboarding, avatar ; rôle de plateforme : `dependencies.py` garde `require_super_admin`, `roles.py` CLI `python -m app.users.roles` (seul écrivain de `platform_role`) | `/users/me…` |
| `admin/` | Backoffice, garde `require_super_admin` posée sur le router : vue d'ensemble des jobs de maintenance, demande de passe manuelle (jamais exécutée ici) | `/admin/maintenance/jobs…` |
| `ai_credentials/` | Configurations IA nommées (plusieurs par utilisateur, clé chiffrée, au plus une active), cascade `effective_config`, quota quotidien de l'IA par défaut | `/users/me/ai-credentials…` |
| `subjects/`, `education_levels/` | Taxonomies seedées (`seed_data.py` APPEND-ONLY), arbres en une requête | `/subjects/tree`, `/education-levels/tree` |
| `courses/` | Cours (`service.py`), blocs (`blocks.py`), lectures partagées (`queries.py` : `get_owned_course`, lectures batchées) | `/courses…` |
| `resources/` | Bibliothèque S3 d'un cours, flow presigned en trois temps | `/courses/{id}/resources…` |
| `modules/` | Bibliothèque de modules interactifs (code HTML/CSS/JS en base) | `/courses/{id}/modules…` |
| `share_links/` | Liens de partage élèves (token opaque, expiration, révocation) | `/courses/{id}/share-links…` |
| `course_transfer/` | Export (`export.py`) / import (`importer.py`) d'un cours en `.zip`, `archive.py` sécurité de lecture ; `insert_manifest_course` = phase DB partagée (sans commit ni S3) | `/courses/{id}/export`, `/courses/import` |
| `starter_course/` | Cours d'exemple seedé à l'onboarding d'un prof : `manifest.json` embarqué (format v2, **aucun binaire**), seed best-effort, rattrapage manuel | `/courses/starter` |
| `public/` | Régime élève **sans JWT** : `access.py` (autorisation visibilité + token, 404 uniforme), `service.py` (lectures filtrées) | `/public…` |
| `search/` | Recherche FTS publique : `queries.py` builders purs, `service.py` | `/public/search…` |
| `course_assistant/` | Assistant IA du prof : `service.py` CRUD conversations, `streaming.py` flux + reprises HITL + driver des sous-assistants (`_drive_turn`), `turn_encoder.py` boucle SSE partagée, `context.py`/`render.py`/`replay.py`/`refs.py` helpers purs, `tools.py`, `hitl.py` registre + `suspend`, `questions.py` tool `ask_questions` (tous contextes), `delegation.py` tools `edit_block`/`edit_module` de l'édition globale, `editing/` descripteurs des contextes d'édition | `/courses/{id}/assistant…` |
| `student_exercises/` | Tuteur d'exercice de l'élève **authentifié** (JWT + accès au cours par le régime public) et routes prof d'effacement | `/student/courses/{id}/blocks/{id}…`, `/courses/{id}/blocks/{id}/submissions…` |
| `ai/` | Routes de smoke-test du client IA, banc d'essai de la cascade config × quota (supprimable, cf. TODO.md) | `/ai/chat…` |
| `maintenance/` | Jobs hors API, déclenchés par le service compose `scheduler` : `registry.py` la seule liste des neuf jobs, `service.py` les sept purges, `checks.py` les deux contrôles en lecture seule, `runner.py` l'exécution d'un job (leviers d'inactivité, garde, chrono, état), `state.py` l'upsert de `maintenance_job_state` sur session dédiée, `control.py` le canal backoffice ↔ scheduler par Redis (demandes à expiration, statut publié : plan, passe en cours), `schema.py` la garde Alembic, `scheduler.py` le process résident APScheduler et sa boucle de contrôle, `__main__.py` le one-shot | — |

Tests : `tests/fakes.py` (fausse session FIFO, faux S3, faux magasin Redis `FakeKV`, `make_client`, `parse_sse`), `tests/course_assistant_fakes.py` (lignes de données et faux client IA de l'assistant/tuteur), `tests/maintenance_fakes.py` (session FIFO à liste d'événements partagée, faux S3 scriptable et mesurant sa concurrence), `tests/conftest.py` (JWT de test, JWKS mocké, neutralisation des `AI_*`).

## Invariants

**Auth et régimes d'accès**
- Resource server pur : l'API n'émet jamais de token ; token absent/invalide → **401** + `WWW-Authenticate: Bearer` (jamais 403), IdP injoignable → **503**, jamais 500, jamais le token dans les logs. `get_current_user` **ne touche jamais la base** (la résolution `sub → users` vit dans `app/users/service.py`).
- Régime public (`/public/*`, `/public/search/*`) : **aucune dépendance JWT** ; l'autorisation vit dans `app/public/access.py` ; **404 uniforme « Cours introuvable »**, jamais 401/403/410 (aucun oracle) ; le token de partage voyage en `?token=`.
- Le tuteur élève (`/student/*`) exige le JWT **et** passe par `get_public_course` : l'appel IA est imputé à la config de l'élève.
- Un cours d'autrui est **introuvable (404), jamais interdit (403)** — `get_owned_course`. 401 est réservé au JWT : une clé IA refusée par le provider est un **400**.
- **403 réservé au rôle de plateforme** : `users.platform_role` (`public`/`super_admin`) n'est **jamais écrit par une route** (CLI `app.users.roles`) ; les routes `/admin/*` héritent de la garde `require_super_admin` posée sur leur router (une route qui veut le compte la redemande en paramètre, résolue une fois par requête).

**Confinement des dépendances** (remplaçabilité) : IdP → `core/auth.py` ; boto3 → `core/storage.py` ; redis → `core/kv.py` ; `cryptography` → `core/crypto.py` ; langchain/langgraph/langfuse → `core/ai/` (imports **paresseux** dans les fonctions, rien n'est chargé au boot ; pas `init_chat_model` — son backend huggingface charge un modèle local, mortel sur Pi). Les consommateurs n'importent que les ré-exports de `app.core.ai`.

**Données**
- 100 % async (SQLAlchemy 2.0 + asyncpg, y compris `alembic/env.py`) ; **aucune relation ORM chargée** (lazy-load async interdit : arbres assemblés en Python).
- Un JSONB se remplace par un **nouveau dict** (une mutation in-place n'est pas détectée) ; `touch(course)` à toute mutation d'un contenu du cours (le cours remonte dans la liste) ; un objet de réponse se construit **avant** le commit (piège `MissingGreenlet` sur `updated_at` généré côté SQL).
- Autogenerate ne voit **ni** la modification d'un `CheckConstraint` **ni** les données : élargir un CHECK ou seeder = migration manuelle. `seed_data.py` des taxonomies est APPEND-ONLY, `SEED_NAMESPACE` figé (ids uuid5 déterministes).
- `search_vector` de `courses`/`blocks` : maintenu par **triggers**, jamais écrit par l'ORM (`deferred=True`), et n'indexe **jamais** `expected_answer` (un test scanne les migrations).
- `questions[].id` des exercices sont **stables à vie** (tentatives des élèves et propositions de l'assistant les référencent) : ne jamais les régénérer.
- `expected_answer` n'atteint jamais un élève : `public_content` **reconstruit** le content (jamais un `exclude`), y compris pour l'instantané vu par le tuteur IA (hors la question cible).

**S3** : bucket privé, l'API ne sert que des URL présignées ; `presign_*` = calcul local (sync OK), HEAD/delete/listing = réseau sous threadpool. Purge S3 **après** commit (un échec laisse un orphelin ramassé par la réconciliation, jamais une référence vers un objet absent) ; l'import fait l'inverse (put S3 avant commit) pour la même raison. Seuls `read_object_into`/`put_object` transportent des octets (export/import).

**IA**
- La config voyage à chaque appel ; la cascade (explicite > configuration **active** déchiffrée > fallback `AI_*` sous quota) vit dans `ai_credentials.service.effective_config`, **jamais** dans `AIClient` (singleton stateless) ; `refund_on_error` rembourse un ticket sur échec eager, l'encodeur de tour avant le premier token.
- Configurations IA : table `ai_configurations`, plusieurs par utilisateur, **au plus une active** (index partiel unique, non différable : désactiver en UPDATE Core **puis** activer en ORM, jamais un seul UPDATE), aucune active = IA par défaut ; `POST` crée et active, `PUT /active` est déclaré **avant** `PUT /{config_id}` ; plafond `MAX_CONFIGURATIONS` en service ; toute route mutante renvoie l'enveloppe.
- `stream()`/`stream_agent()` valident **eager** (vrai 4xx avant le flux) ; les erreurs provider sont traduites au bord (`core/ai/errors.py`) ; contrat SSE dans `core/sse.py`, extension agent dans `course_assistant/streaming.py` — **additif**, le front tolère les événements inconnus.
- HITL : `hitl.suspend` est le **seul appelant** d'`agent_interrupt` (propositions via `hitl_gate`, questions via `questions.py`) ; le tool est ré-exécuté à la reprise (validation idempotente, fonction des seuls args) ; une reprise ne consomme que son **genre** (`proposal`/`questions`) et valide la réponse avant de consommer ; **tout tour d'assistant est checkpointé**, jamais le tuteur, et son thread est purgé à `done`/erreur/abandon du flux/suppression/expiration ; une spec `blocking` n'entre dans l'état qu'une fois par réponse du modèle (garde de `core/ai/agent.py` : la réponse est **élaguée** au premier appel bloquant avant l'état — les autres ne sont ni relayés, ni exécutés, ni revus par le modèle) ; checkpointer en mémoire + registre in-process ⇒ **mono-worker** (TODO.md).
- Délégation (édition globale, contexte `course` dont le message porte `allow_edit`) : `edit_block`/`edit_module` figent le parent d'un interrupt de genre `delegation`, **jamais relayé ni enregistré** — le driver `_drive_turn` lance le sous-assistant (descripteur de la cible, thread propre, sur l'instantané du flux : **aucun rechargement dans un flux**, contrats FIFO inchangés) dans le même flux, événements tagués `agent` ; le sink en mode agent **ne persiste rien** du sous-assistant (compte rendu = valeur de reprise du parent) ; une entrée de registre à `delegation` tient **deux threads**, purgés ensemble ; `allow_edit` est rejoué à la reprise ; plafond `MAX_DELEGATIONS_PER_TURN` (le `run_limit` du graphe repart à chaque reprise).
- Le modèle ne manipule que des **références courtes** (`B1`/`R1`/`M1`/`Q1`), réécrites en UUID en flux ; aucun accès DB pendant l'exécution des tools (instantané du tour).
- System prompt **statique** par contexte (`prompts.py`, cacheable) — jamais de contenu de cours. Le **contexte du tour** = cible d'édition en entier + **sommaire** du cours (`render_outline` : titres, types, plans ; jamais le contenu d'un bloc) voyage en tête du message utilisateur, après l'historique (`turn_message`) ; le modèle lit le reste avec `read_block`. Replay **abrégé** et déterministe (`replay.py`, fenêtre à hystérésis) : jamais un résultat d'outil intégral rejoué, et le contenu d'une proposition passée **élidé** — pas même sa tête, que le modèle recopierait (marqueur de troncature compris) dans sa proposition suivante. Même règle pour le tuteur.
- Cache de prompt : le message `system` de tête est le `system_prompt` du graphe (hors état, repassé à la reprise HITL) ; middleware Anthropic ajouté par `AIClient` pour ce seul provider ; `cached_input_tokens` ⊂ `input_tokens` dans l'usage SSE et les colonnes.
- Préférences de raisonnement (`AIRequestConfig.reasoning`/`reasoning_effort` — persistées avec chaque configuration, ou settings `AI_REASONING`/`AI_REASONING_EFFORT` pour le fallback serveur, résolus dans `resolve_config`) : capacités **par provider** déclarées dans `core/ai/types.py` (frozensets + niveaux natifs `PROVIDER_REASONING_EFFORTS`, miroir front `ai-credentials.model.ts`), règle de gating unique `check_reasoning_support` (422 à l'écriture d'une configuration comme à la résolution du fallback), options **proposées** par couple (provider, modèle) par le catalogue `core/ai/reasoning.py` (règles par préfixe maintenues à la main + profil embarqué `core/ai/profiles.py` ; il propose, n'impose pas ; les **règles se suffisent** — le profil est une donnée tierce qui varie avec la version du paquet, donc les familles sans raisonnement ont leur règle en dur et la table de test tourne profil neutralisé), encodage dans `core/ai/providers.py` (helpers purs, un par provider) — jamais un paramètre de raisonnement hors de `core/ai/` ; le support par **modèle** est laissé au provider (transmis, jamais abandonné en silence).
- Le prompt `MODULE_RUNTIME` (`course_assistant/prompts.py`) est le miroir du bac à sable front (`shared/module-runner/module-document.ts`) et `MODULE_LIBRARY_NAMES` celui de son catalogue de librairies (`module-libraries.ts`) : les faire évoluer ensemble.

**Configuration** : `settings = get_settings()` s'évalue **à l'import** — dans les tests, les variables d'env sont posées en tête de `tests/conftest.py` avant tout import de `app.*`. Tous les délais **et toutes les cadences** de maintenance vivent dans `config/*.yaml`, jamais dans `.env` ; le défaut d'une cadence vit sur le champ `Settings`, jamais recopié dans le registre.

**Tests** : sans réseau, Postgres ni Zitadel. **L'ordre des `execute` d'une fonction de service est un contrat** rejoué par la fausse session FIFO — documenté dans chaque docstring, à préserver à tout refactor. FTS et purge sont testées sur le **SQL compilé** (`stmt.compile(dialect=postgresql.dialect())`). `GenericFakeChatModel` ne sait pas streamer des tool calls : les tests agent utilisent `SeqToolModel` (`tests/test_ai_agent.py`).

## Décisions à ne pas « corriger »

Détails et contexte dans `../docs/decisions.md`.

- Pas d'extension Postgres, sauf `unaccent` + config `french_unaccent` ; pas de pgvector.
- Pas de binaire par le backend, sauf l'export/import de cours.
- Modules interactifs : code en base, jamais un bundle S3.
- Token de partage stocké en clair ; 404 uniforme partout côté public.
- Dépendances IA inutilisées de `requirements.txt` conservées ; `langchain*` sur la ligne 1.x.
- `requirements.txt` en **versions exactes** (`==`) : l'image fait un `pip install` au build, une fourchette y ferait entrer autre chose qu'en dev. Monter une version = `pip install -U <paquet>`, `pytest` + `ruff check`, puis reporter le numéro obtenu. Pas de lock des transitives (les wheels nvidia/cuda de la pile ML n'existent pas en ARM64).
- Pas de reverse proxy dans ce repo (nginx d'infra) ; l'API écoute sur 8000.
- Maintenance en job compose séparé (`scheduler`, APScheduler résident, un cron par job), jamais dans le process uvicorn : le backoffice **demande** une passe par Redis (`control.py`), seul le scheduler l'exécute — jamais `app.maintenance.scheduler` importé par l'API. **Aucun accès périodique à Postgres** (ni battement, ni relève en base, ni vérification de vie — c'est l'affaire de docker) : la base managée (Neon) doit pouvoir se mettre en veille entre deux passes. Boucle de relève en tâche asyncio, pas en job APScheduler à intervalle (bruit de logs, `get_jobs()` faussé). Jour de semaine cron **en lettres** (APScheduler indexe 0 = lundi, pas dimanche) ; aucune occurrence entre 02:00 et 02:59 (heure inexistante au passage à l'heure d'été). Jamais `shutdown(wait=True)` : l'exécuteur asyncio **annule** au lieu d'attendre.
- Cours d'exemple : seed **best effort** après le commit de l'onboarding (une erreur ne remonte jamais), manifeste JSON **sans ressource** (donc sans `Storage`), et `POST /courses/starter` ne déduplique pas.

## Approfondissements

`docs/architecture.md` — Auth et comptes · Configuration · Taxonomies · Modèle des cours et contrats JSONB · Ressources S3 · Modules · Export/import · Régime public · Recherche FTS · Client IA · Credentials et quota · Assistant de cours · Tuteur d'exercice · Maintenance · Backoffice.
