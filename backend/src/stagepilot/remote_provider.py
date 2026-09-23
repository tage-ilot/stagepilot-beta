"""Cloudflare control-plane adapter. Never imported by the production runtime."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

import httpx

from stagepilot.remote_files import ControlConfig


class ProviderError(Exception):
    """Only sanitized operational messages may cross this boundary."""


class InstallationPermanentlyRevokedError(ProviderError):
    """The control plane has confirmed this installation's credential can
    never be used again (401/403 on reactivate).

    Distinct from a plain ProviderError so callers can tell "transient,
    retrying might work" apart from "this identity is dead, offer a reset"
    without parsing message text.
    """


class TunnelProvider(Protocol):
    def ensure(self, name: str, hostname: str, port: int) -> tuple[str, str]: ...
    def revoke(self, name: str, hostname: str) -> None: ...


class CloudflareProvider:
    def __init__(self, config: ControlConfig, client: httpx.Client) -> None:
        self.client = client
        self.tunnels = f"/accounts/{config.account_id}/cfd_tunnel"
        self.records = f"/zones/{config.zone_id}/dns_records"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, path, **kwargs)
            if response.status_code == 404:
                return None
            if not response.is_success:
                raise ProviderError(f"Provider request failed (HTTP {response.status_code})")
            data = response.json()
            if data.get("success") is not True:
                raise ProviderError("Provider rejected request")
            return data["result"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderError(
                "Provider response unavailable or invalid; retry reconciliation"
            ) from exc

    def tunnel(self, name: str) -> dict[str, Any] | None:
        rows = self.request("GET", self.tunnels, params={"name": name, "is_deleted": "false"})
        if not isinstance(rows, list):
            raise ProviderError("Cannot enumerate installation tunnel")
        rows = [r for r in rows if r.get("name") == name and not r.get("deleted_at")]
        if len(rows) > 1:
            raise ProviderError("Ambiguous installation tunnel; refusing changes")
        if not rows:
            return None
        row: dict[str, Any] = rows[0]
        try:
            UUID(row["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("Invalid tunnel identity") from exc
        if row.get("config_src") != "cloudflare":
            raise ProviderError("Refusing to adopt a locally managed tunnel")
        return row

    def dns(self, hostname: str) -> dict[str, Any] | None:
        rows = self.request("GET", self.records, params={"name": hostname})
        if not isinstance(rows, list) or len(rows) > 1:
            raise ProviderError("Ambiguous hostname; refusing changes")
        return rows[0] if rows else None

    @staticmethod
    def owned(record: dict[str, Any], name: str, hostname: str, tunnel_id: str) -> bool:
        return (
            record.get("comment") == name
            and record.get("name") == hostname
            and record.get("type") == "CNAME"
            and record.get("content") == f"{tunnel_id}.cfargotunnel.com"
            and record.get("proxied") is True
        )

    def configuration(self, tunnel_id: str, ingress: list[dict[str, str]]) -> None:
        path = f"{self.tunnels}/{tunnel_id}/configurations"
        config = {"ingress": ingress, "warp-routing": {"enabled": False}}
        self.request("PUT", path, json={"config": config})
        actual = self.request("GET", path)
        if not isinstance(actual, dict) or actual.get("config", {}).get("ingress") != ingress:
            raise ProviderError("Tunnel route read-back did not match")
        if actual.get("config", {}).get("warp-routing", {}).get("enabled") is not False:
            raise ProviderError("Private network routing must be disabled")

    def ensure(self, name: str, hostname: str, port: int) -> tuple[str, str]:
        existing = self.dns(hostname)
        tunnel = self.tunnel(name)
        if existing and (not tunnel or not self.owned(existing, name, hostname, tunnel["id"])):
            raise ProviderError("Hostname is owned by another route; refusing changes")
        if not tunnel:
            # Never blindly retry POST after a lost response. The next invocation
            # reconciles the durable random installation/generation name first.
            self.request("POST", self.tunnels, json={"name": name, "config_src": "cloudflare"})
            tunnel = self.tunnel(name)
            if not tunnel:
                raise ProviderError("Tunnel creation not confirmed")
        tunnel_id = str(tunnel["id"])
        self.configuration(
            tunnel_id,
            [
                {"hostname": hostname, "service": f"http://127.0.0.1:{port}"},
                {"service": "http_status:404"},
            ],
        )
        if not existing:
            self.request(
                "POST",
                self.records,
                json={
                    "type": "CNAME",
                    "name": hostname,
                    "content": f"{tunnel_id}.cfargotunnel.com",
                    "proxied": True,
                    "ttl": 1,
                    "comment": name,
                },
            )
        actual = self.dns(hostname)
        if not actual or not self.owned(actual, name, hostname, tunnel_id):
            raise ProviderError("Hostname route read-back did not match")
        token = self.request("GET", f"{self.tunnels}/{tunnel_id}/token")
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ProviderError("Installation credential unavailable")
        return tunnel_id, token

    def revoke(self, name: str, hostname: str) -> None:
        tunnel = self.tunnel(name)
        record = self.dns(hostname)
        if record:
            if not tunnel or not self.owned(record, name, hostname, tunnel["id"]):
                raise ProviderError("Refusing to delete an unowned hostname")
            self.request("DELETE", f"{self.records}/{record['id']}")
            if self.dns(hostname) is not None:
                raise ProviderError("Hostname removal not confirmed")
        if tunnel:
            tunnel_id = tunnel["id"]
            # Block routes before forcing connections closed. Deleting the tunnel
            # retires its run credential; do not confuse stop with revocation.
            self.configuration(tunnel_id, [{"service": "http_status:404"}])
            self.request("DELETE", f"{self.tunnels}/{tunnel_id}/connections")
            self.request("DELETE", f"{self.tunnels}/{tunnel_id}")
            if self.tunnel(name) is not None:
                raise ProviderError("Tunnel revocation not confirmed")
