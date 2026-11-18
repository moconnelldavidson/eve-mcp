"""Static game data for the fitting check, kept in a SQLite file.

Types, dogma effects and dogma attributes come from EVE Ref
(ref-data.everef.net). ESI has the same data but leaves out which skill or
group a "modules requiring skill X" modifier points at, and the fitting
check can't work without that. Each record is fetched once and kept in
fitdata.sqlite, so a fit costs no requests once its items have been seen.
Once an hour the cache checks the game's build against EVE Ref's and empties
itself when EVE Ref has newer data (see freshness). To fetch afresh by hand,
delete the file or run server.py --clear-fit-cache.
"""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from eve_common import HERE, esi_get, http

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
        self.db.execute("CREATE TABLE IF NOT EXISTS meta "
                        "(key TEXT PRIMARY KEY, value TEXT)")

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

    def meta(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?",
                                  (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, **values):
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                                [(k, str(v)) for k, v in values.items()])
            self.db.commit()

    def versions(self):
        """(the game's build, the build EVE Ref's data comes from)."""
        r = http.get(f"{REF}/meta")
        r.raise_for_status()
        return str(esi_get("/status/")["server_version"]), str(r.json()["sde"]["version"])

    def freshness(self, now=None):
        """Keep the cache in step with the game, and say if it is behind.

        Once an hour this compares the game's build (ESI) with the build the
        cache was filled from (EVE Ref). When EVE Ref has newer data the cache
        is emptied and refills on demand. Right after a patch EVE Ref can lag
        the game by a while, and then the answer is a sentence saying so.
        Returns "" when all is well."""
        now = time.time() if now is None else now
        if now - float(self.meta("checked", 0)) < 3600:
            return self.meta("note", "")
        self.set_meta(checked=now)  # one try an hour, even when it fails
        try:
            game, ref = self.versions()
        except (httpx.HTTPError, KeyError, ValueError, RuntimeError):
            return self.meta("note", "")
        # A cache from before builds were recorded has no known age.
        if ref != self.meta("build"):
            self.clear()
        note = ("" if game == ref else
                f"The game data here is from build {ref} but the game is on "
                f"{game}, so figures for anything changed in that patch may be off.")
        self.set_meta(build=ref, note=note)
        return note
