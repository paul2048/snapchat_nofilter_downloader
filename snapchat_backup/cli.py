"""Command line interface."""

import argparse
import glob
import logging
import os
import sys
from pathlib import Path

from .adb import AdbError
from .media import MediaError
from .pipeline import Backup, Options
from .segments import SegmentConfig

EXAMPLES = """
examples:
  # Phone connected with USB debugging: compare, then copy only new files to the phone
  python -m snapchat_backup --export ~/Downloads/mydata~1700000000*.zip --adb

  # Same, and also hide redundant copies already on the phone (moved, never deleted)
  python -m snapchat_backup --export ~/Downloads/mydata~*.zip --adb --clean-phone

  # No adb: compare against a copy of the phone's DCIM/Pictures/Movies folders
  python -m snapchat_backup --export mydata~1700000000.zip --gallery ~/PhoneCopy
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m snapchat_backup",
        description="Back up Snapchat Memories with their original dates, re-join long videos that "
                    "Snapchat split into parts, and skip anything already in your phone's gallery.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--export", nargs="+", type=Path, metavar="ZIP_OR_FOLDER",
                   help="Snapchat export zip file(s) (all parts) or extracted folder(s). "
                        "Default: every mydata* zip/folder in the current folder.")
    p.add_argument("--out", type=Path, default=Path("snapchat_backup_output"),
                   help="output folder (default: %(default)s)")

    g = p.add_argument_group("phone gallery")
    g.add_argument("--adb", action="store_true",
                   help="read the gallery from the phone over USB (adb) and copy the new files to it")
    g.add_argument("--gallery", nargs="+", type=Path, metavar="FOLDER",
                   help="instead of --adb: folder(s) holding a copy of the phone's DCIM, Pictures and Movies")
    g.add_argument("--adb-serial", help="which phone, if several are connected")
    g.add_argument("--adb-folders", nargs="+", default=["DCIM", "Pictures", "Movies"], metavar="FOLDER",
                   help="phone folders to compare against (default: %(default)s)")
    g.add_argument("--adb-refresh", action="store_true",
                   help="pull the phone folders again even if they were copied by an earlier run")
    g.add_argument("--phone-folder", default="DCIM/Snapchat Memories",
                   help="where new files go on the phone (default: %(default)s)")
    g.add_argument("--no-push", action="store_true", help="with --adb: compare only, do not copy to the phone")
    g.add_argument("--clean-phone", action="store_true",
                   help="with --adb: move redundant copies already on the phone into a hidden review "
                        "folder (nothing is deleted)")
    g.add_argument("-y", "--yes", action="store_true", help="do not ask before changing the phone")

    t = p.add_argument_group("dates")
    t.add_argument("--timezone", metavar="ZONE",
                   help="time zone for memories without a location, e.g. Europe/London "
                        "(default: this computer's)")
    t.add_argument("--no-location-timezone", action="store_true",
                   help="do not use a memory's location to find its time zone")
    t.add_argument("--no-gps", action="store_true", help="do not write the location into the files")

    v = p.add_argument_group("videos and overlays")
    v.add_argument("--no-join", action="store_true", help="keep split videos as separate parts")
    v.add_argument("--segment-min", type=float, default=4.0, metavar="SECONDS",
                   help="minimum length of a part that is followed by another part (default: %(default)s)")
    v.add_argument("--segment-slack", type=float, default=3.0, metavar="SECONDS",
                   help="tolerance on the time between parts (default: %(default)s)")
    v.add_argument("--overlays", choices=["separate", "burn", "drop"], default="separate",
                   help="captions/stickers: keep them as separate PNGs in extras/overlays (default), "
                        "burn them into the photos/videos, or drop them")

    p.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4), help=argparse.SUPPRESS)
    p.add_argument("--download-workers", type=int, default=6, help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def expand_paths(paths):
    """Expand ~ and wildcards ourselves: Windows shells pass them through as-is."""
    if not paths:
        return paths
    out = []
    for p in paths:
        p = Path(p).expanduser()
        matches = sorted(glob.glob(str(p))) if not p.exists() and glob.has_magic(str(p)) else []
        if matches:
            out.extend(Path(m) for m in matches)
        else:
            out.append(p)
    return out


def options_from_args(args):
    if args.adb and args.gallery:
        raise SystemExit("Use either --adb or --gallery, not both.")
    return Options(
        exports=expand_paths(args.export),
        out=args.out.expanduser(),
        gallery=expand_paths(args.gallery),
        use_adb=args.adb,
        adb_serial=args.adb_serial,
        adb_folders=tuple(args.adb_folders),
        adb_refresh=args.adb_refresh,
        phone_folder=args.phone_folder,
        push=not args.no_push,
        clean_phone=args.clean_phone,
        assume_yes=args.yes,
        timezone=args.timezone,
        location_timezone=not args.no_location_timezone,
        gps=not args.no_gps,
        overlays=args.overlays,
        segments=SegmentConfig(enabled=not args.no_join, min_segment=args.segment_min, slack=args.segment_slack),
        workers=max(1, args.workers),
        download_workers=max(1, args.download_workers),
    )


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
                        stream=sys.stdout)
    for noisy in ("urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return Backup(options_from_args(args)).run()
    except (AdbError, MediaError) as e:
        logging.getLogger("snapchat_backup").error("Error: %s", e)
        return 1
    except KeyboardInterrupt:
        logging.getLogger("snapchat_backup").error("Interrupted. Run the same command again to resume.")
        return 130
