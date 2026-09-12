"""Cours d'exemple : le manifeste embarqué et la route de rattrapage.

Le contenu du manifeste est du markdown destiné aux moteurs de rendu du
front (markdown de base, KaTeX, Mermaid, encadrés et les langages d'extension :
JSXGraph, TikZ, frise, passage, SMILES, Vega-Lite, ABC, SQL, Python…), dont les contraintes
ne sont vérifiées nulle part côté serveur. Les tests de gardes de syntaxe
ci-dessous en encodent ce qui est mécaniquement vérifiable — ils n'attestent
pas du rendu final (cf. TODO.md), mais ils rattrapent les fautes qui
donneraient au prof un exemple faux : un dollar dans un nœud Mermaid, une
fraction dans un ``point=`` JSXGraph, une commande LaTeX indisponible.
"""

import ast
import json
import re
import sqlite3
import sys
import unicodedata
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.course_assistant.prompts import MODULE_LIBRARY_NAMES
from app.course_transfer.archive import REF_RE
from app.courses.schemas import PreviewSettings
from app.starter_course import service
from tests.fakes import FakeSession, FakeStorage, inserts, make_client

MANIFEST = service.load_manifest()

_FENCE_RE = re.compile(r"^```(\w+)\n(.*?)^```", re.MULTILINE | re.DOTALL)
# Code (fences et `en ligne`), puis formules : ce qui reste est de la prose.
_CODE_RE = re.compile(r"^```.*?^```|`[^`\n]*`", re.MULTILINE | re.DOTALL)
_MATH_RE = re.compile(r"\$\$.+?\$\$|\$[^$\n]+\$", re.DOTALL)
_MHCHEM_RE = re.compile(r"\\(?:ce|pu)\{")


def _markdowns() -> list[str]:
    """Toutes les chaînes markdown du manifeste (blocs texte et exercices)."""
    texts = []
    for block in MANIFEST.blocks:
        if block.type == "text":
            texts.append(block.content["markdown"])
        elif block.type == "exercise":
            texts.append(block.content["statement"])
            texts.extend(q["statement"] for q in block.content["questions"])
    return texts


def _fences(language: str) -> list[str]:
    """Le corps de chaque bloc de code d'un langage donné."""
    return [
        body
        for markdown in _markdowns()
        for lang, body in _FENCE_RE.findall(markdown)
        if lang == language
    ]


# --- Manifeste embarqué ----------------------------------------------------


def test_manifest_is_valid_and_titled():
    assert MANIFEST.format_version == 2
    assert MANIFEST.course.title == "Cours d'exemple"
    assert MANIFEST.blocks


def test_manifest_has_no_binary_resource():
    # Invariant load-bearing : sans ressource, le seed ne touche jamais S3 et
    # ne peut donc pas laisser une ligne « available » sans objet derrière.
    assert MANIFEST.resources == []


def test_manifest_preview_settings_are_valid():
    settings = PreviewSettings.model_validate(MANIFEST.course.preview_settings)
    # Personnalisés, sinon le prof ne découvre pas le réglage de lecture.
    assert settings.font == "serif"


def test_manifest_covers_the_showcased_block_types():
    assert {b.type for b in MANIFEST.blocks} == {"text", "exercise", "module"}


def test_manifest_module_refs_resolve():
    # Non couvert par Pydantic : ``_refs_consistent`` ne valide que la colonne
    # ``module_ref``, jamais les références écrites dans le markdown.
    declared = {str(m.id).lower() for m in MANIFEST.modules}
    cited = {
        ref.lower()
        for markdown in _markdowns()
        for kind, ref in REF_RE.findall(markdown)
        if kind == "module"
    }
    assert cited, "le cours doit démontrer une référence oc-module:"
    assert cited <= declared


# Pragma des bibliothèques de module — même lecture que ``parseModuleLibraries``
# (``shared/module-runner/module-libraries.ts`` côté front).
_PRAGMA_RE = re.compile(r"^[ \t]*//[ \t]*@oc-libs[ \t]*:(.*)$", re.MULTILINE)
# Trace d'usage attendue dans le JS d'un module qui déclare la bibliothèque
# (p5 en « mode global » : ses fonctions sont nues, sans préfixe).
_LIBRARY_USAGE = {
    "matter": "Matter.",
    "chart": "new Chart(",
    "p5": "createCanvas(",
    "jsxgraph": "JXG.",
    "d3": "d3.",
    "three": "THREE.",
}


def _declared_libraries(js: str) -> set[str]:
    return {
        name.lower()
        for line in _PRAGMA_RE.findall(js)
        for name in re.split(r"[\s,]+", line)
        if name
    }


def test_manifest_modules_are_all_shown():
    # Un module du manifeste sans bloc serait invisible dans le cours.
    shown = {b.module_ref for b in MANIFEST.blocks if b.module_ref}
    assert shown == {m.id for m in MANIFEST.modules}


def test_manifest_modules_use_the_libraries_they_declare():
    assert set(_LIBRARY_USAGE) == set(MODULE_LIBRARY_NAMES)
    for module in MANIFEST.modules:
        declared = _declared_libraries(module.js)
        # Un nom hors catalogue serait ignoré en silence par le runtime.
        assert declared <= set(MODULE_LIBRARY_NAMES), module.title
        for name in declared:
            assert _LIBRARY_USAGE[name] in module.js, (module.title, name)


def test_manifest_showcases_every_module_library():
    declared = set().union(*(_declared_libraries(m.js) for m in MANIFEST.modules))
    assert declared == set(MODULE_LIBRARY_NAMES)


def test_manifest_exercise_questions_have_no_id():
    # Des ids figés donneraient les mêmes question_id à tous les profs.
    for block in MANIFEST.blocks:
        if block.type == "exercise":
            assert all(q.get("id") is None for q in block.content["questions"])


# Le markdown de base — celui sur lequel se posent encadrés, formules et
# langages d'extension. Titres rendus par ``.course-content`` du front, tableaux
# GFM, séparateurs : rien ici n'est propre à OpenCartable, et c'est bien pour ça
# que le cours d'exemple doit l'enseigner.
_HEADING_RE = re.compile(r"^(#{1,6}) ", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^\|[ :|-]+\|$", re.MULTILINE)


def _prose() -> list[str]:
    """Les markdowns du manifeste, code (fences et `en ligne`) retiré."""
    return [_CODE_RE.sub("", markdown) for markdown in _markdowns()]


def test_manifest_showcases_the_classic_markdown_syntax():
    prose = "\n\n".join(_prose())
    assert re.search(r"^### ", prose, re.MULTILINE), "le cours doit démontrer un sous-titre"
    assert _TABLE_SEPARATOR_RE.search(prose), "le cours doit démontrer un tableau"
    assert re.search(r"^---$", prose, re.MULTILINE), "le cours doit démontrer un séparateur"
    assert re.search(r"^  - ", prose, re.MULTILINE), "le cours doit démontrer une liste imbriquée"
    assert "~~" in prose, "le cours doit démontrer du texte barré"
    assert re.search(r"\[[^\]]+\]\(https://", prose), "le cours doit démontrer un lien"


def test_manifest_text_blocks_open_with_their_title():
    # `course-blocks-view` ne rend ``block.title`` que pour les blocs
    # ``document`` et ``module`` : sans titre dans le markdown, un bloc de texte
    # arrive nu dans l'aperçu global comme sur la page de l'élève. Le titre du
    # bloc est donc recopié en `##`, et les sous-parties descendent à `###`.
    for block in MANIFEST.blocks:
        if block.type == "text":
            markdown = block.content["markdown"]
        elif block.type == "exercise":
            markdown = block.content["statement"]
        else:
            continue
        assert markdown.startswith(f"## {block.title}\n"), block.title
        for hashes in _HEADING_RE.findall(_CODE_RE.sub("", markdown))[1:]:
            assert len(hashes) >= 3, block.title


def test_manifest_horizontal_rules_follow_a_blank_line():
    # `---` collé sous un paragraphe est un titre setext, pas un séparateur :
    # le paragraphe du dessus deviendrait un <h2>, sans le moindre message.
    for markdown in _prose():
        lines = markdown.splitlines()
        for index, line in enumerate(lines):
            if line.strip() == "---" and index:
                assert not lines[index - 1].strip(), markdown


def test_manifest_mermaid_fences_have_no_math():
    # `htmlLabels: false` : un dollar dans la source affiche un avertissement
    # sous le diagramme au lieu de rendre la formule.
    fences = _fences("mermaid")
    assert fences, "le cours doit démontrer un diagramme Mermaid"
    for body in fences:
        assert "$" not in body


def test_manifest_jsxgraph_fences_are_well_formed():
    fences = _fences("jsxgraph")
    assert fences, "le cours doit démontrer une figure JSXGraph"
    for body in fences:
        bboxes = 0
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            assert key in {"equation", "point", "bbox"}, line
            if key == "point":
                # parseFloat lit « 8/3 » comme 8 : les fractions mentent.
                parts = [p.strip() for p in value.split(",")]
                assert len(parts) == 2, line
                for part in parts:
                    assert "/" not in part, line
                    float(part)
            elif key == "bbox":
                bboxes += 1
                assert len([float(p) for p in value.split(",")]) == 4, line
            else:
                # JessieCode veut une multiplication explicite : 2*x, pas 2x.
                assert not re.search(r"\d\s*x", value), line
        assert bboxes <= 1, body


_TIKZ_LIBRARY_RE = re.compile(r"\\usetikzlibrary\s*\{([^}]*)\}")
# Sous-ensemble des ``tikzlibrary*`` embarqués par le TikZJax du front : une
# bibliothèque absente fait échouer la compilation (« Code TikZ invalide »).
_EMBEDDED_TIKZ_LIBRARIES = {
    "angles",
    "arrows.meta",
    "calc",
    "circuits.ee.IEC",
    "circuits.logic.IEC",
    "circuits.logic.US",
    "positioning",
    "quotes",
}


def test_manifest_tikz_fences_use_only_the_embedded_distribution():
    fences = _fences("tikz")
    assert fences, "le cours doit démontrer un schéma TikZ"
    libraries = set()
    for body in fences:
        assert "\\usepackage" not in body
        assert "\\documentclass" not in body
        assert "circuitikz" not in body
        for names in _TIKZ_LIBRARY_RE.findall(body):
            libraries |= {name.strip() for name in names.split(",")}
    assert libraries <= _EMBEDDED_TIKZ_LIBRARIES
    assert "circuits.ee.IEC" in libraries, "le cours doit démontrer un circuit électrique"


_TIMELINE_DATE_RE = re.compile(r"-?\d{1,4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?")


def _timeline_date(raw: str) -> float:
    """Miroir de ``parseTimelineDate`` (front) : l'année suffit à ordonner."""
    raw = raw.strip()
    assert _TIMELINE_DATE_RE.fullmatch(raw), raw
    year, _, rest = raw.lstrip("-").partition("-")
    sign = -1 if raw.startswith("-") else 1
    return sign * int(year) + (int(rest[:2]) - 1) / 12 if rest else sign * int(year)


def test_manifest_timeline_fences_are_well_formed():
    # Une ligne invalide est ignorée en silence par la frise (seul un compteur
    # le signale) : l'exemple doit tracer chacune de ses lignes.
    fences = _fences("timeline")
    assert fences, "le cours doit démontrer une frise chronologique"
    for body in fences:
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            assert key in {"start", "end", "step", "period", "event"}, line
            if key == "period":
                start, end, label = (part.strip() for part in value.split(",", 2))
                assert _timeline_date(start) < _timeline_date(end), line
                assert label, line
            elif key == "event":
                date, _, label = value.partition(",")
                _timeline_date(date)
                assert label.strip(), line
            elif key == "step":
                assert int(value) > 0, line
            else:
                _timeline_date(value)


_PASSAGE_HEADER_RE = re.compile(r"(start|step)\s*=\s*(\d+)")


def test_manifest_passage_fences_show_line_numbers():
    # Miroir de ``parsePassageConfig`` (front) : en-tête start=/step= en tête du
    # bloc, puis le texte, lignes vides non comptées. Une valeur hors bornes est
    # ignorée en silence, un en-tête égaré s'affiche comme une ligne du texte,
    # et un extrait qui n'atteint aucun multiple du pas n'affiche aucun numéro.
    fences = _fences("passage")
    assert fences, "le cours doit démontrer un texte à lignes numérotées"
    for body in fences:
        lines = body.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        start, step = 1, 5
        while lines and (header := _PASSAGE_HEADER_RE.fullmatch(lines[0].strip())):
            lines.pop(0)
            key, value = header[1], int(header[2])
            if key == "start":
                assert 1 <= value <= 99_999, body
                start = value
            else:
                assert 1 <= value <= 100, body
                step = value
        numbered = [line for line in lines if line.strip()]
        assert not any(_PASSAGE_HEADER_RE.fullmatch(line.strip()) for line in numbered), body
        assert any((start + i) % step == 0 for i in range(len(numbered))), body


_SMILES_RE = re.compile(r"[A-Za-z0-9@+\-\[\]()=#$:/\\%.*]+")


def test_manifest_smiles_fences_are_well_formed():
    # Parser dédié côté front : une molécule par ligne, « | légende » après.
    fences = _fences("smiles")
    assert fences, "le cours doit démontrer une molécule SMILES"
    for body in fences:
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            smiles, _, _legend = line.partition("|")
            smiles = smiles.strip()
            assert _SMILES_RE.fullmatch(smiles), line
            assert smiles.count("(") == smiles.count(")"), line
            assert smiles.count("[") == smiles.count("]"), line


def _has_url_key(value: object) -> bool:
    if isinstance(value, dict):
        return "url" in value or any(_has_url_key(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_url_key(v) for v in value)
    return False


def test_manifest_vegalite_fences_are_inline_json():
    # Le front refuse toute clé `url` (aucune requête vers un tiers depuis le
    # navigateur d'un élève) et n'affiche qu'une notice pour un JSON invalide.
    fences = _fences("vegalite")
    assert fences, "le cours doit démontrer un graphique Vega-Lite"
    for body in fences:
        spec = json.loads(body)
        assert isinstance(spec, dict)
        assert not _has_url_key(spec), body
        assert spec["data"]["values"], body


def test_manifest_abc_fences_are_playable_on_the_piano():
    # Le front retire les directives MIDI (seul le piano est hébergé) : un
    # exemple qui en porte montrerait un réglage sans effet.
    fences = _fences("abc")
    assert fences, "le cours doit démontrer une partition ABC"
    for body in fences:
        headers = [line.split(":", 1)[0] for line in body.splitlines() if re.match(r"[A-Z]:", line)]
        assert headers[0] == "X", body
        assert headers[-1] == "K", body  # K: clôt l'en-tête, les notes suivent
        assert not re.search(r"%%MIDI|I:\s*MIDI", body, re.IGNORECASE), body


_SQL_QUERY_MARKER = re.compile(r"^[ \t]*--[ \t]*@query[ \t]*$", re.MULTILINE | re.IGNORECASE)


def test_manifest_sql_fences_run_on_sqlite():
    # Vraie exécution (SQLite du back, même dialecte que sql.js) : la
    # préparation passe et la requête montrée à l'élève renvoie des lignes.
    fences = _fences("sql")
    assert fences, "le cours doit démontrer une requête SQL exécutable"
    for body in fences:
        parts = _SQL_QUERY_MARKER.split(body, maxsplit=1)
        setup, query = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(setup)
            cursor = connection.execute(query.strip())
            assert cursor.description is not None, query
            assert cursor.fetchall(), query


# Paquets hébergés à côté de Pyodide (scripts/prepare-pyodide.mjs du front).
_PYTHON_PACKAGES = {"numpy", "matplotlib", "mpl_toolkits", "pylab"}


def test_manifest_python_fences_compile_and_import_only_hosted_packages():
    fences = _fences("python")
    assert fences, "le cours doit démontrer un programme Python exécutable"
    for body in fences:
        tree = ast.parse(body, filename="main.py")  # lève sur une erreur de syntaxe
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".")[0]}
            else:
                continue
            allowed = sys.stdlib_module_names | _PYTHON_PACKAGES
            assert roots <= allowed, f"import non hébergé : {roots - allowed}"


# Mots-clés canoniques des encadrés, normalisés par la lecture de
# ``calloutKind`` (``core/markdown/course-callouts.ts`` côté front) : les six
# types eux-mêmes. Le front reconnaît en plus des alias (mots-clés français
# d'origine, alertes GitHub) — le cours d'exemple, lui, enseigne la graphie
# canonique, celle du catalogue de l'assistant.
_CALLOUT_CANONICAL = {
    "DEFINITION",
    "KEYPOINT",
    "METHOD",
    "EXAMPLE",
    "NOTE",
    "WARNING",
}


_QUOTE_RE = re.compile(r"^(?:[ \t]*>)*")
_CALLOUT_RE = re.compile(r"^((?:[ \t]*>)+)[ \t]?\[!([^\]\n]{1,32})\]")


def _callout_keyword(raw: str) -> str:
    """Majuscules, sans accent, espaces simples : la lecture de ``calloutKind``."""
    decomposed = unicodedata.normalize("NFD", raw)
    bare = "".join(c for c in decomposed if not unicodedata.category(c).startswith("M"))
    return " ".join(bare.upper().split())


def test_manifest_callouts_open_their_quote_with_a_canonical_keyword():
    # Type inconnu ou marqueur ailleurs qu'en tête de citation : le front rend
    # une simple citation, marqueur visible. Le cours d'exemple enseigne la
    # graphie canonique — les alias ne s'y citent qu'en prose.
    keywords = []
    for markdown in _markdowns():
        lines = _CODE_RE.sub("", markdown).splitlines()
        for index, line in enumerate(lines):
            if not (marker := _CALLOUT_RE.match(line)):
                continue
            keywords.append(marker[2])
            assert _callout_keyword(marker[2]) in _CALLOUT_CANONICAL, line
            above = _QUOTE_RE.match(lines[index - 1])[0].count(">") if index else 0
            assert above < marker[1].count(">"), line
    assert keywords, "le cours doit démontrer un encadré"


def test_manifest_mhchem_commands_stay_inside_formulas():
    # `\ce`/`\pu` sont des commandes KaTeX (extension mhchem) : hors d'une
    # formule, elles s'affichent en texte brut.
    shown = 0
    for markdown in _markdowns():
        prose = _CODE_RE.sub("", markdown)
        shown += len(_MHCHEM_RE.findall(prose))
        assert not _MHCHEM_RE.search(_MATH_RE.sub("", prose)), markdown
    assert shown, "le cours doit démontrer la notation chimique (mhchem)"


def test_manifest_uses_no_katex_macro():
    # Aucune macro n'est déclarée côté front : `\newcommand` ne rendrait rien.
    for markdown in _markdowns():
        assert "\\newcommand" not in markdown


# --- Route de rattrapage ---------------------------------------------------


def _seed_session(user, subject_ids=(), level_ids=()):
    """FIFO d'``insert_manifest_course`` : matières, niveaux, RETURNING cours."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return FakeSession([[user], list(subject_ids), list(level_ids), [(now, now)]])


@pytest.fixture
def user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def test_starter_requires_auth(client: TestClient):
    response = client.post("/api/v1/courses/starter")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_starter_creates_draft_course(user_row):
    subject, level = uuid.uuid4(), uuid.uuid4()
    session = _seed_session(user_row, [subject], [level])
    response = make_client(session).post("/api/v1/courses/starter")

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Cours d'exemple"
    assert body["visibility"] == "draft"
    assert body["block_count"] == len(MANIFEST.blocks)
    assert body["subject_ids"] == [str(subject)]
    assert body["preview_settings"] == MANIFEST.course.preview_settings
    assert session.commits == 2  # get_or_create_by_sub, puis le cours


def test_starter_never_touches_s3(user_row):
    storage = FakeStorage()
    session = _seed_session(user_row)
    response = make_client(session, storage).post("/api/v1/courses/starter")

    assert response.status_code == 201
    assert inserts(session, "resources") == []
    assert storage.put_calls == [] and storage.head_calls == [] and storage.deleted == []


def test_starter_ignores_unknown_taxonomy_codes(user_row):
    # Instance sans la taxonomie française : le cours arrive quand même, nu.
    session = _seed_session(user_row)
    response = make_client(session).post("/api/v1/courses/starter")

    assert response.status_code == 201
    assert response.json()["subject_ids"] == []
    assert inserts(session, "course_subjects") == []


def test_starter_rewrites_the_module_references(user_row):
    session = _seed_session(user_row)
    response = make_client(session).post("/api/v1/courses/starter")
    assert response.status_code == 201

    [(_, module_params)] = inserts(session, "modules")
    # Titres uniques : correspondance id du manifeste → uuid FRAIS.
    fresh = {p["title"]: p["id"] for p in module_params}
    renamed = {str(m.id): fresh[m.title] for m in MANIFEST.modules}
    assert len(set(renamed.values())) == len(MANIFEST.modules)

    [(_, block_params)] = inserts(session, "blocks")
    # Colonnes des blocs « module » et références oc-module: du markdown
    # pointent les uuid frais ; ceux du manifeste ont disparu partout.
    shown = [str(b.module_ref) for b in MANIFEST.blocks if b.module_ref]
    assert [p["module_id"] for p in block_params if p["module_id"]] == [
        renamed[ref] for ref in shown
    ]
    cited_in_manifest = {
        ref.lower()
        for markdown in _markdowns()
        for kind, ref in REF_RE.findall(markdown)
        if kind == "module"
    }
    cited = {
        ref.lower()
        for p in block_params
        if p["type"] == "text"
        for _kind, ref in REF_RE.findall(p["content"]["markdown"])
    }
    assert cited == {str(renamed[ref]).lower() for ref in cited_in_manifest}
    for manifest_id in renamed:
        assert manifest_id not in repr(block_params)


def test_starter_generates_fresh_question_ids(user_row):
    session = _seed_session(user_row)
    make_client(session).post("/api/v1/courses/starter")

    [(_, block_params)] = inserts(session, "blocks")
    [exercise] = [p for p in block_params if p["type"] == "exercise"]
    ids = [q["id"] for q in exercise["content"]["questions"]]
    assert all(uuid.UUID(qid) for qid in ids)
    assert len(set(ids)) == len(ids)


def test_starter_broken_manifest_returns_503(user_row, monkeypatch):
    def _boom():
        raise ValueError("manifeste illisible")

    monkeypatch.setattr(service, "load_manifest", _boom)
    session = _seed_session(user_row)
    response = make_client(session).post("/api/v1/courses/starter")

    assert response.status_code == 503
    assert inserts(session, "courses") == []
