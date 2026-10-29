#!/bin/sh
# Draws dropper_drive_assembly.png: how a servo drives its axle, in assembly order, from ../card_dropper.scad.
#   1. OpenSCAD exports every item of every step (drive.scad: `panel`, `only`) as its own STL
#   2. Blender colours and renders each step (render_steps.py)
#   3. Pillow lays the renders out with captions, labels and notes (compose.py: the texts are there)
# Needs OpenSCAD (a program `openscad`, or the Flathub one: org.openscad.OpenSCAD), Blender (4.2 or newer: its STL
# importer) and Python with Pillow. Run it again after a change to the .scad, drive.scad or the texts.
set -e
here=$(cd "$(dirname "$0")" && pwd)
top=$(cd "$here/.." && pwd)
work=$(mktemp -d)
trap '[ -n "$KEEP" ] || rm -rf "$work"' EXIT
mkdir -p "$work/stl"

command -v openscad >/dev/null 2>&1 || { echo "OpenSCAD is not installed (the openscad package)" >&2; exit 1; }
osc() { openscad "$@"; }

for p in 1 2 3 4 5 6 7; do
    # the step's items: drive.scad echoes each item's name (ITEM:<name>) as it draws it
    osc -D panel=$p -o "$work/stl/p$p.echo" "$here/drive.scad" >/dev/null 2>&1
    items=$(grep -o 'ITEM:[a-z_0-9]*' "$work/stl/p$p.echo" | cut -d: -f2 | sort -u)
    for i in $items; do
        osc -D panel=$p -D "only=\"$i\"" -o "$work/stl/p${p}_$i.stl" "$here/drive.scad" >/dev/null 2>&1
    done
    echo "step $p:" $items
done

blender -b -P "$here/render_steps.py" -- "$work" >/dev/null 2>&1
ls "$work"/png/step*.png >/dev/null      # stops here if Blender rendered nothing
"${PYTHON:-python3}" "$here/compose.py" "$work" "$here/dropper_drive_assembly.png"
