from pathlib import Path

from intellipage import cli
from intellipage.config import AppConfig, MySQLConfig, OracleConfig


def test_oracle_setup_persists_client_mode_used_for_local_sysdba(
    monkeypatch, tmp_path: Path
) -> None:
    oracle_home = Path(r"C:\app\test\product\21c\dbhomeXE")
    config = AppConfig(
        mysql=MySQLConfig(),
        oracle=OracleConfig(oracle_home=oracle_home),
        database_backend="oracle",
    )
    (tmp_path / ".env.example").write_text(
        "DATABASE_BACKEND=oracle\n"
        "ORACLE_DSN=127.0.0.1:1521/XEPDB1\n"
        "ORACLE_USER=intellipage_app\n"
        "ORACLE_PASSWORD=change_me\n"
        "ORACLE_HOME=\n"
        "ORACLE_THICK_MODE=false\n",
        encoding="utf-8",
    )
    answers = iter(["", ""])
    passwords = iter(["admin-password", "runtime-password"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(passwords))
    monkeypatch.setattr(cli, "BUNDLE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "bootstrap_oracle_database", lambda **_kwargs: None)

    env_path = tmp_path / ".env"
    assert cli._setup_oracle_interactive(config, env_path) == 0

    saved = env_path.read_text(encoding="utf-8")
    assert f"ORACLE_HOME={oracle_home}" in saved
    assert "ORACLE_THICK_MODE=true" in saved

