"""Authentication, pairing URI generation, and terminal/PNG QR utilities."""

from __future__ import annotations

import hmac
import io
import os
from urllib.parse import urlencode

import qrcode
from fastapi import Header, HTTPException, Query, status

from .config import BridgeStore, detect_tailscale_info
from .models import PairingInfo


def verify_bearer_token(store: BridgeStore, token: str | None) -> None:
    if os.environ.get("ANTIGRAVITY_BRIDGE_DISABLE_AUTH") == "1":
        return
    # Compare as bytes: hmac.compare_digest raises TypeError on non-ASCII str.
    if not token or not hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"), store.auth_token.encode("utf-8", "surrogatepass")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Pocket Relay Bridge bearer token.",
        )


def extract_token(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return token


def build_pairing_info(store: BridgeStore, port: int = 8765, public_url: str | None = None) -> PairingInfo:
    dns, _ = detect_tailscale_info()
    local_url = f"http://127.0.0.1:{port}"
    tailscale_url = f"https://{dns}" if dns else None
    effective_base = (
        public_url
        or os.environ.get("ANTIGRAVITY_PUBLIC_URL")
        or tailscale_url
        or local_url
    )
    params = {
        "host": effective_base,
        "token": store.auth_token,
        "name": store.host_name,
        "host_id": store.host_id,
    }
    # Carry the Wi-Fi address too, so a scanned QR works on the local network even when the
    # primary address (Tailscale / public URL) is not reachable from the phone.
    try:
        from .services.discovery import _detect_lan_ip  # lazy: discovery imports this module

        lan_ip = _detect_lan_ip()
    except Exception:
        lan_ip = ""
    if lan_ip and not lan_ip.startswith("127."):
        lan_url = f"http://{lan_ip}:{port}"
        if lan_url != effective_base:
            params["lan_url"] = lan_url
    query = urlencode(params)
    pairing_uri = f"pocketrelay://pair?{query}"
    return PairingInfo(
        host_id=store.host_id,
        host_name=store.host_name,
        base_url=effective_base,
        tailscale_url=tailscale_url,
        local_url=local_url,
        token=store.auth_token,
        pairing_uri=pairing_uri,
    )


def render_qr_ascii(data: str) -> str:
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()
