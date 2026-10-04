"""Platform and protocol definitions for release packaging."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Protocol:
    """A VPN protocol configuration."""

    name: str


PROTOCOLS = [
    Protocol(name="trojan"),
    Protocol(name="hysteria2"),
    Protocol(name="naive"),
]

ROUTE_STRATEGIES = ("china", "gfw", "ai", "global")


@dataclass(frozen=True)
class ClientProfile:
    """One client config profile a platform's archive carries.

    ``set_system_proxy`` is ``None`` when the key must not be emitted at all. A
    TUN inbound has no system-proxy toggle, so adding one would produce a config
    `sing-box check` rejects.
    """

    name: str
    filename_infix: str
    set_system_proxy: bool | None


# The desktop mixed inbound (default port 1080, auto-detected at runtime).
# The `proxy` helper owns the system proxy on every platform we ship, so
# sing-box must not set it itself.
MIXED_PROFILE = ClientProfile(name="mixed", filename_infix="", set_system_proxy=False)

# Android/SFA, where the platform owns the tunnel and there is nothing for this
# repository to provision.
MOBILE_TUN_PROFILE = ClientProfile(
    name="mobile-tun", filename_infix="-tun", set_system_proxy=None
)


def client_config_filename(protocol: str, profile: ClientProfile) -> str:
    """Archive filename holding one protocol's config for one profile."""
    return f"{protocol}{profile.filename_infix}-client.json"


def route_client_config_filename(protocol: str, strategy: str) -> str:
    """Return the archive filename for a desktop mixed routing strategy."""
    if strategy not in ROUTE_STRATEGIES:
        raise ValueError(f"Unsupported route strategy: {strategy}")
    if strategy == "china":
        return f"{protocol}-client.json"
    return f"{protocol}-{strategy}-client.json"


@dataclass
class Platform:
    """A target platform for sing-box distribution.

    A platform without client profiles gets no per-user archive. Its upstream
    download serves the sbc downloads, and the Linux one also runs at build
    time.
    """

    name: str
    url_filename: str  # filename template (version substituted at build time)
    client_profiles: tuple[ClientProfile, ...]  # rendered configs, in archive order
    package_format_override: str | None = None
    package_ext_override: str | None = None
    extras: list[str] = field(default_factory=list)  # files to copy (relative to root)
    protocol_extras: dict[str, list[str]] = field(
        default_factory=dict
    )  # protocol -> extra files

    @property
    def os_name(self) -> str:
        """Operating system this archive targets: linux, darwin, or windows.

        Client configs are rendered per platform, and the system resolver the
        ai route uses is the one setting that differs between them.
        """
        return self.name.split("-", 1)[0]

    @property
    def download_format(self) -> str:
        """Archive format of the upstream download."""
        if self.url_filename.endswith(".tar.gz"):
            return "tar.gz"
        if self.url_filename.endswith(".zip"):
            return "zip"
        return "apk"

    @property
    def package_format(self) -> str:
        """Format string for shutil.make_archive."""
        if self.package_format_override is not None:
            return self.package_format_override
        return "gztar" if self.download_format == "tar.gz" else "zip"

    @property
    def package_ext(self) -> str:
        """File extension for the packaged user archive."""
        if self.package_ext_override is not None:
            return self.package_ext_override
        return ".tar.gz" if self.download_format == "tar.gz" else ".zip"

    def local_filename(self, version: str) -> str:
        """Local filename for the downloaded archive."""
        return self.url_filename.format(version=version)

    def extraction_dir(self, download_path: Path) -> Path | None:
        """Directory created after extracting the download, or None for APKs."""
        name = str(download_path)
        if self.download_format == "tar.gz":
            return Path(name[:-7])  # strip ".tar.gz"
        if self.download_format == "zip":
            return Path(name[:-4])  # strip ".zip"
        return None  # APK — no extraction


PLATFORMS = [
    # Linux and macOS install sbc, which fetches its config from the portal, so
    # these platforms have no per-user archive.
    Platform(
        name="linux-amd64",
        url_filename="sing-box-{version}-linux-amd64.tar.gz",
        client_profiles=(),
    ),
    Platform(
        name="linux-arm64",
        url_filename="sing-box-{version}-linux-arm64.tar.gz",
        client_profiles=(),
    ),
    Platform(
        name="darwin-arm64",
        url_filename="sing-box-{version}-darwin-arm64.tar.gz",
        client_profiles=(),
    ),
    Platform(
        name="darwin-amd64",
        url_filename="sing-box-{version}-darwin-amd64.tar.gz",
        client_profiles=(),
    ),
    Platform(
        name="windows-amd64",
        url_filename="sing-box-{version}-windows-amd64.zip",
        client_profiles=(),
    ),
    Platform(
        name="android-arm64",
        url_filename="sing-box-{version}-android-arm64.tar.gz",
        client_profiles=(MOBILE_TUN_PROFILE,),
        package_format_override="zip",
        package_ext_override=".zip",
        extras=[],
        protocol_extras={},
    ),
]

# The platforms that get a per-user archive.
ARCHIVE_PLATFORMS = tuple(
    platform for platform in PLATFORMS if platform.client_profiles
)
