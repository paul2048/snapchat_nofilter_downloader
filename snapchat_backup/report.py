"""CSV and HTML reports of what was done with every memory."""

import csv
import html
import os
from pathlib import Path

ITEM_COLUMNS = [
    "file", "status", "taken (local time)", "time zone", "type", "duration (s)", "parts",
    "overlay", "same as", "match detail", "memory #", "notes",
]
CLEANUP_COLUMNS = ["phone file", "reason", "keep instead", "detail", "moved"]

STATUS_TEXT = {
    "new": "Copied to to_phone/ (not on the phone yet)",
    "on_phone": "Already on the phone (skipped)",
    "export_duplicate": "Duplicate inside the Snapchat export (kept once)",
    "failed": "Could not be saved",
}


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _link(out_dir, target):
    if not target:
        return ""
    target = Path(target)
    try:
        rel = os.path.relpath(target, out_dir)
    except ValueError:
        rel = str(target)
    return f'<a href="{html.escape(Path(rel).as_posix())}">{html.escape(target.name)}</a>'


def write_reports(out_dir: Path, rows, cleanup, summary):
    _write_csv(out_dir / "report.csv", ITEM_COLUMNS, rows)
    if cleanup:
        _write_csv(out_dir / "phone_cleanup.csv", CLEANUP_COLUMNS, cleanup)

    def table(columns, data, linkcols=()):
        head = "".join(f"<th>{html.escape(c)}</th>" for c in columns)
        body = []
        for row in data:
            cells = []
            for c in columns:
                v = row.get(c, "")
                if c in linkcols and row.get("_path_" + c):
                    cells.append(f"<td>{_link(out_dir, row['_path_' + c])}</td>")
                else:
                    cells.append(f"<td>{html.escape(str(v))}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"

    sections = []
    order = ["failed", "new", "on_phone", "export_duplicate"]
    for status in order:
        subset = [r for r in rows if r.get("_status") == status]
        if subset:
            sections.append(f"<h2>{html.escape(STATUS_TEXT[status])} ({len(subset)})</h2>")
            sections.append(table(ITEM_COLUMNS, subset, linkcols=("file",)))
    if cleanup:
        sections.append(f"<h2>Redundant copies already on the phone ({len(cleanup)})</h2>")
        sections.append(table(CLEANUP_COLUMNS, cleanup))

    summary_html = "".join(f"<li>{html.escape(line)}</li>" for line in summary)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Snapchat backup report</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#d2d2d7; --head:#f5f5f7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1c1e; --fg:#f5f5f7; --muted:#a1a1a6; --line:#3a3a3c; --head:#2c2c2e; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; margin:24px 16px; }}
h1 {{ font-size:22px; }} h2 {{ font-size:17px; margin-top:32px; }}
li {{ margin:2px 0; }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th, td {{ border-bottom:1px solid var(--line); padding:4px 8px; text-align:left; vertical-align:top; }}
th {{ background:var(--head); position:sticky; top:0; }}
td:nth-child(1), td:nth-child(3) {{ white-space:nowrap; }}
a {{ color:inherit; }}
</style></head><body>
<h1>Snapchat backup report</h1>
<ul>{summary_html}</ul>
<div class="wrap">{''.join(sections)}</div>
</body></html>"""
    (out_dir / "report.html").write_text(page, encoding="utf-8")
