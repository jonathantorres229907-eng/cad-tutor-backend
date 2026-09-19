import trimesh

def generate_cylinder(diameter, height):
    radius = diameter / 2
    mesh = trimesh.creation.cylinder(radius=radius, height=height)
    return mesh

def generate_block(width, length, height):
    mesh = trimesh.creation.box(extends=[width, length, height])
    return mesh

def generate_bracket(width, height, thickness):
    base = trimesh.creation.box(extents=[width, thickness, thickness])
    arm = trimesh.creation.box(extents=[thickness, thickness, height])
    arm.apply_translation([width/2 - thickness/2, 0, height/2])
    return trimesh.util.concatenate([base, arm])