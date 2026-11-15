"""Offline tests for the wormhole kill summary.

Run from the project folder with the project's Python:

    .venv/bin/python tests/test_kills.py          (Linux/macOS)
    .venv\\Scripts\\python tests\\test_kills.py     (Windows)

zKillboard has been seen to answer in two shapes: a bare ID and "zkb"
block per kill, or the whole killmail as well. The fixtures cover both.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eve_common  # noqa: E402
from server import (WORMHOLE_CLASSES, fill_details, is_wormhole,  # noqa: E402
                    kill_summary)

NOW = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)

# What ESI's /killmails/{id}/{hash}/ holds for each kill, trimmed to the
# fields the summary reads. Corp 10 is the resident. 1000125 is an NPC corp.
ESI = {
    905: {"killmail_time": "2026-11-15T19:30:00Z",   # half an hour ago
          "attackers": [{"character_id": 1, "corporation_id": 10},
                        {"character_id": 2, "corporation_id": 10},
                        {"character_id": 3, "corporation_id": 20}]},
    904: {"killmail_time": "2026-11-15T18:00:00Z",   # two hours ago
          "attackers": [{"character_id": 1, "corporation_id": 10}]},
    903: {"killmail_time": "2026-11-15T02:00:00Z",   # Sleepers, this morning
          "attackers": [{"corporation_id": 1000125, "ship_type_id": 30460}]},
    902: {"killmail_time": "2026-11-14T18:00:00Z",   # yesterday, over a day
          "attackers": [{"character_id": 4, "corporation_id": 30},
                        {"corporation_id": 1000125, "ship_type_id": 30460}]},
    901: {"killmail_time": "2026-11-12T12:00:00Z",   # three days ago
          "attackers": [{"character_id": 5, "corporation_id": 20}]},
}


def bare(killmail_id, npc=False):
    """A kill as zKillboard sends it without the killmail."""
    return {"killmail_id": killmail_id,
            "zkb": {"hash": f"hash{killmail_id}", "npc": npc,
                    "totalValue": 1000000.0, "solo": False}}


def bare_page():
    return [bare(905), bare(904), bare(903, npc=True), bare(902), bare(901)]


def whole_page():
    return [{**kill, **ESI[kill["killmail_id"]]} for kill in bare_page()]


def test_no_kills():
    assert kill_summary([], NOW) == {
        "kills_last_hour": 0, "kills_last_24h": 0, "kills_last_7d": 0,
        "last_kill": None, "attackers": [], "attackers_from": 0}


def test_whole_killmails_are_counted_by_age_without_the_npc_kill():
    s = kill_summary(whole_page(), NOW)
    assert (s["kills_last_hour"], s["kills_last_24h"], s["kills_last_7d"]) == (1, 2, 4)
    assert s["last_kill"] == "2026-11-15T19:30:00Z"


def test_attackers_are_ranked_and_counted_once_per_kill():
    s = kill_summary(whole_page(), NOW)
    assert s["attackers"] == [(10, 2), (20, 2), (30, 1)]
    assert s["attackers_from"] == 4


def test_bare_records_alone_give_unknown_not_zero():
    s = kill_summary(bare_page(), NOW)
    assert s["kills_last_hour"] is None and s["kills_last_24h"] is None
    assert s["kills_last_7d"] == 4
    assert s["last_kill"] is None and s["attackers"] is None


def test_bare_records_are_filled_from_esi_until_a_day_old():
    asked = []

    def fetch(killmail_id, killmail_hash):
        asked.append((killmail_id, killmail_hash))
        return ESI[killmail_id]

    s = kill_summary(fill_details(bare_page(), NOW, fetch), NOW)
    # Newest first, never the NPC kill, and nothing past the first old one.
    assert asked == [(905, "hash905"), (904, "hash904"), (902, "hash902")]
    assert (s["kills_last_hour"], s["kills_last_24h"], s["kills_last_7d"]) == (1, 2, 4)
    assert s["last_kill"] == "2026-11-15T19:30:00Z"
    assert s["attackers"] == [(10, 2), (20, 1), (30, 1)]
    assert s["attackers_from"] == 3


def test_whole_killmails_need_no_fetching():
    def fetch(killmail_id, killmail_hash):
        raise AssertionError("nothing should be fetched")

    fill_details(whole_page(), NOW, fetch)


def test_the_fetch_limit_turns_the_counts_into_floors():
    kills = [bare(1000 - n) for n in range(5)]
    recent = {"killmail_time": "2026-11-15T19:59:00Z", "attackers": []}
    s = kill_summary(fill_details(kills, NOW, lambda i, h: recent, limit=2), NOW)
    assert (s["kills_last_hour"], s["kills_last_24h"]) == ("2+", "2+")
    assert s["last_kill"] == "2026-11-15T19:59:00Z"


def test_the_hour_can_be_complete_when_the_day_is_only_a_floor():
    kills = [bare(1000 - n) for n in range(5)]
    earlier = {"killmail_time": "2026-11-15T15:00:00Z", "attackers": []}
    s = kill_summary(fill_details(kills, NOW, lambda i, h: earlier, limit=2), NOW)
    assert (s["kills_last_hour"], s["kills_last_24h"]) == (0, "2+")


def test_esi_failing_leaves_the_counts_unknown():
    def fetch(killmail_id, killmail_hash):
        raise httpx.ConnectError("down")

    s = kill_summary(fill_details(bare_page(), NOW, fetch), NOW)
    assert s["kills_last_hour"] is None and s["kills_last_24h"] is None


def test_esi_giving_up_after_retries_leaves_the_counts_unknown():
    def fetch(killmail_id, killmail_hash):
        raise RuntimeError("EVE's API isn't answering (503).")

    s = kill_summary(fill_details(bare_page(), NOW, fetch), NOW)
    assert s["kills_last_hour"] is None and s["kills_last_24h"] is None
    assert s["kills_last_7d"] == 4


def test_an_error_object_from_zkillboard_is_refused():
    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "Invalid request"}

    real = eve_common.http.get
    eve_common.http.get = lambda *args, **kwargs: Reply()
    try:
        eve_common.zkill_system_kills(31000005)
    except ValueError:
        pass
    else:
        raise AssertionError("an error object should raise ValueError")
    finally:
        eve_common.http.get = real


def test_a_full_page_is_reported_as_at_least():
    kills = [{**bare(n), **ESI[905]} for n in range(200)]
    assert kill_summary(kills, NOW)["kills_last_7d"] == "200+"


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
