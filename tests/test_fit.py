"""Offline tests for the fit parser, the fitting check and the training-time
sums.

Run from the project folder with the project's Python:

    .venv/bin/python tests/test_fit.py          (Linux/macOS)
    .venv\\Scripts\\python tests\\test_fit.py     (Windows)
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitdata  # noqa: E402
from fitdata import GameData  # noqa: E402
from fitting import Dogma, check_fit, eft_fit  # noqa: E402
from server import duration, fit_items, sp_for_level  # noqa: E402

FIT = """[Tristan, Belt ratter]
Drone Damage Amplifier II
Small Ancillary Armor Repairer, Nanite Repair Paste
[Empty Low slot]

1MN Afterburner II
Warp Scrambler II /offline

125mm Railgun II, Caldari Navy Antimatter Charge S
125mm Railgun II, Caldari Navy Antimatter Charge S

Hobgoblin II x5
Nanite Repair Paste x20
"""


def test_single_name_is_one_item():
    assert fit_items("Stratios") == ["Stratios"]
    assert fit_items("  Covert Ops Cloaking Device II \n") == ["Covert Ops Cloaking Device II"]


def test_fit_gives_hull_modules_charges_and_drones_once_each():
    assert fit_items(FIT) == [
        "Tristan",
        "Drone Damage Amplifier II",
        "Small Ancillary Armor Repairer",
        "Nanite Repair Paste",
        "1MN Afterburner II",
        "Warp Scrambler II",
        "125mm Railgun II",
        "Caldari Navy Antimatter Charge S",
        "Hobgoblin II",
    ]


def test_empty_text_gives_nothing():
    assert fit_items("") == []
    assert fit_items("[Empty High slot]\n\n") == []


def test_sp_for_level_matches_the_game():
    # Rank 1 is the familiar 250 / 1,414 / 8,000 / 45,255 / 256,000.
    assert [sp_for_level(1, n) for n in range(6)] == [0, 250, 1414, 8000, 45255, 256000]
    assert sp_for_level(2, 4) == 90510
    assert sp_for_level(2, 5) == 512000


def test_duration_reads_like_the_skill_queue():
    assert duration(12) == "12m"
    assert duration(312) == "5h 12m"
    assert duration(1440 * 3 + 250) == "3d 4h"
    assert duration(0) == "0m"


# ---------- does it fit? ----------
# These run on game data recorded from EVE Ref (tests/fixtures/dogma.json,
# made by tests/record_fixtures.py), so the engine is tested on real
# effects and no network is used. Expected figures are worked out by hand
# in the comments.

REC = json.loads((Path(__file__).parent / "fixtures" / "dogma.json").read_text())
IDS = REC["ids"]


class Recorded:
    """Stands in for fitdata.GameData, answering from the recorded file."""

    def type(self, type_id):
        t = REC["types"][str(type_id)]
        return {**t, "attrs": {k: v for k, v in t["attrs"]}}

    def effect(self, effect_id):
        return REC["dogma_effects"][str(effect_id)]

    def attribute(self, attribute_id):
        return REC["dogma_attributes"][str(attribute_id)]

    def preload(self, type_ids):
        pass


MAXED = ["CPU Management", "Power Grid Management", "Mining Upgrades",
         "Shield Upgrades", "Weapon Upgrades", "Advanced Weapon Upgrades",
         "Mining", "Gunnery", "Missile Launcher Operation",
         "Mining Destroyer", "Covert Ops", "Caldari Frigate", "Cloaking"]


def run(eft, **levels):
    """Check a fit with every recorded skill at V, except those named
    (underscores for spaces, as in mining_upgrades=1)."""
    skills = {IDS[name.lower()]: 5 for name in MAXED}
    for name, level in levels.items():
        skills[IDS[name.replace("_", " ")]] = level
    return check_fit(eft_fit(eft), IDS, skills, Recorded())


TOYO = """[Pioneer, Toyo Highsec Miner]
Mining Laser Upgrade I
Mining Laser Upgrade I

Small F-S9 Regolith Compact Shield Extender
Multispectrum Shield Hardener I
1MN Y-S8 Compact Afterburner

Miner II
Miner II
Miner II
[Empty High slot]

Small Core Defense Field Extender I
Small Core Defense Field Extender I
Small Core Defense Field Extender I

Hobgoblin II x2
"""
EMPTY = "[Empty Low slot]\n\n[Empty Med slot]\n\n[Empty High slot]\n\n[Empty Rig slot]"


def test_the_real_fit_is_over_on_cpu():
    # An upgrade is a CPU penalty on miners, and Mining Upgrades I shrinks it
    # to 9.5%. Three Miner IIs: 3 x 80 x 1.095 x 1.095 = 287.77, plus the
    # upgrades' own 60 and 16 + 40 + 13. Powergrid: the extender's 2 MW
    # is cut by Shield Upgrades V to 1.5, and 2 + 1 + 9 + 12.
    r = run(TOYO, mining_upgrades=1)
    assert r["verdict"] == "does not fit: CPU is 16.77 tf over"
    assert r["cpu"] == "CPU 416.77 / 400 tf"
    assert r["powergrid"] == "Powergrid 25.5 / 62.5 MW"
    assert r["calibration"] == "Calibration 150 / 400"
    assert r["slots"] == "High 3 / 4, Mid 3 / 3, Low 2 / 2, Rig 3 / 3"
    assert r["hardpoints"] == "Turrets 3 / 3, Launchers 0 / 0"
    assert r["drone_bay"] == "Drone bay 10 / 40 m3"
    assert r["drone_bandwidth"] == "Drone bandwidth 10 / 20 Mbit/s"
    assert "unreadable" not in r["not_modelled"]


def test_a_higher_skill_level_shrinks_the_penalty_more():
    # Mining Upgrades V: 7.5% each, 240 x 1.075^2 = 277.35, plus 129.
    r = run(TOYO, mining_upgrades=5)
    assert r["cpu"] == "CPU 406.35 / 400 tf"
    assert r["verdict"] == "does not fit: CPU is 6.35 tf over"


def test_within_five_percent_is_too_close_to_call():
    # The same without the extender: 277.35 + 60 + 40 + 13 = 390.35 of 400.
    r = run(TOYO.replace("Small F-S9 Regolith Compact Shield Extender\n", ""))
    assert r["cpu"] == "CPU 390.35 / 400 tf"
    assert r["verdict"].startswith("too close to call: CPU")
    assert "fitting window" in r["verdict"]


def test_fit_that_fits():
    # One upgrade at Mining Upgrades V adds 7.5% to the miner's 80 tf: 86.
    r = run("[Pioneer, ok]\nMining Laser Upgrade I\n\n1MN Y-S8 Compact "
            "Afterburner\n\nMiner II\n\nSmall Core Defense Field Extender I")
    assert r["verdict"] == "fits"
    assert r["cpu"] == "CPU 129 / 400 tf"               # 30 + 86 + 13
    assert r["powergrid"] == "Powergrid 14 / 62.5 MW"   # 1 + 9 + 4
    assert r["calibration"] == "Calibration 50 / 400"


def test_offline_module_costs_nothing_but_keeps_its_slot():
    # Offline miners add nothing and cost nothing, and the upgrades'
    # penalty has nothing to act on: 60 + 16 + 40 + 13.
    r = run(TOYO.replace("Miner II", "Miner II /offline"), mining_upgrades=1)
    assert r["verdict"] == "fits"
    assert r["cpu"] == "CPU 129 / 400 tf"
    assert r["hardpoints"].startswith("Turrets 3 / 3")
    assert r["slots"].startswith("High 3 / 4")


def test_too_many_turrets():
    r = run(TOYO.replace("[Empty High slot]", "Miner II"))
    assert "Turret hardpoints: 4 / 3" in r["verdict"]
    assert r["verdict"].startswith("does not fit")


def test_weapon_upgrades_cut_a_launchers_cpu_and_advanced_its_powergrid():
    launcher = "[Kestrel, t]\n[Empty Low slot]\n\n[Empty Med slot]\n\nLight Missile Launcher II"
    # The launcher needs 24 tf and 7 MW. Weapon Upgrades V takes 25% off the
    # CPU and Advanced Weapon Upgrades V 10% off the powergrid.
    r = run(launcher)
    assert r["cpu"].startswith("CPU 18 /")
    assert r["powergrid"].startswith("Powergrid 6.3 /")
    r = run(launcher, weapon_upgrades=0, advanced_weapon_upgrades=0)
    assert r["cpu"].startswith("CPU 24 /")
    assert r["powergrid"].startswith("Powergrid 7 /")


def test_a_covert_ops_hull_makes_the_cloak_fit():
    # The cloak needs 100 tf. The Buzzard takes 20% off per level of Covert
    # Ops (the hull's bonus is scaled by the skill), so V makes it free.
    cloak = ("[Buzzard, t]\n[Empty Low slot]\n\n[Empty Med slot]\n\n"
             "Covert Ops Cloaking Device II")
    r = run(cloak)
    assert r["cpu"].startswith("CPU 0 /")
    assert r["verdict"] == "fits"
    assert run(cloak, covert_ops=3)["cpu"].startswith("CPU 40 /")


def test_a_cloak_on_the_wrong_hull_is_refused():
    r = run("[Kestrel, t]\n[Empty Low slot]\n\n[Empty Med slot]\n\n"
            "Covert Ops Cloaking Device II")
    assert "Covert Ops Cloaking Device II can't be fitted to a Kestrel" in r["verdict"]
    assert r["verdict"].startswith("does not fit")


def test_two_damage_controls_break_max_group_fitted():
    r = run("[Pioneer, t]\nDamage Control II\nDamage Control II")
    assert "Damage Control II: 2 fitted, only 1 allowed" in r["verdict"]
    assert r["verdict"].count("Damage Control II") == 1  # said once, not per line


def test_a_medium_rig_on_a_small_ship_is_refused():
    r = run(f"[Pioneer, t]\n{EMPTY.rsplit(chr(10), 1)[0]}\nMedium Core Defense Field Extender I")
    assert "wrong rig size" in r["verdict"]


def test_spare_drones_beyond_bandwidth_still_fit():
    # Five Hobgoblin IIs take 25 Mbit/s of 20, so only four launch, but
    # they are 25 m3 in a 40 m3 bay. Drones in the cargo count for nothing.
    r = run(f"[Pioneer, t]\n{EMPTY}\n\nHobgoblin II x5\n\nHobgoblin II x50")
    assert r["verdict"] == "fits"
    assert r["drone_bay"] == "Drone bay 25 / 40 m3"
    assert "not all can be launched" in r["drone_bandwidth"]


def test_too_many_drones_for_the_bay():
    r = run(f"[Pioneer, t]\n{EMPTY}\n\nHobgoblin II x9")
    assert "Drone bay: 45 / 40" in r["verdict"]


def test_an_unknown_cargo_item_is_listed_but_does_not_stop_a_verdict():
    r = run(f"[Pioneer, t]\n{EMPTY}\n\nHobgoblin II x2\n\nMystery Goo x3")
    assert r["verdict"] == "fits"
    assert r["not_found"] == ["Mystery Goo"]


def test_fitting_modules_and_rigs_add_to_the_ships_output():
    # Co-Processor I: 320 x 1.08 x 1.25 = 432. The power core adds 10
    # before the skill, (50 + 10) x 1.25 = 75, and the router rig 10%.
    r = run("[Pioneer, t]\nCo-Processor I\nMicro Auxiliary Power Core I\n\n"
            "[Empty Med slot]\n\n[Empty High slot]\n\nSmall Ancillary Current Router I")
    assert r["cpu"] == "CPU 15 / 432 tf"
    assert r["powergrid"] == "Powergrid 1 / 82.5 MW"
    # Processor Overclocking Unit: 320 x 1.25 x 1.071.
    r = run("[Pioneer, t]\n" + EMPTY.rsplit("\n", 1)[0] + "\nSmall Processor Overclocking Unit I")
    assert r["cpu"].endswith("/ 428.4 tf")


def test_a_module_we_know_nothing_about_is_unknown_never_fits():
    r = run("[Pioneer, t]\nMining Laser Upgrade I\n\n[Empty Med slot]\n\n"
            "Miner II\nMystery Gun")
    assert r["verdict"].startswith("unknown:")
    assert "Mystery Gun" in r["verdict"]
    assert "at least" in r["cpu"]


def test_a_module_in_the_wrong_section_is_unknown():
    r = run("[Pioneer, x]\nMiner II")  # a high-slot module in the lows
    assert r["verdict"].startswith("unknown:")


def test_strategic_cruisers_are_unknown_for_now():
    assert run("[Tengu, x]\n" + EMPTY)["verdict"].startswith("unknown: Strategic")


# ---------- the evaluator on its own ----------

class Made:
    """A few hand-made items, effects and attributes."""

    def __init__(self, types, effects, stackable=True):
        self.types, self.effects, self.stackable = types, effects, stackable

    def type(self, type_id):
        attrs, effects = self.types[type_id]
        return {"group_id": 1, "volume": 0, "attrs": attrs, "effects": effects}

    def effect(self, effect_id):
        return {"name": f"effect{effect_id}", "category": 0,
                "modifiers": self.effects[effect_id]}

    def attribute(self, attribute_id):
        return {"name": f"attr{attribute_id}", "default": 0,
                "stackable": self.stackable}


def mod(op, modified, modifying, func="ItemModifier", domain="shipID", **target):
    return {"operator": op, "modified_attribute_id": modified,
            "modifying_attribute_id": modifying, "func": func,
            "domain": domain, **target}


def evaluate(types, effects, sources, wanted, stackable=True):
    d = Dogma(Made(types, effects, stackable))
    for key, type_id, kind in sources:
        d.add(key, type_id, kind)
    d.link([key for key, *_ in sources])
    return d, d.value(*wanted)


def test_operators_apply_in_the_games_order():
    # ((100 x 2) + 10) x 3 x 1.5, whatever order they are listed in.
    types = {1: ({48: 100}, []),
             2: ({1: 2, 2: 10, 3: 3, 4: 50}, [7])}
    effects = {7: [mod(6, 48, 4), mod(4, 48, 3), mod(2, 48, 2), mod(0, 48, 1)]}
    _, cpu = evaluate(types, effects, [("ship", 1, "ship"), ("m", 2, "module")],
                      ("ship", 48))
    assert cpu == 945


def test_a_skills_bonus_is_scaled_by_its_level():
    # The skill's own effect multiplies its 5% by its level (280), and its
    # other effect passes that on to the ship.
    types = {1: ({48: 100}, []), 2: ({424: 5, 280: 0}, [8, 9])}
    effects = {8: [mod(0, 424, 280, domain="itemID")], 9: [mod(6, 48, 424)]}
    d = Dogma(Made(types, effects))
    d.add("ship", 1, "ship")
    d.add("skill", 2, "skill", level=3)
    d.link(["ship", "skill"])
    assert round(d.value("ship", 48), 6) == 115


def test_a_modifier_with_no_target_is_flagged_unless_repeated_with_one():
    types = {1: ({50: 10}, []), 2: ({1: 5}, [7])}
    lone = mod(6, 50, 1, func="LocationRequiredSkillModifier")
    sources = [("ship", 1, "ship"), ("m", 2, "module")]
    d, _ = evaluate(types, {7: [lone]}, sources, ("m", 50))
    assert d.unreadable == ["effect7 doesn't say what it applies to"]
    d, _ = evaluate(types, {7: [lone, {**lone, "skill_type_id": 9}]}, sources, ("m", 50))
    assert d.unreadable == []


def test_two_unstackable_multipliers_are_flagged_not_guessed():
    types = {1: ({48: 100}, []), 2: ({1: 1.1}, [7]), 3: ({1: 1.1}, [7])}
    effects = {7: [mod(4, 48, 1)]}
    d, _ = evaluate(types, effects, [("ship", 1, "ship"), ("a", 2, "module"),
                                     ("b", 3, "module")], ("ship", 48), stackable=False)
    assert d.unreadable == ["stacking penalties on attr48"]


def test_a_modifier_that_depends_on_itself_does_not_loop():
    types = {1: ({48: 100}, [7])}
    effects = {7: [mod(6, 48, 48)]}
    d, _ = evaluate(types, effects, [("ship", 1, "ship")], ("ship", 48))
    assert d.unreadable == ["a modifier that depends on itself"]


# ---------- the cache ----------

class Reply:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_game_data_is_fetched_once_then_kept_in_sqlite():
    calls = []
    body = {"type_id": 5, "group_id": 9, "volume": 2.5, "name": "x",
            "dogma_attributes": {"50": {"attribute_id": 50, "value": 8.0}},
            "dogma_effects": {"12": {"effect_id": 12, "is_default": False}}}
    original = fitdata.http.get
    fitdata.http.get = lambda url: calls.append(url) or Reply(body)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.sqlite"
            data = GameData(path)
            first = data.type(5)
            assert first == {"group_id": 9, "volume": 2.5, "attrs": {50: 8.0},
                             "effects": [12]}
            assert data.type(5) == first
            assert len(calls) == 1 and calls[0].endswith("/types/5")
            data.db.close()
            # A new connection to the same file still has it.
            data = GameData(path)
            assert data.type(5) == first and len(calls) == 1
            data.clear()
            data.type(5)
            assert len(calls) == 2
            data.db.close()
    finally:
        fitdata.http.get = original


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"{len(tests)} tests passed.")
