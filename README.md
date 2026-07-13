# Nomad Card Tools

Simple tools to set up and update a Nomad SD card from your computer.

Works on **Windows** and **Linux**. macOS should work too, but it is **untested
at the moment** — treat it as experimental for now.

---

## Start here

- **Windows:** double-click **`START-Windows.bat`**
- **Linux:** open a terminal in this folder and run **`./start-linux.sh`**

A menu opens. It finds your Nomad card, you pick it from the list, and then you
choose what you want to do. You can do several jobs in a row, then choose
**Quit** when you're finished.

If your card doesn't show up in the list, pick **"Enter a path manually"** and
type where it is — for example `E:\` on Windows or `/media/you/NOMAD` on Linux.

---

## What the tools do

### 1. Add ZIM archives
Copies offline libraries (Wikipedia, Gutenberg, and so on) onto the card and
builds the search index so Nomad can find articles.

- Point it at a single `.zim` file **or** a whole folder of them.
- New archives are **added to** what's already on the card — it does **not**
  erase your existing ones. It rebuilds the search so your old and new archives
  all work together.
- Re-adding a file that's already there (same name) simply replaces that one.
- Very large files are split automatically so they fit the card.

### 2. Optimize cover images
Shrinks the cover pictures in **Movies**, **Shows** and **Books** so pages load
fast on Nomad. You choose which of those folders to run on.

- This **overwrites** the original images (no backup), so it asks you to confirm.
- It only ever touches those three folders, and skips pictures that are already
  small enough.

### 3. Rebuild media index
Refreshes the list Nomad uses to show your media, after you've added or removed
files. Nothing is deleted.

**A good order to do things:** add your media / ZIMs first, optimize images next,
then rebuild the index last so it reflects everything.

---

## What you need

- **Python 3** — for everything.
  On Windows, install it from [python.org](https://www.python.org/downloads/) and
  tick **"Add python.exe to PATH"** during setup.
- **Pillow** — only needed for *Optimize cover images*.
  Install with `py -m pip install Pillow` (Windows) or `pip3 install Pillow` (Linux).
  If it says Pillow is missing even though you installed it, run the exact command
  the tool prints — that just means you have more than one Python and it's telling
  you which one needs it.
- **Node.js** — only needed for *Add ZIM archives*.
  Install it from [nodejs.org](https://nodejs.org).

---

## When you're done

**Eject the card safely** before you unplug it, then put it back into Nomad.

---

## Advanced (optional)

You can run any tool on its own instead of using the menu. Use `python` on
Windows or `python3` on Linux:

```
python3 import_zim.py ~/Downloads            # add every .zim in a folder
python3 optimize_images.py --dirs Movies     # optimize just one folder
python3 build_index.py /media/you/NOMAD      # rebuild the index
```

Add `-o E:\` (Windows) or `-o /media/you/NOMAD` (Linux) to skip the card picker.
