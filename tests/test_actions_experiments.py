import os
import stat
from pathlib import Path

import pytest

from intellipage.actions import SafetyGuard, prefetch_mapped_file, warm_file
from intellipage.experiments import ExperimentRunner, timed_file_read
from intellipage.models import ActionKind, SafetyMode
from intellipage.monitoring import WindowsMonitor


def test_warm_file_touches_real_bytes(tmp_path: Path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"x" * 8192)
    result = warm_file(target)
    assert result.success
    assert result.bytes_touched == 8192
    assert result.kind == ActionKind.FILE_WARM


def test_file_read_helpers_reject_non_positive_limits(tmp_path: Path):
    target = tmp_path / "data.bin"
    target.write_bytes(b"data")
    result = warm_file(target, max_bytes=0)
    assert not result.success
    with pytest.raises(ValueError, match="max_bytes"):
        timed_file_read(target, max_bytes=0)


def test_file_experiment_uses_real_file_and_writes_audit(tmp_path: Path):
    target = tmp_path / "payload.bin"
    target.write_bytes(b"a" * (1024 * 1024))
    audit = tmp_path / "audit.jsonl"
    runner = ExperimentRunner(WindowsMonitor(), audit_path=audit)
    result = runner.run_file_experiment(
        target, policy="LRU", action_kind=ActionKind.FILE_WARM
    )
    assert result.action.success
    assert result.baseline_latency_ms >= 0
    assert result.post_action_latency_ms >= 0
    assert audit.exists()


def test_confirm_guard_requires_exact_yes():
    denied = SafetyGuard(SafetyMode.CONFIRM, confirmer=lambda _: False)
    allowed = SafetyGuard(SafetyMode.CONFIRM, confirmer=lambda _: True)
    assert not denied.authorize("test")
    assert allowed.authorize("test")


def test_prefetch_accepts_read_only_file_on_windows(tmp_path: Path):
    if os.name != "nt":
        return
    target = tmp_path / "read-only.bin"
    target.write_bytes(b"p" * (1024 * 1024))
    target.chmod(stat.S_IREAD)
    try:
        result = prefetch_mapped_file(target)
        assert result.success, result.message
        assert result.bytes_touched == 1024 * 1024
    finally:
        target.chmod(stat.S_IWRITE | stat.S_IREAD)

