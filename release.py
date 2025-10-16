#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys

import dotenv  # type: ignore[import-not-found]
from tqdm import tqdm  # type: ignore[import-untyped]

root_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(root_dir)
env_file = os.path.join(root_dir, ".env")
if os.path.exists(env_file):
    env = dotenv.dotenv_values(env_file)
else:
    print("Error: '.env' does not exist")
    sys.exit(1)

sing_box_version = str(env.get("SING_BOX_VERSION"))
config_git_repo = str(env.get("CONFIG_GIT_REPO"))
config_git_hash = str(env.get("CONFIG_GIT_HASH"))
trojan_server_config = os.path.abspath(str(env.get("TROJAN_SERVER_CONFIG")))
hysteria2_server_config = os.path.abspath(str(env.get("HYSTERIA2_SERVER_CONFIG")))
trojan_client_config = os.path.abspath(str(env.get("TROJAN_CLIENT_CONFIG")))
hysteria2_client_config = os.path.abspath(str(env.get("HYSTERIA2_CLIENT_CONFIG")))
trojan_tun_client_config = os.path.abspath(str(env.get("TROJAN_TUN_CLIENT_CONFIG")))

if os.path.exists(config_git_repo):
    os.chdir(config_git_repo)
    subprocess.run(["git", "reset", "--hard", "--quiet"])
    subprocess.run(["git", "checkout", config_git_hash, "--quiet"])
else:
    print("CONFIG_GIT_REPO=", config_git_repo, " doesn't exist", sep="")
    sys.exit(1)

os.chdir(root_dir)
release_dir = os.path.join(
    root_dir, "releases", "sing-box-v" + sing_box_version + "-" + config_git_hash
)
os.makedirs(release_dir, exist_ok=True)

official_releases = [
    {
        "platform": "linux-amd64",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v"
        + sing_box_version
        + "/sing-box-"
        + sing_box_version
        + "-linux-amd64.tar.gz",
        "path": "",
    },
    {
        "platform": "windows-amd64",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v"
        + sing_box_version
        + "/sing-box-"
        + sing_box_version
        + "-windows-amd64.zip",
        "path": "",
    },
    {
        "platform": "linux-arm64",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v"
        + sing_box_version
        + "/sing-box-"
        + sing_box_version
        + "-linux-arm64.tar.gz",
        "path": "",
    },
    {
        "platform": "android-arm64",
        "url": "https://github.com/SagerNet/sing-box/releases/download/v"
        + sing_box_version
        + "/SFA-"
        + sing_box_version
        + "-universal.apk",
        "path": "",
    },
]

print("Download prebuilt binaries from SagerNet/sing-box on Github.")
for official_release in tqdm(official_releases):
    official_release["path"] = os.path.join(
        release_dir, os.path.basename(official_release["url"])
    )
    if not os.path.exists(official_release["path"]):
        subprocess.run(
            [
                "wget",
                official_release["url"],
                "--no-check-certificate",
                "--no-verbose",
                "-O",
                official_release["path"],
            ]
        )

os.chdir(release_dir)
for official_release in tqdm(official_releases):
    if official_release["path"].endswith(".tar.gz"):
        subprocess.run(
            [
                "tar",
                "xf",
                official_release["path"],
            ]
        )
    elif official_release["path"].endswith(".zip"):
        subprocess.run(
            [
                "unzip",
                official_release["path"],
            ]
        )

users = {}
with open(trojan_server_config, "r") as server_config_file:
    users = json.loads(server_config_file.read())["inbounds"][0]["users"]

print("Parse", len(users), "users information from the server configuration.")
print("Copy execuables on all platforms for each user.")
for i, user in tqdm(enumerate(users)):
    user_dir = os.path.join(
        release_dir, os.path.basename(release_dir) + "-" + str(user["name"])
    )
    os.makedirs(user_dir, exist_ok=True)
    for official_release in official_releases:
        user_platform_dir = os.path.join(user_dir, official_release["platform"])
        os.makedirs(user_platform_dir, exist_ok=True)
        basename, _ = os.path.splitext(official_release["path"])
        if official_release["platform"] == "linux-amd64":
            official_release_extraction = official_release["path"].rstrip(".tar.gz")
            print(official_release_extraction)
            for filename in os.listdir(official_release_extraction):
                shutil.copy(
                    os.path.join(official_release_extraction, filename),
                    user_platform_dir,
                )
        elif official_release["platform"] == "linux-arm64":
            official_release_extraction = official_release["path"].rstrip(".tar.gz")
            for filename in os.listdir(official_release_extraction):
                shutil.copy(
                    os.path.join(official_release_extraction, filename),
                    user_platform_dir,
                )
        elif official_release["platform"] == "windows-amd64":
            official_release_extraction = official_release["path"].rstrip(".zip")
            for filename in os.listdir(official_release_extraction):
                shutil.copy(
                    os.path.join(official_release_extraction, filename),
                    user_platform_dir,
                )
        elif official_release["platform"] == "android-arm64":
            shutil.copy(official_release["path"], user_platform_dir)
        else:
            sys.exit(1)

print("Prepare configuration files for all platforms for each user.")
for official_release in official_releases:
    if official_release["platform"] == "linux-amd64":
        print(official_release["platform"])
        with open(file=trojan_client_config, mode="r") as trojan_client_config_file:
            trojan_client_config_dict = json.loads(trojan_client_config_file.read())
            for i, user in tqdm(enumerate(users)):
                trojan_client_config_dict["inbounds"][0]["set_system_proxy"] = False
                trojan_client_config_dict["outbounds"][0]["password"] = user["password"]
                user_dir = os.path.join(
                    release_dir, os.path.basename(release_dir) + "-" + str(user["name"])
                )
                user_trojan_client_config_path = os.path.join(
                    user_dir,
                    official_release["platform"],
                    os.path.basename(trojan_client_config),
                )
                with open(
                    file=user_trojan_client_config_path, mode="w"
                ) as user_client_config_file:
                    json.dump(
                        trojan_client_config_dict, user_client_config_file, indent=4
                    )

                shutil.copy(
                    os.path.join(root_dir, "scripts", "install.sh"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "tui_utils.sh"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "docs", "README_LINUX.md"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "sing-box-trojan.service"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "sing-box-hysteria2.service"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "setup.sh"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                os.makedirs(
                    name=os.path.join(user_dir, official_release["platform"], "lib"),
                    exist_ok=True,
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "lib", "ui.sh"),
                    os.path.join(user_dir, official_release["platform"], "lib"),
                )

                if user["name"] == "xiaoshuqi":
                    user_hysteria2_client_config_path = os.path.join(
                        user_dir,
                        official_release["platform"],
                        os.path.basename(hysteria2_client_config),
                    )
                    shutil.copy(
                        os.path.join(
                            root_dir, "config", "client", "hysteria2-client.json"
                        ),
                        user_hysteria2_client_config_path,
                    )

    elif official_release["platform"] == "linux-arm64":
        print(official_release["platform"])
        with open(file=trojan_client_config, mode="r") as trojan_client_config_file:
            trojan_client_config_dict = json.loads(trojan_client_config_file.read())
            for i, user in tqdm(enumerate(users)):
                trojan_client_config_dict["inbounds"][0]["set_system_proxy"] = False
                trojan_client_config_dict["outbounds"][0]["password"] = user["password"]
                user_dir = os.path.join(
                    release_dir, os.path.basename(release_dir) + "-" + str(user["name"])
                )
                user_trojan_client_config_path = os.path.join(
                    user_dir,
                    official_release["platform"],
                    os.path.basename(trojan_client_config),
                )
                with open(
                    file=user_trojan_client_config_path, mode="w"
                ) as user_client_config_file:
                    json.dump(
                        trojan_client_config_dict, user_client_config_file, indent=4
                    )

                shutil.copy(
                    os.path.join(root_dir, "scripts", "install.sh"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "tui_utils.sh"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "docs", "README_LINUX.md"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "sing-box-trojan.service"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "sing-box-hysteria2.service"),
                    os.path.join(user_dir, official_release["platform"]),
                )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "setup.sh"),
                    os.path.join(user_dir, official_release["platform"]),
                )

                if user["name"] == "xiaoshuqi":
                    user_hysteria2_client_config_path = os.path.join(
                        user_dir,
                        official_release["platform"],
                        os.path.basename(hysteria2_client_config),
                    )
                    shutil.copy(
                        os.path.join(
                            root_dir, "config", "client", "hysteria2-client.json"
                        ),
                        user_hysteria2_client_config_path,
                    )

    elif official_release["platform"] == "windows-amd64":
        print(official_release["platform"])
        with open(file=trojan_client_config, mode="r") as trojan_client_config_file:
            trojan_client_config_dict = json.loads(trojan_client_config_file.read())
            for i, user in tqdm(enumerate(users)):
                trojan_client_config_dict["inbounds"][0]["set_system_proxy"] = True
                trojan_client_config_dict["outbounds"][0]["password"] = user["password"]
                user_dir = os.path.join(
                    release_dir, os.path.basename(release_dir) + "-" + str(user["name"])
                )
                user_trojan_client_config_path = os.path.join(
                    user_dir,
                    official_release["platform"],
                    os.path.basename(trojan_client_config),
                )
                with open(
                    file=user_trojan_client_config_path, mode="w"
                ) as user_client_config_file:
                    json.dump(
                        trojan_client_config_dict, user_client_config_file, indent=4
                    )
                shutil.copy(
                    os.path.join(root_dir, "scripts", "start_proxy.bat"),
                    os.path.join(user_dir, official_release["platform"]),
                )
    elif official_release["platform"] == "android-arm64":
        print(official_release["platform"])
        with open(file=trojan_tun_client_config, mode="r") as trojan_client_config_file:
            trojan_client_config_dict = json.loads(trojan_client_config_file.read())
            for i, user in tqdm(enumerate(users)):
                trojan_client_config_dict["outbounds"][0]["password"] = user["password"]
                user_dir = os.path.join(
                    release_dir, os.path.basename(release_dir) + "-" + str(user["name"])
                )
                user_trojan_client_config_path = os.path.join(
                    user_dir,
                    official_release["platform"],
                    os.path.basename(trojan_tun_client_config),
                )
                with open(
                    file=user_trojan_client_config_path, mode="w"
                ) as user_client_config_file:
                    json.dump(
                        trojan_client_config_dict, user_client_config_file, indent=4
                    )
    else:
        sys.exit(1)

print("Package per-platform releases.")
for i, user in tqdm(enumerate(users)):
    user_dir = os.path.join(
        release_dir, os.path.basename(release_dir) + "-" + str(user["name"])
    )

    # Linux package (tar.gz)
    linux_dir = os.path.join(user_dir, "linux-amd64")
    if os.path.isdir(linux_dir):
        archive_base = os.path.join(
            user_dir, os.path.basename(user_dir) + "-linux-amd64"
        )
        shutil.make_archive(
            archive_base, "gztar", root_dir=user_dir, base_dir="linux-amd64"
        )
        dest_path = archive_base + ".tar.gz"
        if os.path.exists(os.path.join(release_dir, os.path.basename(dest_path))):
            os.remove(os.path.join(release_dir, os.path.basename(dest_path)))
        shutil.move(dest_path, release_dir)

    # Linux ARM64 package (tar.gz)
    linux_arm64_dir = os.path.join(user_dir, "linux-arm64")
    if os.path.isdir(linux_arm64_dir):
        archive_base = os.path.join(
            user_dir, os.path.basename(user_dir) + "-linux-arm64"
        )
        shutil.make_archive(
            archive_base, "gztar", root_dir=user_dir, base_dir="linux-arm64"
        )
        dest_path = archive_base + ".tar.gz"
        if os.path.exists(os.path.join(release_dir, os.path.basename(dest_path))):
            os.remove(os.path.join(release_dir, os.path.basename(dest_path)))
        shutil.move(dest_path, release_dir)

    # Windows package (zip)
    windows_dir = os.path.join(user_dir, "windows-amd64")
    if os.path.isdir(windows_dir):
        archive_base = os.path.join(
            user_dir, os.path.basename(user_dir) + "-windows-amd64"
        )
        shutil.make_archive(
            archive_base, "zip", root_dir=user_dir, base_dir="windows-amd64"
        )
        dest_path = archive_base + ".zip"
        if os.path.exists(os.path.join(release_dir, os.path.basename(dest_path))):
            os.remove(os.path.join(release_dir, os.path.basename(dest_path)))
        shutil.move(dest_path, release_dir)

    # Android package (zip)
    android_dir = os.path.join(user_dir, "android-arm64")
    if os.path.isdir(android_dir):
        archive_base = os.path.join(
            user_dir, os.path.basename(user_dir) + "-android-arm64"
        )
        shutil.make_archive(
            archive_base, "zip", root_dir=user_dir, base_dir="android-arm64"
        )
        dest_path = archive_base + ".zip"
        if os.path.exists(os.path.join(release_dir, os.path.basename(dest_path))):
            os.remove(os.path.join(release_dir, os.path.basename(dest_path)))
        shutil.move(dest_path, release_dir)

print("Release for server.")
server_dir = os.path.join(release_dir, os.path.basename(release_dir) + "-" + "server")
os.makedirs(server_dir, exist_ok=True)
official_release_path = official_releases[0]["path"]
if official_release_path and official_release_path.endswith(".tar.gz"):
    official_release_extraction = official_release_path[:-7]
    for filename in os.listdir(official_release_extraction):
        shutil.copy(os.path.join(official_release_extraction, filename), server_dir)

shutil.copy(hysteria2_server_config, server_dir)
shutil.copy(trojan_server_config, server_dir)
shutil.copy(os.path.join(root_dir, "scripts", "install.sh"), server_dir)
shutil.copy(os.path.join(root_dir, "scripts", "tui_utils.sh"), server_dir)
shutil.copy(os.path.join(root_dir, "scripts", "sing-box-trojan.service"), server_dir)
shutil.copy(os.path.join(root_dir, "scripts", "sing-box-hysteria2.service"), server_dir)
subprocess.run(
    (
        [
            "tar",
            "-czf",
            os.path.basename(server_dir) + ".tar.gz",
            os.path.basename(server_dir),
        ]
    )
)

os.chdir(root_dir)
if os.path.exists(config_git_repo):
    os.chdir(config_git_repo)
    subprocess.run(["git", "checkout", "main"])

print("Done.")
