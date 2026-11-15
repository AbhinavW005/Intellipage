from __future__ import annotations

import argparse
import getpass
import secrets
import shutil
import string
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from intellipage.database import bootstrap_database  # noqa: E402
from intellipage.admin import ensure_admin, is_admin, is_windows  # noqa: E402
from intellipage.oracle_database import bootstrap_oracle_database  # noqa: E402


def generated_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> int:
    parser = argparse.ArgumentParser(description="Secure IntelliPage database bootstrap")
    parser.add_argument("--backend", choices=("oracle", "mysql"), default="oracle")
    parser.add_argument("--dsn", default="127.0.0.1:1521/XEPDB1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--database", default="intellipage")
    parser.add_argument("--admin-user")
    parser.add_argument("--app-user", default="intellipage_app")
    parser.add_argument("--tablespace", default="USERS")
    parser.add_argument("--sysdba", action="store_true")
    parser.add_argument(
        "--local-sysdba",
        action="store_true",
        help="Use the local Oracle instance and switch into the DSN's PDB",
    )
    sqlplus = shutil.which("sqlplus")
    detected_home = str(Path(sqlplus).resolve().parent.parent) if sqlplus else None
    parser.add_argument("--oracle-home", default=detected_home)
    parser.add_argument("--oracle-sid", default="XE")
    args = parser.parse_args()

    if (
        args.backend == "oracle"
        and args.local_sysdba
        and is_windows()
        and not is_admin()
        and not ensure_admin(auto_elevate=True, argv=sys.argv[1:])
    ):
        return 0

    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        raise FileExistsError(
            f"{env_path} already exists; refusing to change the database or overwrite configuration."
        )
    admin_user = args.admin_user or ("SYSTEM" if args.backend == "oracle" else "root")
    admin_password = getpass.getpass(
        f"{args.backend.title()} administrator password (not stored): "
    )
    app_password = generated_password()
    if args.backend == "oracle":
        bootstrap_oracle_database(
            dsn=args.dsn,
            admin_user=admin_user,
            admin_password=admin_password,
            app_user=args.app_user,
            app_password=app_password,
            schema_path=PROJECT_ROOT / "database" / "oracle_schema.sql",
            tablespace=args.tablespace,
            sysdba=args.sysdba or admin_user.upper() == "SYS",
            oracle_home=args.oracle_home,
            local_sysdba=args.local_sysdba,
            local_sid=args.oracle_sid,
        )
    else:
        bootstrap_database(
            host=args.host,
            port=args.port,
            admin_user=admin_user,
            admin_password=admin_password,
            database=args.database,
            app_user=args.app_user,
            app_password=app_password,
            schema_path=PROJECT_ROOT / "database" / "schema.sql",
        )
    env_path.write_text(
        "\n".join(
            [
                f"DATABASE_BACKEND={args.backend}",
                f"ORACLE_DSN={args.dsn}",
                f"ORACLE_USER={args.app_user}",
                f"ORACLE_PASSWORD={app_password if args.backend == 'oracle' else ''}",
                "ORACLE_CONNECT_TIMEOUT=5",
                "ORACLE_HOME=",
                "ORACLE_THICK_MODE=false",
                "",
                f"MYSQL_HOST={args.host}",
                f"MYSQL_PORT={args.port}",
                f"MYSQL_DATABASE={args.database}",
                f"MYSQL_USER={args.app_user}",
                f"MYSQL_PASSWORD={app_password if args.backend == 'mysql' else ''}",
                "MYSQL_CONNECT_TIMEOUT=5",
                "",
                "INTELLIPAGE_SAFETY_MODE=confirm",
                "INTELLIPAGE_MEMORY_BUDGET_MB=256",
                "INTELLIPAGE_SAMPLE_INTERVAL_SECONDS=1.0",
                "INTELLIPAGE_ALLOWLIST=",
                "INTELLIPAGE_MODEL_PATH=models/intellipage_dqn.pt",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(
        f"{args.backend.title()} schema and restricted user created. "
        f"Runtime configuration written to {env_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

