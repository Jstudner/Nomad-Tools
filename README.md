# Nomad Card Tools

Simple tools to set up and update a Nomad SD card from your computer.

Works on **Windows** and **Linux**. macOS should work too, but it is **untested
at the moment**, treat it as experimental for now.

---

## Start here

- **Windows:** double-click **`START-Windows.bat`**
- **Linux:** open a terminal in this folder and run **`./start-linux.sh`**

A menu opens. It finds your Nomad card, you pick it from the list, and then you
choose what you want to do. You can do several jobs in a row, then choose
**Quit** when you're finished.

If your card doesn't show up in the list, pick **"Enter a path manually"** and
type where it is, for example `E:\` on Windows or `/media/you/NOMAD` on Linux.

---

## What the tools do

### 1. Add media
Copies films, shows, books, music, photos, games, recipes or 3D models onto the
card, into the folder whose page reads them.

- Point it at a file or a folder, then pick where on the card it goes.
- Before anything is copied it lists which files the page will show, and which
  will copy but never appear, and why.
- Episodes dropped loose into **Shows** never appear, so it offers to put them
  in a show folder.
- When it's done it offers to rebuild the media index.

### 2. Check what nomad will show
For a card you filled by hand. Goes through every folder and reports what Nomad
will show, what it will skip and why, and whether the index needs rebuilding.
It checks **Maps** and **Archive** too. Nothing on the card is changed.

### 3. Add ZIM archives
Copies offline libraries (Wikipedia, Gutenberg, and so on) onto the card and
builds the search index so Nomad can find articles.

- Point it at a single `.zim` file **or** a whole folder of them.
- New archives are **added to** what's already on the card. It does **not**
  erase your existing ones, and it rebuilds the search so your old and new
  archives all work together.
- Re-adding a file that's already there (same name) simply replaces that one.
- Point it at the card's own `/Archive` folder to build the search index for
  archives that are already on the card.
- Very large files are split automatically so they fit the card.

### 4. Optimize cover images
Shrinks the cover pictures in **Movies**, **Shows** and **Books** so pages load
fast on Nomad. You choose which of those folders to run on.

- This **overwrites** the original images (no backup), so it asks you to confirm.
- It only ever touches those three folders, and skips pictures that are already
  small enough.

### 5. Rebuild media index
Refreshes the list Nomad uses to show your media, after you've added or removed
files. Nothing is deleted.

### 6. Download offline maps
Downloads map tiles for an area you choose, so Nomad's **Maps** page works with
no internet. You give it a name, the area (four numbers, the tool shows you how
to get them from openstreetmap.org) and how much detail you want.

- Before anything downloads it shows the tile count, the estimated download size
  and the free space on the card. You confirm before it starts.
- Safe to interrupt, run it again with the same region and it picks up where it
  left off.
- The built-in map sources are USGS (public domain, US only). You can paste any
  other tile server's URL, just make sure that provider allows bulk downloading.

### 7. Build a routing map
A map plus the road network and place search, for **turn by turn directions with
no internet**. You pick from ready made areas instead of typing in a box:

- any of the 50 states, plus DC, Puerto Rico and the US Virgin Islands
- five big regions (the Northeast, the South, the Midwest, the West, Alaska and
  Hawaii) or the whole United States
- sets like East Coast, New England, Great Lakes, Route 66 and the Lower 48

Pick as many as you like and they join into one map. US only for now, the map
picture comes from USGS, which only covers the United States.

**Routing maps don't show up in the Maps page list yet.** Open one directly at
`http://192.168.4.1/Maps/<region-id>/index.html`, or
`http://nomad.local/Maps/<region-id>/index.html` in WiFi Mode. The tool prints
the address when it finishes.

### 8. See map areas and sizes
The same list as *Build a routing map*, but it only shows sizes and downloads
nothing. Use it to see what fits before starting a long download.

- Detail level makes the biggest difference. Each level past 12 is about four
  times as many tiles as the one below it, and the tool shows the size at each
  level before you choose.
- You can still zoom in past what you downloaded, the picture just gets softer.

### 9. Optimize map tiles
Re-encodes a region's tiles to WebP, about 40% smaller. The original tiles are
replaced, routing data is left alone.

### 10. Add translation languages
Installs offline language packs so the **Translate** page works with no
internet. Translation runs in the browser of whoever is connected, nothing heavy
runs on the Nomad itself.

- Each language installs both directions, so English to Spanish *and* Spanish to
  English.
- Comes in collections (`starter`, `asia`, `traveler`, `europe`, `world`) or
  single languages. `world` is everything, about 3.8 GB.
- Korean, Polish and Turkish only exist as that language into English.
- It checks the card has room first and skips packs that are already there, so
  you can run it again to add more.
- A phone only downloads the pair it uses, about 115 MB per direction, and keeps
  it after that.

### 11. Format a card as exFAT
Erases a card and sets it up as exFAT, labeled `NOMAD`. **Linux only**, it needs
`exfatprogs` and asks for your password.

- This **deletes everything on the card**. It only lists memory cards and other
  disks marked removable, never USB hard drives, and you have to type the device
  path back to confirm.
- On Windows, use **Format...** in Explorer instead: exFAT, allocation unit size
  4096 bytes (8192 on cards over 512 GB). On a Mac, erase it as exFAT in Disk
  Utility.

**A good order to do things:** add your media / ZIMs first, optimize images next,
then rebuild the index last so it reflects everything. Maps and languages can be
added any time.

**Some pages are switched off on a new card:** Games, Maps, Archive, Chat,
Translate, Cookbook and Workshop. Turn them on in **Admin > Menu Pages**. The
tools tell you if the page you just added to is off.

---

## What goes where

Each page has its own list of formats and only looks so far into folders. A file
outside these rules still copies, but its page won't list it.
`python3 add_media.py --rules` prints this list too.

| Folder | Formats the page reads | Layout | How deep it looks |
|---|---|---|---|
| `/Movies` | `.avi`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.webm` | a folder per film, or loose files at the top | any depth |
| `/Shows` | `.avi`, `.m4v`, `.mkv`, `.mov`, `.mp4`, `.webm` | a folder per show, seasons inside it | any depth |
| `/Books` | `.aac`, `.azw3`, `.cb7`, `.cbr`, `.cbt`, `.cbz`, `.djvu`, `.epub`, `.flac`, `.m4a`, `.m4b`, `.mobi`, `.mp3`, `.ogg`, `.pdf`, `.txt`, `.wav` | loose files, or a folder per book or series | one level deep |
| `/Music` | `.aac`, `.aiff`, `.alac`, `.flac`, `.m4a`, `.mp3`, `.ogg`, `.opus`, `.wav` | Artist/Album/song, a playlist folder, or loose songs | any depth |
| `/Gallery` | `.avif`, `.gif`, `.jpeg`, `.jpg`, `.mov`, `.mp4`, `.ogg`, `.png`, `.webm`, `.webp` | loose files, or folders to group them | any depth |
| `/Games` | `.gb`, `.gba`, `.gbc`, `.gen`, `.iwad`, `.md`, `.n64`, `.nes`, `.ps1`, `.psx`, `.sfc`, `.smc`, `.sms`, `.wad` | loose ROMs, or folders to group them | any depth |
| `/Cookbook` | `.cook`, `.json`, `.markdown`, `.md`, `.txt` | loose recipes, or a folder per category | one level deep |
| `/Workshop` | `.3ds`, `.3mf`, `.amf`, `.bgcode`, `.blend`, `.dae`, `.dxf`, `.f3d`, `.gco`, `.gcode`, `.ipt`, `.obj`, `.ply`, `.scad`, `.sldprt`, `.step`, `.stl`, `.stp`, `.svg`, `.zip` | loose models, or a folder per collection | one level deep |
| `/Files` | anything | anything | any depth |

- **`/Shows` lists folders only.** One folder per show, seasons inside it. A
  loose episode is indexed but never appears.
- **Cover art has the same name as the file.** `Dune.mp4` + `Dune.jpg`. For
  Shows, the image goes inside the show folder, named `poster`, `cover`,
  `folder` or `art`.
- **A zipped ROM keeps its console extension.** `Zelda.nes.zip` runs,
  `Zelda.zip` does not.
- **A folder of three or more images in `/Books`, with no ebook or audio file
  beside them, is one comic.** A `.cbz` or `.cbr` is just a file and needs no
  folder.
- **`/Cookbook` skips names starting with `_`**, and README, index and notes
  files.
- **`.md` in `/Games` is a Sega Mega Drive ROM**, not Markdown.
- **A file of 4 GB or more needs an exFAT card.** FAT32 stops just under it.
- **Nothing new appears until the index is rebuilt.**

**`/Archive`** and **`/Maps`** need more than a copy, so they aren't in the
table. Use *Add ZIM archives* for `.zim` files (a `.zim` dropped in by hand has
no search index), and *Download offline maps* or *Build a routing map* for maps.

---

## What you need

- **Python 3**, for everything.
  On Windows, install it from [python.org](https://www.python.org/downloads/) and
  tick **"Add python.exe to PATH"** during setup.
- **Pillow**, only needed for *Optimize cover images* and *Optimize map tiles*.
  Install with `py -m pip install Pillow` (Windows) or `pip3 install Pillow` (Linux).
  If it says Pillow is missing even though you installed it, run the exact command
  the tool prints. That just means you have more than one Python and it's telling
  you which one needs it.
- **Node.js**, only needed for *Add ZIM archives*.
  Install it from [nodejs.org](https://nodejs.org).
- **numpy** and **pyosmium**, only needed for routing maps bigger than a town.
  Install with `pip3 install numpy osmium`.
- **exfatprogs**, only needed for *Format a card as exFAT* (Linux).
  Install with `sudo apt install exfatprogs`, or your distro's package manager.

---

## When you're done

**Eject the card safely** before you unplug it, then put it back into Nomad.

---

## Advanced (optional)

You can run any tool on its own instead of using the menu. Use `python` on
Windows or `python3` on Linux:

```
python3 add_media.py --check                 # what will this card actually show?
python3 add_media.py --rules                 # every folder's formats and layout
python3 add_media.py ~/rips --to Shows       # copy, checking formats first
python3 import_zim.py ~/Downloads            # add every .zim in a folder
python3 optimize_images.py --dirs Movies     # optimize just one folder
python3 build_index.py /media/you/NOMAD      # rebuild the index
python3 translate_packs.py --list            # language packs and sizes
python3 translate_packs.py --add world       # every language, both ways
python3 map_tiles.py --name "Pennsylvania" --bbox -80.52,39.72,-74.69,42.27 --zooms 6-12
python3 map_buckets.py --list states         # sizes for every state
python3 map_buckets.py --list sets           # East Coast, Route 66, Lower 48...
python3 nomad_route.py --areas pa nj de --zooms 6-14
```

Add `-o E:\` (Windows) or `-o /media/you/NOMAD` (Linux) to skip the card picker.

Area names can be state codes (`pa`), full names (`pennsylvania`), ids
(`us/pennsylvania`, `us-northeast`) or set names (`east-coast`, `conus`).

Big routing maps download a bulk road data file from
[Geofabrik](https://download.geofabrik.de/) and keep it in `map-data/` next to
the tools, so a second map that uses the same state doesn't download it again.
Building one uses a lot of memory, about 18 times the size of that download, and
the tool warns you before a pick gets too big. Build a few smaller maps instead
of one huge one.

### Map region format

A map region is just a folder in `/Maps` with a `manifest.json` in it, and the
tools build that for you. To make one by hand:

```
/Maps/
  west-chester/
    manifest.json
    tiles/12/1194/1540.jpg      tiles/{z}/{x}/{y}.jpg
```

```json
{
  "id": "west-chester",
  "name": "West Chester, PA",
  "bounds": [-75.70, 39.89, -75.51, 40.03],
  "minZoom": 12,
  "maxZoom": 16,
  "tilePath": "tiles/{z}/{x}/{y}.jpg",
  "attribution": "USGS The National Map",
  "sizeBytes": 24696061
}
```

- `bounds` is `[west, south, east, north]` in degrees.
- `tilePath` is optional and defaults to `tiles/{z}/{x}/{y}.jpg`. Set it if your
  tiles are `.webp` or `.png`.
- Tiles are ordinary XYZ (slippy map) tiles, the same scheme Leaflet and
  OpenStreetMap use.
- Nomad lists up to 64 regions, and skips folders starting with `.`.
- A `.osm.pbf` from Geofabrik is road data, not tiles. *Build a routing map* uses
  it for directions; the map picture is separate.
