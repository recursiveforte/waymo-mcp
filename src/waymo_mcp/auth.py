"""Authentication for the private Waymo rider API.

Two credential routes are supported:

* iOS refresh token (the original route). A refresh token captured from the
  signed-in Waymo iOS app is exchanged at ``oauth2.googleapis.com`` for an
  access token, which is then traded at ``oauthaccountmanager.googleapis.com``
  for a short-lived chauffeur token.

* Android master token. An ``aas_et/...`` master token (obtained once from the
  account's web ``EmbeddedSetup`` ``oauth_token`` via gpsoauth) is exchanged,
  through Google Play Services OAuth, for an access token scoped directly to
  ``chauffeur/hail``. That token is itself accepted by the Waymo gRPC backend,
  so no separate issuetoken step is needed. The master token is long-lived;
  only the derived access token expires, and it is re-minted on demand.
"""

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

# Android master-token route. The Waymo Android app package and the SHA-1 of
# its APK signing certificate (CN=carapp, Google Inc.). Google Play Services
# OAuth only grants this restricted client the single ``chauffeur/hail`` scope;
# requesting any other scope, or more than one at once, returns
# RESTRICTED_CLIENT. That lone scope is enough: the Waymo gRPC backend accepts
# the resulting access token directly.
WAYMO_ANDROID_APP = "com.waymo.carapp"
WAYMO_ANDROID_SIG = "8cb0740811e55b4b260c8c62fc09283dfb47c484"
ANDROID_OAUTH_SERVICE = "oauth2:https://www.googleapis.com/auth/chauffeur/hail"


@dataclass(frozen=True)
class Credentials:
    """Waymo credentials for either the iOS or the Android route.

    An iOS credential sets ``refresh_token``; an Android credential sets
    ``master_token``, ``email`` and ``android_id``.
    """

    refresh_token: str | None = None
    device_id: str = DEFAULT_DEVICE_ID
    master_token: str | None = None
    email: str | None = None
    android_id: str | None = None

    @property
    def is_android(self) -> bool:
        return bool(self.master_token)

    @classmethod
    def android(cls, email: str, master_token: str, android_id: str) -> "Credentials":
        if not (email and master_token and android_id):
            raise ValueError("Android credentials need email, master_token and android_id")
        return cls(master_token=master_token, email=email, android_id=android_id)


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


def _perform_oauth(email: str, master_token: str, android_id: str) -> dict[str, str]:
    """Mint a chauffeur-scoped access token from an Android master token.

    Kept as a thin wrapper so gpsoauth is imported only on the Android route
    and so tests can substitute it.
    """
    import gpsoauth

    return gpsoauth.perform_oauth(
        email,
        master_token,
        android_id,
        service=ANDROID_OAUTH_SERVICE,
        app=WAYMO_ANDROID_APP,
        client_sig=WAYMO_ANDROID_SIG,
    )


def _authenticate_android(credentials: Credentials) -> dict[str, Any]:
    assert credentials.email and credentials.master_token and credentials.android_id
    result = _perform_oauth(
        credentials.email, credentials.master_token, credentials.android_id
    )
    token = result.get("Auth")
    if not token:
        error = result.get("Error") or result.get("ErrorDetail") or "unknown error"
        raise RuntimeError(f"Waymo Android authentication failed: {error}")
    expiry = int(result.get("Expiry") or 0)
    if not expiry:
        expiry = int(time.time()) + int(result.get("ExpiresInDurationSec", 3600))
    return {"chauffeur_token": token, "expires_at": expiry}


def _authenticate_ios(credentials: Credentials) -> dict[str, Any]:
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


def authenticate(credentials: Credentials) -> dict[str, Any]:
    if credentials.is_android:
        return _authenticate_android(credentials)
    if not credentials.refresh_token:
        raise ValueError("Credentials have neither a refresh_token nor a master_token")
    return _authenticate_ios(credentials)


def load_credentials(path: str | Path) -> Credentials:
    data = json.loads(Path(path).expanduser().read_text())
    if data.get("master_token"):
        missing = [key for key in ("email", "android_id") if not data.get(key)]
        if missing:
            raise ValueError(f"Android credentials in {path} missing {', '.join(missing)}")
        return Credentials(
            master_token=data["master_token"],
            email=data["email"],
            android_id=data["android_id"],
            device_id=data.get("device_id", DEFAULT_DEVICE_ID),
        )
    token = data.get("refresh_token")
    if not token:
        raise ValueError(f"No refresh_token or master_token in {path}")
    return Credentials(refresh_token=token, device_id=data.get("device_id", DEFAULT_DEVICE_ID))
