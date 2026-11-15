# IntelliPage Console Backend

IntelliPage is an administrator-level Windows companion optimizer. This first phase provides a console application that collects live process and memory information, executes real file and memory-mapped prefetch operations, records measured outcomes in Oracle Database 21c or MySQL, and trains a CPU Double-DQN from those outcomes. It does not replace or modify the Windows kernel.

## What this phase demonstrates

- Live Windows process monitoring, including page-fault count, working set, private memory, CPU use, and I/O totals.
- Real file-cache warming by reading selected file ranges.
- Real memory-mapped prefetch hints through the Windows `PrefetchVirtualMemory` API.
- Real, read-only Oracle query warming with `EXPLAIN PLAN` and measured execution time. MySQL remains available as an optional backend.
- FIFO, LRU, and LFU policies driving a bounded in-process byte cache backed by real workload files; misses read storage and hits serve retained bytes without rereading the file.
- A clearly labelled retrospective Optimal baseline for previously recorded real traces. It is not used as a live controller because future accesses are unavailable in real time.
- A CPU Double-DQN with replay memory, target network, Huber loss, epsilon decay, and gradient clipping.
- An experimental working-set trim action that always requires the user to type `YES` immediately before execution.
- Local JSONL audit records plus normalized Oracle/MySQL tables, indexes, foreign keys, views, and transactions.
- Interactive console and automatic demonstration modes, with live dependency-free console graphs for latency, query history, FIFO/LRU/LFU/DQN comparison, and DQN rewards. The menu includes a dedicated live graph viewer (Option 10).

All reported experiment metrics come from executed operating-system, file, or database work. IntelliPage does not generate simulated page-fault results.

## Safety boundary

Windows remains responsible for paging and physical-memory replacement. IntelliPage works in user space and can:

- observe permitted process statistics;
- warm real files and accessible mapped ranges;
- run validated, database-enforced read-only warming queries;
- manage its own resource-retention decisions;
- measure whether an action helped or harmed the selected workload.

The default safety mode is `confirm`. Working-set trimming is treated as experimental and cannot run automatically, even when other actions use automatic mode.

## Requirements

- Windows 10 or Windows 11
- Python 3.11 for the evaluator environment
- Oracle Database XE/Free 21c with SQL*Plus 21c (default), or MySQL 8.0/8.4
- Administrator permission for complete process visibility and administrative actions

The source also runs under Python 3.12 for development and executable packaging.

## Installation

Create and activate a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

Copy `.env.example` only if you intend to configure the database manually. Never commit `.env`.

## Secure Oracle 21c setup

This PC's SQL*Plus installation is detected automatically from its executable path. The default Oracle service is `127.0.0.1:1521/XEPDB1`. With the XE listener and database running, execute:

```powershell
$env:PYTHONPATH = "$PWD\src"
python scripts\setup_database.py --backend oracle --dsn 127.0.0.1:1521/XEPDB1 --admin-user SYS --sysdba --local-sysdba
```

The setup command:

1. requests UAC elevation and asks for the Oracle `SYS AS SYSDBA` password without saving it;
2. connects through the local XE instance, then switches into `XEPDB1`;
3. creates `intellipage_app` in `XEPDB1` with the table, identity-sequence, and view creation privileges needed to install its schema, plus a 100 MB quota on `USERS`;
4. creates the Oracle 21c tables, constraints, indexes, and views in `database/oracle_schema.sql`;
5. writes only the restricted runtime account to `.env`.

Administrative credentials are never retained. To use MySQL instead, set `DATABASE_BACKEND=mysql` or run `python scripts\setup_database.py --backend mysql`.

## Running the backend

Set the package path when running directly from source:

```powershell
$env:PYTHONPATH = "$PWD\src"
python run_intellipage.py
```

The default command opens an interactive menu. The program requests UAC elevation before commands that need administrative access.

Useful direct commands:

```powershell
python run_intellipage.py --no-elevate snapshot
python run_intellipage.py demo --yes
python run_intellipage.py monitor 1234 --samples 20
python run_intellipage.py file-warm C:\path\to\large-file.bin
python run_intellipage.py mmap-prefetch C:\path\to\large-file.bin
python run_intellipage.py train-dqn --steps 5
python run_intellipage.py train-dqn --steps 5 --database-query "SELECT COUNT(*) FROM experiments"
python run_intellipage.py setup-db
```

Global options such as `--no-elevate` should normally precede the subcommand:

```powershell
python run_intellipage.py --no-elevate snapshot
```

## Recommended console demonstration

Run:

```powershell
python run_intellipage.py demo
```

After confirmation, the demonstration:

1. prints a live Windows memory and process snapshot;
2. creates a real 32 MB file-backed workload;
3. measures an actual read;
4. warms the file through the Windows cache;
5. measures the real post-action read and process counters;
6. issues a real memory-mapped prefetch hint;
7. compares FIFO, LRU, and LFU over distinct real files and reports actual I/O;
8. stores audit records locally and in the selected database when configured.

Measurements can vary because Windows caching, concurrent processes, storage hardware, and available memory affect the outcome. A negative or neutral result is retained rather than replaced with an invented improvement.

For a complete source demonstration, run `scripts\run_showcase.ps1`. Example file-access and read-only SQL workloads are provided in `samples\`.

For a reproducible live-process test, start `samples\runtime_workload.py` against a real file, then pass its PID to the `monitor` command. It performs actual file reads and resident-memory writes; it does not generate synthetic metrics.

## DQN training

`train-dqn` collects transitions by executing real file operations. Its four output actions are:

1. no action;
2. file warming;
3. memory-mapped prefetch;
4. database warming.

Database actions can be executed as real read-only experiments when `--database-query` is supplied and a database is configured. Without that query, selecting an unavailable database action receives a measured failure penalty instead of a fabricated result.

The model is saved to `models/intellipage_dqn.pt`. Loading it for evaluation sets epsilon to zero, ensuring deterministic exploitation rather than random exploration.

## Database design

The schema includes:

- `processes`
- `process_samples`
- `system_samples`
- `optimization_actions`
- `experiments`
- `dqn_experiences`
- `query_metrics`
- `policy_performance` view
- `latest_process_metrics` view

Composite indexes support process timelines, policy comparisons, action history, and query-performance analysis. Runtime writes use transactions and parameterized values.

## Tests

```powershell
$env:PYTHONPATH = "$PWD\src"
pytest
```

Tests cover policy selection, secure query restrictions, configuration, real file actions, process collection, audit persistence, and DQN training/model loading.

## Windows executable

With development dependencies installed:

```powershell
.\build_windows.ps1
```

The command creates `dist\IntelliPage.exe`. Its application manifest requests administrator privileges. Oracle Database or MySQL Server remains a separate system dependency for database demonstrations.

## Project structure

```text
database/                 Oracle 21c and MySQL schemas
scripts/                  Secure database bootstrap
src/intellipage/          Backend package
tests/                    Automated tests
build_windows.ps1         PyInstaller build
run_intellipage.py        Source entry point
```



### Image graphs

The console keeps the live ASCII graph for immediate progress and also writes a PNG image graph while the operation runs. The latest images are available under `outputs\graphs\`:

- `file_warm_latency.png` and `mmap_prefetch_latency.png` — baseline and post-action latency;
- `database_latency.png` — Oracle query baseline, warm, and post-action timings;
- `policy_comparison.png` — FIFO, LRU, LFU, and DQN comparison;
- `dqn_training.png` — reward and loss progress during training;
- `oracle_query_history.png` — saved query metrics from Oracle.

Windows opens the resulting PNG automatically when an experiment finishes. In the interactive menu, choose Option 10 to select a graph type and open it again. Pillow is bundled with the executable, so no separate plotting installation is required.