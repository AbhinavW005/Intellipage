from pathlib import Path

import numpy as np
import pytest

from intellipage.dqn import DoubleDQNAgent, STATE_SIZE


def test_double_dqn_trains_and_round_trips(tmp_path: Path):
    agent = DoubleDQNAgent(seed=7)
    for index in range(12):
        state = np.full(STATE_SIZE, index / 10, dtype=np.float32)
        next_state = state + 0.01
        agent.remember(state, index % agent.action_size, float(index), next_state, index == 11)
    loss = agent.train_step(batch_size=8)
    assert loss is not None
    model = tmp_path / "model.pt"
    agent.save(model)
    loaded = DoubleDQNAgent(seed=8)
    loaded.load(model, evaluation=True)
    assert loaded.epsilon == 0.0
    assert 0 <= loaded.act(np.zeros(STATE_SIZE), explore=False) < loaded.action_size


def test_dqn_rejects_non_positive_batch_size():
    with pytest.raises(ValueError, match="batch_size"):
        DoubleDQNAgent().train_step(batch_size=0)

