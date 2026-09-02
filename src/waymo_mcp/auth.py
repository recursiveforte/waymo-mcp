"""Authentication for the private Waymo rider API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OAUTH_CLIENT_ID = "936475272427.apps.googleusercontent.com"
WAYMO_CLIENT_ID = "431988300665-qr3bvhc24trr8i3bb7laavu4qrs5on1r"
FULL_SCOPES = (
    "https://www.googleapis.com/auth/cclog "
    "https://www.googleapis.com/auth/chauffeur/hail "
    "https://www.googleapis.com/auth/experimentsandconfigs "
    "profile email "
    "https://www.googleapis.com/auth/supportcontent "
    "https://www.googleapis.com/auth/account_settings_mobile"
)
USER_AGENT = (
    "com.waymo.client/2.3.26081700 iSL/3.4 "
    "iPhone/26.6.1 hw/iPhone17_4 (gzip)"
)
DEFAULT_DEVICE_ID = "F33B57DB-8668-40AD-A34E-9B2E0EE84F78"


@dataclass(frozen=True)
class Credentials:
    refresh_token: str
    device_id: str = DEFAULT_DEVICE_ID


def _post_form(
    url: str,
    data: dict[str, str],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"Authentication failed ({exc.code}): {detail}") from exc


def authenticate(credentials: Credentials) -> dict[str, Any]:
    oauth = _post_form(
        "https://oauth2.googleapis.com/token",
        {
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh_token,
        },
    )
    chauffeur = _post_form(
        "https://oauthaccountmanager.googleapis.com/v1/issuetoken",
        {
            "app_id": "com.waymo.client",
            "client_id": WAYMO_CLIENT_ID,
            "device_id": credentials.device_id,
            "hl": "en-US",
            "lib_ver": "3.4",
            "response_type": "token",
            "scope": FULL_SCOPES,
        },
        {
            "Authorization": f"Bearer {oauth['access_token']}",
            "X-OAuth-Client-ID": WAYMO_CLIENT_ID,
        },
    )
    return {
        "chauffeur_token": chauffeur["token"],
        "expires_at": int(time.time()) + int(chauffeur.get("expiresIn", 3600)),
    }


def load_credentials(path: str | Path) -> Credentials:
    data = json.loads(Path(path).expanduser().read_text())
    token = data.get("refresh_token")
    if not token:
        raise ValueError(f"No refresh_token in {path}")
    return Credentials(token, data.get("device_id", DEFAULT_DEVICE_ID))
