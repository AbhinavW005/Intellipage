import os
import subprocess
import sys
import time

from intellipage.monitoring import WindowsMonitor


def test_current_process_is_accessible():
    sample = WindowsMonitor().sample_process(os.getpid())
    assert sample.accessible
    assert sample.pid == os.getpid()
    assert sample.working_set_bytes > 0


def test_repeated_sample_reports_live_cpu_usage():
    monitor = WindowsMonitor()
    monitor.sample_process(os.getpid())
    deadline = time.perf_counter() + 0.15
    value = 0
    while time.perf_counter() < deadline:
        value += 1
    sample = monitor.sample_process(os.getpid())
    assert value > 0
    assert sample.cpu_percent > 0


def test_completed_process_is_reported_separately():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5)
    sample = WindowsMonitor().sample_process(process.pid)
    assert not sample.accessible
    assert sample.error == "process_ended"

