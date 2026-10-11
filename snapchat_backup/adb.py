"""Talking to an Android phone over USB with adb (Android platform-tools).

Only three kinds of operations touch the phone:
  * pull: copy the gallery folders to the computer (read-only for the phone);
  * push: copy new files into one new folder (default DCIM/Snapchat Memories),
    never overwriting an existing file;
  * clean (opt-in): move redundant copies into a review folder that holds a
    `.nomedia` file, which hides them from the gallery without deleting them.
"""

import logging
import shlex
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

PHONE_ROOT = "/sdcard"


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, serial=None, exe=None):
        self.exe = exe or shutil.which("adb")
        if not self.exe:
            raise AdbError(
                "adb not found. Install Android platform-tools (Windows: `winget install "
                "Google.PlatformTools`, macOS: `brew install android-platform-tools`, Linux: "
                "`sudo apt install adb`) or copy your gallery to the computer and use --gallery."
            )
        self.serial = serial

    def _cmd(self, *args):
        return [self.exe] + (["-s", self.serial] if self.serial else []) + [str(a) for a in args]

    def run(self, *args, check=True, show_progress=False):
        if show_progress:  # let adb print its own progress to the terminal
            proc = subprocess.run(self._cmd(*args), stdout=None, stderr=subprocess.PIPE, text=True,
                                  encoding="utf-8", errors="replace")
        else:
            proc = subprocess.run(self._cmd(*args), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise AdbError(f"adb {' '.join(map(str, args[:2]))} failed: {(proc.stderr or proc.stdout or '').strip()[-300:]}")
        return proc

    def shell(self, script, check=True):
        return self.run("shell", script, check=check).stdout

    # ------------------------------------------------------------------
    def check_device(self):
        lines = self.run("devices").stdout.strip().splitlines()[1:]
        devices = [line.split("\t") for line in lines if "\t" in line]
        if self.serial:
            devices = [d for d in devices if d[0] == self.serial]
        if not devices:
            raise AdbError(
                "No phone found. Connect it with a USB cable, enable Developer options > USB "
                "debugging, and accept the prompt on the phone."
            )
        if len(devices) > 1:
            raise AdbError("Several devices connected; choose one with --adb-serial " + ", ".join(d[0] for d in devices))
        serial, state = devices[0][0], devices[0][1].strip()
        if state == "unauthorized":
            raise AdbError("The phone has not authorised this computer yet: unlock it and accept the USB debugging prompt.")
        if state != "device":
            raise AdbError(f"Phone is {state!r}; reconnect it and try again.")
        self.serial = serial
        model = self.shell("getprop ro.product.model", check=False).strip()
        return model or serial

    def exists(self, remote):
        return self.shell(f"[ -e {shlex.quote(remote)} ] && echo yes || echo no", check=False).strip() == "yes"

    def pull_folder(self, rel, mirror: Path):
        """Copy PHONE_ROOT/rel into mirror/rel. Returns False if it does not exist."""
        remote = f"{PHONE_ROOT}/{rel.strip('/')}"
        if not self.exists(remote):
            return False
        parent = mirror / Path(rel.strip("/")).parent
        parent.mkdir(parents=True, exist_ok=True)
        self.run("pull", "-a", remote, str(parent), show_progress=True)
        return True

    def list_files(self, remote_dir):
        """{name: size} of the files directly inside remote_dir."""
        out = self.shell(
            f"cd {shlex.quote(remote_dir)} 2>/dev/null && for f in *; do [ -f \"$f\" ] && "
            f"echo \"$(stat -c %s \"$f\")/$f\"; done",
            check=False,
        )
        files = {}
        for line in out.splitlines():
            size, _, name = line.partition("/")
            if size.isdigit() and name:
                files[name] = int(size)
        return files

    def mkdir(self, remote):
        self.shell(f"mkdir -p {shlex.quote(remote)}")

    def push_files(self, files, remote_dir, batch=100):
        """Copy files into remote_dir (adb keeps their modification time)."""
        self.mkdir(remote_dir)
        files = list(files)
        for i in range(0, len(files), batch):
            chunk = files[i:i + batch]
            self.run("push", *[str(f) for f in chunk], remote_dir.rstrip("/") + "/")
            log.info("  pushed %d/%d", min(i + batch, len(files)), len(files))

    def move(self, remote_src, remote_dst):
        self.shell(f"mv -n {shlex.quote(remote_src)} {shlex.quote(remote_dst)}")

    def touch(self, remote):
        self.shell(f"touch {shlex.quote(remote)}")

    def media_scan(self, remote_paths, batch=50):
        """Ask the media scanner to index new/moved files. Android 11+ notices
        them by itself; on older versions this makes them show up immediately."""
        paths = list(remote_paths)
        for i in range(0, len(paths), batch):
            script = "; ".join(
                "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "
                + shlex.quote("file://" + p) + " >/dev/null 2>&1"
                for p in paths[i:i + batch]
            )
            self.shell(script, check=False)
