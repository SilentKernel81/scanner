"""Renders each step's STLs (<folder>/stl/p<step>_<item>.stl) to <folder>/png/step<step>.png, in Blender's Workbench
   (flat light, the colours below, outlines - a drawing more than a photo). make_sheet.sh runs it:
   blender -b -P render_steps.py -- <folder> [steps...]"""
import bpy
import sys
from pathlib import Path
from mathutils import Vector

folder = Path(sys.argv[sys.argv.index('--') + 1])
steps = [int(s) for s in sys.argv[sys.argv.index('--') + 2:]] or range(1, 8)

# colour of each item: printed parts in strong colours, bought ones in metal / plain colours, the arrows red
COLOURS = {
    'hub': (1.0, 0.45, 0.05), 'block': (0.62, 0.72, 0.85), 'bracket': (0.36, 0.42, 0.50), 'wheels': (0.95, 0.75, 0.15),
    'rings': (0.85, 0.12, 0.10), 'rod': (0.80, 0.80, 0.82), 'bearing1': (0.25, 0.25, 0.28), 'bearing2': (0.25, 0.25, 0.28),
    'clutch': (0.15, 0.45, 0.80), 'clutch_arrow': (1, 1, 1), 'horn': (0.96, 0.96, 0.94), 'servo': (0.10, 0.25, 0.75),
    'spline': (0.95, 0.80, 0.30), 'screws_horn': (0.08, 0.08, 0.08), 'screws_servo': (0.08, 0.08, 0.08),
    'screws_block': (0.08, 0.08, 0.08), 'arrow1': (0.90, 0.05, 0.05),
}
# where the camera looks from, per step (the axle runs along y: the servo at -y, the block at +y)
VIEWS = {1: (1, 0.75, 0.45), 2: (0.75, -1, 0.45), 3: (0.9, -1, 0.7), 4: (0.9, 1, 0.55),
         5: (0.85, -1, 0.6), 6: (0.85, -1, 0.6), 7: (1, -0.2, 0.22)}


def material(name, rgb):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.diffuse_color = (*rgb, 1)
    return m


def render(step):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    objects = []
    for stl in sorted(folder.glob(f'stl/p{step}_*.stl')):
        item = stl.stem.split('_', 1)[1]
        bpy.ops.wm.stl_import(filepath=str(stl))
        ob = bpy.context.selected_objects[0]
        ob.data.materials.append(material(item, COLOURS.get(item, (0.5, 0.5, 0.5))))
        objects.append(ob)

    # the camera: orthographic, looking at the middle of everything from the step's direction
    corners = [ob.matrix_world @ Vector(c) for ob in objects for c in ob.bound_box]
    centre = sum(corners, Vector()) / len(corners)
    direction = Vector(VIEWS[step]).normalized()
    cam_data = bpy.data.cameras.new('cam')
    cam_data.type = 'ORTHO'
    cam = bpy.data.objects.new('cam', cam_data)
    scene.collection.objects.link(cam)
    cam.location = centre + direction * 600
    cam.rotation_euler = (-direction).to_track_quat('-Z', 'Y').to_euler()
    cam_data.clip_end = 2000
    bpy.context.view_layer.update()
    # framing: the extent of the corners seen from the camera, with a margin
    inverse = cam.matrix_world.inverted()
    seen = [inverse @ c for c in corners]
    width = max(p.x for p in seen) - min(p.x for p in seen)
    height = max(p.y for p in seen) - min(p.y for p in seen)
    res_x, res_y = 1200, 900
    cam_data.ortho_scale = max(width, height * res_x / res_y) * 1.12
    cam_data.shift_x = ((max(p.x for p in seen) + min(p.x for p in seen)) / 2) / cam_data.ortho_scale
    cam_data.shift_y = ((max(p.y for p in seen) + min(p.y for p in seen)) / 2) / cam_data.ortho_scale
    scene.camera = cam

    # Workbench: flat studio light, the materials' colours, outlines - a drawing more than a photo
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'STUDIO'
    shading.color_type = 'MATERIAL'
    shading.show_object_outline = True
    shading.object_outline_color = (0.1, 0.1, 0.1)
    shading.show_cavity = True
    shading.cavity_type = 'WORLD'
    shading.show_shadows = False
    scene.display.render_aa = '8'
    scene.render.resolution_x, scene.render.resolution_y = res_x, res_y
    scene.render.film_transparent = False
    world = bpy.data.worlds.new('w')
    scene.world = world
    shading.background_type = 'VIEWPORT'
    shading.background_color = (1, 1, 1)
    scene.view_settings.view_transform = 'Standard'
    out = folder / 'png' / f'step{step}.png'
    out.parent.mkdir(exist_ok=True)
    scene.render.filepath = str(out)
    bpy.ops.render.render(write_still=True)
    print('RENDERED', out)


for step in steps:
    render(step)
