"""Unified, strict Pydantic configuration and initial-import inventory."""

from __future__ import annotations

import ipaddress
import math
import re
import subprocess
from base64 import b64decode, b64encode
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from argon2 import PasswordHasher
from argon2.low_level import Type
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from sing_box_manager.runtime_env import load_local_env

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_ROOT = Path("./config")
RELEASES_ROOT = Path("./releases")
UPSTREAM_CACHE_ROOT = Path("./.cache/sing-box-manager/upstream")
SUPPORTED_PROTOCOLS = ("trojan", "hysteria2", "naive", "snell")
CLIENT_UPGRADE_POLICIES = ("off", "suggested", "required")
STATE_ROOT = Path("/var/lib/sing-box-manager")
DEFAULT_TRAFFIC_STATS_DATABASE_PATH = STATE_ROOT / "traffic-stats.sqlite3"
DEFAULT_SUBSCRIPTION_DATABASE_PATH = STATE_ROOT / "subscriptions.sqlite3"
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
ARGON2ID_VERIFIER = PasswordHasher(type=Type.ID)
ARGON2ID_ENCODING = re.compile(
    r"\$argon2id(?:\$v=(?:16|19))?\$m=([1-9][0-9]{0,9}),"
    r"t=([1-9][0-9]{0,9}),p=([1-9][0-9]{0,9})"
    r"\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)"
)
INVENTORY_KEYS = {
    "deployment",
    "web_portal",
    "trojan",
    "hysteria2",
    "naive",
    "snell",
    "vps_info",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_default=True,
        serialize_by_alias=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls.model_validate({} if value is None else value)

    @field_validator("*", mode="before")
    @classmethod
    def validate_paths(cls, value: object, info: ValidationInfo) -> object:
        field = cls.model_fields[info.field_name]
        if (
            field.annotation in (Path, Path | None)
            and value is not None
            and not str(value).strip()
        ):
            raise ValueError(f"{info.field_name} must be a non-empty path string")
        return value


def _validate_username(value: str) -> str:
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("username must match ^[a-zA-Z0-9_-]{3,32}$")
    return value


def _valid_argon2id_encoding(value: str) -> bool:
    """Check the PHC encoding without running its input-selected password work."""
    match = ARGON2ID_ENCODING.fullmatch(value)
    if match is None:
        return False
    memory, iterations, parallelism = map(int, match.groups()[:3])
    if not (
        1 <= parallelism <= 2**24 - 1
        and 8 * parallelism <= memory <= 2**32 - 1
        and 1 <= iterations <= 2**32 - 1
    ):
        return False
    for encoded, minimum in zip(match.groups()[3:], (8, 4), strict=True):
        try:
            decoded = b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        except ValueError:
            return False
        if (
            len(decoded) < minimum
            or b64encode(decoded).decode("ascii").rstrip("=") != encoded
        ):
            return False
    return True


def _validate_password_hash(value: str) -> str:
    if not _valid_argon2id_encoding(value):
        raise ValueError("password_hash must be a valid Argon2id hash")
    return value


class HasUsername(Protocol):
    username: str


def _validate_unique_usernames(users: Sequence[HasUsername]) -> None:
    seen: set[str] = set()
    for user in users:
        if user.username in seen:
            raise ValueError(f"Duplicate username: {user.username}")
        seen.add(user.username)


def normalize_domain(value: str) -> str:
    value = value.strip().lower().removeprefix(".").removesuffix(".")
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        raise ValueError("Invalid direct domain name")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("Domain suffix must not be an IP address")


class TlsConfig(StrictModel):
    enabled: bool
    server_name: str | None = None
    key_path: str | None = None
    certificate_path: str | None = None
    self_signed_cert: bool = False

    @field_validator("server_name", "key_path", "certificate_path")
    @classmethod
    def validate_optional_text(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value.strip()

    @model_validator(mode="after")
    def validate_tls_shape(self) -> Self:
        if self.enabled:
            if self.self_signed_cert and self.server_name is None:
                raise ValueError(
                    "server_name is required when deployment.tls.self_signed_cert is true"
                )
            for name in ("key_path", "certificate_path"):
                if getattr(self, name) is None:
                    raise ValueError(
                        f"{name} is required when deployment.tls.enabled is true"
                    )
        return self


CLIENT_TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
CLIENT_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}-(?:[A-Za-z]{2}|[0-9]{3})$")


class ClientRegionConfig(StrictModel):
    """The time zone and language clients should present for one node."""

    timezone: str
    language: str

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or value in {"Local", "localtime"}
            or value.startswith(("/", ".", "posix/", "right/"))
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or CLIENT_TIMEZONE_PATTERN.fullmatch(value) is None
        ):
            raise ValueError(
                "deployment.client_region.timezone must be a valid IANA timezone"
            )
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                "deployment.client_region.timezone must be a valid IANA timezone"
            ) from exc
        return value

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        value = value.strip()
        if CLIENT_LANGUAGE_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "deployment.client_region.language must look like zh-CN, with a region"
            )
        language, region = value.split("-", 1)
        return f"{language.lower()}-{region.upper()}"


class DeploymentConfig(StrictModel):
    host: str
    direct_domain_suffixes: list[str] = Field(default_factory=list)
    ip: str
    # Optional. Clients that opt in present this region while using the node.
    client_region: ClientRegionConfig | None = None
    trojan_port: int = Field(ge=1, le=65535)
    hysteria2_port: int = Field(ge=1, le=65535)
    hysteria2_port_range: str | None = None
    naive_port: int = Field(ge=1, le=65535)
    snell_port: int | None = Field(default=None, ge=1, le=65535)
    tls: TlsConfig

    @field_validator("direct_domain_suffixes")
    @classmethod
    def validate_direct_suffixes(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_domain(value) for value in values))

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError:
            return normalize_domain(value)

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("deployment.ip must be a non-empty string")
        return value.strip()

    @field_validator("hysteria2_port_range")
    @classmethod
    def validate_hysteria2_port_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        match = re.fullmatch(r"(\d{1,5}):(\d{1,5})", value.strip())
        if match is None:
            raise ValueError(
                'deployment.hysteria2_port_range must be "START:END" (e.g. "20000:50000")'
            )
        start, end = int(match[1]), int(match[2])
        if not 1 <= start <= end <= 65535:
            raise ValueError(
                "deployment.hysteria2_port_range ports must be between 1 and 65535 and start must not exceed end"
            )
        return f"{start}:{end}"

    @model_validator(mode="after")
    def validate_tls_hostname(self) -> Self:
        if self.tls.enabled and not self.tls.self_signed_cert:
            if (
                self.tls.server_name is not None
                and self.validate_host(self.tls.server_name) != self.host
            ):
                raise ValueError(
                    "deployment.tls.server_name must match deployment.host when deployment.tls.self_signed_cert is false"
                )
            self.tls.server_name = self.host
        return self


class WebPortalUser(StrictModel):
    username: str
    password_hash: str = Field(repr=False)
    enabled: bool = True
    admin: bool = False

    _username = field_validator("username")(_validate_username)
    _password_hash = field_validator("password_hash")(_validate_password_hash)


class ProtocolUser(StrictModel):
    username: str
    password: str = Field(repr=False)
    enabled: bool = True

    _username = field_validator("username")(_validate_username)


class TrojanUser(ProtocolUser):
    pass


class Hysteria2User(ProtocolUser):
    pass


class NaiveUser(ProtocolUser):
    pass


class SnellUser(ProtocolUser):
    """The inventory password is the per-account Snell userkey, not the PSK."""

    @field_validator("password")
    @classmethod
    def validate_userkey(cls, value: str) -> str:
        if (
            not 1 <= len(value.encode("utf-8")) <= 255
            or value.strip() != value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError(
                "Snell userkey must be 1 to 255 UTF-8 bytes without outer whitespace or control characters"
            )
        return value


class SnellInventory(StrictModel):
    psk: str | None = Field(default=None, repr=False)
    users: list[SnellUser] = Field(default_factory=list)

    @field_validator("psk")
    @classmethod
    def validate_psk(cls, value: str | None) -> str | None:
        if value is not None and (
            not 12 <= len(value.encode("utf-8")) <= 255
            or value.strip() != value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError(
                "Snell v6 PSK must be 12 to 255 UTF-8 bytes without outer whitespace or control characters"
            )
        return value

    @model_validator(mode="after")
    def validate_users(self) -> Self:
        _validate_unique_usernames(self.users)
        if len({user.password for user in self.users}) != len(self.users):
            raise ValueError("Snell userkeys must be unique")
        return self


class WebPortalInventory(StrictModel):
    users: list[WebPortalUser]

    @model_validator(mode="after")
    def validate_unique_usernames(self) -> Self:
        _validate_unique_usernames(self.users)
        return self


class TrojanInventory(StrictModel):
    users: list[TrojanUser]

    @model_validator(mode="after")
    def validate_unique_usernames(self) -> Self:
        _validate_unique_usernames(self.users)
        return self


class Hysteria2Inventory(StrictModel):
    obfs_password: str
    users: list[Hysteria2User]
    up_mbps: int | None = None
    down_mbps: int | None = None

    @field_validator("up_mbps", "down_mbps")
    @classmethod
    def validate_bandwidth(cls, value: int | None, info: ValidationInfo) -> int | None:
        if value is not None and value <= 0:
            raise ValueError(
                f"hysteria2.{info.field_name} must be a positive integer when set"
            )
        return value

    @field_validator("obfs_password")
    @classmethod
    def validate_obfs_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                'hysteria2.obfs_password must be a non-empty string when obfs.type is "salamander" (sing-box rejects empty obfs passwords)'
            )
        return value

    @model_validator(mode="after")
    def validate_unique_usernames(self) -> Self:
        _validate_unique_usernames(self.users)
        return self


class NaiveInventory(StrictModel):
    users: list[NaiveUser]

    @model_validator(mode="after")
    def validate_unique_usernames(self) -> Self:
        _validate_unique_usernames(self.users)
        return self


def _text(value: str, context: str) -> str:
    if not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value.strip()


class WebSettings(StrictModel):
    # Accept legacy input, but use control_plane.port and loopback for serving.
    port: int = Field(default=47070, ge=1, le=65535, exclude=True)
    host: str = Field(default="127.0.0.1", exclude=True)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    session_secret: str | None = Field(default=None, repr=False)
    subscription_secret: str | None = Field(default=None, repr=False)
    subscription_database_path: Path = DEFAULT_SUBSCRIPTION_DATABASE_PATH

    @field_validator("port", mode="before")
    @classmethod
    def validate_port(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("web.port must be an integer")
        return value

    @field_validator("host", "session_secret", "subscription_secret")
    @classmethod
    def validate_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        return None if value is None else _text(value, f"web.{info.field_name}")

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, values: list[str]) -> list[str]:
        return [_text(value, "web.allowed_hosts") for value in values]


class ControlPlaneSettings(StrictModel):
    """Django portal and control plane with a local PostgreSQL database."""

    enabled: bool = Field(default=False, strict=True)
    authoritative: bool = Field(default=False, strict=True)
    port: int = Field(default=47071, ge=1024, le=65535, strict=True)
    secret_key: str | None = Field(default=None, min_length=50, repr=False)
    database_name: str = "sbm_control_plane"
    database_socket_dir: Path = Path("/var/run/postgresql")
    database_port: int = Field(default=5432, ge=1, le=65535, strict=True)

    @model_validator(mode="after")
    def validate_authoritative_requires_enabled(self) -> Self:
        if self.authoritative and not self.enabled:
            raise ValueError(
                "control_plane.authoritative requires control_plane.enabled"
            )
        return self

    @field_validator("database_name")
    @classmethod
    def validate_database_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
            raise ValueError(
                "control_plane.database_name must be a PostgreSQL identifier"
            )
        if value in {"postgres", "template0", "template1"}:
            raise ValueError(
                "control_plane.database_name must name a dedicated database"
            )
        return value

    @field_validator("database_socket_dir")
    @classmethod
    def validate_socket_dir(cls, value: Path) -> Path:
        if not value.is_absolute() or any(char.isspace() for char in str(value)):
            raise ValueError(
                "control_plane.database_socket_dir must be an absolute path without whitespace"
            )
        return value


class ClientUpgradeSettings(StrictModel):
    policy: str = "suggested"
    message: str = ""

    @field_validator("policy")
    @classmethod
    def validate_policy(cls, value: str) -> str:
        if value.strip() not in CLIENT_UPGRADE_POLICIES:
            raise ValueError(
                "client_upgrade.policy must be one of: off, suggested, required"
            )
        return value.strip()

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 200 or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError(
                "client_upgrade.message must be a single line of at most 200 characters without control characters"
            )
        return value


def split_listen_address(value: str) -> tuple[str, int]:
    host, _, port_text = value.rpartition(":")
    return host.removeprefix("[").removesuffix("]"), int(port_text)


class TrafficStatsSettings(StrictModel):
    enabled: bool = Field(default=False, strict=True)
    database_path: Path = DEFAULT_TRAFFIC_STATS_DATABASE_PATH
    timezone: str = "Asia/Shanghai"
    relay_multiplier: float = 2.0
    api_listen: str = "127.0.0.1:19080"
    connection_api_listen: str = "127.0.0.1:19090"
    connection_api_secret: str = Field(default="", repr=False)
    domain_top_n: int = Field(default=50, ge=1, strict=True)
    show_user_domains: bool = Field(default=False, strict=True)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value.strip())
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                "traffic_stats.timezone must be a valid IANA timezone"
            ) from exc
        return value.strip()

    @field_validator("relay_multiplier", mode="before")
    @classmethod
    def validate_multiplier(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 1
        ):
            raise ValueError("traffic_stats.relay_multiplier must be a number >= 1")
        return float(value)

    @field_validator("api_listen", "connection_api_listen")
    @classmethod
    def validate_listen(cls, value: str, info: ValidationInfo) -> str:
        try:
            host, port = split_listen_address(value.strip())
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(
                f"traffic_stats.{info.field_name} must be a host:port string"
            ) from exc
        if not 1 <= port <= 65535:
            raise ValueError(
                f"traffic_stats.{info.field_name} must be a host:port string"
            )
        return value.strip()

    @field_validator("connection_api_secret")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_listeners(self) -> Self:
        if split_listen_address(self.api_listen) == split_listen_address(
            self.connection_api_listen
        ):
            raise ValueError("traffic_stats API listeners must differ")
        if not ipaddress.ip_address(
            split_listen_address(self.api_listen)[0]
        ).is_loopback:
            raise ValueError(
                "traffic_stats.api_listen must be on loopback (the counter API has no authentication)"
            )
        if (
            not self.connection_api_secret
            and not ipaddress.ip_address(
                split_listen_address(self.connection_api_listen)[0]
            ).is_loopback
        ):
            raise ValueError(
                "traffic_stats.connection_api_secret is required off loopback"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def migrate_listeners(cls, value: object) -> object:
        """Read one release's legacy inventory keys; only emit the two new keys."""
        if isinstance(value, dict):
            value = dict(value)
            for suffix, target in (
                ("api_listen", "api_listen"),
                ("connection_api_listen", "connection_api_listen"),
            ):
                old = value.pop(f"trojan_{suffix}", None)
                if old is not None:
                    value.setdefault(target, old)
                for protocol in ("hysteria2", "naive"):
                    value.pop(f"{protocol}_{suffix}", None)
        return value


class VpsInfoSettings(StrictModel):
    kiwi_veid: str = ""
    kiwi_api_key: str = Field(default="", repr=False)


VpsInfoConfig = VpsInfoSettings


class Settings(StrictModel):
    """One root model for application settings and runtime inventory.

    Settings-only fixtures can omit inventory. Inventory properties fail closed
    when missing; file loaders require the complete inventory whenever present.
    """

    sing_box_version: str = "1.14.3"
    default_protocol: str = "trojan"
    web: WebSettings = Field(default_factory=WebSettings)
    control_plane: ControlPlaneSettings = Field(default_factory=ControlPlaneSettings)
    client_upgrade: ClientUpgradeSettings = Field(default_factory=ClientUpgradeSettings)
    traffic_stats: TrafficStatsSettings = Field(default_factory=TrafficStatsSettings)
    vps_info: VpsInfoSettings = Field(default_factory=VpsInfoSettings)
    config_root: Path = CONFIG_ROOT
    releases_root: Path = RELEASES_ROOT
    upstream_cache_root: Path = UPSTREAM_CACHE_ROOT
    config_path: Path | None = None
    client_signing_key: str | None = Field(default=None, repr=False)
    deployment_data: DeploymentConfig | None = Field(
        default=None, alias="deployment", repr=False
    )
    web_portal_data: WebPortalInventory | None = Field(
        default=None, alias="web_portal", repr=False
    )
    trojan_data: TrojanInventory | None = Field(
        default=None, alias="trojan", repr=False
    )
    hysteria2_data: Hysteria2Inventory | None = Field(
        default=None, alias="hysteria2", repr=False
    )
    naive_data: NaiveInventory | None = Field(default=None, alias="naive", repr=False)
    snell: SnellInventory = Field(default_factory=SnellInventory, repr=False)

    @field_validator("sing_box_version", "client_signing_key")
    @classmethod
    def validate_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        return None if value is None else _text(value, str(info.field_name))

    @field_validator("default_protocol")
    @classmethod
    def validate_protocol(cls, value: str) -> str:
        if value.strip() not in SUPPORTED_PROTOCOLS:
            raise ValueError(
                "default_protocol must be one of: trojan, hysteria2, naive, snell"
            )
        return value.strip()

    @model_validator(mode="after")
    def validate_control_plane_port(self) -> Self:
        if not self.control_plane.enabled:
            return self
        ports = {self.control_plane.database_port}
        if self.traffic_stats.enabled:
            ports.update(
                split_listen_address(address)[1]
                for address in (
                    self.traffic_stats.api_listen,
                    self.traffic_stats.connection_api_listen,
                )
            )
        if self.deployment_data is not None:
            ports.update(
                (self.deployment_data.trojan_port, self.deployment_data.naive_port)
            )
        if self.control_plane.port in ports:
            raise ValueError("control_plane.port must differ from configured TCP ports")
        return self

    @model_validator(mode="after")
    def validate_snell(self) -> Self:
        port = None if self.deployment_data is None else self.deployment_data.snell_port
        configured = self.snell.psk is not None
        if configured != (port is not None):
            raise ValueError("Snell requires both snell.psk and deployment.snell_port")
        if self.snell.users and not configured:
            raise ValueError("Snell users require configured Snell infrastructure")
        if self.default_protocol == "snell" and not configured:
            raise ValueError(
                "The Snell default protocol requires configured Snell infrastructure"
            )
        if configured:
            version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", self.sing_box_version)
            if version is None or tuple(map(int, version.groups())) < (1, 14, 0):
                raise ValueError("Snell v6 requires sing-box 1.14.0 or later")
            deployment = self.deployment
            ports = {80, 443, deployment.trojan_port, deployment.naive_port}
            if self.control_plane.enabled:
                ports.update(
                    (self.control_plane.port, self.control_plane.database_port)
                )
            if self.traffic_stats.enabled:
                ports.update(
                    split_listen_address(address)[1]
                    for address in (
                        self.traffic_stats.api_listen,
                        self.traffic_stats.connection_api_listen,
                    )
                )
            if port in ports:
                raise ValueError(
                    "deployment.snell_port must differ from configured TCP ports"
                )
        return self

    @model_validator(mode="after")
    def validate_inventory(self) -> Self:
        values = [
            self.deployment_data,
            self.web_portal_data,
            self.trojan_data,
            self.hysteria2_data,
            self.naive_data,
        ]
        if any(value is not None for value in values) and any(
            value is None for value in values
        ):
            raise ValueError(
                "Runtime inventory requires deployment, web_portal, trojan, hysteria2 and naive"
            )
        return self

    @classmethod
    def from_dict(cls, value: object, *, config_path: Path | None = None) -> Self:
        if not isinstance(value, dict) or "sing_box_version" not in value:
            raise ValueError("Missing required setting: sing_box_version")
        return cls.model_validate({**value, "config_path": config_path})

    @property
    def deployment(self) -> DeploymentConfig:
        if self.deployment_data is None:
            raise ValueError("Runtime inventory requires deployment")
        return self.deployment_data

    @property
    def web_portal(self) -> WebPortalInventory:
        if self.web_portal_data is None:
            raise ValueError("Runtime inventory requires web_portal")
        return self.web_portal_data

    @property
    def trojan(self) -> TrojanInventory:
        if self.trojan_data is None:
            raise ValueError("Runtime inventory requires trojan")
        return self.trojan_data

    @property
    def hysteria2(self) -> Hysteria2Inventory:
        if self.hysteria2_data is None:
            raise ValueError("Runtime inventory requires hysteria2")
        return self.hysteria2_data

    @property
    def naive(self) -> NaiveInventory:
        if self.naive_data is None:
            raise ValueError("Runtime inventory requires naive")
        return self.naive_data

    @property
    def generated_root(self) -> Path:
        return self.config_root / "generated"

    def require_config_path(self) -> Path:
        if self.config_path is None:
            raise ValueError("A config path is required for this operation")
        return self.config_path

    def without_accounts(self) -> Self:
        """Keep infrastructure settings without copying inventory account records."""
        return self.model_copy(
            update={
                "web_portal_data": WebPortalInventory(users=[]),
                "trojan_data": TrojanInventory(users=[]),
                "hysteria2_data": self.hysteria2.model_copy(update={"users": []}),
                "naive_data": NaiveInventory(users=[]),
                "snell": self.snell.model_copy(update={"users": []}),
            }
        )

    @property
    def release_info_path(self) -> Path:
        return self.generated_root / "release-info.json"

    @property
    def deployment_info_path(self) -> Path:
        return self.generated_root / "deployment-info.json"

    @property
    def allowed_hosts(self) -> list[str]:
        return self.web.allowed_hosts

    @property
    def session_secret(self) -> str | None:
        return self.web.session_secret

    @property
    def release_name(self) -> str:
        return f"sing-box-v{self.sing_box_version}"

    @property
    def release_dir(self) -> Path:
        return self.releases_root / self.release_name


# Existing type names refer to the same root model, not a second validation path.
RuntimeInventory = Settings


def load_config_text(path: Path) -> str:
    if path.name.endswith((".sops.yaml", ".sops.yml")):
        load_local_env()
        return subprocess.run(
            ["sops", "--decrypt", str(path)], check=True, capture_output=True, text=True
        ).stdout
    return path.read_text(encoding="utf-8")


def resolve_settings_path(path: Path | str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def load_settings(path: Path | str) -> Settings:
    resolved = resolve_settings_path(path)
    return Settings.from_dict(
        yaml.safe_load(load_config_text(resolved)) or {}, config_path=resolved
    )


def load_runtime_inventory(path: Path) -> RuntimeInventory:
    inventory = Settings.model_validate(yaml.safe_load(load_config_text(path)) or {})
    # Accessing this property rejects settings-only files before an operation.
    _ = inventory.deployment
    return inventory
