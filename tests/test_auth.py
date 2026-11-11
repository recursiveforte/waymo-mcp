from __future__ import annotations

import json
import time

import pytest

from waymo_mcp import auth
from waymo_mcp.auth import Credentials, authenticate, load_credentials


def test_android_credentials_constructor_requires_all_fields() -> None:
    with pytest.raises(ValueError):
        Credentials.android("", "aas_et/x", "0123456789abcdef")
    creds = Credentials.android("me@gmail.com", "aas_et/x", "0123456789abcdef")
    assert creds.is_android
    assert creds.refresh_token is None


def test_authenticate_android_uses_perform_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_perform_oauth(email: str, master_token: str, android_id: str) -> dict[str, str]:
        calls.append((email, master_token, android_id))
        return {"Auth": "ya29.fake", "Expiry": str(int(time.time()) + 1800)}

    monkeypatch.setattr(auth, "_perform_oauth", fake_perform_oauth)
    creds = Credentials.android("me@gmail.com", "aas_et/x", "0123456789abcdef")
    result = authenticate(creds)

    assert calls == [("me@gmail.com", "aas_et/x", "0123456789abcdef")]
    assert result["chauffeur_token"] == "ya29.fake"
    assert result["expires_at"] > time.time() + 60


def test_authenticate_android_without_expiry_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth,
        "_perform_oauth",
        lambda *a: {"Auth": "ya29.fake", "ExpiresInDurationSec": "3600"},
    )
    result = authenticate(Credentials.android("me@gmail.com", "aas_et/x", "aid"))
    assert result["expires_at"] == pytest.approx(time.time() + 3600, abs=5)


def test_authenticate_android_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_perform_oauth", lambda *a: {"Error": "RESTRICTED_CLIENT"})
    with pytest.raises(RuntimeError, match="RESTRICTED_CLIENT"):
        authenticate(Credentials.android("me@gmail.com", "aas_et/x", "aid"))


def test_authenticate_ios_uses_refresh_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[str] = []

    def fake_post(url: str, data: dict[str, str], headers=None) -> dict[str, str]:
        posts.append(url)
        if "oauth2.googleapis.com" in url:
            assert data["refresh_token"] == "refresh-xyz"
            return {"access_token": "access-xyz"}
        return {"token": "chauffeur-xyz", "expiresIn": "3600"}

    monkeypatch.setattr(auth, "_post_form", fake_post)
    result = authenticate(Credentials(refresh_token="refresh-xyz"))

    assert result["chauffeur_token"] == "chauffeur-xyz"
    assert any("oauth2.googleapis.com" in url for url in posts)
    assert any("issuetoken" in url for url in posts)


def test_authenticate_without_any_credential_raises() -> None:
    with pytest.raises(ValueError):
        authenticate(Credentials())


def test_load_credentials_android(tmp_path) -> None:
    path = tmp_path / "waymo.json"
    path.write_text(
        json.dumps(
            {"master_token": "aas_et/x", "email": "me@gmail.com", "android_id": "aid"}
        )
    )
    creds = load_credentials(path)
    assert creds.is_android
    assert creds.email == "me@gmail.com"
    assert creds.master_token == "aas_et/x"
    assert creds.android_id == "aid"


def test_load_credentials_android_missing_field(tmp_path) -> None:
    path = tmp_path / "waymo.json"
    path.write_text(json.dumps({"master_token": "aas_et/x", "email": "me@gmail.com"}))
    with pytest.raises(ValueError, match="android_id"):
        load_credentials(path)


def test_load_credentials_ios(tmp_path) -> None:
    path = tmp_path / "waymo.json"
    path.write_text(json.dumps({"refresh_token": "refresh-xyz"}))
    creds = load_credentials(path)
    assert not creds.is_android
    assert creds.refresh_token == "refresh-xyz"


def test_load_credentials_empty_raises(tmp_path) -> None:
    path = tmp_path / "waymo.json"
    path.write_text(json.dumps({}))
    with pytest.raises(ValueError, match="refresh_token or master_token"):
        load_credentials(path)
