"""One-off login. Run it once per character (and again if you add scopes).

    .venv/bin/python auth.py          (Linux/macOS)
    .venv\\Scripts\\python auth.py      (Windows)

Your browser opens EVE's login page. Pick the character and approve.
EVE then sends the browser to http://localhost:8765/callback, where this
script is listening, and the token is saved to tokens.json.

If that page doesn't load, copy the whole address from the browser's
address bar and paste it here instead.
"""

import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from eve_common import (SCOPES, SSO_AUTHORIZE, decode_jwt, load_tokens,
                        require, save_tokens, setting, token_request)

result = {}


class Callback(BaseHTTPRequestHandler):
    """A throwaway web server with one job: catch the code EVE sends back."""

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            result["code"] = query["code"][0]
            result["state"] = query.get("state", [""])[0]
            body = b"Got it. Close this tab and go back to the terminal."
        else:
            body = b"No login code in that request."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the terminal quiet


def main():
    callback = setting("EVE_CALLBACK_URL", "http://localhost:8765/callback")
    port = urlparse(callback).port or 80

    # 'state' is a random word we send and expect back, so nobody else can
    # slip their own login into this window.
    state = secrets.token_urlsafe(16)
    url = SSO_AUTHORIZE + "?" + urlencode({
        "response_type": "code",
        "redirect_uri": callback,
        "client_id": require("EVE_CLIENT_ID"),
        "scope": " ".join(SCOPES),
        "state": state,
    })

    server = None
    try:
        server = HTTPServer(("127.0.0.1", port), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError as e:
        print(f"(Couldn't listen on port {port}: {e}. Use the paste option.)")

    print("\nOpening EVE's login page. If it doesn't open, use this link:\n")
    print(url, "\n")
    webbrowser.open(url)
    print("Waiting... (or paste the address you landed on and press Enter)")

    pasted = {}

    def read_paste():
        try:
            pasted["url"] = input().strip()
        except EOFError:
            pass

    threading.Thread(target=read_paste, daemon=True).start()
    while "code" not in result and not pasted.get("url"):
        time.sleep(0.3)
    if server:
        server.shutdown()

    if "code" not in result:
        query = parse_qs(urlparse(pasted["url"]).query)
        if "code" not in query:
            raise SystemExit("That address has no login code in it. Try again.")
        result["code"] = query["code"][0]
        result["state"] = query.get("state", [""])[0]

    if result["state"] != state:
        raise SystemExit("That login wasn't started here. Run auth.py again.")

    resp = token_request({"grant_type": "authorization_code",
                          "code": result["code"]})
    claims = decode_jwt(resp["access_token"])
    char_id = claims["sub"].split(":")[-1]   # "CHARACTER:EVE:12345" -> "12345"
    name = claims["name"]

    tokens = load_tokens()
    tokens[char_id] = {
        "name": name,
        "refresh_token": resp["refresh_token"],
        "access_token": resp["access_token"],
        "expires_at": time.time() + int(resp.get("expires_in", 1199)),
    }
    save_tokens(tokens)

    granted = claims.get("scp", [])
    granted = [granted] if isinstance(granted, str) else granted
    missing = [s for s in SCOPES if s not in granted]
    print(f"\nSaved {name} ({char_id}).")
    if missing:
        print("Not granted (tick these on your app, then run auth.py again):")
        for s in missing:
            print("  ", s)


if __name__ == "__main__":
    main()
