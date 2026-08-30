"""nomenclature anglaise

Passage de toute la nomenclature persistée en anglais (décision actée) :
colonnes, noms de contraintes/index, valeurs d'enum stockées, clés des
JSONB ``blocks.content``, et recréation des fonctions/triggers FTS qui
référencent les anciens noms. Migration écrite à la main : autogenerate
ne sait ni renommer (il drop/recrée), ni migrer des données.

Revision ID: e4a9c7f21b03
Revises: 458652f0f3d9
Create Date: 2026-08-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4a9c7f21b03'
down_revision: Union[str, None] = '458652f0f3d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --- Renommages de colonnes (table, ancien, nouveau) ---------------------
_COLUMN_RENAMES: list[tuple[str, str, str]] = [
    ("users", "nom_public", "public_name"),
    ("users", "cherchable", "searchable"),
    ("users", "est_prof", "is_teacher"),
    ("users", "est_eleve", "is_student"),
    ("users", "systeme_scolaire", "school_system"),
    ("users", "avatar_statut", "avatar_status"),
    ("users", "ai_api_key_chiffree", "ai_api_key_encrypted"),
    ("users", "ai_chiffrement_sel", "ai_encryption_salt"),
    ("users", "ai_quota_appels", "ai_daily_call_quota"),
    ("user_subjects", "contexte", "context"),
    ("user_education_levels", "contexte", "context"),
    ("courses", "titre", "title"),
    ("courses", "visibilite", "visibility"),
    ("blocks", "titre", "title"),
    ("modules", "titre", "title"),
    ("resources", "nom_original", "original_name"),
    ("resources", "taille", "size"),
    ("resources", "statut", "status"),
    ("share_links", "libelle", "label"),
    ("subjects", "nom", "name"),
    ("subjects", "profondeur", "depth"),
    ("education_levels", "nom", "name"),
    ("education_levels", "systeme", "system"),
    ("education_levels", "profondeur", "depth"),
    ("ai_daily_usage", "jour", "day"),
    ("ai_daily_usage", "appels", "calls"),
]

# --- Contraintes renommées à expression inchangée (table, ancien, nouveau).
# Postgres réécrit lui-même les expressions CHECK lors d'un RENAME COLUMN
# (arbre stocké, pas du texte) : seul le nom reste à changer ici.
_CONSTRAINT_RENAMES: list[tuple[str, str, str]] = [
    ("users", "ck_users_onboarde_implique_role", "ck_users_onboarded_requires_role"),
    ("users", "ck_users_ai_coherence", "ck_users_ai_consistency"),
    ("users", "ck_users_ai_cle_sel", "ck_users_ai_key_salt"),
    ("blocks", "ck_blocks_document_coherence", "ck_blocks_document_consistency"),
    ("blocks", "ck_blocks_module_coherence", "ck_blocks_module_consistency"),
    ("resources", "ck_resources_taille_positive", "ck_resources_size_positive"),
    ("subjects", "uq_subjects_parent_id_nom", "uq_subjects_parent_id_name"),
    ("subjects", "ck_subjects_profondeur", "ck_subjects_depth"),
    ("subjects", "ck_subjects_pas_son_propre_parent", "ck_subjects_not_own_parent"),
    (
        "education_levels",
        "uq_education_levels_systeme_parent_id_nom",
        "uq_education_levels_system_parent_id_name",
    ),
    ("education_levels", "ck_education_levels_profondeur", "ck_education_levels_depth"),
    (
        "education_levels",
        "ck_education_levels_pas_son_propre_parent",
        "ck_education_levels_not_own_parent",
    ),
]


def _drop_fts() -> None:
    op.execute("DROP TRIGGER trg_blocks_search_vector ON blocks")
    op.execute("DROP TRIGGER trg_courses_search_vector ON courses")
    op.execute("DROP FUNCTION blocks_search_vector_trigger()")
    op.execute("DROP FUNCTION courses_search_vector_trigger()")
    op.execute("DROP FUNCTION blocks_tsvector(text, text, jsonb)")
    op.execute("DROP FUNCTION courses_tsvector(text, text)")


def upgrade() -> None:
    # 1) Les fonctions/triggers FTS référencent titre + clés du content :
    #    on les retire avant tout renommage, on les recrée en anglais à la fin.
    _drop_fts()

    # 2) Renommages de colonnes.
    for table, old, new in _COLUMN_RENAMES:
        op.alter_column(table, old, new_column_name=new)
    op.execute("ALTER INDEX ix_education_levels_systeme RENAME TO ix_education_levels_system")

    # 3) Contraintes : simple renommage quand l'expression ne change pas.
    for table, old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")

    # 4) Valeurs d'enum stockées : drop du CHECK, migration des données,
    #    recréation du CHECK avec les valeurs anglaises.
    op.drop_constraint("ck_courses_visibilite", "courses", type_="check")
    op.execute("UPDATE courses SET visibility = 'private' WHERE visibility = 'prive'")
    op.execute("UPDATE courses SET visibility = 'draft' WHERE visibility = 'en_cours'")
    op.create_check_constraint(
        "ck_courses_visibility",
        "courses",
        "visibility IN ('public', 'private', 'draft')",
    )
    op.alter_column("courses", "visibility", server_default=sa.text("'draft'"))

    op.drop_constraint("ck_blocks_type", "blocks", type_="check")
    op.execute("UPDATE blocks SET type = 'text' WHERE type = 'texte'")
    op.execute("UPDATE blocks SET type = 'exercise' WHERE type = 'exercice'")
    op.create_check_constraint(
        "ck_blocks_type",
        "blocks",
        "type IN ('text', 'exercise', 'document', 'module')",
    )

    op.drop_constraint("ck_resources_statut", "resources", type_="check")
    op.execute("UPDATE resources SET status = 'pending' WHERE status = 'en_attente'")
    op.execute("UPDATE resources SET status = 'available' WHERE status = 'disponible'")
    op.create_check_constraint(
        "ck_resources_status",
        "resources",
        "status IN ('pending', 'available')",
    )
    op.alter_column("resources", "status", server_default="pending")

    op.drop_constraint("ck_users_avatar_coherence", "users", type_="check")
    op.execute("UPDATE users SET avatar_status = 'pending' WHERE avatar_status = 'en_attente'")
    op.execute("UPDATE users SET avatar_status = 'available' WHERE avatar_status = 'disponible'")
    op.create_check_constraint(
        "ck_users_avatar_consistency",
        "users",
        "(avatar_s3_key IS NULL AND avatar_mime IS NULL AND avatar_status IS NULL) "
        "OR (avatar_s3_key IS NOT NULL AND avatar_mime IS NOT NULL "
        "AND avatar_status IN ('pending', 'available'))",
    )

    for table in ("user_subjects", "user_education_levels"):
        op.drop_constraint(f"ck_{table}_contexte", table, type_="check")
        op.execute(f"UPDATE {table} SET context = 'teaching' WHERE context = 'enseigne'")
        op.execute(f"UPDATE {table} SET context = 'learning' WHERE context = 'apprend'")
        op.create_check_constraint(
            f"ck_{table}_context",
            table,
            "context IN ('teaching', 'learning')",
        )

    # 5) Clés des JSONB blocks.content (contrat applicatif de block.py).
    #    Reconstruction explicite conforme au contrat — l'ordre des questions
    #    est préservé (WITH ORDINALITY), les ids de questions (stables à vie,
    #    contrat J2) sont recopiés tels quels.
    op.execute(
        """
        UPDATE blocks
        SET content = jsonb_build_object(
            'statement', coalesce(content->'enonce', '""'::jsonb),
            'questions', coalesce((
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'id', q->'id',
                        'statement', coalesce(q->'enonce', '""'::jsonb),
                        'type', CASE WHEN q->>'type' = 'texte_libre'
                                     THEN '"free_text"'::jsonb
                                     ELSE coalesce(q->'type', '"free_text"'::jsonb) END,
                        'expected_answer', coalesce(q->'reponse_attendue', '""'::jsonb)
                    ) ORDER BY ord)
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(content->'questions') = 'array'
                         THEN content->'questions' ELSE '[]'::jsonb END
                ) WITH ORDINALITY AS t(q, ord)
            ), '[]'::jsonb)
        )
        WHERE type = 'exercise'
        """
    )
    op.execute(
        """
        UPDATE blocks
        SET content = jsonb_build_object(
            'caption', coalesce(content->'legende', '""'::jsonb),
            'display', CASE WHEN content->>'affichage' = 'telechargement'
                            THEN '"download"'::jsonb
                            ELSE coalesce(content->'affichage', '"inline"'::jsonb) END
        )
        WHERE type = 'document'
        """
    )

    # 6) Recréation FTS en anglais. Poids inchangés ; champ par champ, JAMAIS
    #    le content entier : le corrigé du prof (expected_answer) ne doit pas
    #    devenir cherchable — le garde-fou tests/test_search_api.py scanne ce
    #    corps $$…$$. Pas de backfill : seules les CLÉS du content changent,
    #    les textes indexés sont identiques, les vecteurs stockés restent
    #    valides (les triggers étaient retirés pendant les UPDATE ci-dessus).
    op.execute(
        """
        CREATE FUNCTION courses_tsvector(title text, description text)
        RETURNS tsvector
        LANGUAGE sql STABLE AS $$
            SELECT setweight(
                       to_tsvector('public.french_unaccent', coalesce(title, '')), 'A')
                || setweight(
                       to_tsvector('public.french_unaccent', coalesce(description, '')), 'B')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION blocks_tsvector(title text, description text, content jsonb)
        RETURNS tsvector
        LANGUAGE sql STABLE AS $$
            SELECT setweight(
                       to_tsvector('public.french_unaccent', coalesce(title, '')), 'B')
                || setweight(
                       to_tsvector('public.french_unaccent',
                           coalesce(description, '') || ' '
                        || coalesce(content->>'markdown', '') || ' '
                        || coalesce(content->>'statement', '') || ' '
                        || coalesce(content->>'caption', '') || ' '
                        || coalesce((
                               SELECT string_agg(q->>'statement', ' ')
                               FROM jsonb_array_elements(
                                   CASE WHEN jsonb_typeof(content->'questions') = 'array'
                                        THEN content->'questions'
                                        ELSE '[]'::jsonb END) AS q
                           ), '')
                       ), 'C')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION courses_search_vector_trigger() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.search_vector := courses_tsvector(NEW.title, NEW.description);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_courses_search_vector
        BEFORE INSERT OR UPDATE OF title, description ON courses
        FOR EACH ROW EXECUTE FUNCTION courses_search_vector_trigger()
        """
    )
    op.execute(
        """
        CREATE FUNCTION blocks_search_vector_trigger() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.search_vector := blocks_tsvector(NEW.title, NEW.description, NEW.content);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_blocks_search_vector
        BEFORE INSERT OR UPDATE OF title, description, content ON blocks
        FOR EACH ROW EXECUTE FUNCTION blocks_search_vector_trigger()
        """
    )


def downgrade() -> None:
    _drop_fts()

    # Données : retour aux valeurs françaises.
    op.execute(
        """
        UPDATE blocks
        SET content = jsonb_build_object(
            'legende', coalesce(content->'caption', '""'::jsonb),
            'affichage', CASE WHEN content->>'display' = 'download'
                              THEN '"telechargement"'::jsonb
                              ELSE coalesce(content->'display', '"inline"'::jsonb) END
        )
        WHERE type = 'document'
        """
    )
    op.execute(
        """
        UPDATE blocks
        SET content = jsonb_build_object(
            'enonce', coalesce(content->'statement', '""'::jsonb),
            'questions', coalesce((
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'id', q->'id',
                        'enonce', coalesce(q->'statement', '""'::jsonb),
                        'type', CASE WHEN q->>'type' = 'free_text'
                                     THEN '"texte_libre"'::jsonb
                                     ELSE coalesce(q->'type', '"texte_libre"'::jsonb) END,
                        'reponse_attendue', coalesce(q->'expected_answer', '""'::jsonb)
                    ) ORDER BY ord)
                FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(content->'questions') = 'array'
                         THEN content->'questions' ELSE '[]'::jsonb END
                ) WITH ORDINALITY AS t(q, ord)
            ), '[]'::jsonb)
        )
        WHERE type = 'exercise'
        """
    )

    for table in ("user_subjects", "user_education_levels"):
        op.drop_constraint(f"ck_{table}_context", table, type_="check")
        op.execute(f"UPDATE {table} SET context = 'enseigne' WHERE context = 'teaching'")
        op.execute(f"UPDATE {table} SET context = 'apprend' WHERE context = 'learning'")
        op.create_check_constraint(
            f"ck_{table}_contexte",
            table,
            "context IN ('enseigne', 'apprend')",
        )

    op.drop_constraint("ck_users_avatar_consistency", "users", type_="check")
    op.execute("UPDATE users SET avatar_status = 'en_attente' WHERE avatar_status = 'pending'")
    op.execute("UPDATE users SET avatar_status = 'disponible' WHERE avatar_status = 'available'")
    op.create_check_constraint(
        "ck_users_avatar_coherence",
        "users",
        "(avatar_s3_key IS NULL AND avatar_mime IS NULL AND avatar_status IS NULL) "
        "OR (avatar_s3_key IS NOT NULL AND avatar_mime IS NOT NULL "
        "AND avatar_status IN ('en_attente', 'disponible'))",
    )

    op.alter_column("resources", "status", server_default="en_attente")
    op.drop_constraint("ck_resources_status", "resources", type_="check")
    op.execute("UPDATE resources SET status = 'en_attente' WHERE status = 'pending'")
    op.execute("UPDATE resources SET status = 'disponible' WHERE status = 'available'")
    op.create_check_constraint(
        "ck_resources_statut",
        "resources",
        "status IN ('en_attente', 'disponible')",
    )

    op.drop_constraint("ck_blocks_type", "blocks", type_="check")
    op.execute("UPDATE blocks SET type = 'texte' WHERE type = 'text'")
    op.execute("UPDATE blocks SET type = 'exercice' WHERE type = 'exercise'")
    op.create_check_constraint(
        "ck_blocks_type",
        "blocks",
        "type IN ('texte', 'exercice', 'document', 'module')",
    )

    op.alter_column("courses", "visibility", server_default=sa.text("'en_cours'"))
    op.drop_constraint("ck_courses_visibility", "courses", type_="check")
    op.execute("UPDATE courses SET visibility = 'prive' WHERE visibility = 'private'")
    op.execute("UPDATE courses SET visibility = 'en_cours' WHERE visibility = 'draft'")
    op.create_check_constraint(
        "ck_courses_visibilite",
        "courses",
        "visibility IN ('public', 'prive', 'en_cours')",
    )

    for table, old, new in reversed(_CONSTRAINT_RENAMES):
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")

    op.execute("ALTER INDEX ix_education_levels_system RENAME TO ix_education_levels_systeme")
    for table, old, new in reversed(_COLUMN_RENAMES):
        op.alter_column(table, new, new_column_name=old)

    # Recréation FTS à l'identique de feb3a436271b (noms français).
    op.execute(
        """
        CREATE FUNCTION courses_tsvector(titre text, description text)
        RETURNS tsvector
        LANGUAGE sql STABLE AS $$
            SELECT setweight(
                       to_tsvector('public.french_unaccent', coalesce(titre, '')), 'A')
                || setweight(
                       to_tsvector('public.french_unaccent', coalesce(description, '')), 'B')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION blocks_tsvector(titre text, description text, content jsonb)
        RETURNS tsvector
        LANGUAGE sql STABLE AS $$
            SELECT setweight(
                       to_tsvector('public.french_unaccent', coalesce(titre, '')), 'B')
                || setweight(
                       to_tsvector('public.french_unaccent',
                           coalesce(description, '') || ' '
                        || coalesce(content->>'markdown', '') || ' '
                        || coalesce(content->>'enonce', '') || ' '
                        || coalesce(content->>'legende', '') || ' '
                        || coalesce((
                               SELECT string_agg(q->>'enonce', ' ')
                               FROM jsonb_array_elements(
                                   CASE WHEN jsonb_typeof(content->'questions') = 'array'
                                        THEN content->'questions'
                                        ELSE '[]'::jsonb END) AS q
                           ), '')
                       ), 'C')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION courses_search_vector_trigger() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.search_vector := courses_tsvector(NEW.titre, NEW.description);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_courses_search_vector
        BEFORE INSERT OR UPDATE OF titre, description ON courses
        FOR EACH ROW EXECUTE FUNCTION courses_search_vector_trigger()
        """
    )
    op.execute(
        """
        CREATE FUNCTION blocks_search_vector_trigger() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.search_vector := blocks_tsvector(NEW.titre, NEW.description, NEW.content);
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_blocks_search_vector
        BEFORE INSERT OR UPDATE OF titre, description, content ON blocks
        FOR EACH ROW EXECUTE FUNCTION blocks_search_vector_trigger()
        """
    )
