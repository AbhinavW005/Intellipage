from pathlib import Path

import pytest

from intellipage.dqn import DoubleDQNAgent
from intellipage.live_training import LiveFileDQNTrainer
from intellipage.monitoring import WindowsMonitor


def test_training_rejects_non_positive_inputs(tmp_path: Path) -> None:
    trainer = LiveFileDQNTrainer(
        WindowsMonitor(), DoubleDQNAgent(), audit_path=tmp_path / "audit.jsonl"
    )
    with pytest.raises(ValueError):
        trainer.train(tmp_path / "workload.bin", steps=0)
    with pytest.raises(ValueError):
        trainer.train(tmp_path / "workload.bin", max_bytes=0)
    with pytest.raises(ValueError):
        trainer.train(tmp_path / "workload.bin", batch_size=0)
