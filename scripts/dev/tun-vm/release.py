"""Sign disposable fixture downloads without changing production trust keys."""
# ruff: noqa: INP001

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def sign_release(
    assets: Path,
    choice: str = "good",
    *,
    release: Path | None = None,
    sbc_version: str = "tun-e2e-new",
    engine_version: str | None = None,
) -> None:
    release = release or assets / "release"
    rules = release / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    source = assets / ("changed-rules" if choice == "changed" else "rules")
    for path in source.glob("*.srs"):
        shutil.copy2(path, rules / path.name)
    files = []
    for path in sorted(release.rglob("*")):
        if not path.is_file() or path.name in {"manifest.json", "manifest.json.sig"}:
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(release).as_posix(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = release / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "sbc": sbc_version,
                "sing_box": engine_version
                or (assets / "sing-box.version").read_text().strip(),
                "files": files,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    signature = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-sign",
            "-rawin",
            "-inkey",
            str(assets.parent / "signing.key"),
            "-in",
            str(manifest),
        ],
        check=True,
        capture_output=True,
    ).stdout
    (release / "manifest.json.sig").write_bytes(base64.b64encode(signature) + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assets", type=Path)
    parser.add_argument("--choice", choices=["good", "changed"], default="good")
    args = parser.parse_args()
    sign_release(args.assets, args.choice)


if __name__ == "__main__":
    main()
