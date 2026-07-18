# OpenShelf

A tiny local "continue watching" shelf for folders of video files. Point it at the folders where your videos live and it gives you what Kodi makes painful: a clean list of your shows and movies, with Netflix-style resume, one click from playing.

- **Continue watching** front and center: the thing you were last in, resumable at the exact timestamp
- Groups `S01E02`-style filenames into shows with seasons, watched checkmarks, and a "next episode" button
- Resume positions come from VLC itself, so they work even for stuff you watched before installing OpenShelf
- Right-click anything to mark it watched or unwatched ("mark watched up to here" catches up a whole show); sort by recently-watched or A-to-Z, and hide finished stuff
- One window, no server, no database, no accounts. Two small JSON files next to the app hold everything
- Nothing ever leaves your machine

OpenShelf does not play video. It launches [VLC](https://www.videolan.org/vlc/) at your saved position, so every format VLC plays just works (mkv, x265, 4K, anything).

## Install (Windows)

1. Install [VLC](https://www.videolan.org/vlc/) if you don't have it.
2. Download `OpenShelf.exe` from the [latest release](../../releases/latest).
3. Put it in its own folder (it writes its settings next to itself) and run it.
4. Open the **Folders** tab, add your video folders, done.

Windows SmartScreen may warn on first run because the exe is unsigned. Choose "More info" then "Run anyway", or run from source instead.

## Run from source

Any OS with Python 3.9+ and Tkinter (included in the standard Windows/macOS installers). No dependencies to install.

```
git clone https://github.com/mrgratz/OpenShelf
cd OpenShelf
python app.py
```

On Windows, `pythonw app.py` (or `run.bat`) runs it without a console window.

macOS and Linux: the VLC and config paths are wired but untested. Reports and fixes welcome.

## Sharing it

Send someone the [releases link](../../releases/latest), or just send them the exe file itself. It is fully self-contained; the only thing they need installed is VLC.

## How resume works

VLC remembers where you stopped in each file. OpenShelf reads those saved positions, shows them on your shelf, and relaunches VLC at that spot (minus a few seconds of lead-in). Positions update when you close VLC; OpenShelf refreshes itself when you come back to its window.

The watched flag is OpenShelf's own: anything you launched through it gets tracked in `state.json`, and a show's card always offers the next unwatched episode.

## Settings

`settings.json` lives next to the app and is meant to be edited (the Folders tab has an "Open settings.json" button). Save the file, hit **Rescan**, changes apply.

| Key | What it does |
| --- | --- |
| `folders` | The folders to scan (recursively). Same list the Folders tab edits. |
| `vlc_path` | Explicit path to the VLC executable, if auto-detection misses yours. |
| `video_extensions` | File types treated as video. |
| `skip_dirs` | Directory names ignored during scans (extras, samples, ...). |
| `rewind_seconds` | How far to back up when resuming, for context. |

`state.json` holds your watch history. Delete it to reset everything to unwatched.

## Filename handling

- `Show.Name.S01E02.*`, `1x02`, and `Episode 12` patterns become show episodes
- A folder with 3+ videos and no episode patterns is treated as a numbered collection (common for anime packs)
- Everything else is a movie; release-group junk and quality tags are stripped from titles

## Build the exe yourself

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name OpenShelf app.py
```

The exe lands in `dist/`. Tagged releases build this automatically via GitHub Actions.

## License

MIT
