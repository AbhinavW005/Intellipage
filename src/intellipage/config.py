from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience dependency
    load_dotenv = None

from .models import SafetyMode


@dataclass(frozen=True, slots=True)
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    database: str = "intellipage"
    user: str = "intellipage_app"
    password: str = ""
    connect_timeout: int = 5

    @property
    def configured(self) -> bool:
        return bool(self.password)


@dataclass(frozen=True, slots=True)
class OracleConfig:
    dsn: str = "127.0.0.1:1521/XEPDB1"
    user: str = "intellipage_app"
    password: str = ""
    connect_timeout: int = 5
    oracle_home: Path | None = None
    thick_mode: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.password)


@dataclass(frozen=True, slots=True)
class AppConfig:
    mysql: MySQLConfig
    oracle: OracleConfig
    database_backend: str = "oracle"
    safety_mode: SafetyMode = SafetyMode.CONFIRM
    memory_budget_mb: int = 256
    sample_interval_seconds: float = 1.0
    process_allowlist: tuple[str, ...] = ()
    model_path: Path = Path("models/intellipage_dqn.pt")

    @classmethod
    def load(cls, env_path: str | Path = ".env") -> "AppConfig":
        path = Path(env_path)
        if load_dotenv is not None and path.exists():
            load_dotenv(path, override=False)
        elif path.exists():
            _load_simple_env(path)

        mode_raw = os.getenv("INTELLIPAGE_SAFETY_MODE", "confirm").strip().lower()
        try:
            mode = SafetyMode(mode_raw)
        except ValueError as exc:
            raise ValueError(
                "INTELLIPAGE_SAFETY_MODE must be monitor, confirm, or auto"
            ) from exc

        allowlist = tuple(
            item.strip().lower()
            for item in os.getenv("INTELLIPAGE_ALLOWLIST", "").split(",")
            if item.strip()
        )
        backend_default = "oracle" if shutil.which("sqlplus") else "mysql"
        backend = os.getenv("DATABASE_BACKEND", backend_default).strip().lower()
        if backend not in {"oracle", "mysql"}:
            raise ValueError("DATABASE_BACKEND must be oracle or mysql")
        oracle_home_raw = os.getenv("ORACLE_HOME", "").strip()
        oracle_home = Path(oracle_home_raw) if oracle_home_raw else _detect_oracle_home()
        return cls(
            mysql=MySQLConfig(
                host=os.getenv("MYSQL_HOST", "127.0.0.1"),
                port=int(os.getenv("MYSQL_PORT", "3306")),
                database=os.getenv("MYSQL_DATABASE", "intellipage"),
                user=os.getenv("MYSQL_USER", "intellipage_app"),
                password=os.getenv("MYSQL_PASSWORD", ""),
                connect_timeout=int(os.getenv("MYSQL_CONNECT_TIMEOUT", "5")),
            ),
            oracle=OracleConfig(
                dsn=os.getenv("ORACLE_DSN", "127.0.0.1:1521/XEPDB1"),
                user=os.getenv("ORACLE_USER", "intellipage_app"),
                password=os.getenv("ORACLE_PASSWORD", ""),
                connect_timeout=int(os.getenv("ORACLE_CONNECT_TIMEOUT", "5")),
                oracle_home=oracle_home,
                thick_mode=_env_bool("ORACLE_THICK_MODE", False),
            ),
            database_backend=backend,
            safety_mode=mode,
            memory_budget_mb=max(16, int(os.getenv("INTELLIPAGE_MEMORY_BUDGET_MB", "256"))),
            sample_interval_seconds=max(
                0.1, float(os.getenv("INTELLIPAGE_SAMPLE_INTERVAL_SECONDS", "1.0"))
            ),
            process_allowlist=allowlist,
            model_path=Path(
                os.getenv("INTELLIPAGE_MODEL_PATH", "models/intellipage_dqn.pt")
            ),
        )


def _load_simple_env(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _detect_oracle_home() -> Path | None:
    executable = shutil.which("sqlplus")
    if not executable:
        return None
    path = Path(executable).resolve()
    return path.parent.parent if path.parent.name.lower() == "bin" else None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")

