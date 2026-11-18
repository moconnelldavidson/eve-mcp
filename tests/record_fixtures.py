"""Record the game data the fitting tests use, from EVE Ref, into
tests/fixtures/dogma.json. The tests then run offline.

Only needed when a test wants an item that isn't recorded yet. Add its name
to ITEMS or SKILLS and run, with the project's Python:

    .venv/bin/python tests/record_fixtures.py          (Linux/macOS)
    .venv\\Scripts\\python tests\\record_fixtures.py     (Windows)
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eve_common import esi_post  # noqa: E402
from fitdata import GameData  # noqa: E402

ITEMS = [
    "Pioneer", "Kestrel", "Buzzard", "Miner II", "Mining Laser Upgrade I",
    "Small F-S9 Regolith Compact Shield Extender",
    "Multispectrum Shield Hardener I", "1MN Y-S8 Compact Afterburner",
    "Small Core Defense Field Extender I", "Medium Core Defense Field Extender I",
    "Hobgoblin II", "Co-Processor I", "Micro Auxiliary Power Core I",
    "Small Ancillary Current Router I", "Small Processor Overclocking Unit I",
    "Light Missile Launcher II", "Covert Ops Cloaking Device II",
    "Core Probe Launcher II", "Damage Control II", "Nanite Repair Paste",
    "Tengu",
]
SKILLS = [
    "CPU Management", "Power Grid Management", "Mining Upgrades",
    "Shield Upgrades", "Weapon Upgrades", "Advanced Weapon Upgrades",
    "Mining", "Gunnery", "Missile Launcher Operation", "Mining Destroyer",
    "Covert Ops", "Caldari Frigate", "Cloaking",
]
OUT = Path(__file__).resolve().parent / "fixtures" / "dogma.json"


def main():
    wanted = ITEMS + SKILLS
    found = esi_post("/universe/ids/", wanted).get("inventory_types", [])
    ids = {r["name"].lower(): r["id"] for r in found}
    for name in wanted:
        if name.lower() not in ids:
            sys.exit(f"EVE doesn't know {name!r}")
    with tempfile.TemporaryDirectory() as tmp:
        data = GameData(Path(tmp) / "record.sqlite")
        data.preload(ids.values())
        attributes = set()
        for effect_id in {e for i in ids.values() for e in data.type(i)["effects"]}:
            for m in data.effect(effect_id)["modifiers"]:
                attributes |= {m["modified_attribute_id"], m["modifying_attribute_id"]}
        for attribute in attributes:
            data.attribute(attribute)
        rows = data.db.execute("SELECT kind, id, body FROM data").fetchall()
        data.db.close()
    out = {"ids": ids}
    for kind, record_id, body in rows:
        out.setdefault(kind, {})[str(record_id)] = json.loads(body)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))
    print(f"Wrote {OUT.name}: {len(ids)} items, {len(out['dogma_effects'])} "
          f"effects, {len(out['dogma_attributes'])} attributes.")


if __name__ == "__main__":
    main()
