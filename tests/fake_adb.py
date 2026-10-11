"""A stand-in for `adb` that maps the phone's /sdcard to a local folder
(FAKE_ADB_ROOT). Supports the commands snapchat_backup uses."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_ADB_ROOT"])
LOG = os.environ.get("FAKE_ADB_LOG")


def local(remote):
    assert remote.startswith("/sdcard"), remote
    return ROOT / remote[len("/sdcard"):].lstrip("/")


def main(args):
    if LOG:
        with open(LOG, "a") as f:
            f.write(" ".join(args) + "\n")
    if args[:1] == ["-s"]:
        args = args[2:]
    cmd = args[0]
    if cmd == "devices":
        print("List of devices attached\nFAKE0001\tdevice\n")
        return 0
    if cmd == "shell":
        script = " ".join(args[1:]).replace("/sdcard", str(ROOT))
        return subprocess.run(["sh", "-c", script]).returncode
    if cmd == "pull":
        rest = [a for a in args[1:] if a != "-a"]
        src, dst = local(rest[0]), Path(rest[1])
        shutil.copytree(src, dst / src.name, dirs_exist_ok=True)
        return 0
    if cmd == "push":
        *files, dest = args[1:]
        target = local(dest)
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, target / Path(f).name)
        return 0
    print(f"fake adb: unsupported {args}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
