"""Taxonomie pré-remplie des matières (lycée -> master).

Module de données PUR : aucun import de code applicatif (la migration de
seed l'importe directement). Contrat APPEND-ONLY : ne jamais renommer ni
supprimer un nœud existant d'une façon qui change son ``code`` (les IDs
uuid5 et la migration de seed en dépendent) ; les ajouts se font par de
nouvelles data migrations réutilisant ``iter_rows()``.

Structure : ``("Nom affiché", [enfants])`` ; une chaîne nue = feuille.
La profondeur est implicite (niveau d'imbrication, 0 à 3).
"""

import unicodedata
import uuid
from collections.abc import Iterator

# Namespace figé — NE JAMAIS CHANGER (les IDs seedés en dérivent).
SEED_NAMESPACE = uuid.UUID("6b7a9f2e-0000-4000-8000-6f70656e6361")

Node = str | tuple[str, list["Node"]]


def _slugify(nom: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode()
    slug = "".join(c if c.isalnum() else "-" for c in ascii_.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def subject_id(code: str) -> uuid.UUID:
    """ID déterministe d'un nœud seedé, dérivé de son chemin slug complet."""
    return uuid.uuid5(SEED_NAMESPACE, code)


TAXONOMIE: list[Node] = [
    ("Mathématiques", [
        ("Analyse", [
            ("Suites numériques", [
                "Suites arithmétiques et géométriques",
                "Raisonnement par récurrence",
                "Limites de suites",
                "Suites adjacentes et théorèmes de convergence",
            ]),
            ("Fonctions", [
                "Limites de fonctions",
                "Continuité",
                "Dérivation",
                "Convexité",
                "Fonction exponentielle",
                "Fonction logarithme",
                "Fonctions trigonométriques",
                "Étude de fonctions",
            ]),
            ("Calcul intégral", [
                "Primitives",
                "Intégrales et aires",
                "Techniques d'intégration",
                "Intégrales généralisées",
            ]),
            ("Équations différentielles", [
                "Équations différentielles du premier ordre",
                "Équations différentielles linéaires du second ordre",
                "Systèmes différentiels",
            ]),
            ("Séries", [
                "Séries numériques",
                "Séries entières",
                "Suites et séries de fonctions",
                "Séries de Fourier",
            ]),
            ("Calcul différentiel et topologie", [
                "Topologie des espaces vectoriels normés",
                "Fonctions de plusieurs variables",
                "Calcul différentiel",
                "Optimisation",
            ]),
            ("Analyse fonctionnelle", [
                "Espaces de Banach et de Hilbert",
                "Théorie de la mesure et intégrale de Lebesgue",
                "Distributions",
            ]),
        ]),
        ("Algèbre", [
            ("Arithmétique", [
                "Divisibilité et division euclidienne",
                "Congruences",
                "Nombres premiers",
                "PGCD et théorème de Bézout",
            ]),
            ("Nombres complexes", [
                "Forme algébrique et représentation géométrique",
                "Forme trigonométrique et exponentielle",
                "Racines de l'unité et équations",
            ]),
            ("Algèbre linéaire", [
                "Espaces vectoriels",
                "Applications linéaires",
                "Matrices",
                "Déterminants",
                "Réduction des endomorphismes",
                "Espaces euclidiens et préhilbertiens",
            ]),
            ("Polynômes et fractions rationnelles", [
                "Arithmétique des polynômes",
                "Racines et factorisation",
            ]),
            ("Structures algébriques", [
                "Groupes",
                "Anneaux et corps",
                "Théorie de Galois",
            ]),
        ]),
        ("Géométrie", [
            ("Géométrie plane", [
                "Vecteurs et repérage",
                "Produit scalaire dans le plan",
                "Droites et cercles",
                "Transformations du plan",
            ]),
            ("Géométrie dans l'espace", [
                "Positions relatives de droites et de plans",
                "Produit scalaire dans l'espace",
                "Représentations paramétriques et équations de plans",
            ]),
            ("Géométrie différentielle", [
                "Courbes paramétrées",
                "Surfaces",
            ]),
            "Géométrie affine et projective",
        ]),
        ("Probabilités et statistiques", [
            ("Probabilités", [
                "Probabilités conditionnelles et indépendance",
                "Variables aléatoires discrètes",
                "Loi binomiale",
                "Lois à densité",
                "Loi normale",
                "Théorèmes limites",
                "Chaînes de Markov",
            ]),
            ("Statistiques", [
                "Statistique descriptive",
                "Échantillonnage et estimation",
                "Tests d'hypothèses",
                "Régression linéaire",
            ]),
        ]),
        ("Logique et raisonnement", [
            "Logique propositionnelle",
            "Ensembles et applications",
            "Dénombrement",
        ]),
    ]),
    ("Physique", [
        ("Mécanique", [
            ("Cinématique", [
                "Description du mouvement",
                "Mouvements rectilignes et circulaires",
            ]),
            ("Dynamique newtonienne", [
                "Lois de Newton",
                "Forces usuelles",
                "Mouvement dans un champ uniforme",
                "Gravitation et mouvement des satellites",
            ]),
            ("Énergie mécanique", [
                "Travail d'une force",
                "Énergie cinétique et potentielle",
                "Conservation de l'énergie",
            ]),
            ("Mécanique du solide", [
                "Cinétique du solide",
                "Théorèmes généraux de la dynamique",
                "Rotation autour d'un axe fixe",
            ]),
            ("Mécanique des fluides", [
                "Statique des fluides",
                "Dynamique des fluides parfaits",
                "Fluides visqueux",
            ]),
            ("Mécanique analytique", [
                "Formalisme lagrangien",
                "Formalisme hamiltonien",
            ]),
        ]),
        ("Ondes et signaux", [
            ("Ondes mécaniques", [
                "Ondes progressives",
                "Ondes périodiques et stationnaires",
                "Interférences et diffraction",
            ]),
            ("Optique géométrique", [
                "Réflexion et réfraction",
                "Lentilles minces",
                "Instruments d'optique",
            ]),
            ("Optique ondulatoire", [
                "Interférences lumineuses",
                "Diffraction",
                "Polarisation",
            ]),
            ("Acoustique", [
                "Intensité et niveau sonore",
                "Effet Doppler",
            ]),
            ("Traitement du signal", [
                "Analyse spectrale",
                "Filtrage",
            ]),
        ]),
        ("Électricité et électromagnétisme", [
            ("Circuits électriques", [
                "Lois de l'électrocinétique",
                "Dipôles et régimes transitoires",
                "Régime sinusoïdal forcé",
            ]),
            ("Électrostatique", [
                "Champ et potentiel électrostatiques",
                "Théorème de Gauss",
                "Condensateurs",
            ]),
            ("Magnétostatique", [
                "Champ magnétique",
                "Théorème d'Ampère",
                "Forces de Laplace et de Lorentz",
            ]),
            ("Induction électromagnétique", [
                "Lois de l'induction",
                "Auto-induction et circuits couplés",
            ]),
            ("Équations de Maxwell", [
                "Équations de Maxwell dans le vide",
                "Ondes électromagnétiques",
                "Propagation dans les milieux",
            ]),
        ]),
        ("Thermodynamique", [
            ("États de la matière", [
                "Gaz parfait",
                "Changements d'état",
            ]),
            ("Principes de la thermodynamique", [
                "Premier principe",
                "Second principe et entropie",
                "Machines thermiques",
            ]),
            ("Transferts thermiques", [
                "Conduction",
                "Convection et rayonnement",
            ]),
            ("Physique statistique", [
                "Facteur de Boltzmann",
                "Ensembles statistiques",
            ]),
        ]),
        ("Physique moderne", [
            ("Physique quantique", [
                "Dualité onde-corpuscule",
                "Équation de Schrödinger",
                "Spin et moment cinétique",
                "Atome d'hydrogène",
            ]),
            ("Relativité", [
                "Relativité restreinte",
                "Relativité générale",
            ]),
            ("Physique nucléaire", [
                "Radioactivité",
                "Réactions nucléaires",
                "Fission et fusion",
            ]),
            ("Physique des particules", [
                "Modèle standard",
                "Interactions fondamentales",
            ]),
            ("Physique de la matière condensée", [
                "Structure cristalline",
                "Semi-conducteurs",
            ]),
        ]),
    ]),
    ("Chimie", [
        ("Chimie générale", [
            ("Structure de la matière", [
                "Modèle de l'atome",
                "Classification périodique",
                "Liaisons chimiques",
                "Géométrie des molécules",
            ]),
            ("Transformations chimiques", [
                "Avancement et bilan de matière",
                "Cinétique chimique",
                "Équilibres chimiques",
            ]),
            ("Réactions acide-base", [
                "pH et acidité",
                "Titrages",
                "Solutions tampons",
            ]),
            ("Oxydoréduction", [
                "Piles et électrolyse",
                "Potentiels d'électrode",
            ]),
            ("Solutions aqueuses", [
                "Concentration et dilution",
                "Solubilité et précipitation",
            ]),
        ]),
        ("Chimie organique", [
            ("Nomenclature et représentation", [
                "Familles fonctionnelles",
                "Stéréochimie",
            ]),
            ("Mécanismes réactionnels", [
                "Substitutions nucléophiles",
                "Additions et éliminations",
            ]),
            ("Synthèse organique", [
                "Stratégie de synthèse",
                "Protection de fonctions",
            ]),
            ("Analyse et spectroscopie", [
                "Spectroscopie infrarouge",
                "Résonance magnétique nucléaire",
                "Spectrométrie de masse",
            ]),
        ]),
        ("Chimie physique", [
            ("Thermochimie", [
                "Enthalpie de réaction",
                "Enthalpie libre et équilibres",
            ]),
            ("Électrochimie", [
                "Courbes intensité-potentiel",
                "Corrosion",
            ]),
            "Chimie quantique",
        ]),
        ("Chimie inorganique", [
            "Chimie de coordination",
            ("Cristallochimie", [
                "Structures cristallines",
                "Défauts cristallins",
            ]),
        ]),
    ]),
    ("Sciences de la vie et de la Terre", [
        ("Biologie cellulaire et moléculaire", [
            ("Biologie cellulaire", [
                "Structure de la cellule",
                "Membranes et échanges",
                "Division cellulaire",
            ]),
            ("Génétique moléculaire", [
                "Réplication de l'ADN",
                "Expression génétique",
                "Mutations",
            ]),
            ("Biochimie", [
                "Protéines et enzymes",
                "Métabolisme énergétique",
            ]),
        ]),
        ("Biologie des organismes", [
            ("Physiologie animale", [
                "Système nerveux",
                "Système immunitaire",
                "Hormones et régulation",
            ]),
            ("Physiologie végétale", [
                "Photosynthèse",
                "Développement des plantes",
            ]),
            ("Reproduction", [
                "Reproduction sexuée",
                "Méiose et brassage génétique",
            ]),
        ]),
        ("Écologie et évolution", [
            ("Écologie", [
                "Écosystèmes",
                "Cycles biogéochimiques",
                "Biodiversité",
            ]),
            ("Évolution", [
                "Sélection naturelle",
                "Spéciation",
                "Phylogénie",
            ]),
        ]),
        ("Sciences de la Terre", [
            ("Géologie interne", [
                "Structure du globe",
                "Tectonique des plaques",
                "Volcanisme et séismes",
            ]),
            ("Géologie externe", [
                "Érosion et sédimentation",
                "Cycle de l'eau",
            ]),
            ("Climatologie", [
                "Climats passés",
                "Changement climatique",
            ]),
        ]),
    ]),
    ("Informatique", [
        ("Algorithmique et programmation", [
            ("Bases de la programmation", [
                "Variables et types",
                "Structures de contrôle",
                "Fonctions",
            ]),
            ("Structures de données", [
                "Listes, tableaux et dictionnaires",
                "Piles et files",
                "Arbres",
                "Graphes",
            ]),
            ("Algorithmes", [
                "Tri et recherche",
                "Récursivité",
                "Programmation dynamique",
                "Algorithmes gloutons",
            ]),
            ("Complexité et calculabilité", [
                "Complexité algorithmique",
                "Décidabilité",
                "Classes P et NP",
            ]),
        ]),
        ("Systèmes et réseaux", [
            ("Architecture des ordinateurs", [
                "Codage de l'information",
                "Portes logiques et circuits",
                "Processeur et mémoire",
            ]),
            ("Systèmes d'exploitation", [
                "Processus et ordonnancement",
                "Gestion de la mémoire",
                "Systèmes de fichiers",
            ]),
            ("Réseaux", [
                "Protocoles et modèle en couches",
                "Adressage IP et routage",
                "Sécurité des communications",
            ]),
        ]),
        ("Bases de données", [
            "Modèle relationnel",
            "Langage SQL",
            "Conception et normalisation",
        ]),
        ("Génie logiciel", [
            "Gestion de versions",
            "Tests et qualité",
            "Méthodes de développement",
        ]),
        ("Intelligence artificielle", [
            ("Apprentissage automatique", [
                "Apprentissage supervisé",
                "Apprentissage non supervisé",
                "Réseaux de neurones et apprentissage profond",
            ]),
            ("Intelligence artificielle symbolique", [
                "Recherche et heuristiques",
                "Représentation des connaissances",
            ]),
        ]),
        ("Théorie des langages", [
            "Langages formels et automates",
            "Compilation",
            "Logique et démonstration",
        ]),
    ]),
    ("Histoire", [
        ("Histoire ancienne", [
            "Mésopotamie et Égypte",
            "Grèce antique",
            "Rome antique",
        ]),
        ("Histoire médiévale", [
            "Moyen Âge occidental",
            "Monde byzantin et islam médiéval",
        ]),
        ("Histoire moderne", [
            "Renaissance et humanisme",
            "Grandes découvertes",
            "Ancien Régime",
            "Révolution française",
        ]),
        ("Histoire contemporaine", [
            "XIXe siècle",
            "Première Guerre mondiale",
            "Seconde Guerre mondiale",
            "Guerre froide",
            "Décolonisation",
            "Monde depuis 1991",
        ]),
        "Historiographie et méthodes",
    ]),
    ("Géographie", [
        ("Géographie humaine", [
            "Population et peuplement",
            "Urbanisation",
            "Migrations",
        ]),
        ("Géographie économique", [
            "Mondialisation",
            "Développement et inégalités",
            "Agriculture et alimentation",
        ]),
        ("Géographie physique", [
            "Climats et milieux",
            "Reliefs et hydrographie",
            "Risques naturels",
        ]),
        ("Géopolitique", [
            "Frontières et conflits",
            "Puissances et rivalités",
            "Mers et océans",
        ]),
        ("Aménagement des territoires", [
            "Territoires en France",
            "Union européenne",
        ]),
        "Cartographie et outils",
    ]),
    ("Français et littérature", [
        ("Étude de la langue", [
            "Grammaire",
            "Orthographe",
            "Vocabulaire et lexique",
        ]),
        ("Littérature", [
            "Poésie",
            "Roman et récit",
            "Théâtre",
            "Littérature d'idées",
        ]),
        ("Expression écrite et orale", [
            "Commentaire de texte",
            "Dissertation",
            "Éloquence et oral",
        ]),
        ("Histoire littéraire", [
            "Du Moyen Âge au XVIe siècle",
            "Classicisme et Lumières",
            "XIXe siècle",
            "XXe et XXIe siècles",
        ]),
    ]),
    ("Philosophie", [
        ("La connaissance", [
            "La raison",
            "La science",
            "La vérité",
        ]),
        ("L'existence humaine", [
            "La conscience",
            "L'inconscient",
            "Le temps",
            "La liberté",
        ]),
        ("La morale et la politique", [
            "Le devoir",
            "La justice",
            "L'État",
            "Le bonheur",
        ]),
        ("La culture", [
            "L'art",
            "Le travail et la technique",
            "Le langage",
            "La religion",
        ]),
        ("Histoire de la philosophie", [
            "Philosophie antique",
            "Philosophie moderne",
            "Philosophie contemporaine",
        ]),
    ]),
    ("Langues vivantes", [
        ("Anglais", [
            "Grammaire anglaise",
            "Compréhension et expression",
            "Civilisation anglophone",
        ]),
        ("Espagnol", [
            "Grammaire espagnole",
            "Compréhension et expression",
            "Civilisation hispanophone",
        ]),
        ("Allemand", [
            "Grammaire allemande",
            "Compréhension et expression",
            "Civilisation germanophone",
        ]),
        ("Italien", [
            "Grammaire italienne",
            "Compréhension et expression",
            "Civilisation italophone",
        ]),
        ("Langues anciennes", [
            "Latin",
            "Grec ancien",
        ]),
    ]),
    ("Sciences économiques et sociales", [
        ("Science économique", [
            "Marchés et prix",
            "Monnaie et financement",
            "Croissance économique",
            "Chômage et emploi",
            "Commerce international",
        ]),
        ("Sociologie", [
            "Socialisation",
            "Groupes et réseaux sociaux",
            "Stratification et mobilité sociale",
            "Déviance et contrôle social",
        ]),
        ("Science politique", [
            "Pouvoir et démocratie",
            "Engagement politique",
            "Opinion publique",
        ]),
        ("Regards croisés", [
            "Protection sociale",
            "Environnement et économie",
        ]),
    ]),
    ("Arts", [
        ("Arts plastiques", [
            "Dessin et peinture",
            "Sculpture",
            "Photographie",
        ]),
        ("Musique", [
            "Théorie musicale",
            "Histoire de la musique",
            "Pratique instrumentale",
        ]),
        ("Histoire de l'art", [
            "Art antique et médiéval",
            "Art moderne",
            "Art contemporain",
        ]),
        ("Arts du spectacle", [
            "Théâtre et mise en scène",
            "Cinéma et audiovisuel",
            "Danse",
        ]),
    ]),
]


def iter_rows() -> Iterator[dict]:
    """Aplatit ``TAXONOMIE`` en lignes prêtes pour la table ``subjects``.

    Yield des dicts ``{id, parent_id, nom, code, profondeur, position}``,
    parents toujours AVANT leurs enfants (ordre FK garanti pour l'insert).
    C'est la seule API consommée par la migration de seed et les tests.
    """

    def walk(
        node: Node,
        parent_code: str | None,
        parent_id: uuid.UUID | None,
        profondeur: int,
        position: int,
    ) -> Iterator[dict]:
        nom, enfants = node if isinstance(node, tuple) else (node, [])
        code = f"{parent_code}.{_slugify(nom)}" if parent_code else _slugify(nom)
        sid = subject_id(code)
        yield {
            "id": sid,
            "parent_id": parent_id,
            "nom": nom,
            "code": code,
            "profondeur": profondeur,
            "position": position,
        }
        for i, enfant in enumerate(enfants):
            yield from walk(enfant, code, sid, profondeur + 1, i)

    for i, discipline in enumerate(TAXONOMIE):
        yield from walk(discipline, None, None, 0, i)
