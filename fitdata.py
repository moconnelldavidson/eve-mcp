"""Static game data for the fitting check, kept in a SQLite file.

Types, dogma effects and dogma attributes come from EVE Ref
(ref-data.everef.net). ESI has the same data but leaves out which skill or
group a "modules requiring skill X" modifier points at, and the fitting
check can't work without that. Each record is fetched once and kept in
fitdata.sqlite, so a fit costs no requests once its items have been seen.
Delete the file (or run server.py --clear-fit-cache) to fetch afresh.
"""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from eve_common import HERE, http

REF = "https://ref-data.everef.net"
DB_PATH = HERE / "fitdata.sqlite"


def trim_type(t):
    return {"group_id": t["group_id"], "volume": t.get("volume", 0),
            "attrs": [[int(k), v["value"]]
                      for k, v in t.get("dogma_attributes", {}).items()],
            "effects": [int(k) for k in t.get("dogma_effects", {})]}


def trim_effect(e):
    return {"name": e.get("name") or e.get("effect_name") or f"effect {e['effect_id']}",
            "category": e.get("effect_category", 0),
            "modifiers": e.get("modifiers", [])}


def trim_attribute(a):
    return {"name": a["name"], "default": a.get("default_value") or 0,
            "stackable": a.get("stackable", True)}


KINDS = {"types": trim_type, "dogma_effects": trim_effect,
         "dogma_attributes": trim_attribute}


class GameData:
    """type(id), effect(id) and attribute(id), from the cache or EVE Ref."""

    def __init__(self, path=DB_PATH):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS data "
                        "(kind TEXT, id INTEGER, body TEXT, PRIMARY KEY (kind, id))")

    def get(self, kind, record_id):
        with self.lock:
            row = self.db.execute("SELECT body FROM data WHERE kind = ? AND id = ?",
                                  (kind, record_id)).fetchone()
        if row:
            return json.loads(row[0])
        r = http.get(f"{REF}/{kind}/{record_id}")
        r.raise_for_status()
        record = KINDS[kind](r.json())
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO data VALUES (?, ?, ?)",
                            (kind, record_id, json.dumps(record)))
            self.db.commit()
        return record

    def type(self, type_id):
        t = self.get("types", type_id)
        return {**t, "attrs": {k: v for k, v in t["attrs"]}}

    def effect(self, effect_id):
        return self.get("dogma_effects", effect_id)

    def attribute(self, attribute_id):
        return self.get("dogma_attributes", attribute_id)

    def preload(self, type_ids):
        """Fetch many types and their effects at once. A character's whole
        skill list is a couple of hundred records the first time."""
        with ThreadPoolExecutor(8) as pool:
            types = list(pool.map(self.type, set(type_ids)))
            effects = {e for t in types for e in t["effects"]}
            list(pool.map(self.effect, effects))

    def clear(self):
        with self.lock:
            self.db.execute("DELETE FROM data")
            self.db.commit()
