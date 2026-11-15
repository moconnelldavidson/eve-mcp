"""Offline tests for the wormhole kill summary.

Run from the project folder with the project's Python:

    .venv/bin/python tests/test_kills.py          (Linux/macOS)
    .venv\\Scripts\\python tests\\test_kills.py     (Windows)
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import WORMHOLE_CLASSES, is_wormhole, kill_summary  # noqa: E402

NOW = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)


def kill(time, *corporations, npc=False):
    attackers = [{"corporation_id": c, "character_id": 1} for c in corporations]
    if npc:
        attackers.append({"corporation_id": 1000125, "ship_type_id": 1})
    return {"killmail_time": time, "attackers": attackers}


def test_no_kills():
    assert kill_summary([], NOW) == {
        "kills_last_hour": 0, "kills_last_24h": 0, "kills_last_7d": 0,
        "last_kill": None, "attackers": []}


def test_kills_are_counted_by_age():
    kills = [kill("2026-11-15T19:30:00Z", 10),   # half an hour ago
             kill("2026-11-15T02:00:00Z", 10),   # this morning
             kill("2026-11-12T12:00:00Z", 20)]   # three days ago
    s = kill_summary(kills, NOW)
    assert (s["kills_last_hour"], s["kills_last_24h"], s["kills_last_7d"]) == (1, 2, 3)
    assert s["last_kill"] == "2026-11-15T19:30:00Z"


def test_attackers_are_ranked_and_counted_once_per_kill():
    kills = [kill("2026-11-15T19:30:00Z", 10, 10, 20),  # two pilots from corp 10
             kill("2026-11-15T18:00:00Z", 10),
             kill("2026-11-14T18:00:00Z", 30, npc=True)]
    assert kill_summary(kills, NOW)["attackers"] == [(10, 2), (20, 1), (30, 1)]


def test_a_full_page_is_reported_as_at_least():
    kills = [kill("2026-11-15T19:30:00Z", 10)] * 200
    assert kill_summary(kills, NOW)["kills_last_7d"] == "200+"


def test_kills_without_a_time_still_count_for_the_week():
    s = kill_summary([{"killmail_id": 1}], NOW)
    assert (s["kills_last_7d"], s["kills_last_24h"], s["last_kill"]) == (1, 0, None)


def test_wormhole_ids_and_classes():
    assert is_wormhole(31000876) and is_wormhole(31000005)
    assert not is_wormhole(30000142)
    assert WORMHOLE_CLASSES["B"] == "C2"
    assert WORMHOLE_CLASSES["G"] == "Thera"


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"{len(tests)} tests passed.")
