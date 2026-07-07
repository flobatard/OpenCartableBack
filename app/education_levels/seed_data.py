"""Classification pré-remplie des niveaux d'étude, par système scolaire.

Module de données PUR : aucun import de code applicatif (la migration de
seed l'importe directement). Contrat APPEND-ONLY : ne jamais renommer ni
supprimer un nœud existant d'une façon qui change son ``code`` (les IDs
uuid5 et la migration de seed en dépendent) ; les ajouts (maternelle, voie
professionnelle, BTS/BUT/CPGE, autres systèmes…) se font par de nouvelles
data migrations réutilisant ``iter_rows(systemes=...)``.

Structure : cycle (profondeur 0) > classe (profondeur 1). Les ``slug`` sont
écrits à la main — jamais dérivés du nom affiché — et composent le ``code``
préfixé par le système (ex. ``fr.college.6e``). ``cite`` = niveau CITE/ISCED
2011 (pivot international UNESCO, None quand le nœud en couvre plusieurs) ;
``age_min``/``age_max`` = âges typiques (pivot secondaire entre systèmes).

Les noms sont les noms propres nationaux, jamais traduits (cf. modèle).
Périmètre par système : voie générale, hors préélémentaire (comme la
maternelle fr) ; le supérieur est représenté par diplôme (Bachelor/Master/
Doctorat locaux), pas par année. Les systèmes fédéraux ou éclatés sont
réduits à leur variante dominante ou pertinente pour un public francophone :
``uk`` = Angleterre et pays de Galles, ``be`` = Fédération Wallonie-Bruxelles,
``ch`` = Suisse romande (numérotation HarmoS), ``ca`` = Canada hors Québec,
``ca-qc`` = Québec.
"""

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

# Namespace figé — NE JAMAIS CHANGER (les IDs seedés en dérivent).
# Distinct de celui des subjects ; hex final = « niveau ».
SEED_NAMESPACE = uuid.UUID("8c4d2b1f-0000-4000-8000-6e6976656175")


@dataclass(frozen=True)
class Node:
    nom: str
    slug: str
    cite: int | None
    age_min: int | None
    age_max: int | None
    enfants: tuple["Node", ...] = field(default=())


def education_level_id(code: str) -> uuid.UUID:
    """ID déterministe d'un nœud seedé, dérivé de son code complet."""
    return uuid.uuid5(SEED_NAMESPACE, code)


SYSTEMES: dict[str, tuple[Node, ...]] = {
    # ------------------------------------------------------------------
    # France — voie générale, primaire -> doctorat.
    # ------------------------------------------------------------------
    "fr": (
        Node("Primaire", "primaire", cite=1, age_min=6, age_max=11, enfants=(
            Node("CP", "cp", cite=1, age_min=6, age_max=7),
            Node("CE1", "ce1", cite=1, age_min=7, age_max=8),
            Node("CE2", "ce2", cite=1, age_min=8, age_max=9),
            Node("CM1", "cm1", cite=1, age_min=9, age_max=10),
            Node("CM2", "cm2", cite=1, age_min=10, age_max=11),
        )),
        Node("Collège", "college", cite=2, age_min=11, age_max=15, enfants=(
            Node("6e", "6e", cite=2, age_min=11, age_max=12),
            Node("5e", "5e", cite=2, age_min=12, age_max=13),
            Node("4e", "4e", cite=2, age_min=13, age_max=14),
            Node("3e", "3e", cite=2, age_min=14, age_max=15),
        )),
        Node("Lycée", "lycee", cite=3, age_min=15, age_max=18, enfants=(
            Node("Seconde", "seconde", cite=3, age_min=15, age_max=16),
            Node("Première", "premiere", cite=3, age_min=16, age_max=17),
            Node("Terminale", "terminale", cite=3, age_min=17, age_max=18),
        )),
        # Cycle multi-CITE (licence 6, master 7, doctorat 8) : cite=None.
        Node("Supérieur", "superieur", cite=None, age_min=18, age_max=None, enfants=(
            Node("L1", "l1", cite=6, age_min=18, age_max=19),
            Node("L2", "l2", cite=6, age_min=19, age_max=20),
            Node("L3", "l3", cite=6, age_min=20, age_max=21),
            Node("M1", "m1", cite=7, age_min=21, age_max=22),
            Node("M2", "m2", cite=7, age_min=22, age_max=23),
            Node("Doctorat", "doctorat", cite=8, age_min=23, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Allemagne — Klasse 13 incluse (Länder en G9) ; supérieur post-Bologne.
    # ------------------------------------------------------------------
    "de": (
        Node("Grundschule", "grundschule", cite=1, age_min=6, age_max=10, enfants=(
            Node("Klasse 1", "klasse-1", cite=1, age_min=6, age_max=7),
            Node("Klasse 2", "klasse-2", cite=1, age_min=7, age_max=8),
            Node("Klasse 3", "klasse-3", cite=1, age_min=8, age_max=9),
            Node("Klasse 4", "klasse-4", cite=1, age_min=9, age_max=10),
        )),
        Node("Sekundarstufe I", "sekundarstufe-1", cite=2, age_min=10, age_max=16, enfants=(
            Node("Klasse 5", "klasse-5", cite=2, age_min=10, age_max=11),
            Node("Klasse 6", "klasse-6", cite=2, age_min=11, age_max=12),
            Node("Klasse 7", "klasse-7", cite=2, age_min=12, age_max=13),
            Node("Klasse 8", "klasse-8", cite=2, age_min=13, age_max=14),
            Node("Klasse 9", "klasse-9", cite=2, age_min=14, age_max=15),
            Node("Klasse 10", "klasse-10", cite=2, age_min=15, age_max=16),
        )),
        Node("Sekundarstufe II", "sekundarstufe-2", cite=3, age_min=16, age_max=19, enfants=(
            Node("Klasse 11", "klasse-11", cite=3, age_min=16, age_max=17),
            Node("Klasse 12", "klasse-12", cite=3, age_min=17, age_max=18),
            Node("Klasse 13", "klasse-13", cite=3, age_min=18, age_max=19),
        )),
        Node("Hochschule", "hochschule", cite=None, age_min=19, age_max=None, enfants=(
            Node("Bachelor", "bachelor", cite=6, age_min=19, age_max=22),
            Node("Master", "master", cite=7, age_min=22, age_max=24),
            Node("Promotion", "promotion", cite=8, age_min=24, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Royaume-Uni (Angleterre et pays de Galles) — l'Écosse diffère trop
    # pour être fusionnée ici. Secondary multi-CITE : KS3 (Y7-9) = 2,
    # KS4/GCSE (Y10-11) = 3.
    # ------------------------------------------------------------------
    "uk": (
        Node("Primary school", "primary", cite=1, age_min=5, age_max=11, enfants=(
            Node("Year 1", "year-1", cite=1, age_min=5, age_max=6),
            Node("Year 2", "year-2", cite=1, age_min=6, age_max=7),
            Node("Year 3", "year-3", cite=1, age_min=7, age_max=8),
            Node("Year 4", "year-4", cite=1, age_min=8, age_max=9),
            Node("Year 5", "year-5", cite=1, age_min=9, age_max=10),
            Node("Year 6", "year-6", cite=1, age_min=10, age_max=11),
        )),
        Node("Secondary school", "secondary", cite=None, age_min=11, age_max=16, enfants=(
            Node("Year 7", "year-7", cite=2, age_min=11, age_max=12),
            Node("Year 8", "year-8", cite=2, age_min=12, age_max=13),
            Node("Year 9", "year-9", cite=2, age_min=13, age_max=14),
            Node("Year 10", "year-10", cite=3, age_min=14, age_max=15),
            Node("Year 11", "year-11", cite=3, age_min=15, age_max=16),
        )),
        Node("Sixth form", "sixth-form", cite=3, age_min=16, age_max=18, enfants=(
            Node("Year 12", "year-12", cite=3, age_min=16, age_max=17),
            Node("Year 13", "year-13", cite=3, age_min=17, age_max=18),
        )),
        Node("Higher education", "higher-education", cite=None, age_min=18, age_max=None, enfants=(
            Node("Undergraduate", "undergraduate", cite=6, age_min=18, age_max=21),
            Node("Master's degree", "masters", cite=7, age_min=21, age_max=22),
            Node("Doctorate", "doctorate", cite=8, age_min=22, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Espagne — LOMLOE : Primaria (6 ans), ESO (4), Bachillerato (2).
    # ------------------------------------------------------------------
    "es": (
        Node("Educación Primaria", "primaria", cite=1, age_min=6, age_max=12, enfants=(
            Node("1.º de Primaria", "primaria-1", cite=1, age_min=6, age_max=7),
            Node("2.º de Primaria", "primaria-2", cite=1, age_min=7, age_max=8),
            Node("3.º de Primaria", "primaria-3", cite=1, age_min=8, age_max=9),
            Node("4.º de Primaria", "primaria-4", cite=1, age_min=9, age_max=10),
            Node("5.º de Primaria", "primaria-5", cite=1, age_min=10, age_max=11),
            Node("6.º de Primaria", "primaria-6", cite=1, age_min=11, age_max=12),
        )),
        Node(
            "Educación Secundaria Obligatoria", "eso", cite=2, age_min=12, age_max=16, enfants=(
                Node("1.º de ESO", "eso-1", cite=2, age_min=12, age_max=13),
                Node("2.º de ESO", "eso-2", cite=2, age_min=13, age_max=14),
                Node("3.º de ESO", "eso-3", cite=2, age_min=14, age_max=15),
                Node("4.º de ESO", "eso-4", cite=2, age_min=15, age_max=16),
            )
        ),
        Node("Bachillerato", "bachillerato", cite=3, age_min=16, age_max=18, enfants=(
            Node("1.º de Bachillerato", "bachillerato-1", cite=3, age_min=16, age_max=17),
            Node("2.º de Bachillerato", "bachillerato-2", cite=3, age_min=17, age_max=18),
        )),
        Node("Educación Superior", "superior", cite=None, age_min=18, age_max=None, enfants=(
            Node("Grado", "grado", cite=6, age_min=18, age_max=22),
            Node("Máster", "master", cite=7, age_min=22, age_max=23),
            Node("Doctorado", "doctorado", cite=8, age_min=23, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Italie — noms d'usage (elementare/media/superiore) plus parlants que
    # les intitulés administratifs des classes.
    # ------------------------------------------------------------------
    "it": (
        Node("Scuola primaria", "primaria", cite=1, age_min=6, age_max=11, enfants=(
            Node("Prima elementare", "prima", cite=1, age_min=6, age_max=7),
            Node("Seconda elementare", "seconda", cite=1, age_min=7, age_max=8),
            Node("Terza elementare", "terza", cite=1, age_min=8, age_max=9),
            Node("Quarta elementare", "quarta", cite=1, age_min=9, age_max=10),
            Node("Quinta elementare", "quinta", cite=1, age_min=10, age_max=11),
        )),
        Node(
            "Scuola secondaria di primo grado", "secondaria-1",
            cite=2, age_min=11, age_max=14, enfants=(
                Node("Prima media", "prima", cite=2, age_min=11, age_max=12),
                Node("Seconda media", "seconda", cite=2, age_min=12, age_max=13),
                Node("Terza media", "terza", cite=2, age_min=13, age_max=14),
            )
        ),
        Node(
            "Scuola secondaria di secondo grado", "secondaria-2",
            cite=3, age_min=14, age_max=19, enfants=(
                Node("Prima superiore", "prima", cite=3, age_min=14, age_max=15),
                Node("Seconda superiore", "seconda", cite=3, age_min=15, age_max=16),
                Node("Terza superiore", "terza", cite=3, age_min=16, age_max=17),
                Node("Quarta superiore", "quarta", cite=3, age_min=17, age_max=18),
                Node("Quinta superiore", "quinta", cite=3, age_min=18, age_max=19),
            )
        ),
        Node("Università", "universita", cite=None, age_min=19, age_max=None, enfants=(
            Node("Laurea triennale", "laurea-triennale", cite=6, age_min=19, age_max=22),
            Node("Laurea magistrale", "laurea-magistrale", cite=7, age_min=22, age_max=24),
            Node("Dottorato di ricerca", "dottorato", cite=8, age_min=24, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Belgique (Fédération Wallonie-Bruxelles) — secondaire en 3 degrés de
    # 2 ans, multi-CITE (1re-3e = 2, 4e-6e = 3).
    # ------------------------------------------------------------------
    "be": (
        Node("Enseignement primaire", "primaire", cite=1, age_min=6, age_max=12, enfants=(
            Node("1re primaire", "1re", cite=1, age_min=6, age_max=7),
            Node("2e primaire", "2e", cite=1, age_min=7, age_max=8),
            Node("3e primaire", "3e", cite=1, age_min=8, age_max=9),
            Node("4e primaire", "4e", cite=1, age_min=9, age_max=10),
            Node("5e primaire", "5e", cite=1, age_min=10, age_max=11),
            Node("6e primaire", "6e", cite=1, age_min=11, age_max=12),
        )),
        Node("Enseignement secondaire", "secondaire", cite=None, age_min=12, age_max=18, enfants=(
            Node("1re secondaire", "1re", cite=2, age_min=12, age_max=13),
            Node("2e secondaire", "2e", cite=2, age_min=13, age_max=14),
            Node("3e secondaire", "3e", cite=2, age_min=14, age_max=15),
            Node("4e secondaire", "4e", cite=3, age_min=15, age_max=16),
            Node("5e secondaire", "5e", cite=3, age_min=16, age_max=17),
            Node("6e secondaire", "6e", cite=3, age_min=17, age_max=18),
        )),
        Node("Enseignement supérieur", "superieur", cite=None, age_min=18, age_max=None, enfants=(
            Node("Bachelier", "bachelier", cite=6, age_min=18, age_max=21),
            Node("Master", "master", cite=7, age_min=21, age_max=23),
            Node("Doctorat", "doctorat", cite=8, age_min=23, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Suisse romande — numérotation HarmoS ; 1P-2P (école enfantine,
    # CITE 0) exclues comme la maternelle fr. Secondaire II sur 4 ans pour
    # couvrir Genève (3 ans dans d'autres cantons).
    # ------------------------------------------------------------------
    "ch": (
        Node("Degré primaire", "primaire", cite=1, age_min=6, age_max=12, enfants=(
            Node("3P", "3p", cite=1, age_min=6, age_max=7),
            Node("4P", "4p", cite=1, age_min=7, age_max=8),
            Node("5P", "5p", cite=1, age_min=8, age_max=9),
            Node("6P", "6p", cite=1, age_min=9, age_max=10),
            Node("7P", "7p", cite=1, age_min=10, age_max=11),
            Node("8P", "8p", cite=1, age_min=11, age_max=12),
        )),
        Node("Degré secondaire I", "secondaire-1", cite=2, age_min=12, age_max=15, enfants=(
            Node("9e", "9e", cite=2, age_min=12, age_max=13),
            Node("10e", "10e", cite=2, age_min=13, age_max=14),
            Node("11e", "11e", cite=2, age_min=14, age_max=15),
        )),
        Node("Degré secondaire II", "secondaire-2", cite=3, age_min=15, age_max=19, enfants=(
            Node("1re année de gymnase", "gymnase-1", cite=3, age_min=15, age_max=16),
            Node("2e année de gymnase", "gymnase-2", cite=3, age_min=16, age_max=17),
            Node("3e année de gymnase", "gymnase-3", cite=3, age_min=17, age_max=18),
            Node("4e année de gymnase", "gymnase-4", cite=3, age_min=18, age_max=19),
        )),
        Node("Hautes écoles", "hautes-ecoles", cite=None, age_min=19, age_max=None, enfants=(
            Node("Bachelor", "bachelor", cite=6, age_min=19, age_max=22),
            Node("Master", "master", cite=7, age_min=22, age_max=24),
            Node("Doctorat", "doctorat", cite=8, age_min=24, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Pays-Bas — groep 1-2 (CITE 0) exclus ; voortgezet onderwijs jusqu'au
    # vwo (6 ans, le havo s'arrête en 5e, le vmbo en 4e), multi-CITE.
    # ------------------------------------------------------------------
    "nl": (
        Node("Basisschool", "basisschool", cite=1, age_min=6, age_max=12, enfants=(
            Node("Groep 3", "groep-3", cite=1, age_min=6, age_max=7),
            Node("Groep 4", "groep-4", cite=1, age_min=7, age_max=8),
            Node("Groep 5", "groep-5", cite=1, age_min=8, age_max=9),
            Node("Groep 6", "groep-6", cite=1, age_min=9, age_max=10),
            Node("Groep 7", "groep-7", cite=1, age_min=10, age_max=11),
            Node("Groep 8", "groep-8", cite=1, age_min=11, age_max=12),
        )),
        Node("Voortgezet onderwijs", "voortgezet", cite=None, age_min=12, age_max=18, enfants=(
            Node("Leerjaar 1", "leerjaar-1", cite=2, age_min=12, age_max=13),
            Node("Leerjaar 2", "leerjaar-2", cite=2, age_min=13, age_max=14),
            Node("Leerjaar 3", "leerjaar-3", cite=2, age_min=14, age_max=15),
            Node("Leerjaar 4", "leerjaar-4", cite=3, age_min=15, age_max=16),
            Node("Leerjaar 5", "leerjaar-5", cite=3, age_min=16, age_max=17),
            Node("Leerjaar 6", "leerjaar-6", cite=3, age_min=17, age_max=18),
        )),
        Node("Hoger onderwijs", "hoger-onderwijs", cite=None, age_min=18, age_max=None, enfants=(
            Node("Bachelor", "bachelor", cite=6, age_min=18, age_max=21),
            Node("Master", "master", cite=7, age_min=21, age_max=23),
            Node("Doctoraat", "doctoraat", cite=8, age_min=23, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Portugal — Ensino Básico multi-CITE : 1.º-2.º ciclos (anos 1-6) = 1,
    # 3.º ciclo (anos 7-9) = 2.
    # ------------------------------------------------------------------
    "pt": (
        Node("Ensino Básico", "basico", cite=None, age_min=6, age_max=15, enfants=(
            Node("1.º ano", "ano-1", cite=1, age_min=6, age_max=7),
            Node("2.º ano", "ano-2", cite=1, age_min=7, age_max=8),
            Node("3.º ano", "ano-3", cite=1, age_min=8, age_max=9),
            Node("4.º ano", "ano-4", cite=1, age_min=9, age_max=10),
            Node("5.º ano", "ano-5", cite=1, age_min=10, age_max=11),
            Node("6.º ano", "ano-6", cite=1, age_min=11, age_max=12),
            Node("7.º ano", "ano-7", cite=2, age_min=12, age_max=13),
            Node("8.º ano", "ano-8", cite=2, age_min=13, age_max=14),
            Node("9.º ano", "ano-9", cite=2, age_min=14, age_max=15),
        )),
        Node("Ensino Secundário", "secundario", cite=3, age_min=15, age_max=18, enfants=(
            Node("10.º ano", "ano-10", cite=3, age_min=15, age_max=16),
            Node("11.º ano", "ano-11", cite=3, age_min=16, age_max=17),
            Node("12.º ano", "ano-12", cite=3, age_min=17, age_max=18),
        )),
        Node("Ensino Superior", "superior", cite=None, age_min=18, age_max=None, enfants=(
            Node("Licenciatura", "licenciatura", cite=6, age_min=18, age_max=21),
            Node("Mestrado", "mestrado", cite=7, age_min=21, age_max=23),
            Node("Doutoramento", "doutoramento", cite=8, age_min=23, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # États-Unis — kindergarten (CITE 0) exclu ; découpage institutionnel
    # 5-3-4, mais mapping CITE officiel (UIS) par grade : 1-6 = 1, 7-9 = 2,
    # 10-12 = 3, d'où middle et high school multi-CITE.
    # ------------------------------------------------------------------
    "us": (
        Node("Elementary school", "elementary", cite=1, age_min=6, age_max=11, enfants=(
            Node("Grade 1", "grade-1", cite=1, age_min=6, age_max=7),
            Node("Grade 2", "grade-2", cite=1, age_min=7, age_max=8),
            Node("Grade 3", "grade-3", cite=1, age_min=8, age_max=9),
            Node("Grade 4", "grade-4", cite=1, age_min=9, age_max=10),
            Node("Grade 5", "grade-5", cite=1, age_min=10, age_max=11),
        )),
        Node("Middle school", "middle", cite=None, age_min=11, age_max=14, enfants=(
            Node("Grade 6", "grade-6", cite=1, age_min=11, age_max=12),
            Node("Grade 7", "grade-7", cite=2, age_min=12, age_max=13),
            Node("Grade 8", "grade-8", cite=2, age_min=13, age_max=14),
        )),
        Node("High school", "high", cite=None, age_min=14, age_max=18, enfants=(
            Node("Grade 9", "grade-9", cite=2, age_min=14, age_max=15),
            Node("Grade 10", "grade-10", cite=3, age_min=15, age_max=16),
            Node("Grade 11", "grade-11", cite=3, age_min=16, age_max=17),
            Node("Grade 12", "grade-12", cite=3, age_min=17, age_max=18),
        )),
        Node("Higher education", "higher-education", cite=None, age_min=18, age_max=None, enfants=(
            Node("Undergraduate", "undergraduate", cite=6, age_min=18, age_max=22),
            Node("Master's degree", "masters", cite=7, age_min=22, age_max=24),
            Node("Doctorate", "doctorate", cite=8, age_min=24, age_max=None),
        )),
    ),
    # ------------------------------------------------------------------
    # Canada hors Québec — l'éducation est provinciale ; découpage 6-3-3
    # générique (modèle des Prairies ; l'Ontario fusionne elementary 1-8).
    # ------------------------------------------------------------------
    "ca": (
        Node("Elementary school", "elementary", cite=1, age_min=6, age_max=12, enfants=(
            Node("Grade 1", "grade-1", cite=1, age_min=6, age_max=7),
            Node("Grade 2", "grade-2", cite=1, age_min=7, age_max=8),
            Node("Grade 3", "grade-3", cite=1, age_min=8, age_max=9),
            Node("Grade 4", "grade-4", cite=1, age_min=9, age_max=10),
            Node("Grade 5", "grade-5", cite=1, age_min=10, age_max=11),
            Node("Grade 6", "grade-6", cite=1, age_min=11, age_max=12),
        )),
        Node("Junior high school", "junior-high", cite=2, age_min=12, age_max=15, enfants=(
            Node("Grade 7", "grade-7", cite=2, age_min=12, age_max=13),
            Node("Grade 8", "grade-8", cite=2, age_min=13, age_max=14),
            Node("Grade 9", "grade-9", cite=2, age_min=14, age_max=15),
        )),
        Node("Senior high school", "senior-high", cite=3, age_min=15, age_max=18, enfants=(
            Node("Grade 10", "grade-10", cite=3, age_min=15, age_max=16),
            Node("Grade 11", "grade-11", cite=3, age_min=16, age_max=17),
            Node("Grade 12", "grade-12", cite=3, age_min=17, age_max=18),
        )),
        Node(
            "Postsecondary education", "postsecondary",
            cite=None, age_min=18, age_max=None, enfants=(
                Node("Bachelor's degree", "bachelors", cite=6, age_min=18, age_max=22),
                Node("Master's degree", "masters", cite=7, age_min=22, age_max=24),
                Node("Doctorate", "doctorate", cite=8, age_min=24, age_max=None),
            )
        ),
    ),
    # ------------------------------------------------------------------
    # Québec — secondaire en 5 ans multi-CITE (1re-3e = 2, 4e-5e = 3) ;
    # cégep préuniversitaire = CITE 4 (post-secondaire non tertiaire).
    # ------------------------------------------------------------------
    "ca-qc": (
        Node("Primaire", "primaire", cite=1, age_min=6, age_max=12, enfants=(
            Node("1re année", "1re", cite=1, age_min=6, age_max=7),
            Node("2e année", "2e", cite=1, age_min=7, age_max=8),
            Node("3e année", "3e", cite=1, age_min=8, age_max=9),
            Node("4e année", "4e", cite=1, age_min=9, age_max=10),
            Node("5e année", "5e", cite=1, age_min=10, age_max=11),
            Node("6e année", "6e", cite=1, age_min=11, age_max=12),
        )),
        Node("Secondaire", "secondaire", cite=None, age_min=12, age_max=17, enfants=(
            Node("1re secondaire", "1re", cite=2, age_min=12, age_max=13),
            Node("2e secondaire", "2e", cite=2, age_min=13, age_max=14),
            Node("3e secondaire", "3e", cite=2, age_min=14, age_max=15),
            Node("4e secondaire", "4e", cite=3, age_min=15, age_max=16),
            Node("5e secondaire", "5e", cite=3, age_min=16, age_max=17),
        )),
        Node("Cégep", "cegep", cite=4, age_min=17, age_max=19, enfants=(
            Node("1re année de cégep", "1re", cite=4, age_min=17, age_max=18),
            Node("2e année de cégep", "2e", cite=4, age_min=18, age_max=19),
        )),
        Node("Université", "universite", cite=None, age_min=19, age_max=None, enfants=(
            Node("Baccalauréat", "baccalaureat", cite=6, age_min=19, age_max=22),
            Node("Maîtrise", "maitrise", cite=7, age_min=22, age_max=24),
            Node("Doctorat", "doctorat", cite=8, age_min=24, age_max=None),
        )),
    ),
}


def iter_rows(systemes: Iterable[str] | None = None) -> Iterator[dict]:
    """Aplatit ``SYSTEMES`` en lignes prêtes pour la table ``education_levels``.

    ``systemes`` restreint aux systèmes demandés (None = tous, dans l'ordre
    de déclaration) — c'est le point d'entrée des futures data migrations
    d'append, qui ne doivent insérer que les nouveaux systèmes. Yield des
    dicts ``{id, parent_id, nom, code, systeme, cite, age_min, age_max,
    profondeur, position}``, parents toujours AVANT leurs enfants (ordre FK
    garanti pour l'insert). C'est la seule API consommée par la migration
    de seed et les tests.
    """

    def walk(
        node: Node,
        systeme: str,
        parent_code: str,
        parent_id: uuid.UUID | None,
        profondeur: int,
        position: int,
    ) -> Iterator[dict]:
        code = f"{parent_code}.{node.slug}"
        nid = education_level_id(code)
        yield {
            "id": nid,
            "parent_id": parent_id,
            "nom": node.nom,
            "code": code,
            "systeme": systeme,
            "cite": node.cite,
            "age_min": node.age_min,
            "age_max": node.age_max,
            "profondeur": profondeur,
            "position": position,
        }
        for i, enfant in enumerate(node.enfants):
            yield from walk(enfant, systeme, code, nid, profondeur + 1, i)

    retenus = SYSTEMES if systemes is None else {s: SYSTEMES[s] for s in systemes}
    for systeme, cycles in retenus.items():
        for i, cycle in enumerate(cycles):
            yield from walk(cycle, systeme, systeme, None, 0, i)
