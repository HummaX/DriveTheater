from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urlencode, urlparse
import mimetypes

import httpx

FOLDER_RE = re.compile(r"(?:folders/|[?&]id=)([\w-]+)")
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_TOKEN_EXPIRY_SKEW_SECONDS = 60


def folder_id(value: str) -> str:
    value = value.strip()
    match = FOLDER_RE.search(value)
    result = match.group(1) if match else value
    if not re.fullmatch(r'[\w-]{1,200}', result):
        raise ValueError('Enter a Google Drive folder link or folder ID')
    return result


def validate_url(url: str, allow_private: bool = False) -> str:
    """Validate an HTTP(S) URL and reject local or special-use destinations.

    DNS is resolved for every call so download clients can invoke this again for
    each redirect. This is defence in depth, not a substitute for network-level
    egress controls against DNS rebinding.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port  # Trigger validation of malformed ports.
    except ValueError as exc:
        raise ValueError("a valid HTTP(S) URL is required") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("a valid HTTP(S) URL without embedded credentials is required")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("a valid HTTP(S) URL is required")
    try:
        addresses = {
            ipaddress.ip_address(result[4][0].split("%", 1)[0])
            for result in socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        }
    except (socket.gaierror, ValueError) as exc:
        raise ValueError("URL host could not be resolved") from exc
    if not addresses:
        raise ValueError("URL host could not be resolved")
    if not allow_private:
        forbidden = [address for address in addresses if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified
        )]
        if forbidden:
            raise ValueError("private or special-use source addresses are disabled")
    return url


def normalize_oauth_client_config(credentials: dict[str, Any]) -> dict[str, Any]:
    """Accept Google exported web/installed client JSON, retaining only fields used."""
    if not isinstance(credentials, dict):
        raise ValueError("OAuth client configuration is invalid")
    nested = credentials.get("web") or credentials.get("installed")
    client = nested if isinstance(nested, dict) else credentials
    client_id = client.get("client_id")
    client_secret = client.get("client_secret")
    if not isinstance(client_id, str) or not client_id or not isinstance(client_secret, str) or not client_secret:
        raise ValueError("OAuth client configuration is invalid")
    canonical = {key: value for key, value in client.items() if key in {
        "client_id", "client_secret", "access_token", "refresh_token", "token_type", "expires_at",
    }}
    # Older private configs may have relative expiry from a previous version.
    if "expires_at" not in canonical and isinstance(client.get("expires_in"), (int, float)):
        canonical["expires_at"] = int(time.time()) + int(client["expires_in"])
    return canonical


@dataclass
class GoogleAdapter:
    auth_mode: str
    credentials: dict[str, Any]
    credential_path: Path | None = None

    def _params(self) -> dict[str, str]:
        return {"key": self.credentials["api_key"]} if self.auth_mode == "api_key" else {}

    def _persist_credentials(self) -> None:
        if self.credential_path is None:
            return
        self.credential_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.credential_path.with_suffix(self.credential_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.credentials), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.credential_path)

    def _oauth_token_is_current(self) -> bool:
        token = self.credentials.get("access_token")
        expiry = self.credentials.get("expires_at")
        return bool(token and isinstance(expiry, (int, float)) and int(expiry) > time.time() + _TOKEN_EXPIRY_SKEW_SECONDS)

    def _refresh_oauth(self) -> str:
        refresh_token = self.credentials.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Google credential needs OAuth authorization")
        response = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": self.credentials.get("client_id"), "client_secret": self.credentials.get("client_secret"),
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        }, timeout=20)
        try:
            response.raise_for_status()
            refreshed = response.json()
            token = refreshed["access_token"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise RuntimeError("Google OAuth token refresh failed") from exc
        self.credentials["access_token"] = token
        self.credentials["expires_at"] = int(time.time()) + int(refreshed.get("expires_in", 3600))
        if refreshed.get("refresh_token"):
            self.credentials["refresh_token"] = refreshed["refresh_token"]
        if refreshed.get("token_type"):
            self.credentials["token_type"] = refreshed["token_type"]
        self._persist_credentials()
        return token

    def _token(self) -> str | None:
        if self.auth_mode == "api_key":
            return None
        if self.auth_mode == "oauth":
            if self._oauth_token_is_current():
                return str(self.credentials["access_token"])
            return self._refresh_oauth()
        if self.auth_mode == "service_account":
            # Imported only for this auth mode, keeping API-key-only installs light.
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                self.credentials, scopes=[GOOGLE_DRIVE_SCOPE]
            )
            creds.refresh(Request())
            return creds.token
        raise RuntimeError("Google credential configuration is invalid")

    def download_parameters(self, remote_id: str) -> tuple[str, dict[str, str]]:
        """Return a freshly authorized, validated Drive media request."""
        if not remote_id:
            raise ValueError("Google file id is required")
        url = validate_url(
            f"https://www.googleapis.com/drive/v3/files/{quote(remote_id, safe='')}?alt=media",
            allow_private=False,
        )
        if self.auth_mode == 'api_key':
            url += '&' + urlencode(self._params())
        headers: dict[str, str] = {}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return url, headers

    def list_metadata(self, folder: str) -> Iterator[dict[str, Any]]:
        """Yield Drive file metadata only; callers must not fetch media here."""
        url = "https://www.googleapis.com/drive/v3/files"
        params = {
            "q": f"'{folder}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,thumbnailLink,videoMediaMetadata)",
            "pageSize": "1000", "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", **self._params(),
        }
        headers: dict[str, str] = {}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with httpx.Client(timeout=20) as client:
            while True:
                response = client.get(url, params=params, headers=headers)
                response.raise_for_status()
                body = response.json()
                yield from body.get("files", [])
                next_page = body.get("nextPageToken")
                if not next_page:
                    break
                params["pageToken"] = next_page

    def test_metadata_access(self, folder: str) -> None:
        next(self.list_metadata(folder), None)


class PublicDriveAdapter:
    """Public-link metadata discovery only; no Google Cloud credentials.

    gdown's skip_download mode lists public-folder entries. Google may restrict
    or truncate anonymous listings; do not treat this as private Drive access.
    """

    def list_metadata(self, folder: str) -> Iterator[dict[str, Any]]:
        try:
            import gdown
        except ImportError as exc:
            raise RuntimeError('Public Drive support is not installed in this runtime') from exc
        entries = gdown.download_folder(
            id=folder_id(folder), output='public-drive-catalog', quiet=True,
            skip_download=True, use_cookies=False, timeout=20,
        )
        if entries is None:
            raise RuntimeError('Public folder is unavailable; enable Anyone with the link or use authorized Drive access')
        for entry in entries:
            name = Path(entry.path).name
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            if mime.startswith('video/'):
                yield {'id': entry.id, 'name': name, 'mimeType': mime}

    def test_metadata_access(self, folder: str) -> None:
        next(self.list_metadata(folder), None)


def is_video_metadata(item: dict[str, Any]) -> bool:
    return str(item.get("mimeType", "")).startswith("video/")


def google_remote_metadata(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("videoMediaMetadata") or {}
    return {"remote_id": item["id"], "title": item.get("name") or item["id"], "mime_type": item.get("mimeType"),
            "size_bytes": int(item["size"]) if str(item.get("size", "")).isdigit() else None,
            "thumbnail_url": item.get("thumbnailLink"), "duration": float(meta["durationMillis"])/1000 if meta.get("durationMillis") else None,
            "width": meta.get("width"), "height": meta.get("height")}
