"""Kept so that `python3 app.py` still works: it runs snapchat_backup.

With no arguments it uses every mydata* zip in the current folder and writes
to ./snapchat_backup_output. See `python3 app.py --help` and README.md.
"""

import sys

from snapchat_backup.cli import main

if __name__ == "__main__":
    sys.exit(main())
