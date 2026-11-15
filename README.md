# eve-mcp

A small [MCP](https://modelcontextprotocol.io/) server that gives Claude (or
any other MCP client) live EVE Online data from CCP's ESI API: your own
characters, plus public game data. Everything it does is read-only.

| Tool | What it does | Needs sign-in |
|---|---|---|
| `list_characters` | Who's signed in | – |
| `get_skills` | Skills, levels, SP, unallocated SP (filterable) | yes |
| `get_skill_queue` | Queue order, finish times, when it runs out | yes |
| `get_wallet` | Balance and recent journal entries | yes |
| `get_location` | System, security, station, ship, online | yes |
| `get_assets` | Items by station, and what they're inside | yes |
| `market_price` | Best buy/sell at Jita, Amarr, Dodixie, Rens, Hek | no |
| `item_info` | Group, volume, in-game description | no |
| `system_info` | Security, region, last hour's jumps and kills | no |
| `route` | Gate route with security for each jump | no |

Files:
- `server.py`: the MCP server. Read this one first: each tool is a short function.
- `eve_common.py`: settings, sign-in tokens, talking to ESI
- `auth.py`: one-off login per character
- `.env`: your app's ID and secret (you make this; git-ignored)
- `tokens.json`: sign-in tokens (made by `auth.py`; git-ignored, keep it private)

Tested with Python 3.13, mcp 2.3.0 and httpx 0.28.1 on Debian 13. It needs
Python 3.10 or later. `requirements.lock` lists the exact versions of every
package from that test, if you want to reproduce it.

## Why MCP

You could give a model the ESI documentation and let it make API calls
itself, and for one-off public data (a market price, a route) that works
fine. A server earns its place once sign-in is involved:

- **The server holds the credentials.** The model never sees your app's
  secret or your refresh token; it only sees the results of tool calls.
- **The docstrings do the steering.** Each tool's description tells the
  model when to use it, so you don't have to explain ESI in every chat.
- **Results are shaped for a model.** Names instead of numeric IDs, prices
  formatted, long asset lists grouped and cut to a sensible size.
- **It works across clients.** Anything that speaks MCP can use it.

## 1. Register an EVE application

ESI needs every user to bring their own application. At
https://developers.eveonline.com/ create one with:

- **Callback URL:** `http://localhost:8765/callback`
- **Scopes** (all read-only):
  - `esi-skills.read_skills.v1`
  - `esi-skills.read_skillqueue.v1`
  - `esi-wallet.read_character_wallet.v1`
  - `esi-location.read_location.v1`
  - `esi-location.read_ship_type.v1`
  - `esi-location.read_online.v1`
  - `esi-assets.read_assets.v1`

It gives you a client ID and a secret key.

## 2. Install

From this folder.

Linux or macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Debian or Ubuntu, if the first command complains that `venv` is missing,
run `sudo apt install python3-venv` and try again.

Windows (PowerShell; if `py` isn't found, install Python from python.org and
tick "Add to PATH"):

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

The `.venv` folder keeps these packages to this project. In the steps below,
"the project's Python" means `.venv/bin/python` on Linux/macOS and
`.venv\Scripts\python` on Windows.

## 3. Fill in `.env`

Copy `.env.example` to `.env` and paste in the client ID and secret. Set
`EVE_USER_AGENT` to something with a way to contact you (CCP asks API
clients to identify themselves). On Linux/macOS, keep the file to yourself:

```bash
cp .env.example .env
chmod 600 .env
```

## 4. Sign in

```bash
.venv/bin/python auth.py
```

Your browser opens EVE's login. Pick a character and approve. The terminal
should say "Saved" and list any permissions it didn't get. Run it again for
each further character. If you have several, set `EVE_DEFAULT_CHARACTER` in
`.env` to the one the tools should use when you don't name one.

On a machine with no browser, open the printed link on another computer,
approve, then copy the whole address you land on (the page itself won't
load) and paste it into the terminal.

## 5. Check it works

```bash
.venv/bin/python server.py --check
```

It should print who is signed in, where your character is, their ship and
when their skill queue ends, then "All good."

## 6. Add it to Claude

Both need **absolute** paths to the project's Python and to `server.py`.

**Claude Code:**

```bash
claude mcp add --scope user eve -- /path/to/eve-mcp/.venv/bin/python /path/to/eve-mcp/server.py
claude mcp list
```

`claude mcp list` should show `eve` as connected. The tools only appear in a
*new* Claude Code session: MCP servers are loaded when a session starts.

**Claude Desktop:** open Settings > Developer > Edit Config (the file is
`claude_desktop_config.json`) and add this inside `"mcpServers"`, creating
that section if it isn't there.

macOS:

```json
{
  "mcpServers": {
    "eve": {
      "command": "/path/to/eve-mcp/.venv/bin/python",
      "args": ["/path/to/eve-mcp/server.py"]
    }
  }
}
```

Windows (double backslashes are how JSON writes one backslash):

```json
{
  "mcpServers": {
    "eve": {
      "command": "C:\\path\\to\\eve-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\eve-mcp\\server.py"]
    }
  }
}
```

Then quit Claude completely (on Windows, right-click the tray icon > Quit,
not just closing the window) and start it again.

## Security

- **What it can read:** for each character you sign in, their skills and
  skill queue, wallet balance and journal, location, ship, online status
  and assets. Nothing else, and only what the seven scopes above allow.
- **What it can change:** nothing. Every scope is read-only and the server
  makes no writes to EVE.
- **Where secrets live:** your app's ID and secret in `.env`, your sign-in
  tokens in `tokens.json`, both in this folder on your machine and both
  git-ignored. `tokens.json` is written with mode 600 (owner only) on
  Linux/macOS. Anyone who gets that file can read the same data until you
  revoke it, so don't share or commit it.
- **Revoking:** remove the application's access at
  https://community.eveonline.com/support/third-party-applications/.
- **Not sandboxed:** like any MCP server run over stdio, this is an ordinary
  program running as you, with your access to your files. Read the code
  before you run it; it's short.

## Adding a tool

Copy one of the short tools in `server.py`, change the ESI path, and start
a new Claude session. The ESI endpoints are listed at
https://developers.eveonline.com/api-explorer. If the new endpoint needs a
new permission, add it to `SCOPES` in `eve_common.py`, tick it on the app,
and run `auth.py` again.

There is a small offline test of the security-status rounding:

```bash
.venv/bin/python tests/test_sec.py
```

## If something goes wrong

- **"EVE refused ... (403)"**: the sign-in lacks that permission. Tick the
  scope on your app (step 1), then sign in again (step 4).
- **Login page says invalid scope**: a scope in `SCOPES` isn't ticked on the app.
- **"EVE_CLIENT_ID is missing"**: `.env` isn't there or isn't filled in (step 3).
- **Tools don't appear in Claude**: check the paths are absolute and
  correct, run step 5 to see the error in the terminal, and remember a new
  session (Claude Code) or a full restart (Claude Desktop) is needed. Then
  look at the MCP logs:
  - Claude Code on Linux: `~/.cache/claude-cli-nodejs/<project folder>/mcp-logs-eve/`
  - Claude Desktop on Windows: `%APPDATA%\Claude\logs\mcp-server-eve.log`
- **Everything fails after an ESI change**: the server uses
  `https://esi.evetech.net/latest` (`ESI` in `eve_common.py`). If CCP
  changes how the API is versioned, that address may need updating.

## Licence

MIT; see `LICENSE`.

Not affiliated with or endorsed by CCP Games. EVE Online and all related
names are trademarks of CCP hf.
