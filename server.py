"""eve-mcp: a small MCP server for EVE Online.

Claude starts this script and talks to it over stdin/stdout. Each tool is
a plain function with the @mcp.tool decorator on it. The function name is
the tool's name, the type hints are its inputs, and the docstring is what
Claude reads to decide when to use it. So the tool docstrings are part of
the interface. Edit them with the same care as the code.

Never print() here. Stdout carries the conversation with Claude and a
stray line garbles it. To report a problem, raise an exception and Claude
sees the message.

    python server.py           run as an MCP server (Claude does this)
    python server.py --check   quick test in the terminal

Use the project's own Python for both: .venv/bin/python on Linux/macOS,
.venv\\Scripts\\python on Windows.
"""

import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from eve_common import (PATCH_NOTES, esi_get, esi_get_all, esi_post, find_id,
                        killmail, load_tokens, names, setting,
                        zkill_system_kills)

mcp = MCPServer(
    "eve",
    instructions=(
        "Live EVE Online data from CCP's ESI API: the user's own characters "
        "(skills, skill queue, wallet, location, assets) and public game data "
        "(market prices at trade hubs, item info, systems, routes). "
        "Character tools default to the user's main character. "
        "Use these tools for anything about the current state of the game or "
        "the user's characters, rather than answering from memory. "
        "Reply in ordinary conversational prose and mention only the findings "
        "that matter. Don't announce tool calls or recite raw fields. "
        "The game changes after your training data ends. If something looks "
        "unfamiliar or recently changed and you can browse, the patch notes "
        f"are an optional reference: {PATCH_NOTES}"
    ),
)

# Every tool only reads. Saying so lets a client skip its "allow this?" prompt.
READ_ONLY = ToolAnnotations(read_only_hint=True)

HUBS = {  # name: (region ID, station ID)
    "jita": (10000002, 60003760),     # Jita IV - Moon 4 - Caldari Navy Assembly Plant
    "amarr": (10000043, 60008494),
    "dodixie": (10000032, 60011866),
    "rens": (10000030, 60004588),
    "hek": (10000042, 60005686),
}


# ---------- helpers ----------

def character(name=None):
    """Pick a signed-in character by name, or the default one."""
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("No characters signed in yet. Run auth.py first.")
    wanted = name or setting("EVE_DEFAULT_CHARACTER")
    if wanted:
        for char_id, t in tokens.items():
            if t["name"].lower() == wanted.lower():
                return char_id, t["name"]
        if name:  # only complain if the caller asked for someone specific
            signed_in = ", ".join(t["name"] for t in tokens.values())
            raise ValueError(f"'{name}' isn't signed in. Signed in: {signed_in}")
    char_id, t = next(iter(tokens.items()))
    return char_id, t["name"]


def sec(status):
    """Security status as the game shows it. EVE rounds to one decimal,
    except that anything just above 0.0 shows as 0.1."""
    return 0.1 if 0 < status < 0.05 else round(status, 1)


def sec_band(status):
    s = sec(status)
    return "highsec" if s >= 0.5 else "lowsec" if s > 0 else "nullsec/wormhole"


_systems = {}


def system(system_id):
    if system_id not in _systems:
        _systems[system_id] = esi_get(f"/universe/systems/{system_id}/")
    return _systems[system_id]


def isk(amount):
    return f"{amount:,.2f} ISK"


_prices = {"fetched": 0, "by_type": {}}


def average_prices():
    """CCP's average price for every item type, as {type_id: ISK}."""
    # The list covers the whole game and runs to over a megabyte, so keep
    # it for an hour rather than fetch it on every assets call.
    if time.time() - _prices["fetched"] > 3600:
        _prices["by_type"] = {p["type_id"]: p.get("average_price", 0)
                              for p in esi_get("/markets/prices/")}
        _prices["fetched"] = time.time()
    return _prices["by_type"]


def hub_prices(type_id, hub):
    """One hub's prices for an item, plus its lowest sell price as a number."""
    region_id, station_id = HUBS[hub]
    orders = esi_get_all(f"/markets/{region_id}/orders/",
                         params={"type_id": type_id, "order_type": "all"})
    here = [o for o in orders if o["location_id"] == station_id]
    sells = [o for o in here if not o["is_buy_order"]]
    buys = [o for o in here if o["is_buy_order"]]
    try:
        history = esi_get(f"/markets/{region_id}/history/",
                          params={"type_id": type_id})[-30:]
    except httpx.HTTPStatusError:  # ESI refuses items that can't be sold
        history = []
    lowest = min(o["price"] for o in sells) if sells else None
    return {
        "hub": names([station_id])[station_id],
        "best_sell": isk(lowest) if sells else None,
        "best_buy": isk(max(o["price"] for o in buys)) if buys else None,
        "units_for_sale": sum(o["volume_remain"] for o in sells),
        "sell_orders": len(sells),
        "buy_orders": len(buys),
        "region_avg_30d": (isk(sum(d["average"] for d in history) / len(history))
                           if history else None),
    }, lowest


# How ESI says "this needs skill X at level Y": six pairs of dogma
# attributes, one holding the skill's type ID and one the level.
REQUIRED_SKILLS = ((182, 277), (183, 278), (184, 279),
                   (1285, 1286), (1289, 1287), (1290, 1288))
SKILL_RANK, PRIMARY, SECONDARY = 275, 180, 181
TRAINING_ATTRIBUTES = {164: "charisma", 165: "intelligence", 166: "memory",
                       167: "perception", 168: "willpower"}

_dogma = {}
_effects = {}


def dogma(type_id):
    """An item's dogma attributes as {attribute_id: value}."""
    if type_id not in _dogma:
        t = esi_get(f"/universe/types/{type_id}/")
        _dogma[type_id] = {a["attribute_id"]: a["value"]
                           for a in t.get("dogma_attributes", [])}
        _effects[type_id] = {e["effect_id"] for e in t.get("dogma_effects", [])}
    return _dogma[type_id]


def effect_ids(type_id):
    """The IDs of an item's dogma effects. They say which slot it takes
    and whether it uses a turret or launcher hardpoint."""
    dogma(type_id)
    return _effects[type_id]


def required_skills(type_id, found=None):
    """Every skill an item needs as {skill_id: level}, in training order."""
    found = {} if found is None else found
    attributes = dogma(type_id)
    for skill_attr, level_attr in REQUIRED_SKILLS:
        if skill_attr not in attributes:
            continue
        skill, level = int(attributes[skill_attr]), int(attributes[level_attr])
        if found.get(skill, 0) < level:
            # Skills have prerequisites of their own. Going into them first
            # leaves the dict in an order the skills can be trained in.
            required_skills(skill, found)
            found[skill] = max(found.get(skill, 0), level)
    return found


def sp_for_level(rank, level):
    return round(250 * rank * 32 ** ((level - 1) / 2)) if level > 0 else 0


def duration(minutes):
    days, rest = divmod(round(minutes), 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins}m" if hours else f"{mins}m"


def fit_items(text):
    """The item names in a single name, a list of names or an EFT fit."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("[empty"):
            continue
        if line.startswith("[") and line.endswith("]"):
            parts = [line[1:-1].split(",")[0]]  # "[Hull, fit name]"
        else:
            parts = line.split(",")  # "Module, Charge"
        for part in parts:
            part = part.strip().removesuffix("/offline").strip()
            part = re.sub(r"\s+x\d+$", "", part)  # "Hobgoblin II x5"
            if part and part.lower() not in (f.lower() for f in found):
                found.append(part)
    return found


# ---------- does it fit? ----------
# Every ID below was checked against a live ESI response.

CPU_OUTPUT, POWER_OUTPUT, CALIBRATION = 48, 11, 1132  # on the ship
SLOTS = {"high": 14, "mid": 13, "low": 12, "rig": 1137}  # on the ship
SLOT_EFFECTS = {"high": 12, "mid": 13, "low": 11, "rig": 2663}  # on a module
TURRETS, LAUNCHERS = 102, 101  # hardpoints on the ship
TURRET_FITTED, LAUNCHER_FITTED = 42, 40  # effects on a module
DRONE_BANDWIDTH, BANDWIDTH_USED = 1271, 1272
CPU_NEED, POWER_NEED, RIG_COST = 50, 30, 1153  # on a module
CPU_MULTIPLIER, POWER_MULTIPLIER, POWER_INCREASE = 202, 145, 549  # Co-Processor, RCU, power core
CPU_RIG, POWER_RIG = 424, 313  # percent added to the ship's output by a rig
MINING_PENALTY = 1082  # percent a Mining Laser Upgrade adds to miners' CPU
SUBSYSTEMS = 1367  # only Strategic Cruisers have this

# The skill type IDs a module can require, for the skills that cut its fitting.
GUNNERY, MISSILE_LAUNCHER_OPERATION, MINING, SHIELD_UPGRADES = 3300, 3319, 3386, 3425

# Skills that change fitting, with the attribute that holds their percent
# per level (negative when the skill makes things cheaper).
FITTING_SKILLS = {"CPU Management": CPU_RIG, "Power Grid Management": POWER_RIG,
                  "Mining Upgrades": 927, "Shield Upgrades": 323,
                  "Weapon Upgrades": 310}

NOT_MODELLED = (
    "Not accounted for: ship hull bonuses, implants and boosters, "
    "overheating, how many drones can be launched, other fitting skills, "
    "and any fitting module or rig except Co-Processors, power cores, "
    "reactor control and power diagnostic units, Processor Overclocking "
    "Units and Ancillary Current Routers.")


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


def check_fit(fit, items, levels):
    """Whether the modules in an EFT fit (from eft_fit) fit the hull.
    items maps a lowercase name to {"attrs": {id: value}, "effects": {id}}
    and levels maps a lowercase skill name to the character's level.
    Anything that can't be worked out makes the verdict "unknown", never
    "fits"."""
    hull_name, sections = fit
    hull = items.get(hull_name.lower())
    if hull is None or CPU_OUTPUT not in hull["attrs"]:
        return {"verdict": f"unknown: no fitting figures for {hull_name}",
                "not_modelled": NOT_MODELLED}
    ship = hull["attrs"]
    if SUBSYSTEMS in ship:
        return {"verdict": "unknown: Strategic Cruisers get their slots and "
                           "fitting from subsystems, which aren't modelled",
                "not_modelled": NOT_MODELLED}

    unknown, missing = [], []

    def note(reason):
        if reason not in unknown:
            unknown.append(reason)

    # Every listed module, rig and drone, with its figures if we have them.
    modules, drones = [], []   # (slot, name, count, offline, data or None)
    order = ("low", "mid", "high", "rig")  # the order EFT lists them in
    for slot, block in zip(order, sections):
        for name, count, offline in block:
            data = items.get(name.lower())
            needed = RIG_COST if slot == "rig" else None
            known = data is not None and (
                needed in data["attrs"] if needed
                else CPU_NEED in data["attrs"] or POWER_NEED in data["attrs"])
            if not known:
                missing.append(name)
                data = None
            elif data["effects"] and SLOT_EFFECTS[slot] not in data["effects"]:
                note(f"{name} is listed as a {slot}-slot item but isn't one")
            modules.append((slot, name, count, offline, data))
    for block in sections[len(order):]:  # drones and cargo
        for name, count, offline in block:
            data = items.get(name.lower())
            if data is None:
                missing.append(name)
            elif BANDWIDTH_USED in data["attrs"]:
                drones.append((count, data["attrs"][BANDWIDTH_USED]))

    def bonus(skill):
        """The fraction a skill gives at the character's level."""
        data = items.get(skill.lower())
        if data is None or FITTING_SKILLS[skill] not in data["attrs"]:
            note(f"no figures for the {skill} skill")
            return 0
        return (data["attrs"][FITTING_SKILLS[skill]]
                * levels.get(skill.lower(), 0) / 100)

    # Offline modules still fill a slot but add and cost nothing.
    live = [(count, d["attrs"]) for _, _, count, offline, d in modules
            if d and not offline]

    cpu_out = ship[CPU_OUTPUT]
    power_out = ship.get(POWER_OUTPUT, 0)
    # The game applies additions, then multipliers, then percentages.
    power_out += sum(n * a.get(POWER_INCREASE, 0) for n, a in live)
    for n, a in live:
        cpu_out *= a.get(CPU_MULTIPLIER, 1) ** n
        power_out *= a.get(POWER_MULTIPLIER, 1) ** n
    cpu_out *= 1 + bonus("CPU Management")
    power_out *= 1 + bonus("Power Grid Management")
    for n, a in live:
        cpu_out *= (1 + a.get(CPU_RIG, 0) / 100) ** n
        power_out *= (1 + a.get(POWER_RIG, 0) / 100) ** n

    # A Mining Laser Upgrade makes mining lasers cost more CPU. The Mining
    # Upgrades skill shrinks that penalty. It doesn't cut the upgrade's own CPU.
    mining_penalty = 1.0
    for n, a in live:
        if MINING_PENALTY in a:
            mining_penalty *= (1 + a[MINING_PENALTY] / 100
                               * (1 + bonus("Mining Upgrades"))) ** n

    cpu_used = power_used = 0.0
    for n, a in live:
        required = {int(a[skill]) for skill, _ in REQUIRED_SKILLS if skill in a}
        cpu, power = a.get(CPU_NEED, 0), a.get(POWER_NEED, 0)
        if required & {GUNNERY, MISSILE_LAUNCHER_OPERATION}:
            cpu *= 1 + bonus("Weapon Upgrades")
        if SHIELD_UPGRADES in required:
            power *= 1 + bonus("Shield Upgrades")
        if MINING in required:
            cpu *= mining_penalty
        cpu_used += n * cpu
        power_used += n * power

    calibration = sum(n * d["attrs"][RIG_COST] for s, _, n, _, d in modules
                      if d and s == "rig")
    bandwidth = sum(n * used for n, used in drones)
    turrets = sum(n for _, _, n, _, d in modules
                  if d and TURRET_FITTED in d["effects"])
    launchers = sum(n for _, _, n, _, d in modules
                    if d and LAUNCHER_FITTED in d["effects"])
    slots_used = {s: sum(n for slot, _, n, _, _ in modules if slot == s)
                  for s in SLOTS}

    # What we couldn't count only ever adds, so say "at least".
    low = "at least " if missing else ""
    over, close = [], []
    for label, used, limit, unit in (
            ("CPU", cpu_used, cpu_out, "tf"),
            ("Powergrid", power_used, power_out, "MW")):
        if used > limit:
            over.append(f"{label} is {num(used - limit)} {unit} over")
        elif used > limit * 0.95:
            close.append(label)
    exact = [("Calibration", calibration, ship.get(CALIBRATION, 0)),
             ("Drone bandwidth", bandwidth, ship.get(DRONE_BANDWIDTH, 0)),
             ("Turret hardpoints", turrets, ship.get(TURRETS, 0)),
             ("Launcher hardpoints", launchers, ship.get(LAUNCHERS, 0))]
    exact += [(f"{s.title()} slots", slots_used[s], ship.get(SLOTS[s], 0))
              for s in SLOTS]
    # CPU and powergrid depend on modelled bonuses, so an over there only
    # counts when nothing else is uncertain. The rest are plain counts.
    failures = [f"{label}: {num(used)} / {num(limit)}"
                for label, used, limit in exact if used > limit]
    if not unknown:
        failures += over

    if failures:
        verdict = "does not fit: " + "; ".join(failures)
    elif unknown or missing:
        why = list(unknown)
        if missing:
            why.append("no fitting figures found for " + ", ".join(missing))
        verdict = "unknown: " + "; ".join(why)
    elif close:
        verdict = (f"too close to call: {' and '.join(close)} within 5% of "
                   "the limit. Check it in the game's fitting window.")
    else:
        verdict = "fits"

    return {
        "verdict": verdict,
        "cpu": f"CPU {low}{num(cpu_used)} / {num(cpu_out)} tf",
        "powergrid": f"Powergrid {low}{num(power_used)} / {num(power_out)} MW",
        "calibration": (f"Calibration {low}{num(calibration)} / "
                        f"{num(ship.get(CALIBRATION, 0))}"),
        "slots": ", ".join(f"{s.title()} {slots_used[s]} / "
                           f"{num(ship.get(SLOTS[s], 0))}" for s in SLOTS),
        "hardpoints": (f"Turrets {low}{turrets} / {num(ship.get(TURRETS, 0))}, "
                       f"Launchers {low}{launchers} / "
                       f"{num(ship.get(LAUNCHERS, 0))}"),
        "drone_bandwidth": (f"Drone bandwidth {low}{num(bandwidth)} / "
                            f"{num(ship.get(DRONE_BANDWIDTH, 0))} Mbit/s"),
        "not_modelled": NOT_MODELLED,
    }


# Wormhole regions are named by class: A-R00001 is C1 space, B- is C2
# and so on. ESI has no field for the class, so the name is the way in.
WORMHOLE_CLASSES = {"A": "C1", "B": "C2", "C": "C3", "D": "C4", "E": "C5",
                    "F": "C6", "G": "Thera", "H": "C13 (shattered)",
                    "K": "Drifter"}


def is_wormhole(system_id):
    return 31_000_000 <= system_id < 32_000_000


def kill_time(kill):
    return datetime.fromisoformat(kill["killmail_time"].replace("Z", "+00:00"))


def player_kills(kills):
    """A zKillboard page without the NPC kills, newest first."""
    # Sleepers killing a ratter say nothing about who is hunting.
    return sorted((k for k in kills if not k.get("zkb", {}).get("npc")),
                  key=lambda k: k["killmail_id"], reverse=True)


def fill_details(kills, now, fetch=killmail, limit=30):
    """Add the time and attackers to recent kills that arrived without."""
    # zKillboard sometimes sends whole killmails and sometimes only an ID
    # and a hash. ESI has the rest. Only the last day needs it, so stop
    # at the first kill older than that.
    fetched = 0
    for kill in player_kills(kills):
        if "killmail_time" not in kill or "attackers" not in kill:
            if fetched >= limit:
                break
            try:
                kill.update(fetch(kill["killmail_id"], kill["zkb"]["hash"]))
            # RuntimeError is what ESI being down or rate-limiting looks
            # like once the retries are spent.
            except (httpx.HTTPError, KeyError, RuntimeError):
                break
            fetched += 1
        if now - kill_time(kill) > timedelta(hours=24):
            break
    return kills


def kill_summary(kills, now):
    """Count a week of zKillboard kills by age and by who did the killing.
    Attackers come back as [(corporation_id, kills)], busiest first. An
    hour or day count is "N+" when it is only a floor and None when it
    isn't known at all."""
    # zKillboard sends 200 at a time, so a full page means "at least".
    full_page = len(kills) >= 200
    kills = player_kills(kills)

    # Newest first, a count is only complete once a kill older than its
    # window turns up, or the list runs out. If a kill with no time gets
    # in the way first, the count is a floor ("30+"), or None if nothing
    # was counted at all. It must never fall back to zero, which would
    # read as "quiet".
    hour = day = 0
    hour_complete = day_complete = False
    for kill in kills:
        if "killmail_time" not in kill:
            break
        age = now - kill_time(kill)
        if age > timedelta(hours=24):
            hour_complete = day_complete = True
            break
        day += 1
        if age <= timedelta(hours=1):
            hour += 1
        else:
            hour_complete = True
    else:
        hour_complete = day_complete = True

    def count(n, complete):
        return n if complete else f"{n}+" if n else None

    attackers, detailed = Counter(), 0
    for kill in kills:
        if "attackers" not in kill:
            continue
        detailed += 1
        # NPCs have no character, and a corporation counts once per kill
        # however many of its pilots were on it.
        attackers.update({a["corporation_id"] for a in kill["attackers"]
                          if "character_id" in a and "corporation_id" in a})

    timed = [k for k in kills if "killmail_time" in k]
    return {
        "kills_last_hour": count(hour, hour_complete),
        "kills_last_24h": count(day, day_complete),
        "kills_last_7d": "200+" if full_page else len(kills),
        "last_kill": (kill_time(timed[0]).strftime("%Y-%m-%dT%H:%M:%SZ")
                      if timed else None),
        "attackers": attackers.most_common(3) if detailed or not kills else None,
        "attackers_from": detailed,
    }


# ---------- your characters ----------

@mcp.tool(annotations=READ_ONLY)
def list_characters() -> list[str]:
    """Names of the EVE characters signed in to this server."""
    return [t["name"] for t in load_tokens().values()]


@mcp.tool(annotations=READ_ONLY)
def get_skills(character_name: str | None = None,
               name_contains: str | None = None) -> dict:
    """A character's trained skills with levels and skill points, plus total
    and unallocated SP. Use name_contains to filter (e.g. "drone")."""
    char_id, who = character(character_name)
    data = esi_get(f"/characters/{char_id}/skills/", char_id)
    skill_names = names([s["skill_id"] for s in data["skills"]])
    skills = [
        {"skill": skill_names[s["skill_id"]],
         "level": s["trained_skill_level"],
         "sp": s["skillpoints_in_skill"]}
        for s in data["skills"]
    ]
    if name_contains:
        skills = [s for s in skills if name_contains.lower() in s["skill"].lower()]
    return {
        "character": who,
        "total_sp": data["total_sp"],
        "unallocated_sp": data.get("unallocated_sp", 0),
        "skills": sorted(skills, key=lambda s: s["skill"]),
    }


@mcp.tool(annotations=READ_ONLY)
def get_skill_queue(character_name: str | None = None) -> dict:
    """A character's skill queue in order, with finish times (UTC), and when
    the whole queue runs out. now_utc is the current time, for working out
    how long is left."""
    char_id, who = character(character_name)
    queue = sorted(esi_get(f"/characters/{char_id}/skillqueue/", char_id),
                   key=lambda q: q["queue_position"])
    skill_names = names([q["skill_id"] for q in queue])
    entries = [
        {"skill": f"{skill_names[q['skill_id']]} {q['finished_level']}",
         "finishes": q.get("finish_date")}
        for q in queue
    ]
    paused = bool(queue) and not queue[0].get("finish_date")
    return {
        "character": who,
        "now_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paused": paused,
        "queue_ends": entries[-1]["finishes"] if entries and not paused else None,
        "queue": entries,
    }


@mcp.tool(annotations=READ_ONLY)
def get_wallet(character_name: str | None = None,
               recent_entries: int = 10) -> dict:
    """A character's ISK balance and their most recent wallet journal
    entries (bounties, market sales, fees and so on)."""
    char_id, who = character(character_name)
    balance = esi_get(f"/characters/{char_id}/wallet/", char_id)
    journal = esi_get(f"/characters/{char_id}/wallet/journal/", char_id)
    return {
        "character": who,
        "balance": isk(balance),
        "recent": [
            {"date": j["date"],
             "type": j["ref_type"].replace("_", " "),
             "amount": isk(j.get("amount", 0)),
             "note": j.get("description", "")}
            for j in journal[:recent_entries]
        ],
    }


@mcp.tool(annotations=READ_ONLY)
def get_location(character_name: str | None = None) -> dict:
    """Where a character is right now: system, security, docked station
    (if any), current ship, and whether they're online. is_wormhole marks
    a wormhole system, where system_info has the recent kills."""
    char_id, who = character(character_name)
    loc = esi_get(f"/characters/{char_id}/location/", char_id)
    ship = esi_get(f"/characters/{char_id}/ship/", char_id)
    online = esi_get(f"/characters/{char_id}/online/", char_id)

    sys_info = system(loc["solar_system_id"])
    lookup = names([loc["solar_system_id"], ship["ship_type_id"],
                    *([loc["station_id"]] if "station_id" in loc else [])])
    if "station_id" in loc:
        docked = lookup[loc["station_id"]]
    elif "structure_id" in loc:
        docked = f"a player structure (ID {loc['structure_id']})"
    else:
        docked = None  # in space

    result = {
        "character": who,
        "online": online.get("online", False),
        "last_login": online.get("last_login"),
        "system": lookup[loc["solar_system_id"]],
        "security": sec(sys_info["security_status"]),
        "docked_at": docked,
        "ship": f"{ship['ship_name']} ({lookup[ship['ship_type_id']]})",
    }
    if is_wormhole(loc["solar_system_id"]):
        result["is_wormhole"] = True
    return result


@mcp.tool(annotations=READ_ONLY)
def get_assets(character_name: str | None = None,
               name_contains: str | None = None,
               max_rows: int = 100) -> dict:
    """A character's items, grouped by station or system, with what they're
    inside (a ship, a container) where relevant. Most valuable first, so
    max_rows keeps what matters. est_isk is an estimate from CCP's average
    prices, not a quote. Use name_contains to find something specific
    (e.g. "Buzzard", "probe")."""
    char_id, who = character(character_name)
    items = esi_get_all(f"/characters/{char_id}/assets/", char_id)
    by_id = {i["item_id"]: i for i in items}

    # Items inside ships or containers point at their parent item, not a
    # place. Climb up until we reach a real location.
    def top_location(item):
        seen = 0
        while item["location_type"] == "item" and item["location_id"] in by_id and seen < 10:
            item = by_id[item["location_id"]]
            seen += 1
        return item["location_id"]

    type_names = names({i["type_id"] for i in items})
    prices = average_prices()

    rows = defaultdict(lambda: [0, 0.0])  # (place, inside, item) -> [quantity, value]
    for i in items:
        item_name = type_names[i["type_id"]]
        if name_contains and name_contains.lower() not in item_name.lower():
            continue
        parent = by_id.get(i["location_id"])
        inside = type_names[parent["type_id"]] if parent else i["location_flag"]
        row = rows[(top_location(i), inside, item_name)]
        row[0] += i["quantity"]
        # A blueprint copy has the same type as the original, so the
        # average price would value it at far more than it is worth.
        if not i.get("is_blueprint_copy"):
            row[1] += i["quantity"] * prices.get(i["type_id"], 0)

    top = sorted(rows.items(), key=lambda r: (-r[1][1], r[0][2]))[:max(max_rows, 0)]
    place_names = names({place for (place, _, _), _ in top})
    locations = defaultdict(list)
    for (place, inside, item_name), (qty, value) in top:
        label = place_names[place]
        if label.startswith("#"):
            label = f"player structure {place}"
        locations[label].append({"item": item_name, "qty": qty, "in": inside,
                                 "est_isk": round(value)})

    return {"character": who, "rows": len(rows), "shown": len(top),
            "truncated": len(top) < len(rows),
            "est_total": isk(sum(value for _, value in rows.values())),
            "locations": locations}


@mcp.tool(annotations=READ_ONLY)
def can_i_fly(ship_or_fit: str, character_name: str | None = None) -> dict:
    """Whether a character has the skills for a ship, a module, or a whole
    fit pasted in EFT format (hull, modules, charges and drones all count).
    Lists each missing skill in training order, prerequisites included,
    with the level needed, the level held and how long it takes to train.
    Times assume an Omega clone. An Alpha trains at half speed, and the
    times are doubled when the character is visibly Alpha, but an Alpha
    can't always be told apart.

    Given a whole EFT fit it also checks that the fit fits, using the
    character's own skills: CPU, powergrid, calibration, slots, turret and
    launcher hardpoints and drone bandwidth, in a "fits" section with a
    verdict. Run this before suggesting any fit, and don't call a fit
    workable unless the verdict says "fits". "too close to call" means a
    figure is within 5% of its limit: say so and send the user to the
    game's fitting window. "unknown" means something couldn't be worked
    out. The "not_modelled" line lists what isn't counted, so mention it
    when it matters. For a single name there is no fitting check."""
    char_id, who = character(character_name)
    asked = fit_items(ship_or_fit)
    if not asked:
        raise ValueError("Give a ship or module name, or paste a fit.")
    resolved = esi_post("/universe/ids/", asked[:500]).get("inventory_types", [])
    if not resolved:
        raise ValueError("None of those are item names EVE recognises. "
                         "It needs exact names (capitals don't matter).")
    recognised = {r["name"].lower() for r in resolved}

    needed, wanted_by = {}, defaultdict(list)
    for item in resolved:
        for skill, level in required_skills(item["id"]).items():
            needed[skill] = max(needed.get(skill, 0), level)
            wanted_by[skill].append(item["name"])

    sheet = esi_get(f"/characters/{char_id}/skills/", char_id)
    held = {s["skill_id"]: s for s in sheet["skills"]}
    # ESI has no "is Omega" field. A skill trained higher than it is
    # active proves an Alpha. The lack of one proves nothing.
    alpha = any(s["active_skill_level"] < s["trained_skill_level"]
                for s in sheet["skills"])
    attributes = esi_get(f"/characters/{char_id}/attributes/", char_id)
    skill_names = names(list(needed))

    missing, total_minutes = [], 0
    for skill, level in needed.items():
        mine = held.get(skill, {})
        if mine.get("active_skill_level", 0) >= level:
            continue
        entry = {"skill": skill_names[skill], "need": level,
                 "have": mine.get("active_skill_level", 0)}
        if mine.get("trained_skill_level", 0) >= level:
            entry["note"] = "trained, but switched off on an Alpha clone"
        else:
            d = dogma(skill)
            sp = (sp_for_level(d[SKILL_RANK], level)
                  - mine.get("skillpoints_in_skill", 0))
            rate = (attributes[TRAINING_ATTRIBUTES[int(d[PRIMARY])]]
                    + attributes[TRAINING_ATTRIBUTES[int(d[SECONDARY])]] / 2)
            if alpha:
                rate /= 2
            total_minutes += sp / rate
            entry["train"] = duration(sp / rate)
        if len(resolved) > 1:
            entry["for"] = wanted_by[skill]
        missing.append(entry)

    result = {
        "character": who,
        "can_fly": not missing,
        "skills_checked": len(needed),
        "missing": missing,
        "total_training": duration(total_minutes) if total_minutes else None,
    }
    if missing:
        result["training_speed"] = ("Alpha, half speed" if alpha
                                    else "Omega assumed")
    unknown = [name for name in asked if name.lower() not in recognised]
    if unknown:
        result["not_recognised"] = unknown
    fit = eft_fit(ship_or_fit)
    if fit:
        skill_ids = {r["name"]: r["id"] for r in esi_post(
            "/universe/ids/", list(FITTING_SKILLS)).get("inventory_types", [])}
        items = {r["name"].lower(): {"attrs": dogma(r["id"]),
                                     "effects": effect_ids(r["id"])}
                 for r in resolved}
        items.update({name.lower(): {"attrs": dogma(skill), "effects": set()}
                      for name, skill in skill_ids.items()})
        levels = {name.lower(): held.get(skill, {}).get("active_skill_level", 0)
                  for name, skill in skill_ids.items()}
        result["fits"] = check_fit(fit, items, levels)
    return result


# ---------- public game data ----------

@mcp.tool(annotations=READ_ONLY)
def market_price(item_name: str, hub: str = "Jita") -> dict:
    """Best sell and buy prices for an item at a trade hub station
    (Jita, Amarr, Dodixie, Rens or Hek), with the region's 30-day average
    price for comparison. hub="all" compares the five hubs and names the
    cheapest. Needs the exact item name."""
    hub = hub.lower()
    if hub != "all" and hub not in HUBS:
        raise ValueError("Hub must be one of: "
                         f"{', '.join(h.title() for h in HUBS)}, or all")
    type_id, proper = find_id(item_name, "inventory_types")
    if hub != "all":
        return {"item": proper, **hub_prices(type_id, hub)[0]}
    hubs = [hub_prices(type_id, h) for h in HUBS]
    selling = [(lowest, prices["hub"]) for prices, lowest in hubs if lowest]
    return {
        "item": proper,
        "cheapest_sell": min(selling)[1] if selling else None,
        "hubs": [prices for prices, _ in hubs],
    }


@mcp.tool(annotations=READ_ONLY)
def item_info(item_name: str) -> dict:
    """What an item, ship or module is: its group, size and in-game
    description. Needs the exact name."""
    type_id, proper = find_id(item_name, "inventory_types")
    t = esi_get(f"/universe/types/{type_id}/")
    group = esi_get(f"/universe/groups/{t['group_id']}/")
    description = re.sub(r"<[^>]+>", "", t.get("description", "")).strip()
    return {
        "item": proper,
        "type_id": type_id,
        "group": group["name"],
        "volume_m3": t.get("volume"),
        "packaged_volume_m3": t.get("packaged_volume"),
        "description": description[:1500],
    }


@mcp.tool(annotations=READ_ONLY)
def system_info(system_name: str) -> dict:
    """A solar system's security, constellation, region, number of
    stations, and activity in the last hour (jumps, kills). CCP publishes
    no activity for wormhole systems. For those it gives the wormhole
    class and zKillboard's player kills instead: last hour, day and week,
    and the corporations behind the most recent ones, who are often the
    residents. There, a count like "30+" means at least that many, and a
    null count means unknown, not zero."""
    system_id, proper = find_id(system_name, "systems")
    s = system(system_id)
    constellation = esi_get(f"/universe/constellations/{s['constellation_id']}/")
    region = esi_get(f"/universe/regions/{constellation['region_id']}/")
    result = {
        "system": proper,
        "security": sec(s["security_status"]),
        "band": sec_band(s["security_status"]),
        "constellation": constellation["name"],
        "region": region["name"],
        "stations": len(s.get("stations", [])),
    }
    # Wormhole systems are never in CCP's kill or jump lists. Reporting
    # that as zero would read as "quiet", which is the last thing to tell
    # someone sitting in one.
    if is_wormhole(system_id):
        result["class"] = WORMHOLE_CLASSES.get(region["name"][:1])
        result["last_hour"] = None
        now = datetime.now(timezone.utc)
        try:
            summary = kill_summary(
                fill_details(zkill_system_kills(system_id), now), now)
        except (httpx.HTTPError, ValueError):
            result["note"] = (
                "CCP publishes no jump or kill figures for wormhole space "
                "and zKillboard didn't answer, so don't assume it is quiet. "
                f"Recent kills: https://zkillboard.com/system/{system_id}/ . "
                "Statics and effects: anoik.is.")
            return result
        ranked = summary.pop("attackers")
        counted = summary.pop("attackers_from")
        if ranked is not None:
            corporations = names([corp for corp, _ in ranked])
            summary["recent_attackers"] = [
                {"corporation": corporations[corp], "kills": count}
                for corp, count in ranked]
            summary["recent_attackers_from_kills"] = counted
        else:
            summary["recent_attackers"] = None
        result["zkillboard"] = summary
        result["note"] = (
            "CCP publishes no figures for wormhole space. These are player "
            "kills recorded by zKillboard. \"N+\" means at least N, null "
            "means unknown, not zero, and no kills doesn't mean nobody is "
            "home. Statics and effects: anoik.is.")
        return result
    kills = next((k for k in esi_get("/universe/system_kills/")
                  if k["system_id"] == system_id), {})
    jumps = next((j for j in esi_get("/universe/system_jumps/")
                  if j["system_id"] == system_id), {})
    result["last_hour"] = {
        "jumps": jumps.get("ship_jumps", 0),
        "ship_kills": kills.get("ship_kills", 0),
        "pod_kills": kills.get("pod_kills", 0),
        "npc_kills": kills.get("npc_kills", 0),
    }
    return result


@mcp.tool(annotations=READ_ONLY)
def route(origin: str, destination: str, preference: str = "shortest") -> dict:
    """The gate route between two systems, with each system's security.
    preference: "shortest", "secure" (avoid low/nullsec) or "insecure".
    Systems where ships died in the last hour say how many, which is the
    nearest thing to a gate camp warning. Doesn't know about wormholes."""
    if preference not in ("shortest", "secure", "insecure"):
        raise ValueError('preference must be "shortest", "secure" or "insecure"')
    origin_id, origin_name = find_id(origin, "systems")
    dest_id, dest_name = find_id(destination, "systems")
    for system_id, name in ((origin_id, origin_name), (dest_id, dest_name)):
        if is_wormhole(system_id):
            raise ValueError(
                f"{name} is a wormhole system and gates don't go there. "
                "The way in or out is a wormhole, which no route can plan. "
                "system_info has what is known about the system.")
    try:
        path = esi_get(f"/route/{origin_id}/{dest_id}/",
                       params={"flag": preference})
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        raise ValueError(f"There is no {preference} gate route from "
                         f"{origin_name} to {dest_name}.") from None
    system_names = names(path)
    kills = {k["system_id"]: k["ship_kills"]
             for k in esi_get("/universe/system_kills/")}
    stops = []
    for sid in path:
        status = system(sid)["security_status"]
        stop = {"system": system_names[sid], "security": sec(status),
                "band": sec_band(status)}
        if kills.get(sid):
            stop["ship_kills_last_hour"] = kills[sid]
        stops.append(stop)
    return {
        "from": origin_name,
        "to": dest_name,
        "jumps": len(path) - 1,
        "lowsec_or_worse": sum(1 for s in stops if s["band"] != "highsec"),
        "ship_kills_last_hour": sum(kills.get(sid, 0) for sid in path),
        "route": stops,
    }


# ---------- run ----------

def check():
    """Try a few tools in the terminal, where a failure is easier to read
    than it is inside Claude."""
    print("Signed in:", list_characters() or "nobody yet (run auth.py)")
    print("Jita:", system_info("Jita")["security"], "security")
    if load_tokens():
        loc = get_location()
        print(f"{loc['character']}: in {loc['system']}, flying {loc['ship']}")
        q = get_skill_queue()
        print("Queue ends:", q["queue_ends"] or "(empty or paused)")
        a = get_assets(max_rows=5)
        print(f"Assets: {a['rows']} rows across {len(a['locations'])}+ locations")
    print("o7")


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        mcp.run()
