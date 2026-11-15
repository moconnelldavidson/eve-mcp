"""Offline tests for the fit parser and the training-time sums.

Run from the project folder with the project's Python:

    .venv/bin/python tests/test_fit.py          (Linux/macOS)
    .venv\\Scripts\\python tests\\test_fit.py     (Windows)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"{len(tests)} tests passed.")
