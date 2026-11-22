"""Bonjour/mDNS local discovery (_pocketrelay._tcp) and 6-digit one-time pairing codes."""

from __future__ import annotations

import hmac
import os
import platform as py_platform
import secrets
import socket
import time
from dataclasses import dataclass

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from ..auth import build_pairing_info
from ..config import BridgeStore
from ..models import PairingInfo


SERVICE_TYPE = "_pocketrelay._tcp.local."
BONJOUR_TTL_SECONDS = 120
PAIRING_WINDOW_SECONDS = 600.0


def _detect_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def is_bonjour_disabled() -> bool:
    return os.environ.get("ANTIGRAVITY_DISABLE_BONJOUR", "").strip().lower() in ("1", "true", "yes")


@dataclass
class ActivePairCode:
    code: str
    expires_at: float


class DiscoveryService:
    """Advertises _pocketrelay._tcp via Bonjour/mDNS and issues 6-digit pairing codes."""

    def __init__(
        self,
        store: BridgeStore,
        port: int = 8765,
        public_url: str | None = None,
    ) -> None:
        self.store = store
        self.port = port
        self.public_url = public_url
        self._azc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._pair_code: ActivePairCode | None = None
        self._failed_attempts: int = 0
        self._locked_until: float = 0.0
        # The 6-digit code is only redeemable while "armed": for a limited window
        # after the loopback-only /pair page was last served on this computer.
        self._armed_until: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._azc is not None and self._info is not None

    @property
    def service_name(self) -> str:
        safe_name = (
            "".join(c for c in self.store.host_name if c.isalnum() or c in ("-", "_", " ")).strip()
            or "Antigravity"
        )
        return f"{safe_name}.{SERVICE_TYPE}"

    def is_rate_limited(self) -> bool:
        return time.monotonic() < self._locked_until

    def current_pair_code(self) -> tuple[str, int]:
        """Return (6-digit code, seconds remaining), rotating if expired."""
        now = time.monotonic()
        if self._pair_code is None or now >= self._pair_code.expires_at:
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._pair_code = ActivePairCode(code=code, expires_at=now + 600.0)
            self._failed_attempts = 0
        remaining = max(0, int(self._pair_code.expires_at - now))
        return self._pair_code.code, remaining

    def rotate_pair_code(self) -> tuple[str, int]:
        self._pair_code = None
        return self.current_pair_code()

    def arm_pairing(self) -> tuple[str, int]:
        """Open the pairing window (called when the loopback /pair page is served).

        Re-arms for PAIRING_WINDOW_SECONDS, clears the failure counter / lockout and
        returns (6-digit code, seconds remaining).
        """
        code, _ = self.current_pair_code()
        now = time.monotonic()
        self._armed_until = now + PAIRING_WINDOW_SECONDS
        if self._pair_code is not None:
            self._pair_code.expires_at = self._armed_until
        self._failed_attempts = 0
        self._locked_until = 0.0
        return code, int(PAIRING_WINDOW_SECONDS)

    def is_pairing_armed(self) -> bool:
        now = time.monotonic()
        return (
            self._pair_code is not None
            and now < self._armed_until
            and now < self._pair_code.expires_at
        )

    def disarm_pairing(self) -> None:
        self._pair_code = None
        self._armed_until = 0.0

    def exchange_pair_code(self, submitted_code: str) -> PairingInfo | None:
        now = time.monotonic()
        if now < self._locked_until:
            return None
        # Never mint a code here: only serving the loopback /pair page arms pairing.
        if not self.is_pairing_armed() or self._pair_code is None:
            return None
        clean = "".join(ch for ch in submitted_code if ch in "0123456789")
        code = self._pair_code.code
        if not clean or not hmac.compare_digest(clean.encode("utf-8"), code.encode("utf-8")):
            self._failed_attempts += 1
            if self._failed_attempts >= 5:
                self._locked_until = now + 60.0
                self._failed_attempts = 0
            return None

        # Valid 6-digit code: one-time use. Drop it and close the pairing window;
        # pairing another device requires opening /pair on the computer again.
        self._failed_attempts = 0
        self._locked_until = 0.0
        self.disarm_pairing()
        info = build_pairing_info(self.store, port=self.port, public_url=self.public_url)
        lan_url = f"http://{_detect_lan_ip()}:{self.port}"
        info.local_url = lan_url
        if info.base_url.startswith("http://127.0.0.1"):
            info.base_url = lan_url
        return info

    def build_service_info(self, lan_ip: str | None = None) -> ServiceInfo:
        resolved_ip = lan_ip or _detect_lan_ip()
        pairing = build_pairing_info(self.store, port=self.port, public_url=self.public_url)
        advertise_url = (
            pairing.base_url
            if not pairing.base_url.startswith("http://127.0.0.1")
            else f"http://{resolved_ip}:{self.port}"
        )
        sys_name = py_platform.system()
        pretty_os = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(sys_name, sys_name)

        props = {
            b"host_id": self.store.host_id.encode("utf-8"),
            b"host_name": self.store.host_name.encode("utf-8"),
            b"platform": pretty_os.encode("utf-8"),
            b"url": advertise_url.encode("utf-8"),
            b"lan_url": f"http://{resolved_ip}:{self.port}".encode("utf-8"),
            b"version": b"1.0.0",
        }
        return ServiceInfo(
            type_=SERVICE_TYPE,
            name=self.service_name,
            addresses=[socket.inet_aton(resolved_ip)],
            port=self.port,
            properties=props,
            host_ttl=BONJOUR_TTL_SECONDS,
            other_ttl=BONJOUR_TTL_SECONDS,
        )

    async def start(self) -> None:
        if is_bonjour_disabled():
            return
        if self._azc is not None or self._info is not None:
            await self.stop()
        azc: AsyncZeroconf | None = None
        try:
            info = self.build_service_info()
            azc = AsyncZeroconf()
            await azc.async_register_service(
                info,
                ttl=BONJOUR_TTL_SECONDS,
                allow_name_change=False,
                cooperating_responders=True,
            )
            self._info = info
            self._azc = azc
        except Exception:
            # Bonjour is best-effort; bridge still works via Tailscale/QR if mDNS unavailable
            if azc is not None:
                try:
                    await azc.async_close()
                except Exception:
                    pass
            await self.stop()

    async def stop(self) -> None:
        azc = self._azc
        info = self._info
        self._azc = None
        self._info = None
        if not azc:
            return
        try:
            if info:
                await azc.async_unregister_service(info)
            await azc.async_unregister_all_services()
        except Exception:
            pass
        try:
            await azc.async_close()
        except Exception:
            pass
