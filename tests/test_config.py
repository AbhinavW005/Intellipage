from pathlib import Path

from intellipage.config import AppConfig
from intellipage.models import SafetyMode


def test_config_defaults(monkeypatch, tmp_path: Path):
    for key in (
        "MYSQL_PASSWORD",
        "ORACLE_PASSWORD",
        "DATABASE_BACKEND",
        "INTELLIPAGE_SAFETY_MODE",
        "INTELLIPAGE_ALLOWLIST",
    ):
        monkeypatch.delenv(key, raising=False)
    config = AppConfig.load(tmp_path / "missing.env")
    assert config.safety_mode == SafetyMode.CONFIRM
    assert config.memory_budget_mb == 256
    assert not config.mysql.configured
    assert not config.oracle.configured


def test_oracle_configuration(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATABASE_BACKEND", "oracle")
    monkeypatch.setenv("ORACLE_DSN", "127.0.0.1:1521/XEPDB1")
    monkeypatch.setenv("ORACLE_USER", "intellipage_app")
    monkeypatch.setenv("ORACLE_PASSWORD", "test-secret")
    config = AppConfig.load(tmp_path / "missing.env")
    assert config.database_backend == "oracle"
    assert config.oracle.configured
    assert config.oracle.dsn.endswith("/XEPDB1")


def test_config_reads_plain_env_without_secret_logging(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "MYSQL_PASSWORD=secret\nINTELLIPAGE_SAFETY_MODE=monitor\n"
        "INTELLIPAGE_ALLOWLIST=notepad.exe,code.exe\n",
        encoding="utf-8",
    )
    config = AppConfig.load(env)
    assert config.mysql.password == "secret"
    assert config.safety_mode == SafetyMode.MONITOR
    assert config.process_allowlist == ("notepad.exe", "code.exe")

