"""mitmproxy addon: capture your own Waymo iOS app credentials into a 0600 file.

Usage (on the Mac, with the iPhone proxied through it and mitmproxy's CA trusted):
    WAYMO_CREDENTIALS_OUT=~/.config/mandelbot/waymo.json \
        mitmdump -s scripts/capture_credentials.py --set block_global=false
Then open the Waymo app (sign out and back in if it doesn't refresh). It writes
{refresh_token, device_id} as soon as both are seen, prints nothing secret, and
never forwards them anywhere. Stop mitmdump and untrust the CA afterwards.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs

from mitmproxy import http

OUT = Path(os.environ.get("WAYMO_CREDENTIALS_OUT", "~/.config/waymo/credentials.json")).expanduser()
OAUTH_CLIENT_ID = "936475272427.apps.googleusercontent.com"
_seen: dict[str, str] = {}


def _form(flow: http.HTTPFlow) -> dict[str, str]:
    body = flow.request.get_text(strict=False) or ""
    return {key: values[0] for key, values in parse_qs(body).items() if values}


def request(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    form = _form(flow) if flow.request.method == "POST" else {}
    if host == "oauth2.googleapis.com" and form.get("client_id") == OAUTH_CLIENT_ID and form.get("refresh_token"):
        _seen["refresh_token"] = form["refresh_token"]
    elif host == "oauthaccountmanager.googleapis.com" and form.get("app_id") == "com.waymo.client" and form.get("device_id"):
        _seen["device_id"] = form["device_id"]
    else:
        return
    if "refresh_token" in _seen and "device_id" in _seen:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(_seen, handle)
        os.chmod(OUT, 0o600)
        print(f"waymo credentials written to {OUT}")
