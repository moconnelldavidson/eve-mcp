"""Shared plumbing for eve-mcp: settings, sign-in tokens and ESI requests.

server.py (the MCP server) and auth.py (the one-off login) both import this.
Nothing in here knows about MCP; it's just "talk to EVE's API".
"""

import base64
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

try:
    import fcntl  # Linux/Mac only
except ImportError:  # Windows: no cross-process lock, same as before
    fcntl = None

# httpx logs every request; that's noise in Claude's MCP logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / ".env"
TOKENS_PATH = HERE / "tokens.json"

ESI = "https://esi.evetech.net/latest"
SSO_AUTHORIZE = "https://login.eveonline.com/v2/oauth/authorize/"
SSO_TOKEN = "https://login.eveonline.com/v2/oauth/token"

# What we ask permission for. Each one must also be ticked on your app at
# developers.eveonline.com, or the login page will refuse.
SCOPES = [
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-wallet.read_character_wallet.v1",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-location.read_online.v1",
    "esi-assets.read_assets.v1",
]


# ---------- settings (.env) ----------

def load_env():
    """Read KEY=VALUE lines from .env. A tiny parser so we need no extra
    package. 'utf-8-sig' copes with the invisible marker Notepad can add
    to the start of a file."""
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = load_env()


def setting(key, default=None):
    """A setting from .env, or from a real environment variable."""
    return ENV.get(key) or os.environ.get(key) or default


def require(key):
    value = setting(key)
    if not value:
        raise RuntimeError(f"{key} is missing. Copy .env.example to .env "
                           f"and fill it in ({ENV_PATH}).")
    return value


# ---------- HTTP ----------

http = httpx.Client(
    timeout=30,
    headers={"User-Agent": setting("EVE_USER_AGENT", "eve-mcp (personal use)")},
)


def token_request(data):
    """Ask EVE's login server for tokens. Used both for the first login
    (grant_type=authorization_code) and for renewals (refresh_token)."""
    raw = f"{require('EVE_CLIENT_ID')}:{require('EVE_CLIENT_SECRET')}".encode()
    r = http.post(
        SSO_TOKEN,
        data=data,
        headers={"Authorization": "Basic " + base64.b64encode(raw).decode()},
    )
    r.raise_for_status()
    return r.json()


def decode_jwt(token):
    """Read the middle part of EVE's access token, which says who it's for.
    We just got it from EVE over HTTPS, so we don't check its signature."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


# ---------- tokens (tokens.json) ----------

_lock = threading.Lock()


def load_tokens():
    if not TOKENS_PATH.exists():
        return {}
    return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))


def save_tokens(tokens):
    # Write to a temp file then swap, so a crash can't leave half a file.
    # The temp file is created readable by the owner only (mode 600), so
    # the swap never loosens tokens.json. Windows ignores the mode.
    tmp = TOKENS_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)  # in case a leftover temp file had looser bits
        f.write(json.dumps(tokens, indent=2))
    tmp.replace(TOKENS_PATH)


@contextmanager
def _file_lock():
    """Stop two separate programs (say, two Claude sessions each running
    this server) from refreshing the token at the same moment. EVE may rotate the refresh
    token on each use, so the loser would save a dead one. flock waits
    until the other program is done, and lets go if we crash."""
    if fcntl is None:
        yield
        return
    with open(TOKENS_PATH.with_suffix(".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def access_token(char_id):
    """A working access token for this character. They last 20 minutes;
    when one is close to expiring we swap the refresh token for a new pair.
    EVE may hand back a new refresh token too, so we always save it."""
    with _lock, _file_lock():
        # Read inside the lock: if another program just refreshed, we
        # see its fresh token and don't refresh again.
        tokens = load_tokens()
        t = tokens[char_id]
        if t.get("access_token") and t.get("expires_at", 0) > time.time() + 60:
            return t["access_token"]
        resp = token_request({"grant_type": "refresh_token",
                              "refresh_token": t["refresh_token"]})
        t["access_token"] = resp["access_token"]
        t["expires_at"] = time.time() + int(resp.get("expires_in", 1199))
        if resp.get("refresh_token"):
            t["refresh_token"] = resp["refresh_token"]
        save_tokens(tokens)
        return t["access_token"]


# ---------- ESI ----------

def _get(path, char_id=None, params=None):
    headers = {}
    if char_id:
        headers["Authorization"] = f"Bearer {access_token(char_id)}"
    params = {"datasource": "tranquility", **(params or {})}
    for attempt in range(3):
        r = http.get(f"{ESI}{path}", headers=headers, params=params)
        # ESI blocks you for a while if you rack up errors; ease off early.
        remain = r.headers.get("X-ESI-Error-Limit-Remain")
        if remain is not None and int(remain) < 10:
            time.sleep(int(r.headers.get("X-ESI-Error-Limit-Reset", 60)))
        if r.status_code in (502, 503, 504) and attempt < 2:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 403 and char_id:
            raise RuntimeError(
                f"EVE refused {path} (403). The sign-in probably lacks the "
                "permission for it: tick it on your app, then run auth.py again.")
        r.raise_for_status()
        return r
    r.raise_for_status()


def esi_get(path, char_id=None, params=None):
    """GET one ESI endpoint. Pass char_id for private (signed-in) data."""
    return _get(path, char_id, params).json()


def esi_get_all(path, char_id=None, params=None):
    """GET an endpoint that comes in pages (assets, market orders) and
    join the pages into one list. ESI says how many in X-Pages."""
    first = _get(path, char_id, params)
    items = first.json()
    pages = int(first.headers.get("X-Pages", 1))
    for page in range(2, pages + 1):
        items += esi_get(path, char_id, {**(params or {}), "page": page})
    return items


def esi_post(path, body):
    r = http.post(f"{ESI}{path}", json=body, params={"datasource": "tranquility"})
    r.raise_for_status()
    return r.json()


# ---------- names ----------

_names = {}  # id -> name, kept for as long as the server runs


def names(ids):
    """Turn IDs (skills, items, systems, stations...) into names.
    Player structures and individual items can't be looked up this way,
    so we leave out anything with a huge ID and skip failures."""
    wanted = {i for i in ids if i not in _names and i < 10_000_000_000}
    wanted = list(wanted)
    for start in range(0, len(wanted), 1000):  # ESI takes 1000 at a time
        chunk = wanted[start:start + 1000]
        try:
            for row in esi_post("/universe/names/", chunk):
                _names[row["id"]] = row["name"]
        except httpx.HTTPStatusError:
            # One bad ID fails the whole batch; try them one by one.
            for i in chunk:
                try:
                    _names[i] = esi_post("/universe/names/", [i])[0]["name"]
                except httpx.HTTPStatusError:
                    pass
    return {i: _names.get(i, f"#{i}") for i in ids}


def find_id(name, category):
    """Exact name -> ID, e.g. find_id("Jita", "systems").
    Categories: inventory_types, systems, regions, stations, characters..."""
    found = esi_post("/universe/ids/", [name]).get(category, [])
    if not found:
        raise ValueError(f"No {category.replace('_', ' ')} called '{name}'. "
                         "EVE needs the exact name (capitals don't matter).")
    return found[0]["id"], found[0]["name"]
