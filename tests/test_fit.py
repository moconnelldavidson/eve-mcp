"""Offline tests for the fit parser, the fitting check and the training-time
sums.

Run from the project folder with the project's Python:

    .venv/bin/python tests/test_fit.py          (Linux/macOS)
    .venv\\Scripts\\python tests\\test_fit.py     (Windows)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import (check_fit, duration, eft_fit, fit_items,  # noqa: E402
                    sp_for_level)

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
# Hand-written stand-ins for ESI's dogma data, with the Pioneer's real
# figures. Attribute IDs as in server.py: 48 CPU, 11 powergrid, 50 and 30
# a module's CPU and power, 182 and 277 its first required skill.

def thing(attrs, effects=()):
    return {"attrs": attrs, "effects": set(effects)}


ITEMS = {
    "pioneer": thing({48: 320, 11: 50, 1132: 400, 14: 4, 13: 3, 12: 2,
                      1137: 3, 102: 3, 101: 0, 1271: 20}),
    "miner ii": thing({50: 80, 30: 4, 182: 3386, 277: 4}, {12, 42}),
    "mining laser upgrade i": thing({50: 30, 30: 1, 1082: 10, 182: 22578}, {11}),
    "small f-s9 regolith compact shield extender":
        thing({50: 16, 30: 2, 182: 3425}, {13}),
    "multispectrum shield hardener i": thing({50: 40, 30: 1}, {13}),
    "1mn y-s8 compact afterburner": thing({50: 13, 30: 9}, {13}),
    "small core defense field extender i": thing({1153: 50}, {2663}),
    "hobgoblin ii": thing({1272: 5}),
    "co-processor i": thing({50: 0, 30: 1, 202: 1.08}, {11}),
    "micro auxiliary power core i": thing({50: 15, 30: 0, 549: 10}, {11}),
    "small ancillary current router i": thing({1153: 100, 313: 10}, {2663}),
    "light missile launcher ii": thing({50: 24, 30: 7, 182: 3319}, {12, 40}),
    # The skills, with their percent per level.
    "cpu management": thing({424: 5}),
    "power grid management": thing({313: 5}),
    "mining upgrades": thing({927: -5}),
    "shield upgrades": thing({323: -5}),
    "weapon upgrades": thing({310: -5}),
}
MAXED = {"cpu management": 5, "power grid management": 5,
         "mining upgrades": 5, "shield upgrades": 5, "weapon upgrades": 5}


def pioneer(miners=3, upgrades=2, extender=True, offline=""):
    """The Pioneer fit from the README, with the parts varied."""
    lows = ["Mining Laser Upgrade I"] * upgrades or ["[Empty Low slot]"]
    mids = ["Multispectrum Shield Hardener I", "1MN Y-S8 Compact Afterburner"]
    if extender:
        mids.insert(0, "Small F-S9 Regolith Compact Shield Extender")
    highs = ["Miner II" + offline] * miners
    return eft_fit("\n".join([
        "[Pioneer, test]", *lows, "", *mids, "", *highs, "",
        *["Small Core Defense Field Extender I"] * 3, "", "Hobgoblin II x2"]))


def test_eft_fit_keeps_sections_counts_and_offline():
    hull, sections = eft_fit(FIT)
    assert hull == "Tristan"
    assert [len(s) for s in sections] == [2, 2, 2, 2]
    assert sections[0] == [("Drone Damage Amplifier II", 1, False),
                           ("Small Ancillary Armor Repairer", 1, False)]
    assert sections[1][1] == ("Warp Scrambler II", 1, True)
    assert sections[2][0] == ("125mm Railgun II", 1, False)
    assert sections[3] == [("Hobgoblin II", 5, False),
                           ("Nanite Repair Paste", 20, False)]


def test_eft_fit_is_none_for_a_plain_name_or_list():
    assert eft_fit("Stratios") is None
    assert eft_fit("Stratios, Miner II") is None
    assert eft_fit("") is None


def test_fit_that_fits():
    # One upgrade at Mining Upgrades V adds 7.5% to the miner's 80 tf: 86.
    fit = eft_fit("[Pioneer, ok]\nMining Laser Upgrade I\n\n"
                  "1MN Y-S8 Compact Afterburner\n\nMiner II\n\n"
                  "Small Core Defense Field Extender I\n\nHobgoblin II x2")
    r = check_fit(fit, ITEMS, MAXED)
    assert r["verdict"] == "fits"
    assert r["cpu"] == "CPU 129 / 400 tf"               # 30 + 86 + 13
    assert r["powergrid"] == "Powergrid 14 / 62.5 MW"   # 1 + 9 + 4
    assert r["calibration"] == "Calibration 50 / 400"
    assert r["drone_bandwidth"] == "Drone bandwidth 10 / 20 Mbit/s"
    assert r["slots"] == "High 1 / 4, Mid 1 / 3, Low 1 / 2, Rig 1 / 3"


def test_over_on_cpu():
    # The real fit, with Mining Upgrades I. Each upgrade adds 9.5% to
    # miner CPU: 3 x 80 x 1.095 x 1.095 = 287.77, plus 60 + 16 + 40 + 13.
    r = check_fit(pioneer(), ITEMS, {**MAXED, "mining upgrades": 1})
    assert r["cpu"] == "CPU 416.77 / 400 tf"
    assert r["verdict"] == "does not fit: CPU is 16.77 tf over"


def test_within_five_percent_is_too_close_to_call():
    # Mining Upgrades V, no extender: 3 x 80 x 1.075^2 = 277.35, plus
    # 60 + 40 + 13 = 390.35 of 400.
    r = check_fit(pioneer(extender=False), ITEMS, MAXED)
    assert r["cpu"] == "CPU 390.35 / 400 tf"
    assert r["verdict"].startswith("too close to call: CPU")
    assert "fitting window" in r["verdict"]


def test_offline_module_costs_nothing_but_keeps_its_slot():
    # With the miners offline only the upgrades (60), extender (16),
    # hardener (40) and afterburner (13) are left.
    r = check_fit(pioneer(offline=" /offline"), ITEMS,
                  {**MAXED, "mining upgrades": 1})
    assert r["verdict"] == "fits"
    assert r["cpu"] == "CPU 129 / 400 tf"
    assert r["hardpoints"].startswith("Turrets 3 / 3")
    assert r["slots"].startswith("High 3 / 4")


def test_too_many_turrets():
    r = check_fit(pioneer(miners=4, upgrades=1), ITEMS, MAXED)
    assert r["verdict"].startswith("does not fit: Turret hardpoints: 4 / 3")
    assert r["hardpoints"] == "Turrets 4 / 3, Launchers 0 / 0"


def test_launcher_on_a_ship_with_no_launcher_hardpoints():
    fit = eft_fit("[Pioneer, x]\n[Empty Low slot]\n\n[Empty Med slot]\n\n"
                  "Light Missile Launcher II")
    r = check_fit(fit, ITEMS, MAXED)
    assert "Launcher hardpoints: 1 / 0" in r["verdict"]


def test_a_module_we_know_nothing_about_is_unknown_never_fits():
    fit = eft_fit("[Pioneer, x]\nMining Laser Upgrade I\n\n"
                  "[Empty Med slot]\n\nMiner II\nMystery Gun")
    r = check_fit(fit, ITEMS, MAXED)
    assert r["verdict"].startswith("unknown:")
    assert "Mystery Gun" in r["verdict"]
    assert "at least" in r["cpu"]


def test_missing_skill_data_is_unknown():
    items = {k: v for k, v in ITEMS.items() if k != "cpu management"}
    r = check_fit(pioneer(upgrades=1, miners=1), items, MAXED)
    assert r["verdict"].startswith("unknown:")
    assert "CPU Management" in r["verdict"]


def test_flat_bonuses_add_up():
    # Co-Processor I: 320 x 1.08 x 1.25 = 432. The power core adds 10
    # before the skill, (50 + 10) x 1.25 = 75, and the router rig 10%.
    fit = eft_fit("[Pioneer, x]\nCo-Processor I\nMicro Auxiliary Power Core I\n\n"
                  "[Empty Med slot]\n\n[Empty High slot]\n\n"
                  "Small Ancillary Current Router I")
    r = check_fit(fit, ITEMS, {"cpu management": 5, "power grid management": 5})
    assert r["cpu"] == "CPU 15 / 432 tf"
    assert r["powergrid"] == "Powergrid 1 / 82.5 MW"


def test_skills_cut_what_they_should():
    # Shield Upgrades V cuts the extender's 2 MW by 25%. Weapon Upgrades V
    # cuts the launcher's 24 tf by 25%, as it requires Missile Launcher Operation.
    fit = eft_fit("[Pioneer, x]\n[Empty Low slot]\n\n"
                  "Small F-S9 Regolith Compact Shield Extender\n\n"
                  "Light Missile Launcher II")
    r = check_fit(fit, ITEMS, MAXED)
    assert r["cpu"] == "CPU 34 / 400 tf"                # 16 + 24 x 0.75
    assert r["powergrid"] == "Powergrid 8.5 / 62.5 MW"  # 2 x 0.75 + 7


def test_a_module_in_the_wrong_section_is_unknown():
    fit = eft_fit("[Pioneer, x]\nMiner II")  # a high-slot module in the lows
    r = check_fit(fit, ITEMS, MAXED)
    assert r["verdict"].startswith("unknown:")


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"{len(tests)} tests passed.")
