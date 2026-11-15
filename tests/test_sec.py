"""Offline tests for the security-status helpers. No network, no sign-in.

Run from the project folder with the project's Python:

    .venv/bin/python tests/test_sec.py          (Linux/macOS)
    .venv\\Scripts\\python tests\\test_sec.py     (Windows)

Also works under pytest if you have it, but doesn't need it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import sec, sec_band  # noqa: E402


def test_sec_rounds_to_one_decimal():
    assert sec(0.9459) == 0.9
    assert sec(0.46) == 0.5
    assert sec(0.44) == 0.4
    assert sec(1.0) == 1.0
    assert sec(-0.37) == -0.4


def test_sec_just_above_zero_shows_as_point_one():
    assert sec(0.0001) == 0.1
    assert sec(0.04) == 0.1
    assert sec(0.049) == 0.1


def test_sec_zero_and_below_are_not_bumped():
    assert sec(0.0) == 0.0
    assert sec(-0.01) == 0.0
    assert sec(-1.0) == -1.0


def test_sec_band_boundaries():
    assert sec_band(1.0) == "highsec"
    assert sec_band(0.5) == "highsec"
    assert sec_band(0.46) == "highsec"      # shows as 0.5 in game
    assert sec_band(0.44) == "lowsec"       # shows as 0.4
    assert sec_band(0.1) == "lowsec"
    assert sec_band(0.01) == "lowsec"       # shows as 0.1, not 0.0
    assert sec_band(0.0) == "nullsec/wormhole"
    assert sec_band(-0.5) == "nullsec/wormhole"
    assert sec_band(-1.0) == "nullsec/wormhole"


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"{len(tests)} tests passed.")
