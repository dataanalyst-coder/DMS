"""Create the first administrator (there is no default account).

    python -m app.create_admin
"""
import getpass
import sys

from .database import SessionLocal, engine, init_db
from .services import auth
from .services.errors import LedgerError


def main() -> int:
    init_db(engine)
    username = input("Administrator username: ").strip()
    full_name = input("Full name: ").strip()
    password = getpass.getpass(f"Password (min {auth.MIN_PASSWORD_LENGTH} characters): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.")
        return 1
    with SessionLocal() as db:
        try:
            auth.bootstrap_admin(db, username, full_name, password)
        except LedgerError as e:
            print(e.message)
            return 1
    print(f"Administrator {username.lower()} created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
