"""Prepare two pinned real engines and signed disposable lifecycle releases."""
# ruff: noqa: INP001

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

# Digests of the official Linux amd64 release assets. Neither archive is a stub.
ENGINES = {
    "1.14.2": "a684484d7477d1437282ee411f4d131d0340aaad60a7868841ebd5d87dd8a0c6",
    "1.14.3": "e2bdf179c15a3652955dc44867e33ca0965c9c9b91b34267dcc5a5d639a5feee",
}


def checked_archive(path: Path, version: str) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != ENGINES[version]:
        raise ValueError(f"Official engine archive digest mismatch: {version}")
    files = {}
    with tarfile.open(path) as archive:
        for member in archive:
            name = Path(member.name).name
            if name not in {"sing-box", "libcronet.so"}:
                continue
            if not member.isfile() or name in files:
                raise ValueError("Invalid engine archive member")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Unreadable engine archive member")
            files[name] = hashlib.sha256(stream.read()).hexdigest()
    if "sing-box" not in files:
        raise ValueError("Engine archive has no executable")
    return {"version": version, "archive_sha256": digest, "files": files}


def prepare(assets: Path, cache: Path) -> None:
    module_path = Path(__file__).with_name("release.py")
    spec = importlib.util.spec_from_file_location("fixture_release", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the fixture signer")
    release_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release_module)
    cache.mkdir(parents=True, exist_ok=True)
    # Retain the newly built client. The active symlink initially serves old.
    (assets / "release").rename(assets / "new-release")
    receipts = {}
    for index, (version, _digest) in enumerate(ENGINES.items()):
        name = f"sing-box-{version}-linux-amd64.tar.gz"
        override = os.environ.get(
            "SBC_TUN_E2E_OLD_ARCHIVE" if index == 0 else "SBC_TUN_E2E_NEW_ARCHIVE"
        )
        archive = Path(override) if override else cache / name
        if not archive.is_file():
            if override:
                raise FileNotFoundError(archive)
            # The caller clears metered proxy variables. Never retry via a proxy.
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--proto",
                    "=https",
                    "--max-time",
                    "180",
                    "--output",
                    str(archive),
                    f"https://github.com/SagerNet/sing-box/releases/download/v{version}/{name}",
                ],
                check=True,
            )
        receipts[version] = checked_archive(archive, version)
        release = assets / ("old-release" if index == 0 else "new-release")
        destination = release / "sing-box" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, destination)
        if index == 0:
            client = release / "sbc/linux-amd64/sbc"
            client.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(assets / "sbc", client)
        release_module.sign_release(
            assets,
            release=release,
            sbc_version="tun-e2e-old" if index == 0 else "tun-e2e-new",
            engine_version=version,
        )
    (assets / "engines.json").write_text(json.dumps(receipts, indent=2) + "\n")
    (assets / "release").symlink_to("old-release", target_is_directory=True)
    root = Path(__file__).resolve().parents[3]
    shutil.copyfile(
        root / "sing_box_manager/web/client/install.sh", assets / "install.sh"
    )
    shutil.copyfile(Path(__file__).with_name("lifecycle.py"), assets / "lifecycle.py")


def boot_result(text: str, identity: str, boot: int, boots: int) -> str:
    def marker(value: str) -> bool:
        return (
            re.search(r"(?:^|: )" + re.escape(value) + r"\s*$", text, re.MULTILINE)
            is not None
        )

    if marker(f"TUN_E2E_RESULT={identity}:FAIL"):
        return "FAIL"
    if marker(f"TUN_E2E_RESULT={identity}:PASS"):
        return "PASS" if boot == boots else "FAIL"
    phases = {1: "committed", 2: "pending", 3: "dirty"}
    if (
        boots == 4
        and boot in phases
        and marker(f"TUN_LIFECYCLE_REBOOT={identity}:" + phases[boot])
    ):
        return "REBOOT"
    return "FAIL"


if __name__ == "__main__":
    if sys.argv[1] == "result":
        print(
            boot_result(
                Path(sys.argv[2]).read_text(),
                sys.argv[3],
                int(sys.argv[4]),
                int(sys.argv[5]),
            )
        )
    else:
        prepare(Path(sys.argv[1]), Path(sys.argv[2]))
