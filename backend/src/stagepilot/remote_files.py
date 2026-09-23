"""Private file contracts for optional headless Remote deployment."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DesiredRemote(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    generation: str = ""
    public_origin: str = ""
    port: int = Field(default=8766, ge=1024, le=65535)

    @model_validator(mode="after")
    def enabled_policy(self) -> DesiredRemote:
        if self.enabled:
            UUID(self.generation)
            origin = urlsplit(self.public_origin)
            if (
                origin.scheme != "https"
                or not origin.hostname
                or origin.path
                or origin.query
                or origin.fragment
                or origin.username
                or origin.password
            ):
                raise ValueError("Managed Remote requires an exact HTTPS origin")
        return self


def is_group_or_world_readable(path: Path) -> bool:
    """Report whether POSIX permission bits expose a private file to others.

    Windows does not implement POSIX permission bits: `os.stat` synthesizes
    `st_mode` from the read-only attribute alone, so a normal file always
    reports 0o666 and a naive `st_mode & 0o077` check is unconditionally true.
    Privacy there comes from the ACL on the per-user profile directory that
    holds these files, so the bit test is POSIX-only and must never be used to
    conclude that a Windows file is exposed.
    """

    if sys.platform == "win32":
        return False
    return bool(path.stat().st_mode & 0o077)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".remote-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        if sys.platform == "win32":
            return
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def read_desired(path: Path) -> DesiredRemote:
    try:
        if path.stat().st_size > 4096:
            return DesiredRemote()
        return DesiredRemote.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return DesiredRemote()


Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{8}$|^[a-f0-9]{16}$|^[a-f0-9]{32}$")]


class ControlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["cloudflare"] = "cloudflare"
    account_id: Identifier
    zone_id: Identifier
    hostname: str = Field(
        max_length=253,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$",
    )
    api_token_file: Path
    control_dir: Path
    installation_dir: Path
    remote_port: int = Field(default=8766, ge=1024, le=65535)
    lan_port: int = Field(default=8765, ge=1024, le=65535)

    @model_validator(mode="after")
    def boundaries(self) -> ControlConfig:
        for path in (self.api_token_file, self.control_dir, self.installation_dir):
            if not path.is_absolute():
                raise ValueError("Remote paths must be absolute")
        if self.remote_port == self.lan_port:
            raise ValueError("Remote and LAN ports must differ")
        install = self.installation_dir.resolve()
        if self.api_token_file.resolve().is_relative_to(install):
            raise ValueError("Account token must not be in the installation export")
        if self.control_dir.resolve().is_relative_to(install):
            raise ValueError("Control state must not be in the installation export")
        return self


class BetaControlConfig(BaseModel):
    """Installation-only configuration for the hosted private-beta control plane."""

    model_config = ConfigDict(extra="forbid")
    control_plane_url: str
    installation_id: Identifier
    hostname: str = Field(
        max_length=253,
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$",
    )
    credential_file: Path | None = None
    state_dir: Path
    installation_dir: Path
    remote_port: int = Field(default=8766, ge=1024, le=65535)
    lan_port: int = Field(default=8765, ge=1024, le=65535)

    @model_validator(mode="after")
    def boundaries(self) -> BetaControlConfig:
        origin = urlsplit(self.control_plane_url)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or origin.username
            or origin.password
        ):
            raise ValueError("Beta control plane requires an exact HTTPS origin")
        paths = (self.state_dir, self.installation_dir)
        for path in paths:
            if not path.is_absolute():
                raise ValueError("Remote paths must be absolute")
        if self.credential_file is not None and not self.credential_file.is_absolute():
            raise ValueError("Remote paths must be absolute")
        install = self.installation_dir.resolve()
        if self.credential_file is not None and self.credential_file.resolve().is_relative_to(
            install
        ):
            raise ValueError("Provisioning credential must not be in the connector export")
        if self.state_dir.resolve().is_relative_to(install):
            raise ValueError("Provisioning state must not be in the connector export")
        if self.remote_port == self.lan_port:
            raise ValueError("Remote and LAN ports must differ")
        return self


def safe_status(path: Path, **values: object) -> None:
    atomic_write(path, json.dumps(values))
