"""Classification pré-remplie des niveaux d'étude (système français, voie générale).

Module de données PUR : aucun import de code applicatif (la migration de
seed l'importe directement). Contrat APPEND-ONLY : ne jamais renommer ni
supprimer un nœud existant d'une façon qui change son ``code`` (les IDs
uuid5 et la migration de seed en dépendent) ; les ajouts (maternelle, voie
professionnelle, BTS/BUT/CPGE, systèmes étrangers…) se font par de
nouvelles data migrations réutilisant ``iter_rows()``.

Structure : cycle (profondeur 0) > classe (profondeur 1). Les ``slug`` sont
écrits à la main — jamais dérivés du nom affiché — et composent le ``code``
préfixé par le système (ex. ``fr.college.6e``). ``cite`` = niveau CITE/ISCED
2011 (pivot international UNESCO, None quand le nœud en couvre plusieurs) ;
``age_min``/``age_max`` = âges typiques (pivot secondaire entre systèmes).
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

# Namespace figé — NE JAMAIS CHANGER (les IDs seedés en dérivent).
# Distinct de celui des subjects ; hex final = « niveau ».
SEED_NAMESPACE = uuid.UUID("8c4d2b1f-0000-4000-8000-6e6976656175")

SYSTEME = "fr"


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


NIVEAUX: tuple[Node, ...] = (
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
)


def iter_rows() -> Iterator[dict]:
    """Aplatit ``NIVEAUX`` en lignes prêtes pour la table ``education_levels``.

    Yield des dicts ``{id, parent_id, nom, code, systeme, cite, age_min,
    age_max, profondeur, position}``, parents toujours AVANT leurs enfants
    (ordre FK garanti pour l'insert). C'est la seule API consommée par la
    migration de seed et les tests.
    """

    def walk(
        node: Node,
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
            "systeme": SYSTEME,
            "cite": node.cite,
            "age_min": node.age_min,
            "age_max": node.age_max,
            "profondeur": profondeur,
            "position": position,
        }
        for i, enfant in enumerate(node.enfants):
            yield from walk(enfant, code, nid, profondeur + 1, i)

    for i, cycle in enumerate(NIVEAUX):
        yield from walk(cycle, SYSTEME, None, 0, i)
