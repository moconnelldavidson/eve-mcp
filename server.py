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
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from eve_common import (esi_get, esi_get_all, find_id, load_tokens, names,
                        setting)

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
        "that matter. Don't announce tool calls or recite raw fields."
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
    (if any), current ship, and whether they're online."""
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

    return {
        "character": who,
        "online": online.get("online", False),
        "last_login": online.get("last_login"),
        "system": lookup[loc["solar_system_id"]],
        "security": sec(sys_info["security_status"]),
        "docked_at": docked,
        "ship": f"{ship['ship_name']} ({lookup[ship['ship_type_id']]})",
    }


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
    stations, and activity in the last hour (jumps, kills)."""
    system_id, proper = find_id(system_name, "systems")
    s = system(system_id)
    constellation = esi_get(f"/universe/constellations/{s['constellation_id']}/")
    region = esi_get(f"/universe/regions/{constellation['region_id']}/")
    kills = next((k for k in esi_get("/universe/system_kills/")
                  if k["system_id"] == system_id), {})
    jumps = next((j for j in esi_get("/universe/system_jumps/")
                  if j["system_id"] == system_id), {})
    return {
        "system": proper,
        "security": sec(s["security_status"]),
        "band": sec_band(s["security_status"]),
        "constellation": constellation["name"],
        "region": region["name"],
        "stations": len(s.get("stations", [])),
        "last_hour": {
            "jumps": jumps.get("ship_jumps", 0),
            "ship_kills": kills.get("ship_kills", 0),
            "pod_kills": kills.get("pod_kills", 0),
            "npc_kills": kills.get("npc_kills", 0),
        },
    }


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
    path = esi_get(f"/route/{origin_id}/{dest_id}/", params={"flag": preference})
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
