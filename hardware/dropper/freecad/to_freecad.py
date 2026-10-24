# Builds the FreeCAD files from card_dropper.scad's CSG export (run by to_freecad.sh, with freecadcmd):
#   card_dropper.FCStd          every part a solid of its own, in its place
#   step/<part>.step            each printed part alone, in its print orientation
#
# FreeCAD's OpenSCAD importer (importCSG) turns OpenSCAD's tree of cubes, cylinders, extrusions,
# unions and differences into the same operations in FreeCAD, so the result is made of real faces
# (planes and cylinders), not of triangles. What does NOT come over: the named parameters - the
# numbers are fixed in the FreeCAD file - and colours (this runs without FreeCAD's window, which
# is where colours live). card_dropper.scad stays the source: change it there, run to_freecad.sh.
#
# Its inputs come in the environment (freecadcmd would try to open extra arguments as files):
# DROPPER_CSG (the folder with the .csg files and manifest.txt), DROPPER_OUT, DROPPER_OPENSCAD.

import json
import os

import FreeCAD
import importCSG

csg_dir = os.environ['DROPPER_CSG']
out_dir = os.environ['DROPPER_OUT']
openscad = os.environ['DROPPER_OPENSCAD']

# importCSG asks the OpenSCAD program itself for a few operations it cannot do alone (hull)
FreeCAD.ParamGet('User parameter:BaseApp/Preferences/Mod/OpenSCAD').SetString('openscadexecutable', openscad)

# object | placement (used by to_freecad.sh only) | printed part or '-'
with open(os.path.join(csg_dir, 'manifest.txt')) as f:
    manifest = [line.rstrip('\n').split('|') for line in f if line.strip()]


def import_part(doc, path, name):
    """One .csg file into doc; returns its single top-level object, renamed to the part's name."""
    before = set(o.Name for o in doc.Objects)
    importCSG.insert(path, doc.Name)
    doc.recompute()
    new = [o for o in doc.Objects if o.Name not in before]
    tops = [o for o in new if not any(p in new for p in o.InList)]
    if len(tops) != 1:
        raise RuntimeError(f'{name}: {len(tops)} top-level objects instead of one')
    top = tops[0]
    top.Label = name
    return top


BOOLEANS = ('Part::Fuse', 'Part::MultiFuse', 'Part::Cut', 'Part::Common')


def push_placements_down(obj):
    """
    Move each boolean's Placement into its inputs, from the top down, so that every object of the tree
    sits where it really is. The importer turns an OpenSCAD translate()/rotate() around a union or a
    difference into that operation's Placement, and leaves the inputs in their own coordinates: an input
    shown alone appeared somewhere else (the gate's boxes at x = 0, the magazine's rear wall, instead of
    x = 97 - the owner's question of 2026-10-23). A boolean's result is its inputs combined and then
    placed, so placing the inputs instead and the result nowhere gives the same shape (checked below).
    """
    if obj.TypeId in BOOLEANS and not obj.Placement.isIdentity():
        for child in obj.OutList:
            child.Placement = obj.Placement.multiply(child.Placement)
        obj.Placement = FreeCAD.Placement()
    elif obj.TypeId == 'Part::Extrusion' and obj.DirMode == 'Custom' and not obj.Placement.isIdentity():
        # A linear_extrude: the importer draws its 2D outline flat at z = 0 (through a refine step)
        # and puts the move and the turn on the extrusion. Give them to the outline instead, and turn
        # the extrusion's direction with it: the outline then lies where the solid is.
        placement = obj.Placement
        for leaf in flat_profiles(obj.Base):
            leaf.Placement = placement.multiply(leaf.Placement)
        obj.Dir = placement.Rotation.multVec(obj.Dir)
        obj.Placement = FreeCAD.Placement()
        return
    for child in obj.OutList:
        push_placements_down(child)


def flat_profiles(obj):
    """The 2D outlines at the bottom of an extrusion's base (through the importer's refine steps)."""
    inputs = [o for o in obj.OutList]
    if not inputs:
        return [obj]
    return [leaf for o in inputs for leaf in flat_profiles(o)]


def placed_the_same(before, after):
    """The same solid in the same place: volume and bounding box (to 0.001 mm)."""
    if abs(before.Volume - after.Volume) > 0.01:
        return False
    a, b = before.BoundBox, after.BoundBox
    return all(abs(x - y) < 1e-3 for x, y in zip((a.XMin, a.XMax, a.YMin, a.YMax, a.ZMin, a.ZMax),
                                                (b.XMin, b.XMax, b.YMin, b.YMax, b.ZMin, b.ZMax)))


def check(shape):
    return {'valid': shape.isValid(), 'solids': len(shape.Solids), 'faces': len(shape.Faces),
            'volume': round(shape.Volume, 1)}


report = {'card_dropper': {}}
doc = FreeCAD.newDocument('card_dropper')
for name, _, _ in manifest:
    top = import_part(doc, os.path.join(csg_dir, f'asm_{name}.csg'), name)
    before = top.Shape.copy()
    push_placements_down(top)
    doc.recompute()
    if not placed_the_same(before, top.Shape):
        raise RuntimeError(f'{name}: moving the placements into the tree changed the part')
    report['card_dropper'][name] = check(top.Shape)
doc.saveAs(os.path.join(out_dir, 'card_dropper.FCStd'))
FreeCAD.closeDocument(doc.Name)

# Each printed part once, alone, as printed, to STEP
os.makedirs(os.path.join(out_dir, 'step'), exist_ok=True)
report['step'] = {}
for printed in sorted({p for _, _, p in manifest if p != '-'}):
    doc = FreeCAD.newDocument('p_' + printed)
    top = import_part(doc, os.path.join(csg_dir, f'print_{printed}.csg'), printed)
    # the importer keeps the outermost move (the print orientation) in the object's Placement,
    # which Import.export leaves out; the object's Shape has it applied
    top.Shape.copy().exportStep(os.path.join(out_dir, 'step', f'{printed}.step'))
    report['step'][printed] = check(top.Shape)
    FreeCAD.closeDocument(doc.Name)

# written last: to_freecad.sh takes its presence as the proof that everything above worked
with open(os.path.join(out_dir, 'conversion_report.json'), 'w') as f:
    json.dump(report, f, indent=1)
