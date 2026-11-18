"""Does a pasted EFT fit fit? Used by the can_i_fly tool in server.py.

The work is done by a small dogma evaluator. It reads each item's effects
from CCP's data and applies their modifiers (skills, hull bonuses, rigs,
fitting modules) to a handful of fitting attributes, the way the game does.
Nothing here knows about any particular skill or module. The only fitting
rules written out by hand are the plain ones: slots, hardpoints,
maxGroupFitted, canFitShipGroup/Type, rig size and the drone bay.

Every ID below was checked against live ESI or EVE Ref data.
"""

import re
from collections import defaultdict

# On the ship:
CPU_OUTPUT, POWER_OUTPUT, CALIBRATION = 48, 11, 1132
SLOTS = {"high": 14, "mid": 13, "low": 12, "rig": 1137}
TURRETS, LAUNCHERS = 102, 101
DRONE_BANDWIDTH, DRONE_BAY = 1271, 283
SUBSYSTEMS = 1367  # only Strategic Cruisers have this
# On a module or rig:
CPU_NEED, POWER_NEED, RIG_COST = 50, 30, 1153
BANDWIDTH_USED, MAX_GROUP_FITTED, RIG_SIZE = 1272, 1544, 1547
CAN_FIT_GROUP = (1298, 1299, 1300, 1301, 1872, 1879, 1880, 1881, 2065, 2396,
                 *range(2476, 2486))
CAN_FIT_TYPE = (1302, 1303, 1304, 1305, 1944, 2103, 2463, 2486, 2487, 2488, 5948)
SKILL_LEVEL = 280  # set on each skill to the character's level
# Which slot a module takes, and which hardpoint, are effects it carries.
SLOT_EFFECTS = {"high": 12, "mid": 13, "low": 11, "rig": 2663}
TURRET_FITTED, LAUNCHER_FITTED = 42, 40
# The attributes holding the skills an item needs, in pairs with their levels.
REQUIRED_SKILLS = ((182, 277), (183, 278), (184, 279),
                   (1285, 1286), (1289, 1287), (1290, 1288))
# Dogma operators in the order the game applies them.
PRE_ASSIGN, PRE_MUL, PRE_DIV, MOD_ADD, MOD_SUB, POST_MUL, POST_DIV, \
    POST_PERCENT, POST_ASSIGN = range(-1, 8)

NOT_MODELLED = ("Not accounted for: implants and boosters, overheating, "
                "and how many drones can be launched.")


def eft_fit(text):
    """An EFT fit as (hull, sections), or None if the text isn't one.
    A section is a block between blank lines: low, mid, high, rig, then
    drones and cargo. It holds (name, count, offline) for each item, with
    empty slots and charges left out."""
    lines = [line.strip() for line in text.strip().splitlines()]
    if (not lines or not lines[0].startswith("[") or not lines[0].endswith("]")
            or lines[0].lower().startswith("[empty")):
        return None
    hull = lines[0][1:-1].split(",")[0].strip()
    sections, block = [], []
    for line in lines[1:] + [""]:
        if line:
            block.append(line)
        elif block:
            section = []
            for entry in block:
                if entry.lower().startswith("[empty"):
                    continue
                offline = entry.endswith("/offline")
                name = entry.split(",")[0].removesuffix("/offline").strip()
                count = re.search(r"\s+x(\d+)$", name)
                if count:
                    name = name[:count.start()]
                section.append((name, int(count.group(1)) if count else 1,
                                offline))
            sections.append(section)
            block = []
    return hull, sections


def num(x):
    """A figure the way the fitting window shows it: two decimals, no
    trailing zeros."""
    return f"{x:,.2f}".rstrip("0").rstrip(".")


class Dogma:
    """Just enough of the game's dogma to work out fitting attributes.

    Items are the ship, the character's skills and each fitted module or
    rig. The effects of the ship, the skills and every online module add
    modifiers to the items they point at. value() works an attribute out
    on demand, so a skill's bonus scaled by its level, or a penalty that a
    skill shrinks, come out of the same rule."""

    def __init__(self, data):
        self.data = data
        self.items = {}      # key -> {"attrs", "group", "requires", "kind"}
        self.fitted = []     # keys of modules and rigs
        self.skills = []     # keys of the character's skills
        self.mods = defaultdict(list)  # (key, attribute) -> [(operator, source, attribute)]
        self.unreadable = []  # what we couldn't apply, in words
        self.memo, self.busy = {}, set()

    def flag(self, reason):
        if reason not in self.unreadable:
            self.unreadable.append(reason)

    def add(self, key, type_id, kind, level=None):
        t = self.data.type(type_id)
        attrs = dict(t["attrs"])
        if level is not None:
            attrs[SKILL_LEVEL] = level
        self.items[key] = {
            "type": type_id, "kind": kind, "attrs": attrs,
            "group": t["group_id"], "effects": t["effects"],
            "requires": {int(attrs[s]) for s, _ in REQUIRED_SKILLS if s in attrs}}
        if kind in ("module", "rig"):
            self.fitted.append(key)
        elif kind == "skill":
            self.skills.append(key)

    def targets(self, m, source):
        """The items one modifier points at."""
        domain = {"itemID": source, "shipID": "ship",
                  "charID": "char"}.get(m["domain"])
        if domain not in self.items:
            return []
        if m["func"] == "ItemModifier":
            return [domain]
        pool = {"ship": self.fitted, "char": self.skills}.get(domain, [])
        if m["func"] == "LocationModifier":
            return list(pool)
        if m["func"] == "LocationGroupModifier":
            return [k for k in pool if self.items[k]["group"] == m["group_id"]]
        if m["func"] == "LocationRequiredSkillModifier":
            return [k for k in pool if m["skill_type_id"] in self.items[k]["requires"]]
        return []  # other funcs aim at drones, targets or other ships

    def link(self, sources):
        """Collect the modifiers from these items' effects."""
        for source in sources:
            item = self.items[source]
            for effect_id in item["effects"]:
                effect = self.data.effect(effect_id)
                # Passive effects always apply. Online ones are only linked
                # for online modules. Active ones (weapons, boosters) aren't fitting.
                if effect["category"] not in (0, 4):
                    continue
                for m in effect["modifiers"]:
                    needs = {"LocationRequiredSkillModifier": "skill_type_id",
                             "LocationGroupModifier": "group_id"}.get(m["func"])
                    if needs and needs not in m:
                        # The data often repeats a modifier once with its
                        # skill or group and once without. Only a lone one
                        # without is a hole.
                        if not any(needs in o and {**o, needs: 0} == {**m, needs: 0}
                                   for o in effect["modifiers"]):
                            self.flag(f"{effect['name']} doesn't say what it applies to")
                        continue
                    for target in self.targets(m, source):
                        self.mods[(target, m["modified_attribute_id"])].append(
                            (m["operator"], source, m["modifying_attribute_id"]))

    def value(self, key, attribute):
        """An attribute's final value on an item, or None if it has none."""
        k = (key, attribute)
        if k in self.memo:
            return self.memo[k]
        if k in self.busy:
            self.flag("a modifier that depends on itself")
            return None
        self.busy.add(k)
        item, mods = self.items[key], self.mods.get(k, [])
        base = item["attrs"].get(attribute)
        if base is None and mods:
            base = self.data.attribute(attribute)["default"]
        if base is not None:
            base = self.apply(base, k, item, mods)
        self.busy.discard(k)
        self.memo[k] = base
        return base

    def apply(self, base, k, item, mods):
        by_operator = defaultdict(list)
        for operator, source, attribute in mods:
            v = self.value(source, attribute)
            if v is not None:  # a missing attribute modifies nothing
                by_operator[operator].append((v, self.items[source]["kind"]))
        # Stacking penalties aren't modelled. Say so rather than be wrong.
        multiplying = [kind for op in (PRE_MUL, PRE_DIV, POST_MUL, POST_DIV, POST_PERCENT)
                       for _, kind in by_operator[op] if kind in ("module", "rig")]
        if len(multiplying) > 1 and not self.data.attribute(k[1])["stackable"]:
            self.flag(f"stacking penalties on {self.data.attribute(k[1])['name']}")
        values = {op: [v for v, _ in vs] for op, vs in by_operator.items()}
        if values.get(PRE_ASSIGN):
            base = values[PRE_ASSIGN][-1]
        for v in values.get(PRE_MUL, []):
            base *= v
        for v in values.get(PRE_DIV, []):
            base /= v or 1
        base += sum(values.get(MOD_ADD, [])) - sum(values.get(MOD_SUB, []))
        for v in values.get(POST_MUL, []):
            base *= v
        for v in values.get(POST_DIV, []):
            base /= v or 1
        for v in values.get(POST_PERCENT, []):
            base *= 1 + v / 100
        if values.get(POST_ASSIGN):
            base = values[POST_ASSIGN][-1]
        return base


def check_fit(fit, ids, levels, data):
    """Whether the items in an EFT fit (from eft_fit) fit the hull.
    ids maps a lowercase item name to its type ID, levels maps a skill's
    type ID to the character's level, and data is a fitdata.GameData.
    Anything that can't be worked out makes the verdict "unknown" or
    "probably", never "fits"."""
    hull_name, sections = fit
    hull_id = ids.get(hull_name.lower())
    if hull_id is None:
        return {"verdict": f"unknown: {hull_name} isn't an item EVE recognises",
                "not_modelled": NOT_MODELLED}
    dogma = Dogma(data)
    dogma.add("ship", hull_id, "ship")
    if SUBSYSTEMS in dogma.items["ship"]["attrs"]:
        return {"verdict": "unknown: Strategic Cruisers get their slots and "
                           "fitting from subsystems, which aren't checked yet",
                "not_modelled": NOT_MODELLED}

    data.preload(levels)
    dogma.items["char"] = {"kind": "char", "attrs": {}, "group": 0,
                           "effects": [], "requires": set()}
    for skill, level in levels.items():
        dogma.add(f"skill{skill}", skill, "skill", level)

    unknown, missing, not_found = [], [], []
    rows = []   # (slot, name, count, offline, keys)
    order = ("low", "mid", "high", "rig")  # the order EFT lists them in
    for slot, block in zip(order, sections):
        for name, count, offline in block:
            type_id = ids.get(name.lower())
            attrs = data.type(type_id)["attrs"] if type_id else {}
            # Costing no CPU or power is normal (the data then leaves the
            # attribute out), so only an item with no data at all is missing.
            if not attrs:
                missing.append(name)
                rows.append((slot, name, count, offline, []))
                continue
            if SLOT_EFFECTS[slot] not in data.type(type_id)["effects"]:
                unknown.append(f"{name} is listed as a {slot}-slot item but isn't one")
            keys = []
            for i in range(count):
                key = f"{slot}{len(rows)}.{i}"
                dogma.add(key, type_id, "rig" if slot == "rig" else "module")
                keys.append(key)
            rows.append((slot, name, count, offline, keys))
    # Offline modules fill a slot but their effects don't apply.
    dogma.link(["ship", *dogma.skills]
               + [k for _, _, _, offline, keys in rows if not offline for k in keys])

    def get(key, attribute):
        return dogma.value(key, attribute) or 0

    cpu_out, power_out = get("ship", CPU_OUTPUT), get("ship", POWER_OUTPUT)
    live = [k for _, _, _, offline, keys in rows if not offline for k in keys]
    cpu_used = sum(get(k, CPU_NEED) for k in live)
    power_used = sum(get(k, POWER_NEED) for k in live)
    calibration = sum(get(k, RIG_COST) for s, _, _, _, keys in rows
                      if s == "rig" for k in keys)
    slots_used = {s: sum(n for slot, _, n, _, _ in rows if slot == s) for s in SLOTS}
    turrets = sum(1 for *_, keys in rows for k in keys
                  if TURRET_FITTED in dogma.items[k]["effects"])
    launchers = sum(1 for *_, keys in rows for k in keys
                    if LAUNCHER_FITTED in dogma.items[k]["effects"])

    # The plain rules.
    broken = []
    ship_group = dogma.items["ship"]["group"]
    ship_size = dogma.value("ship", RIG_SIZE)
    in_group = defaultdict(int)
    for slot, name, _, _, keys in rows:
        for k in keys:
            in_group[dogma.items[k]["group"]] += 1
    for slot, name, count, _, keys in rows:
        if not keys:
            continue
        attrs = dogma.items[keys[0]]["attrs"]
        limit = attrs.get(MAX_GROUP_FITTED)
        group = dogma.items[keys[0]]["group"]
        if limit is not None and in_group[group] > limit:
            broken.append(f"{name}: {in_group[group]} fitted, only {num(limit)} allowed")
        groups = {int(attrs[a]) for a in CAN_FIT_GROUP if a in attrs}
        types = {int(attrs[a]) for a in CAN_FIT_TYPE if a in attrs}
        if (groups or types) and ship_group not in groups and hull_id not in types:
            broken.append(f"{name} can't be fitted to a {hull_name}")
        if (slot == "rig" and ship_size is not None
                and dogma.value(keys[0], RIG_SIZE) != ship_size):
            broken.append(f"{name} is the wrong rig size for a {hull_name}")

    # Drones. Only the first block after the rigs is the drone bay, and the
    # bay is limited by volume. Bandwidth only limits how many are launched.
    bay = bandwidth = 0
    for position, block in enumerate(sections[len(order):]):
        for name, count, _ in block:
            type_id = ids.get(name.lower())
            if type_id is None:
                not_found.append(name)
                continue
            t = data.type(type_id)
            if position == 0 and BANDWIDTH_USED in t["attrs"]:
                bay += count * t["volume"]
                bandwidth += count * t["attrs"][BANDWIDTH_USED]
    bay_limit, bandwidth_limit = get("ship", DRONE_BAY), get("ship", DRONE_BANDWIDTH)

    # Offline, empty or missing items only ever lower the totals, so say "at least".
    low = "at least " if missing else ""
    over, close = [], []
    for label, used, limit, unit in (("CPU", cpu_used, cpu_out, "tf"),
                                     ("Powergrid", power_used, power_out, "MW")):
        if used > limit:
            over.append(f"{label} is {num(used - limit)} {unit} over")
        elif used > limit * 0.95:
            close.append(label)
    counts = [("Turret hardpoints", turrets, get("ship", TURRETS)),
              ("Launcher hardpoints", launchers, get("ship", LAUNCHERS)),
              ("Calibration", calibration, get("ship", CALIBRATION)),
              ("Drone bay", bay, bay_limit)]
    counts += [(f"{s.title()} slots", slots_used[s], get("ship", SLOTS[s])) for s in SLOTS]
    hard = list(dict.fromkeys(broken)) + [f"{label}: {num(used)} / {num(limit)}"
                     for label, used, limit in counts if used > limit]

    reason = "; ".join(unknown + dogma.unreadable)
    soft = [] if unknown else over
    if hard:
        verdict = "does not fit: " + "; ".join(hard + soft)
    elif soft and dogma.unreadable:
        verdict = ("probably doesn't fit: " + "; ".join(soft)
                   + f". Some effects couldn't be read ({reason})")
    elif soft:
        verdict = "does not fit: " + "; ".join(soft)
    elif unknown or missing or dogma.unreadable:
        why = unknown + dogma.unreadable
        if missing:
            why.append("no fitting figures found for " + ", ".join(missing))
        verdict = "unknown: " + "; ".join(why)
    elif close:
        verdict = (f"too close to call: {' and '.join(close)} within 5% of "
                   "the limit. Check it in the game's fitting window.")
    else:
        verdict = "fits"

    result = {
        "verdict": verdict,
        "cpu": f"CPU {low}{num(cpu_used)} / {num(cpu_out)} tf",
        "powergrid": f"Powergrid {low}{num(power_used)} / {num(power_out)} MW",
        "calibration": f"Calibration {low}{num(calibration)} / {num(get('ship', CALIBRATION))}",
        "slots": ", ".join(f"{s.title()} {slots_used[s]} / {num(get('ship', SLOTS[s]))}"
                           for s in SLOTS),
        "hardpoints": (f"Turrets {turrets} / {num(get('ship', TURRETS))}, "
                       f"Launchers {launchers} / {num(get('ship', LAUNCHERS))}"),
        "drone_bay": f"Drone bay {num(bay)} / {num(bay_limit)} m3",
        "drone_bandwidth": (
            f"Drone bandwidth {num(bandwidth)} / {num(bandwidth_limit)} Mbit/s"
            + (" (not all can be launched at once)"
               if bandwidth > bandwidth_limit else "")),
        "not_modelled": NOT_MODELLED + (
            f" Could not be read, so figures may be off: {'; '.join(dogma.unreadable)}."
            if dogma.unreadable else ""),
    }
    if not_found:
        result["not_found"] = not_found
    return result
