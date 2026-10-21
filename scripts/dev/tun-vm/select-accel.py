"""Select KVM only after QEMU initializes the acceptance machine successfully."""
# ruff: noqa: INP001

import json
import subprocess
import sys


def select_accelerator() -> str:
    # Match the acceptance CPU, memory and vCPU settings. Do not boot a guest.
    command = [
        "qemu-system-x86_64",
        "-machine",
        "accel=kvm",
        "-cpu",
        "max",
        "-m",
        "2048",
        "-smp",
        "2",
        "-nodefaults",
        "-display",
        "none",
        "-S",
        "-qmp",
        "stdio",
    ]
    commands = [
        {"execute": "qmp_capabilities"},
        {"execute": "query-kvm", "id": "kvm-probe"},
        {"execute": "quit"},
    ]
    try:
        result = subprocess.run(
            command,
            input="".join(json.dumps(item) + "\n" for item in commands),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("KVM startup probe timed out. Selecting TCG.", file=sys.stderr)
        return "tcg"
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(message, dict)
                and message.get("id") == "kvm-probe"
                and message.get("return") == {"enabled": True, "present": True}
            ):
                print("KVM startup probe passed. Selecting KVM.", file=sys.stderr)
                return "kvm"
    print("KVM startup probe failed. Selecting TCG.", file=sys.stderr)
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    return "tcg"


if __name__ == "__main__":
    print(select_accelerator())
