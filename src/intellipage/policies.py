from __future__ import annotations

from abc import ABC, abstractmethod
from math import inf
from typing import Sequence

from .models import ResourceState


class ReplacementPolicy(ABC):
    name: str

    @abstractmethod
    def select_victim(self, resources: Sequence[ResourceState]) -> ResourceState:
        raise NotImplementedError

    @staticmethod
    def _require(resources: Sequence[ResourceState]) -> None:
        if not resources:
            raise ValueError("At least one resource is required")


class FIFOPolicy(ReplacementPolicy):
    name = "FIFO"

    def select_victim(self, resources: Sequence[ResourceState]) -> ResourceState:
        self._require(resources)
        return min(resources, key=lambda item: (item.first_seen_order, item.resource_id))


class LRUPolicy(ReplacementPolicy):
    name = "LRU"

    def select_victim(self, resources: Sequence[ResourceState]) -> ResourceState:
        self._require(resources)
        return min(resources, key=lambda item: (item.last_access_order, item.resource_id))


class LFUPolicy(ReplacementPolicy):
    name = "LFU"

    def select_victim(self, resources: Sequence[ResourceState]) -> ResourceState:
        self._require(resources)
        return min(
            resources,
            key=lambda item: (
                item.access_count,
                item.last_access_order,
                item.resource_id,
            ),
        )


class OptimalRetrospectivePolicy(ReplacementPolicy):
    """Clairvoyant baseline for recorded traces; never used as a live controller."""

    name = "OPTIMAL_RETROSPECTIVE"

    def select_victim(self, resources: Sequence[ResourceState]) -> ResourceState:
        self._require(resources)
        return max(
            resources,
            key=lambda item: (
                inf if item.next_access_order is None else item.next_access_order,
                item.resource_id,
            ),
        )


POLICIES: dict[str, ReplacementPolicy] = {
    "fifo": FIFOPolicy(),
    "lru": LRUPolicy(),
    "lfu": LFUPolicy(),
    "optimal": OptimalRetrospectivePolicy(),
}

