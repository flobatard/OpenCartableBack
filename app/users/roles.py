"""Rôle de plateforme d'un compte, à la main : ``python -m app.users.roles``.

Aucune route n'écrit ``users.platform_role`` — un utilisateur pourrait s'y
promouvoir. C'est l'opérateur qui l'attribue, depuis le conteneur api :

    docker compose exec api python -m app.users.roles grant prof@example.org
    docker compose exec api python -m app.users.roles revoke <sub>
    docker compose exec api python -m app.users.roles list

Le compte doit exister : il naît au premier ``GET /users/me``, donc à la
première connexion à l'application. On le désigne par son ``sub`` OIDC ou par
son email (insensible à la casse) ; un email porté par plusieurs comptes est
refusé — l'email n'est qu'un instantané du claim, l'identité est le ``sub``.

Le front ne relit le profil qu'au chargement de l'application : une promotion
s'y voit après un rechargement de la page.

Codes de sortie : **0** succès (y compris « déjà en place »), **1** compte
introuvable ou email ambigu, **2** usage.
"""

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, engine
from app.models.user import PLATFORM_ROLE_PUBLIC, PLATFORM_ROLE_SUPER_ADMIN, User

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2

# Sous-commande → rôle posé.
ROLE_BY_COMMAND = {"grant": PLATFORM_ROLE_SUPER_ADMIN, "revoke": PLATFORM_ROLE_PUBLIC}


class AccountLookupError(Exception):
    """Compte introuvable ou email ambigu ; le message s'adresse à l'opérateur."""


async def find_account(db: AsyncSession, identifier: str) -> User:
    """Le compte de ``sub`` exact, sinon celui de l'email (insensible à la casse).

    Ordre des execute : 1) par ``sub`` ; 2) par email, seulement si 1) n'a rien
    trouvé (deux lignes au plus : assez pour détecter l'ambiguïté).
    """
    user = (await db.execute(select(User).where(User.sub == identifier))).scalars().first()
    if user is not None:
        return user
    matches = (
        (
            await db.execute(
                select(User).where(func.lower(User.email) == identifier.lower()).limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not matches:
        raise AccountLookupError(
            f"aucun compte pour « {identifier} » : la personne doit s'être connectée "
            "une première fois à l'application"
        )
    if len(matches) > 1:
        raise AccountLookupError(
            f"plusieurs comptes portent l'email « {identifier} » : désignez-le par son sub"
        )
    return matches[0]


async def set_platform_role(db: AsyncSession, identifier: str, role: str) -> tuple[User, bool]:
    """Pose le rôle ; renvoie le compte et ``True`` s'il a changé.

    Idempotent : un rôle déjà en place ne commite rien. Ordre des execute :
    ceux de :func:`find_account`, puis le flush de l'UPDATE au commit.
    """
    user = await find_account(db, identifier)
    if user.platform_role == role:
        return user, False
    user.platform_role = role
    await db.commit()
    return user, True


async def list_super_admins(db: AsyncSession) -> list[User]:
    """Les super admins, du plus ancien compte au plus récent (un execute)."""
    return list(
        (
            await db.execute(
                select(User)
                .where(User.platform_role == PLATFORM_ROLE_SUPER_ADMIN)
                .order_by(User.created_at)
            )
        )
        .scalars()
        .all()
    )


def describe(user: User) -> str:
    return f"{user.email or '(sans email)'} — sub {user.sub} — id {user.id}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.users.roles",
        description="Rôle de plateforme d'un compte (public ou super_admin).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("grant", "promouvoir le compte super admin"),
        ("revoke", "ramener le compte au rôle public"),
    ):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("identifier", help="sub OIDC ou email du compte")
    commands.add_parser("list", help="lister les super admins")
    return parser


async def run(db: AsyncSession, args: argparse.Namespace) -> int:
    """Exécute la sous-commande sur une session ouverte ; rend le code de sortie."""
    if args.command == "list":
        admins = await list_super_admins(db)
        for user in admins:
            print(describe(user))
        if not admins:
            print("aucun super admin")
        return EXIT_OK
    role = ROLE_BY_COMMAND[args.command]
    try:
        user, changed = await set_platform_role(db, args.identifier, role)
    except AccountLookupError as exc:
        print(exc, file=sys.stderr)
        return EXIT_NOT_FOUND
    prefix = "rôle posé" if changed else "déjà en place"
    print(f"{prefix} : {role} — {describe(user)}")
    return EXIT_OK


async def main(argv: list[str]) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # Usage invalide (argparse a déjà écrit l'aide sur stderr) ou --help.
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        async with AsyncSessionLocal() as db:
            return await run(db, args)
    finally:
        # Comme le one-shot de maintenance : ne rien retenir du pool.
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
