"""Create a login or change its password: python -m app.passwd <user>

The password is typed at the prompt (never an argument, so it stays out of the
shell history). On the server:
    docker compose exec -u app app python -m app.passwd <user>
Other uses: --list, and <user> --remove.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.backend.auth import Accounts
from app.backend.settings import ConfigError, get_settings


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m app.passwd", description=__doc__.splitlines()[0])
    parser.add_argument("user", nargs="?")
    parser.add_argument("--remove", action="store_true", help="delete the login")
    parser.add_argument("--list", action="store_true", help="show the existing logins")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as e:
        print(f"Configuração inválida: {e}", file=sys.stderr)
        return 2
    accounts = Accounts(settings.data_dir / "accounts.json", settings.secret_key)

    if args.list:
        print("\n".join(accounts.users()) or "(nenhum usuário)")
        return 0
    if not args.user:
        parser.error("informe o usuário (ou --list)")
    user = args.user.strip().lower()
    if args.remove:
        found = accounts.remove(user)
        print(f"{user}: removido." if found else f"{user}: não existe.")
        return 0 if found else 1

    password = getpass.getpass(f"Senha de {user}: ")
    if password != getpass.getpass("Repita a senha: "):
        print("As senhas não conferem.", file=sys.stderr)
        return 1
    try:
        accounts.set_password(user, password)
    except ValueError as e:
        print(f"Recusado: {e}", file=sys.stderr)
        return 1
    print(f"{user}: senha salva. Sessões antigas desse usuário foram encerradas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
