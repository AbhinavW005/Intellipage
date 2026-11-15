from __future__ import annotations

import argparse
import json
import os
import sys
import time
from getpass import getpass
from pathlib import Path
from typing import Callable

from .actions import SafetyGuard, trim_working_set
from .admin import ensure_admin, is_admin, is_windows, try_enable_debug_privilege
from .config import AppConfig
from .database import (
    DatabaseRepository,
    DatabaseUnavailable,
    MySQLRepository,
    bootstrap_database,
)
from .dqn import ACTION_NAMES, DoubleDQNAgent
from .console_graphs import dqn_progress_graph, latency_graph, policy_graph, query_history_graph
from .console_ui import error, menu_header, pause, section, success, title, warning
from .experiments import ExperimentRunner, build_dqn_state, create_demo_file
from .live_training import LiveFileDQNTrainer, load_real_experiences
from .image_graphs import open_graph, save_dqn_graph, save_latency_graph, save_policy_graph, save_query_graph
from .managed_cache import create_policy_workload, run_real_policy_trace
from .models import ActionKind, SafetyMode
from .monitoring import WindowsMonitor
from .oracle_database import (
    OracleRepository,
    bootstrap_oracle_database,
    normalize_oracle_admin_user,
)
from .policies import POLICIES


SOURCE_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
RUNTIME_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SOURCE_ROOT
GRAPH_ROOT = RUNTIME_ROOT / "outputs" / "graphs"


def _mb(value: int) -> str:
    return f"{value / (1024 * 1024):,.1f} MB"


def _print_process(sample) -> None:
    print(
        f"{sample.pid:>7}  {sample.name[:28]:<28}  "
        f"WS {_mb(sample.working_set_bytes):>12}  "
        f"Faults {sample.page_faults:>10}  CPU {sample.cpu_percent:>6.1f}%"
    )


def _repository(config: AppConfig, *, required: bool = False) -> DatabaseRepository | None:
    database_config = config.oracle if config.database_backend == "oracle" else config.mysql
    if not database_config.configured:
        if required:
            raise DatabaseUnavailable(
                f"{config.database_backend.title()} is not configured. "
                "Copy .env.example to .env or run setup-db."
            )
        return None
    repository: DatabaseRepository
    if config.database_backend == "oracle":
        repository = OracleRepository(config.oracle)
    else:
        repository = MySQLRepository(config.mysql)
    try:
        repository.health_check()
    except DatabaseUnavailable:
        if required:
            raise
        return None
    return repository


def show_snapshot(config: AppConfig) -> int:
    monitor = WindowsMonitor()
    system = monitor.system_summary()
    print("\nINTELLIPAGE LIVE WINDOWS SNAPSHOT")
    print(f"Administrator: {'yes' if is_admin() else 'no'}")
    print(f"Memory used:   {system['memory_percent']:.1f}%")
    print(f"Available:     {_mb(int(system['memory_available_bytes']))}")
    print(f"Swap used:     {_mb(int(system['swap_used_bytes']))}")
    print(f"Processes:     {system['process_count']}")
    print("\nTop processes by working set")
    for sample in monitor.list_processes(12):
        _print_process(sample)
    try:
        repository = _repository(config, required=True)
        health = repository.health_check()
        repository.store_system_sample(system)
        container = health.get("container_name")
        suffix = f" / container {container}" if container else ""
        print(
            f"\n{repository.provider_name.title()}: {health['version']} / "
            f"database {health['database_name']}{suffix}"
        )
    except DatabaseUnavailable as exc:
        print(f"\n{config.database_backend.title()} connection failed: {exc}")
    return 0


def monitor_process(config: AppConfig, pid: int, samples: int = 10) -> int:
    debug_enabled = try_enable_debug_privilege()
    monitor = WindowsMonitor()
    repository = _repository(config)
    previous = None
    print(f"Monitoring PID {pid}; every value comes from the running process.")
    print(f"SeDebugPrivilege: {'enabled' if debug_enabled else 'not enabled/needed'}")
    for index in range(max(1, samples)):
        current = monitor.sample_process(pid)
        if not current.accessible:
            if current.error == "process_ended" and previous is not None:
                print(f"PID {pid} completed after {index} collected samples.")
                return 0
            reason = "access was denied" if current.error == "access_denied" else "it is unavailable"
            print(f"PID {pid} could not be monitored because {reason}.")
            return 2
        fault_delta = 0 if previous is None else current.page_faults - previous.page_faults
        io_delta = 0 if previous is None else current.read_bytes - previous.read_bytes
        print(
            f"{index + 1:>3}: faults={current.page_faults} "
            f"delta={fault_delta:+} working_set={_mb(current.working_set_bytes)} "
            f"read_delta={_mb(max(0, io_delta))} cpu={current.cpu_percent:.1f}%"
        )
        previous = current
        if repository is not None:
            repository.store_process_sample(current)
            repository.store_system_sample(
                monitor.system_summary(), sampled_at=current.timestamp
            )
        if index < samples - 1:
            time.sleep(config.sample_interval_seconds)
    return 0


def _prompt_trials() -> int:
    raw = input("Trials [3]: ").strip()
    return _positive_int(raw or "3")


def run_file_action(
    config: AppConfig,
    path: str | Path,
    action: ActionKind,
    *,
    policy: str = "manual",
    trials: int = 3,
) -> int:
    guard = SafetyGuard(config.safety_mode)
    if not guard.authorize(f"Run {action.value} on {Path(path).resolve()} for {trials} trial(s)?"):
        print(warning("Action was not authorized."))
        return 3
    repository = _repository(config)
    runner = ExperimentRunner(
        WindowsMonitor(), repository, audit_path=RUNTIME_ROOT / "data" / "action_audit.jsonl"
    )
    results = []
    print(section(f"LIVE {action.value.upper()} EXPERIMENT"))
    for index in range(trials):
        result = runner.run_file_experiment(
            path,
            policy=policy,
            action_kind=action,
            max_bytes=config.memory_budget_mb * 1024 * 1024,
        )
        results.append(result)
        print(f"\nTRIAL {index + 1}/{trials}")
        print_experiment(result)
        print(latency_graph(
            [(item.baseline_latency_ms, item.post_action_latency_ms) for item in results],
            title=f"LIVE {action.value.upper()} LATENCY",
        ), flush=True)
        graph_path = save_latency_graph(
            [(item.baseline_latency_ms, item.post_action_latency_ms) for item in results],
            GRAPH_ROOT / f"{action.value}_latency.png",
            title=f"Live {action.value} latency",
        )
        print(f"Image graph updated: {graph_path}", flush=True)
    open_graph(GRAPH_ROOT / f"{action.value}_latency.png")
    average_baseline = sum(item.baseline_latency_ms for item in results) / len(results)
    average_post = sum(item.post_action_latency_ms for item in results) / len(results)
    average_reward = sum(item.reward for item in results) / len(results)
    print(
        f"\nAVERAGE ({len(results)} trials): baseline={average_baseline:.3f} ms "
        f"post={average_post:.3f} ms reward={average_reward:+.6f}"
    )
    return 0 if all(result.action.success for result in results) else 2
def print_experiment(result) -> None:
    print("\nREAL EXPERIMENT RESULT")
    print(f"Type:                 {result.experiment_type}")
    print(f"Policy:               {result.policy}")
    print(f"Action:               {result.action.kind.value}")
    print(f"Action succeeded:     {result.action.success}")
    print(f"Bytes touched:        {result.action.bytes_touched:,}")
    print(f"Baseline latency:     {result.baseline_latency_ms:.3f} ms")
    print(f"Post-action latency:  {result.post_action_latency_ms:.3f} ms")
    print(f"Observed fault delta: {result.page_faults_after - result.page_faults_before:+}")
    print(f"Working-set delta:    {_mb(result.working_set_after - result.working_set_before)}")
    print(f"Measured reward:      {result.reward:.6f}")
    print(f"Detail:               {result.action.message}")
    passes = result.action.metadata.get("query_passes")
    if passes:
        print("Query passes:")
        for item in passes:
            print(
                f"  {item['name']:<8} {float(item['duration_ms']):>9.3f} ms "
                f"rows={int(item['rows_returned'])}"
            )
        print(latency_graph(
            [
                (
                    float(passes[0]["duration_ms"]),
                    float(passes[-1]["duration_ms"]),
                )
            ],
            title="LIVE DATABASE WARMING",
        ))


def _dqn_recommendation(
    config: AppConfig,
    monitor: WindowsMonitor | None = None,
) -> dict[str, object] | None:
    model_path = RUNTIME_ROOT / config.model_path
    if not model_path.exists():
        return None
    agent = DoubleDQNAgent()
    agent.load(model_path, evaluation=True)
    active_monitor = monitor or WindowsMonitor()
    sample = active_monitor.sample_process(os.getpid())
    state = build_dqn_state(
        sample,
        active_monitor.system_summary(),
        resource_size_mb=float(config.memory_budget_mb),
        previous_reward=0.0,
    )
    q_values = agent.q_values(state)
    action_index = max(range(len(q_values)), key=q_values.__getitem__)
    return {
        "action_index": action_index,
        "action_name": ACTION_NAMES[action_index],
        "q_values": q_values,
        "state": state,
    }


def _format_training_loss(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.5f}"


def _print_dqn_recommendation(recommendation: dict[str, object]) -> None:
    print("\nDQN INFERENCE RECOMMENDATION")
    print(f"selected action: {recommendation['action_name']}")
    q_values = recommendation["q_values"]
    for name, value in zip(ACTION_NAMES, q_values):
        print(f"  {name:<16} Q={float(value):+.5f}")
    print("This recommendation is advisory; a real action still requires the normal safety guard.")


def train_dqn(config: AppConfig, steps: int = 5, database_query: str | None = None) -> int:
    guard = SafetyGuard(config.safety_mode)
    if not guard.authorize(
        f"Train the CPU DQN using {steps} real file-I/O experiment steps?"
    ):
        print(warning("Training was not authorized."))
        return 3
    monitor = WindowsMonitor()
    repository = _repository(config)
    agent = DoubleDQNAgent()
    if repository is not None:
        try:
            loaded = load_real_experiences(repository, agent)
            print(f"Loaded {loaded} stored DQN experiences.")
        except Exception as exc:
            print(warning(f"Oracle became unavailable during DQN setup: {exc}"))
            print(warning("Continuing DQN training with real file operations only."))
            repository = None
    trainer = LiveFileDQNTrainer(
        monitor,
        agent,
        repository,
        audit_path=RUNTIME_ROOT / "data" / "dqn_training.jsonl",
    )
    live_history: list[dict[str, object]] = []

    def show_progress(record: dict[str, object]) -> None:
        live_history.append(record)
        print(
            f"DQN step {record['step']:>3}/{steps}: action={record['action_name']:<16} "
            f"reward={float(record['reward']):+.4f} loss="
            f"{_format_training_loss(record['loss'])}"
        )
        print(dqn_progress_graph(live_history), flush=True)
        image_path = save_dqn_graph(live_history, GRAPH_ROOT / "dqn_training.png")
        print(f"Image graph updated: {image_path}", flush=True)

    summary: dict[str, object] | None = None
    for attempt in range(1, 3):
        try:
            summary = trainer.train(
                RUNTIME_ROOT / "data" / "dqn_real_workload.bin",
                steps=steps,
                max_bytes=min(config.memory_budget_mb, 32) * 1024 * 1024,
                database_query=database_query,
                progress_callback=show_progress,
            )
            break
        except Exception as exc:
            if attempt == 1:
                print(warning(f"DQN fault: {exc}. Retrying automatically once..."))
                continue
            try:
                agent.save(RUNTIME_ROOT / config.model_path)
            except Exception:
                pass
            raise
    if summary is None:
        raise RuntimeError("DQN training did not produce a result")
    history = summary.pop("history", live_history)
    agent.save(RUNTIME_ROOT / config.model_path)
    print(success("\nDQN TRAINED FROM REAL FILE OPERATIONS"))
    for key, value in summary.items():
        print(f"{key:>18}: {value}")
    print(f"{'model':>18}: {RUNTIME_ROOT / config.model_path}")
    print(dqn_progress_graph(history))
    open_graph(GRAPH_ROOT / "dqn_training.png")
    recommendation = _dqn_recommendation(config, monitor)
    if recommendation is not None:
        _print_dqn_recommendation(recommendation)
    return 0
def run_database_experiment(config: AppConfig, query: str) -> int:
    guard = SafetyGuard(config.safety_mode)
    if not guard.authorize(
        f"Run an explain plan plus three measured read-only {config.database_backend} query passes?"
    ):
        return 3
    try:
        repository = _repository(config, required=True)
    except DatabaseUnavailable as exc:
        print(f"\n{config.database_backend.title()} connection failed: {exc}")
        return 2
    runner = ExperimentRunner(
        WindowsMonitor(), repository, audit_path=RUNTIME_ROOT / "data" / "action_audit.jsonl"
    )
    live_passes: list[dict[str, object]] = []

    def show_database_progress(item: dict[str, object]) -> None:
        live_passes.append(item)
        print(
            f"DATABASE PASS {item['name']:<8} "
            f"{float(item['duration_ms']):.3f} ms rows={int(item['rows_returned'])}"
        )
        if len(live_passes) >= 2:
            print(latency_graph(
                [(float(live_passes[0]["duration_ms"]), float(item["duration_ms"]))],
                title="LIVE DATABASE QUERY LATENCY",
            ), flush=True)
            image_path = save_latency_graph(
                [(float(live_passes[0]["duration_ms"]), float(item["duration_ms"]))],
                GRAPH_ROOT / "database_latency.png",
                title="Live database query latency",
            )
            print(f"Image graph updated: {image_path}", flush=True)

    result = runner.run_database_warm_experiment(
        statement_name="console_query",
        query=query,
        policy="manual",
        progress_callback=show_database_progress,
    )
    open_graph(GRAPH_ROOT / "database_latency.png")
    print_experiment(result)
    try:
        print(query_history_graph(repository.fetch_query_metrics(query, limit=9)))
    except DatabaseUnavailable as exc:
        print(f"Query history unavailable: {exc}")
    return 0


def setup_database_interactive(config: AppConfig) -> int:
    env_path = RUNTIME_ROOT / ".env"
    if env_path.exists():
        print(f"Refusing to overwrite existing {env_path}")
        return 2
    if config.database_backend == "oracle":
        return _setup_oracle_interactive(config, env_path)
    return _setup_mysql_interactive(config, env_path)


def _setup_oracle_interactive(config: AppConfig, env_path: Path) -> int:
    dsn = input(f"Oracle DSN [{config.oracle.dsn}]: ").strip() or config.oracle.dsn
    admin_user_input = input("Oracle administrator user [SYS AS SYSDBA]: ").strip()
    admin_user, inline_sysdba = normalize_oracle_admin_user(
        admin_user_input or "SYS AS SYSDBA"
    )
    use_sysdba = inline_sysdba or admin_user.upper() == "SYS"
    admin_password = getpass("Oracle administrator password (not stored): ")
    app_password = getpass("New intellipage_app password: ")
    if len(app_password) < 12:
        print("The runtime password must contain at least 12 characters.")
        return 2
    bootstrap_oracle_database(
        dsn=dsn,
        admin_user=admin_user,
        admin_password=admin_password,
        app_user=config.oracle.user,
        app_password=app_password,
        schema_path=BUNDLE_ROOT / "database" / "oracle_schema.sql",
        sysdba=use_sysdba,
        oracle_home=config.oracle.oracle_home,
        thick_mode=config.oracle.thick_mode,
        local_sysdba=use_sysdba,
    )
    env_path.write_text(
        (BUNDLE_ROOT / ".env.example")
        .read_text(encoding="utf-8")
        .replace("DATABASE_BACKEND=oracle", "DATABASE_BACKEND=oracle")
        .replace("ORACLE_DSN=127.0.0.1:1521/XEPDB1", f"ORACLE_DSN={dsn}")
        .replace("ORACLE_USER=intellipage_app", f"ORACLE_USER={config.oracle.user}")
        .replace("ORACLE_PASSWORD=change_me", f"ORACLE_PASSWORD={json.dumps(app_password)}")
        .replace("ORACLE_HOME=", f"ORACLE_HOME={config.oracle.oracle_home or ''}")
        .replace(
            "ORACLE_THICK_MODE=false",
            f"ORACLE_THICK_MODE={'true' if use_sysdba else str(config.oracle.thick_mode).lower()}",
        ),
        encoding="utf-8",
    )
    print("Oracle schema, views, indexes, and restricted runtime account were created.")
    return 0


def _setup_mysql_interactive(config: AppConfig, env_path: Path) -> int:
    host = input(f"MySQL host [{config.mysql.host}]: ").strip() or config.mysql.host
    port_text = input(f"MySQL port [{config.mysql.port}]: ").strip()
    port = int(port_text or config.mysql.port)
    admin_user = input("MySQL administrator user [root]: ").strip() or "root"
    admin_password = getpass("MySQL administrator password (not stored): ")
    app_password = getpass("New intellipage_app password: ")
    if len(app_password) < 12:
        print("The runtime password must contain at least 12 characters.")
        return 2
    bootstrap_database(
        host=host,
        port=port,
        admin_user=admin_user,
        admin_password=admin_password,
        database=config.mysql.database,
        app_user=config.mysql.user,
        app_password=app_password,
        schema_path=BUNDLE_ROOT / "database" / "schema.sql",
    )
    env_path.write_text(
        (BUNDLE_ROOT / ".env.example")
        .read_text(encoding="utf-8")
        .replace("MYSQL_HOST=127.0.0.1", f"MYSQL_HOST={host}")
        .replace("MYSQL_PORT=3306", f"MYSQL_PORT={port}")
        .replace("MYSQL_DATABASE=intellipage", f"MYSQL_DATABASE={config.mysql.database}")
        .replace("MYSQL_USER=intellipage_app", f"MYSQL_USER={config.mysql.user}")
        .replace("DATABASE_BACKEND=oracle", "DATABASE_BACKEND=mysql")
        .replace("MYSQL_PASSWORD=", f"MYSQL_PASSWORD={json.dumps(app_password)}"),
        encoding="utf-8",
    )
    print("MySQL schema, views, indexes, and restricted runtime account were created.")
    return 0


def _run_dqn_demo_action(
    config: AppConfig,
    target: Path,
    repository: DatabaseRepository | None,
    runner: ExperimentRunner,
) -> None:
    recommendation = _dqn_recommendation(config, runner.monitor)
    if recommendation is None:
        print("\nDQN model not found; no DQN-selected action was executed.")
        print("Run Option 6 first to train and save a model.")
        return
    _print_dqn_recommendation(recommendation)
    action_index = int(recommendation["action_index"])
    if action_index == 0:
        print("DQN selected noop; no optimization action was executed.")
        return
    if action_index in (1, 2):
        action_kind = ActionKind.FILE_WARM if action_index == 1 else ActionKind.MMAP_PREFETCH
        result = runner.run_file_experiment(
            target,
            policy="DQN",
            action_kind=action_kind,
            max_bytes=min(config.memory_budget_mb, 32) * 1024 * 1024,
        )
        print_experiment(result)
        return
    if repository is None:
        print("DQN selected database_warm, but no database is configured; action skipped safely.")
        return
    query = "SELECT COUNT(*) AS total_experiments FROM experiments"
    result = runner.run_database_warm_experiment(
        statement_name="dqn_demo_query",
        query=query,
        policy="DQN",
    )
    print_experiment(result)


def automatic_demo(config: AppConfig) -> int:
    guard = SafetyGuard(config.safety_mode)
    if not guard.authorize(
        "Run the IntelliPage automatic demonstration using real Windows and file operations?"
    ):
        return 3
    print("\n=== INTELLIPAGE REAL BACKEND DEMONSTRATION ===")
    show_snapshot(config)
    target = create_demo_file(RUNTIME_ROOT / "data" / "console_demo.bin", size_mb=32)
    repository = _repository(config)
    runner = ExperimentRunner(
        WindowsMonitor(), repository, audit_path=RUNTIME_ROOT / "data" / "action_audit.jsonl"
    )
    file_result = runner.run_file_experiment(
        target,
        policy="LRU",
        action_kind=ActionKind.FILE_WARM,
        max_bytes=min(config.memory_budget_mb, 32) * 1024 * 1024,
    )
    print_experiment(file_result)
    _run_dqn_demo_action(config, target, repository, runner)
    _show_policy_comparison(config)
    if repository is None:
        print(
            f"\n{config.database_backend.title()} portion skipped: "
            "configure .env or run setup-db first."
        )
    else:
        print(
            f"\n{repository.provider_name.title()} connection verified; "
            "action and experiment records were stored."
        )
    print(f"\nLocal audit trail: {RUNTIME_ROOT / 'data' / 'action_audit.jsonl'}")
    return 0

def _read_json_lines(path: Path, limit: int = 40) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _show_policy_comparison(config: AppConfig) -> None:
    print(section("LIVE FIFO / LRU / LFU / DQN COMPARISON"))
    comparisons = []
    for key in ("fifo", "lru", "lfu"):
        policy = POLICIES[key]
        trace = create_policy_workload(
            RUNTIME_ROOT / "data" / "policy_workloads", policy.name
        )
        comparison = run_real_policy_trace(
            policy,
            trace,
            capacity=3,
            max_bytes=4 * 1024 * 1024,
        )
        comparisons.append(comparison)
        print(
            f"{comparison.policy:<5} hits={comparison.hits} misses={comparison.misses} "
            f"hit_rate={comparison.hit_rate:.1%} storage={comparison.storage_bytes_read:,} "
            f"cache={comparison.cache_bytes_served:,} I/O={comparison.io_duration_ms:.3f} ms"
        )
    dqn_agent = DoubleDQNAgent()
    model_path = RUNTIME_ROOT / config.model_path
    if model_path.exists():
        dqn_agent.load(model_path, evaluation=False)
    dqn_trainer = LiveFileDQNTrainer(
        WindowsMonitor(), dqn_agent, None,
        audit_path=RUNTIME_ROOT / "data" / "dqn_training.jsonl",
    )
    dqn_summary = dqn_trainer.train(
        RUNTIME_ROOT / "data" / "dqn_real_workload.bin",
        steps=5,
        max_bytes=min(config.memory_budget_mb, 32) * 1024 * 1024,
    )
    history = dqn_summary.get("history", [])
    dqn_success = sum(1 for row in history if row.get("action_success")) / max(1, len(history))
    dqn_io = sum(float(row.get("action_ms", 0.0)) for row in history) / max(1, len(history))
    print(f"DQN   successful_actions={dqn_success:.1%} mean_action={dqn_io:.3f} ms")
    rows = [(item.policy, item.hit_rate, item.io_duration_ms) for item in comparisons]
    rows.append(("DQN", dqn_success, dqn_io))
    print(policy_graph(rows).replace("LIVE POLICY COMPARISON", "LIVE POLICY / CONTROLLER COMPARISON"))
    image_path = save_policy_graph(rows, GRAPH_ROOT / "policy_comparison.png", title="FIFO / LRU / LFU / DQN comparison")
    print(f"Image graph saved: {image_path}")
    open_graph(image_path)
    print("DQN uses successful real actions as its comparable rate; cache policies use hit rate.")


def _show_graph_menu(config: AppConfig) -> int:
    print(title("LIVE GRAPH VIEWER"))
    print("  1. File-action latency")
    print("  2. FIFO/LRU/LFU/DQN comparison")
    print("  3. DQN reward and loss progress")
    print("  4. Oracle query history")
    print("  5. Display all available graphs")
    choice = input("Graph type: ").strip()
    if choice in {"1", "5"}:
        rows = _read_json_lines(RUNTIME_ROOT / "data" / "action_audit.jsonl")
        pairs = [
            (float(row["baseline_latency_ms"]), float(row["post_action_latency_ms"]))
            for row in rows
            if row.get("experiment_type") == "real_file_io"
        ]
        if pairs:
            print(latency_graph(pairs[-12:], title="LIVE FILE-ACTION LATENCY"))
            image_path = save_latency_graph(pairs[-12:], GRAPH_ROOT / "file_action_latency.png", title="File-action latency")
            print(f"Image graph saved: {image_path}")
            open_graph(image_path)
        else:
            print(warning("No file-action measurements are stored yet."))
    if choice in {"2", "5"}:
        _show_policy_comparison(config)
    if choice in {"3", "5"}:
        rows = _read_json_lines(RUNTIME_ROOT / "data" / "dqn_training.jsonl")
        if rows:
            print(dqn_progress_graph(rows[-40:]))
            image_path = save_dqn_graph(rows[-40:], GRAPH_ROOT / "dqn_training.png")
            print(f"Image graph saved: {image_path}")
            open_graph(image_path)
        else:
            print(warning("No DQN training records are stored yet."))
    if choice in {"4", "5"}:
        query = input("Read-only SELECT query for history: ").strip()
        try:
            repository = _repository(config, required=True)
            query_rows = repository.fetch_query_metrics(query, limit=12)
            print(query_history_graph(query_rows))
            image_path = save_query_graph(query_rows, GRAPH_ROOT / "oracle_query_history.png")
            print(f"Image graph saved: {image_path}")
            open_graph(image_path)
        except DatabaseUnavailable as exc:
            print(error(f"Oracle/database history unavailable: {exc}"))
    if choice not in {"1", "2", "3", "4", "5"}:
        print(warning("Invalid graph selection."))
    return 0


def interactive_menu(config: AppConfig) -> int:
    actions: dict[str, tuple[str, Callable[[], int]]] = {
        "1": ("Show live Windows snapshot", lambda: show_snapshot(config)),
        "2": (
            "Monitor a process",
            lambda: monitor_process(config, int(input("PID: ").strip()), 10),
        ),
        "3": (
            "Run real file warming experiment",
            lambda: run_file_action(
                config, input("File path: ").strip(), ActionKind.FILE_WARM,
                trials=_prompt_trials(),
            ),
        ),
        "4": (
            "Run real memory-mapped prefetch experiment",
            lambda: run_file_action(
                config, input("File path: ").strip(), ActionKind.MMAP_PREFETCH,
                trials=_prompt_trials(),
            ),
        ),
        "5": (
            "Run real database warming experiment",
            lambda: run_database_experiment(
                config, input("Read-only SELECT query: ").strip()
            ),
        ),
        "6": (
            "Train DQN from real file operations",
            lambda: train_dqn(config, int(input("Training steps [5]: ").strip() or "5"), input("Optional read-only DQN database query [skip]: ").strip() or None),
        ),
        "7": (
            "Experimental working-set trim",
            lambda: _trim_from_menu(config),
        ),
        "8": ("Run automatic real-backend demo", lambda: automatic_demo(config)),
        "9": (
            f"Set up {config.database_backend.title()} securely",
            lambda: setup_database_interactive(config),
        ),
        "10": (
            "Live graph viewer",
            lambda: _show_graph_menu(config),
        ),
    }
    while True:
        print(menu_header())
        for key, (label, _) in actions.items():
            print(f"  {key}. {label}")
        print("  0. Exit")
        choice = input("Select: ").strip()
        if choice == "0":
            return 0
        selected = actions.get(choice)
        if selected is None:
            print("Invalid selection.")
            continue
        try:
            selected[1]()
        except (ValueError, OSError, DatabaseUnavailable, RuntimeError) as exc:
            print(error(f"Operation failed: {exc}"))
            pause("Press Enter to return to the menu...")

        except Exception as exc:
            print(error(f"Unexpected operation failure: {exc}"))
            pause("Press Enter to return to the menu...")

def _trim_from_menu(config: AppConfig) -> int:
    debug_enabled = try_enable_debug_privilege()
    pid = int(input("PID to trim: ").strip())
    print(f"SeDebugPrivilege: {'enabled' if debug_enabled else 'not enabled/needed'}")
    result = trim_working_set(pid, SafetyGuard(config.safety_mode))
    print(f"Success: {result.success} — {result.message}")
    return 0 if result.success else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intellipage",
        description="Real Windows monitoring and user-space optimization backend",
    )
    parser.add_argument(
        "--no-elevate",
        action="store_true",
        help="Do not request UAC elevation (useful for read-only checks and tests)",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("menu")
    subparsers.add_parser("snapshot")
    demo = subparsers.add_parser("demo")
    demo.add_argument("--yes", action="store_true", help="Approve safe demo actions")
    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("pid", type=int)
    monitor.add_argument("--samples", type=_positive_int, default=10)
    file_warm = subparsers.add_parser("file-warm")
    file_warm.add_argument("path")
    file_warm.add_argument("--trials", type=_positive_int, default=3)
    mmap_prefetch = subparsers.add_parser("mmap-prefetch")
    mmap_prefetch.add_argument("path")
    mmap_prefetch.add_argument("--trials", type=_positive_int, default=3)
    train = subparsers.add_parser("train-dqn")
    train.add_argument("--steps", type=_positive_int, default=5)
    train.add_argument("--database-query", help="Optional read-only query for real DQN database actions")
    subparsers.add_parser("setup-db")
    return parser


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least one")
    return parsed


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = AppConfig.load(RUNTIME_ROOT / ".env")
    command = args.command or "menu"
    needs_admin = command != "snapshot"
    if needs_admin and is_windows() and not args.no_elevate and not is_admin():
        dispatched = ensure_admin(auto_elevate=True, argv=argv)
        if not dispatched:
            return 0
    if command == "snapshot":
        return show_snapshot(config)
    if command == "monitor":
        return monitor_process(config, args.pid, args.samples)
    if command == "file-warm":
        return run_file_action(config, args.path, ActionKind.FILE_WARM, trials=args.trials)
    if command == "mmap-prefetch":
        return run_file_action(config, args.path, ActionKind.MMAP_PREFETCH, trials=args.trials)
    if command == "train-dqn":
        return train_dqn(config, args.steps, args.database_query)
    if command == "setup-db":
        return setup_database_interactive(config)
    if command == "demo":
        if args.yes:
            config = AppConfig(
                mysql=config.mysql,
                oracle=config.oracle,
                database_backend=config.database_backend,
                safety_mode=SafetyMode.AUTO,
                memory_budget_mb=config.memory_budget_mb,
                sample_interval_seconds=config.sample_interval_seconds,
                process_allowlist=config.process_allowlist,
                model_path=config.model_path,
            )
        return automatic_demo(config)
    return interactive_menu(config)


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except (DatabaseUnavailable, OSError, PermissionError, ValueError, RuntimeError) as exc:
        print(error(f"Operation failed: {exc}"), file=sys.stderr)
        pause("Press Enter to close IntelliPage...")
        return 2
    except Exception as exc:
        print(error(f"Unexpected IntelliPage failure: {exc}"), file=sys.stderr)
        pause("Press Enter to close IntelliPage...")
        return 2















