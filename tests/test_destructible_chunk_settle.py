import pytest
import gameplay.destructible_chunk_settle as mod

CASES = [
    ("chunk_spawn_with_collider", (1,)),
    ("pool_peak_reserve", (1,)),
    ("sleep_support_chain", (1,)),
    ("seeded_random", (1,)),
    ("fade_by_distance", (1,)),
    ("recompute_before_free", (1,)),
    ("shatter", (1,)),
    ("settle", (1,)),
    ("fade", (1,)),
    ("present", (1,)),
    ("validate_chunk", (1,)),
    ("metrics", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("determinism", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
