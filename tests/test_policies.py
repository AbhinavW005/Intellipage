from intellipage.models import ResourceState
from intellipage.policies import FIFOPolicy, LFUPolicy, LRUPolicy, OptimalRetrospectivePolicy


def resources():
    return [
        ResourceState("a", first_seen_order=1, last_access_order=8, access_count=5, next_access_order=20),
        ResourceState("b", first_seen_order=2, last_access_order=3, access_count=1, next_access_order=None),
        ResourceState("c", first_seen_order=3, last_access_order=6, access_count=2, next_access_order=12),
    ]


def test_fifo_selects_first_loaded():
    assert FIFOPolicy().select_victim(resources()).resource_id == "a"


def test_lru_selects_oldest_access():
    assert LRUPolicy().select_victim(resources()).resource_id == "b"


def test_lfu_selects_lowest_frequency():
    assert LFUPolicy().select_victim(resources()).resource_id == "b"


def test_optimal_is_retrospective_oracle():
    assert OptimalRetrospectivePolicy().select_victim(resources()).resource_id == "b"

