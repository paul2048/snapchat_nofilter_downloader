# snapchat_nofilter_downloader

Back up **all** your Snapchat Memories before deleting your account, and put them in your
Android phone's gallery **at the right date**, **without duplicates**, with **long videos in one
piece**.

What it does:

- **Correct date and time.** Snapchat's export says when each memory was taken (in UTC). The tool
  writes that date into each file. Photos get EXIF `DateTimeOriginal` with the time-zone offset,
  videos get the MP4 `creation_time`, and every file's modification time is set too. Your gallery
  then shows memories when they happened, not as "today". The time zone comes from the memory's
  location when Snapchat recorded one.
- **Long videos re-joined.** A long recording can come out of Snapchat as several short (~10 s)
  memories. Parts that belong together are detected (saved back to back, same camera settings, and
  the last frame of one part matches the first frame of the next) and joined losslessly into one
  video. The original parts are kept in `extras/`.
- **No duplicates on the phone.** Everything is compared with what is already in your phone's
  gallery, including the copies Snapchat saved to your camera roll with the caption burned in,
  and copies holding only part of a long video. Only new files are copied to the phone. Memories
  you saved twice in Snapchat are kept once.
- **Clean up the phone (optional).** Copies on the phone that became redundant are listed, and
  optionally moved to a hidden review folder. That covers identical copies and pieces of a video
  that is now on the phone in full. **Nothing is ever deleted.**
- **Without filters.** As before, captions, stickers and drawings are kept as separate transparent
  PNGs (`extras/overlays/`) so you get the original pictures. Use `--overlays burn` if you prefer
  them drawn on.
- Everything runs on your computer. The only network traffic is downloading your memories from
  Snapchat when they aren't already inside the export.

## Step by step

### 1. Request your data from Snapchat

In the Snapchat app: **Profile → Settings ⚙ → Privacy Controls → My Data** (or
<https://accounts.snapchat.com> → *My Data*).

- Tick **Export your Memories** *and* **Export JSON Files**. The JSON file holds the date and place
  of every memory.
- Choose **All time** as the date range and submit.

Snapchat emails you when the export is ready. Download **every** zip file (big exports come in
several parts, e.g. `mydata~1700000000.zip`, `mydata~1700000000-2.zip`, …). Do it soon: download
links inside the export stop working after about a week. If they have expired, request a new export
and run the tool again. Files it already has are not downloaded again.

### 2. Install the tools (once)

You need Python 3.9+, FFmpeg, and Android's `adb`:

| | Windows (PowerShell) | macOS (Terminal) |
|---|---|---|
| Python | `winget install Python.Python.3.12` | `brew install python` |
| FFmpeg | `winget install Gyan.FFmpeg` | `brew install ffmpeg` |
| adb | `winget install Google.PlatformTools` | `brew install android-platform-tools` |

Then, in this folder:

```
pip install -r requirements.txt
```

### 3. Connect your phone

1. On the phone: **Settings → About phone**, tap **Build number** 7 times to enable Developer
   options.
2. In **Developer options** (usually under Settings → System): turn on **USB debugging**.
3. Plug the phone in with a USB cable and tap **Allow** on the prompt. Check with `adb devices`:
   it should say `device`.

### 4. Run it

In a terminal opened in this folder:

```
python -m snapchat_backup --export path/to/mydata~1700000000*.zip --adb --clean-phone
```

This will:

1. read the export and download any memories not included in it;
2. re-join split videos and write every file with its original date;
3. copy your phone's `DCIM`, `Pictures` and `Movies` folders to the computer (read-only, to compare);
4. ask before copying the **new** files to `DCIM/Snapchat Memories` on the phone;
5. ask before moving redundant copies on the phone into `Snapchat duplicates (review)`. That
   folder is hidden from the gallery, so look through it with a file manager and delete it when
   you're happy.

Leave out `--clean-phone` if you don't want anything on the phone moved. If you're not sure of the
time zone for memories without a location, add e.g. `--timezone Europe/London` (the default is
your computer's).

### 5. Check, then keep a copy

Open `snapchat_backup_output/report.html`. It lists every memory: copied to the phone, already on
the phone (and which file), saved twice, re-joined, or failed. Keep
`snapchat_backup_output/all_memories/` somewhere safe (external drive, cloud). It is your complete
archive: one file per memory, already dated. **Only delete your Snapchat account once you're happy
with it.**

## Output

```
snapchat_backup_output/
  all_memories/      every memory, dated, long videos joined: your archive
  to_phone/          the subset not yet on your phone (what gets copied with --adb)
  extras/
    overlays/        captions/stickers/drawings as transparent PNGs, named after their photo/video
    video_parts/     the original parts of every re-joined video
    duplicates_in_export/  memories you had saved twice
  report.html, report.csv   what happened to each memory
  phone_cleanup.csv  redundant copies found on the phone
  .work/             downloads and caches: lets a second run skip finished work
```

You can run the tool again at any time (e.g. after a new export, or with different options). The
output is rebuilt, nothing is downloaded twice, and nothing that is already on the phone is copied
again.

## Without adb

Copy the phone's `DCIM`, `Pictures` and `Movies` folders to the computer (USB file transfer), then:

```
python -m snapchat_backup --export mydata~1700000000*.zip --gallery path/to/PhoneCopy
```

Then copy `snapchat_backup_output/to_phone/` to the phone, e.g. into `DCIM`. The dates are inside
the files, so they show up correctly even if the copy changes the files' modification times.
Without `--gallery` nothing is compared and every memory goes to `to_phone/`.

## Options

| Option | |
|---|---|
| `--export ZIP…` | the export zip(s), all parts, or extracted folder(s). Default: `mydata*` in the current folder |
| `--out FOLDER` | where to write (default `snapchat_backup_output`) |
| `--adb` / `--gallery FOLDER…` | compare against the phone over USB, or against a copy of its folders |
| `--adb-folders DCIM Pictures Movies` | phone folders to compare against (e.g. narrow to `Pictures/Snapchat DCIM/Camera` to save time and disk space) |
| `--adb-refresh` | copy the phone's folders again instead of reusing the copy from the last run |
| `--phone-folder "DCIM/Snapchat Memories"` | where new files go on the phone |
| `--no-push` | with `--adb`: compare only, don't copy to the phone |
| `--clean-phone` | move redundant copies on the phone into a hidden review folder |
| `-y`, `--yes` | don't ask before changing the phone |
| `--timezone ZONE` | time zone for memories without a location (default: this computer's) |
| `--no-location-timezone` | use `--timezone` for every memory |
| `--no-gps` | don't write the location into the files |
| `--overlays separate\|burn\|drop` | keep captions/stickers as separate PNGs (default), draw them on, or drop them |
| `--no-join` | keep split videos as separate parts |
| `--segment-min 4`, `--segment-slack 3` | tuning for detecting video parts (seconds) |

## How duplicates are recognised

- **Identical files** by SHA-256.
- **Photos** by a 256-bit difference hash plus a thumbnail comparison that tolerates a caption or
  sticker covering part of the picture. Re-encoded, resized and captioned copies match. Another shot
  of the same scene taken a moment later does not.
- **Videos** by a sequence of frame fingerprints (2 per second), aligned against each other. A clip
  is also found *inside* a longer video. That is how a 10 s piece on the phone is recognised as
  part of a re-joined 30 s memory.
- Black or plain-colour pictures and frames (e.g. text snaps) carry too little detail to compare,
  so they only ever match byte-identical files.
- When unsure, it keeps the file: a missed duplicate costs a little space, a wrong match could hide
  a memory. Every match, with its score, is listed in the report.

## Development

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

The tests build a synthetic export, a fake Snapchat download server and a fake phone (`tests/`),
and check the whole flow: dates, time zones, joining, every duplicate case, adb copying and clean-up.
