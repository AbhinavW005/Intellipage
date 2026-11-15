from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
from torch import nn


STATE_SIZE = 12
ACTION_NAMES = ("noop", "file_warm", "mmap_prefetch", "database_warm")


class QNetwork(nn.Module):
    def __init__(self, state_size: int = STATE_SIZE, action_size: int = len(ACTION_NAMES)):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(state_size, 128),
            nn.LayerNorm(128),
            nn.ReLU(),
            nn.Linear(128, 128),
            nn.ReLU(),
            nn.Linear(128, action_size),
        )

    def forward(self, state: torch.Tensor) -> torch.Tensor:
        return self.layers(state)


@dataclass(slots=True)
class Experience:
    state: np.ndarray
    action: int
    reward: float
    next_state: np.ndarray
    done: bool


class DoubleDQNAgent:
    """CPU Double-DQN with target network, replay memory, and gradient clipping."""

    def __init__(
        self,
        state_size: int = STATE_SIZE,
        action_size: int = len(ACTION_NAMES),
        *,
        learning_rate: float = 1e-3,
        gamma: float = 0.97,
        epsilon: float = 1.0,
        epsilon_min: float = 0.05,
        epsilon_decay: float = 0.995,
        target_update_interval: int = 50,
        replay_capacity: int = 10_000,
        seed: int = 42,
    ) -> None:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        self.state_size = state_size
        self.action_size = action_size
        self.device = torch.device("cpu")
        self.policy_network = QNetwork(state_size, action_size).to(self.device)
        self.target_network = QNetwork(state_size, action_size).to(self.device)
        self.target_network.load_state_dict(self.policy_network.state_dict())
        self.target_network.eval()
        self.optimizer = torch.optim.AdamW(
            self.policy_network.parameters(), lr=learning_rate
        )
        self.loss_fn = nn.SmoothL1Loss()
        self.memory: deque[Experience] = deque(maxlen=replay_capacity)
        self.gamma = gamma
        self.epsilon = epsilon
        self.epsilon_min = epsilon_min
        self.epsilon_decay = epsilon_decay
        self.target_update_interval = target_update_interval
        self.training_steps = 0

    def act(self, state: Iterable[float], *, explore: bool = True) -> int:
        vector = self._vector(state)
        if explore and random.random() < self.epsilon:
            return random.randrange(self.action_size)
        with torch.no_grad():
            tensor = torch.from_numpy(vector).unsqueeze(0).to(self.device)
            return int(self.policy_network(tensor).argmax(dim=1).item())

    def q_values(self, state: Iterable[float]) -> list[float]:
        """Return exploitation scores for all actions without exploration."""
        vector = self._vector(state)
        with torch.no_grad():
            tensor = torch.from_numpy(vector).unsqueeze(0).to(self.device)
            return [float(value) for value in self.policy_network(tensor)[0].tolist()]
    def remember(
        self,
        state: Iterable[float],
        action: int,
        reward: float,
        next_state: Iterable[float],
        done: bool,
    ) -> None:
        if not 0 <= action < self.action_size:
            raise ValueError("Action index is outside the configured action space")
        self.memory.append(
            Experience(
                self._vector(state),
                action,
                float(reward),
                self._vector(next_state),
                bool(done),
            )
        )

    def train_step(self, batch_size: int = 32) -> float | None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        if len(self.memory) < batch_size:
            return None
        batch = random.sample(self.memory, batch_size)
        states = torch.from_numpy(np.stack([item.state for item in batch])).to(self.device)
        actions = torch.tensor([item.action for item in batch], dtype=torch.long).unsqueeze(1)
        rewards = torch.tensor([item.reward for item in batch], dtype=torch.float32).unsqueeze(1)
        next_states = torch.from_numpy(np.stack([item.next_state for item in batch])).to(self.device)
        dones = torch.tensor([item.done for item in batch], dtype=torch.float32).unsqueeze(1)

        current_q = self.policy_network(states).gather(1, actions)
        with torch.no_grad():
            next_actions = self.policy_network(next_states).argmax(dim=1, keepdim=True)
            next_q = self.target_network(next_states).gather(1, next_actions)
            target_q = rewards + (1.0 - dones) * self.gamma * next_q

        loss = self.loss_fn(current_q, target_q)
        self.optimizer.zero_grad(set_to_none=True)
        loss.backward()
        nn.utils.clip_grad_norm_(self.policy_network.parameters(), max_norm=5.0)
        self.optimizer.step()
        self.training_steps += 1
        self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        if self.training_steps % self.target_update_interval == 0:
            self.target_network.load_state_dict(self.policy_network.state_dict())
        return float(loss.item())

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "version": 1,
                "state_size": self.state_size,
                "action_size": self.action_size,
                "policy_state_dict": self.policy_network.state_dict(),
                "target_state_dict": self.target_network.state_dict(),
                "optimizer_state_dict": self.optimizer.state_dict(),
                "epsilon": self.epsilon,
                "training_steps": self.training_steps,
            },
            target,
        )

    def load(self, path: str | Path, *, evaluation: bool = False) -> None:
        checkpoint = torch.load(path, map_location=self.device, weights_only=True)
        if checkpoint["state_size"] != self.state_size or checkpoint["action_size"] != self.action_size:
            raise ValueError("Model dimensions do not match this IntelliPage build")
        self.policy_network.load_state_dict(checkpoint["policy_state_dict"])
        self.target_network.load_state_dict(checkpoint["target_state_dict"])
        if not evaluation and "optimizer_state_dict" in checkpoint:
            self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        self.training_steps = int(checkpoint.get("training_steps", 0))
        self.epsilon = 0.0 if evaluation else float(checkpoint.get("epsilon", 0.1))

    def _vector(self, state: Iterable[float]) -> np.ndarray:
        vector = np.asarray(list(state), dtype=np.float32)
        if vector.shape != (self.state_size,):
            raise ValueError(f"Expected state shape ({self.state_size},), received {vector.shape}")
        vector = np.nan_to_num(vector, nan=0.0, posinf=1e6, neginf=-1e6)
        return np.clip(vector, -1e6, 1e6)


