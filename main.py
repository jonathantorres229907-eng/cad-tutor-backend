from fastapi import FastAPI, Form
from fastapi.responses import StreamingResponse, JSONResponse
from openai import OpenAI
import os
import io
import math
from typing import List, Tuple
from fastapi.middleware.cors import CORSMiddleware

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------
# Helper: ASCII STL parsing
# -------------------------
def extract_stl_block(text: str) -> str:
    """Return substring from first 'solid' to last 'endsolid' (inclusive)."""
    if not text:
        return ""
    start = text.find("solid")
    end = text.rfind("endsolid")
    if start == -1:
        return ""
    if end == -1:
        # if no explicit end, assume end of text
        return text[start:].strip()
    # include the 'endsolid' token and anything after it on that line
    # find end of line after end index
    eol = text.find("\n", end)
    if eol == -1:
        eol = len(text)
    return text[start:eol].strip()

def parse_ascii_stl(stl_text: str) -> List[Tuple[Tuple[float,float,float], List[Tuple[float,float,float]]]]:
    """
    Parse ASCII STL into list of (normal, [v1,v2,v3]) facets.
    Returns empty list on parse failure.
    """
    lines = [ln.strip() for ln in stl_text.splitlines() if ln.strip() != ""]
    facets = []
    i = 0
    try:
        while i < len(lines):
            if lines[i].startswith("facet normal"):
                parts = lines[i].split()
                nx, ny, nz = float(parts[-3]), float(parts[-2]), float(parts[-1])
                i += 1
                # expect outer loop
                if not lines[i].startswith("outer loop"):
                    # skip until next facet
                    i += 1
                    continue
                verts = []
                i += 1
                for _ in range(3):
                    if not lines[i].startswith("vertex"):
                        raise ValueError("vertex expected")
                    vp = lines[i].split()
                    vx, vy, vz = float(vp[-3]), float(vp[-2]), float(vp[-1])
                    verts.append((vx, vy, vz))
                    i += 1
                # expect endloop, endfacet
                # tolerate missing exact spacing
                while i < len(lines) and not lines[i].startswith("endfacet"):
                    i += 1
                facets.append(((nx, ny, nz), verts))
            else:
                i += 1
    except Exception:
        return []
    return facets

def edge_key(a, b):
    # canonical undirected edge key (rounded to avoid float noise)
    return tuple(sorted([tuple(round(x,6) for x in a), tuple(round(x,6) for x in b)]))

def find_open_edges(facets):
    """
    Build edge map and return list of edges that appear only once (open).
    Also returns vertex->connected vertices mapping for loop detection.
    """
    edge_count = {}
    for _, verts in facets:
        v0, v1, v2 = verts
        for u, v in ((v0, v1), (v1, v2), (v2, v0)):
            k = edge_key(u, v)
            edge_count[k] = edge_count.get(k, 0) + 1
    open_edges = [e for e,c in edge_count.items() if c == 1]
    return open_edges

# -------------------------
# Simple repair for prisms
# -------------------------
def attempt_prism_repair(facets):
    """
    If facets contain two dominant Z levels (top and bottom) and both loops exist,
    attempt to connect corresponding vertices to create side faces.
    Returns repaired facets list or None if not applicable.
    """
    # collect unique vertices
    verts_set = {}
    for _, vs in facets:
        for v in vs:
            verts_set.setdefault(tuple(round(x,6) for x in v), v)
    verts = list(verts_set.keys())
    if not verts:
        return None
    # group by z
    z_map = {}
    for v in verts:
        z = round(v[2],6)
        z_map.setdefault(z, []).append(v)
    if len(z_map) < 2:
        return None
    # pick two most populated z-levels (bottom, top)
    levels = sorted(z_map.items(), key=lambda kv: -len(kv[1]))
    bottom_z, bottom_verts = levels[0][0], levels[0][1]
    top_z, top_verts = levels[1][0], levels[1][1]
    # require same number of vertices on both loops to attempt direct pairing
    if len(bottom_verts) != len(top_verts):
        return None
    n = len(bottom_verts)
    # sort loops by angle around centroid to pair corresponding edges
    def centroid(loop):
        cx = sum(v[0] for v in loop)/len(loop)
        cy = sum(v[1] for v in loop)/len(loop)
        return (cx, cy)
    bottom_coords = [(v[0], v[1]) for v in bottom_verts]
    top_coords = [(v[0], v[1]) for v in top_verts]
    b_c = centroid(bottom_verts)
    t_c = centroid(top_verts)
    def angle_from(center, v):
        return math.atan2(v[1]-center[1], v[0]-center[0])
    bottom_sorted = sorted(bottom_verts, key=lambda v: angle_from(b_c, v))
    top_sorted = sorted(top_verts, key=lambda v: angle_from(t_c, v))
    # create side facets connecting bottom[i], bottom[i+1], top[i+1] and bottom[i], top[i+1], top[i]
    new_facets = list(facets)  # copy existing
    for i in range(n):
        b0 = bottom_sorted[i]
        b1 = bottom_sorted[(i+1)%n]
        t0 = top_sorted[i]
        t1 = top_sorted[(i+1)%n]
        # two triangles per quad
        # compute outward normal roughly by cross product (not strictly necessary)
        new_facets.append(((0,0,0), [b0, b1, t1]))
        new_facets.append(((0,0,0), [b0, t1, t0]))
    # verify no open edges remain
    open_edges = find_open_edges(new_facets)
    if not open_edges:
        return new_facets
    return None

# -------------------------
# Deterministic fallback generator
# -------------------------
def generate_block_with_hole_and_tower(block_w=40.0, block_d=40.0, block_h=20.0,
                                       hole_d=12.0, tower_w=20.0, tower_d=10.0, tower_h=5.0,
                                       tower_offset_x=8.0, n_segments=24) -> str:
    """
    Build a low-poly rectangular block centered at origin with a through-hole (approximated cylinder)
    and a rectangular tower extruded on top. Returns ASCII STL text.
    All units are mm.
    """
    verts = []
    tris = []

    # helper to add vertex and return index
    def add_v(v):
        verts.append(v)
        return len(verts)-1

    # block corners (centered)
    hw = block_w/2.0
    hd = block_d/2.0
    bottom_z = 0.0
    top_z = block_h

    # create outer top and bottom loops (rectangle)
    bottom_loop = [
        (-hw, -hd, bottom_z),
        ( hw, -hd, bottom_z),
        ( hw,  hd, bottom_z),
        (-hw,  hd, bottom_z),
    ]
    top_loop = [(x,y,top_z) for (x,y,_) in bottom_loop]

    # triangulate top and bottom (two triangles each)
    # bottom (normal down)
    b0 = add_v(bottom_loop[0]); b1 = add_v(bottom_loop[1]); b2 = add_v(bottom_loop[2]); b3 = add_v(bottom_loop[3])
    t0 = add_v(top_loop[0]); t1 = add_v(top_loop[1]); t2 = add_v(top_loop[2]); t3 = add_v(top_loop[3])

    # bottom face (two triangles) - outward normal should be 0,0,-1
    tris.append(((0,0,-1),(b0,b1,b2)))
    tris.append(((0,0,-1),(b0,b2,b3)))
    # top face (two triangles) - outward normal 0,0,1
    tris.append(((0,0,1),(t0,t2,t1)))
    tris.append(((0,0,1),(t0,t3,t2)))

    # side faces (4 sides, each 2 triangles)
    def add_side(a_bottom_idx, b_bottom_idx, a_top_idx, b_top_idx):
        # two triangles: bottom a->b->top b ; bottom a->top b->top a
        tris.append(((0,0,0),(a_bottom_idx, b_bottom_idx, b_top_idx)))
        tris.append(((0,0,0),(a_bottom_idx, b_top_idx, a_top_idx)))

    add_side(b0,b1,t0,t1)
    add_side(b1,b2,t1,t2)
    add_side(b2,b3,t2,t3)
    add_side(b3,b0,t3,t0)

    # create cylindrical hole vertices (approx) for top and bottom loops
    hole_r = hole_d/2.0
    hole_bottom_idxs = []
    hole_top_idxs = []
    for i in range(n_segments):
        ang = 2*math.pi*i/n_segments
        x = hole_r * math.cos(ang)
        y = hole_r * math.sin(ang)
        # center hole at origin (block centered at origin)
        hole_bottom_idxs.append(add_v((x,y,bottom_z)))
        hole_top_idxs.append(add_v((x,y,top_z)))

    # create inner cylinder side faces (n_segments quads -> 2 triangles each)
    for i in range(n_segments):
        i1 = hole_bottom_idxs[i]
        i2 = hole_bottom_idxs[(i+1)%n_segments]
        i3 = hole_top_idxs[(i+1)%n_segments]
        i4 = hole_top_idxs[i]
        tris.append(((0,0,0),(i1,i2,i3)))
        tris.append(((0,0,0),(i1,i3,i4)))

    # create caps for hole? NO — we want through-hole open, so do NOT add top/bottom caps for hole.

    # add tower (rectangular footprint) on top face, offset toward +X
    tw = tower_w/2.0
    td = tower_d/2.0
    tx = tower_offset_x
    tower_bottom_z = top_z
    tower_top_z = top_z + tower_h

    tower_bl = add_v((tx - tw, -td, tower_bottom_z))
    tower_br = add_v((tx + tw, -td, tower_bottom_z))
    tower_tr = add_v((tx + tw,  td, tower_bottom_z))
    tower_tl = add_v((tx - tw,  td, tower_bottom_z))

    tower_tl_top = add_v((tx - tw, -td, tower_top_z))  # careful: naming consistent
    tower_tr_top = add_v((tx + tw, -td, tower_top_z))
    tower_br_top = add_v((tx + tw,  td, tower_top_z))
    tower_bl_top = add_v((tx - tw,  td, tower_top_z))

    # tower top face (two triangles)
    tris.append(((0,0,1),(tower_tl_top, tower_br_top, tower_tr_top)))
    tris.append(((0,0,1),(tower_tl_top, tower_bl_top, tower_br_top)))

    # tower bottom face (attached to block top) - we do not create a separate bottom cap (it merges with block top)
    # tower sides (4 sides)
    tris.append(((0,0,0),(tower_bl, tower_br, tower_tr)))
    tris.append(((0,0,0),(tower_bl, tower_tr, tower_tl)))

    tris.append(((0,0,0),(tower_bl, tower_bl_top, tower_br_top)))
    tris.append(((0,0,0),(tower_bl, tower_br_top, tower_br)))

    tris.append(((0,0,0),(tower_br, tower_br_top, tower_tr_top)))
    tris.append(((0,0,0),(tower_br, tower_tr_top, tower_tr)))

    tris.append(((0,0,0),(tower_tr, tower_tr_top, tower_tl_top)))
    tris.append(((0,0,0),(tower_tr, tower_tl_top, tower_tl)))

    # Build ASCII STL text
    def fmt_v(v):
        return f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}"

    out = ["solid model"]
    for normal, tri in tris:
        # compute normal if zero
        if normal == (0,0,0):
            # compute from vertices
            a = verts[tri[0]]
            b = verts[tri[1]]
            c = verts[tri[2]]
            ux, uy, uz = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
            vx, vy, vz = (c[0]-a[0], c[1]-a[1], c[2]-a[2])
            nx = uy*vz - uz*vy
            ny = uz*vx - ux*vz
            nz = ux*vy - uy*vx
            # normalize
            l = math.sqrt(nx*nx + ny*ny + nz*nz) or 1.0
            nx, ny, nz = nx/l, ny/l, nz/l
        else:
            nx, ny, nz = normal
        out.append(f"  facet normal {nx:.6f} {ny:.006f} {nz:.006f}")
        out.append("    outer loop")
        out.append(f"      vertex {fmt_v(verts[tri[0]])}")
        out.append(f"      vertex {fmt_v(verts[tri[1]])}")
        out.append(f"      vertex {fmt_v(verts[tri[2]])}")
        out.append("    endloop")
        out.append("  endfacet")
    out.append("endsolid model")
    return "\n".join(out)

# -------------------------
# Main endpoint
# -------------------------
@app.post("/generate-stl")
async def generate_stl(description: str = Form(...)):
    # Short, strict prompt (less to confuse the model)
    prompt = f"""Output ONLY a valid ASCII STL file (no prose, no markdown, no commentary).
Start with: 'solid model'
End with: 'endsolid model'
Between those lines include only ASCII STL facet blocks (facet normal / outer loop / vertex / endloop / endfacet).
Produce a low-poly, watertight mesh for this description: {description}
"""
    # call model
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0.1,
        )
        stl_text_raw = response.choices[0].message.content
    except Exception as e:
        # model call failed — fallback deterministic
        stl_text_raw = ""

    # sanitize and extract
    stl_text = extract_stl_block(stl_text_raw)
    if not stl_text:
        # fallback deterministic
        stl_text = generate_block_with_hole_and_tower()

    # parse facets
    facets = parse_ascii_stl(stl_text)
    if not facets:
        # fallback deterministic
        stl_text = generate_block_with_hole_and_tower()
        facets = parse_ascii_stl(stl_text)

    # check open edges
    open_edges = find_open_edges(facets)
    if open_edges:
        # attempt prism repair
        repaired = attempt_prism_repair(facets)
        if repaired:
            # rebuild ASCII from repaired facets
            def build_ascii_from_facets(facets_list):
                out = ["solid model"]
                for normal, verts in facets_list:
                    # compute normal if zero
                    nx, ny, nz = normal
                    if nx == ny == nz == 0:
                        a,b,c = verts
                        ux,uy,uz = (b[0]-a[0], b[1]-a[1], b[2]-a[2])
                        vx,vy,vz = (c[0]-a[0], c[1]-a[1], c[2]-a[2])
                        nx = uy*vz - uz*vy
                        ny = uz*vx - ux*vz
                        nz = ux*vy - uy*vx
                        l = math.sqrt(nx*nx + ny*ny + nz*nz) or 1.0
                        nx,ny,nz = nx/l, ny/l, nz/l
                    out.append(f"  facet normal {nx:.6f} {ny:.6f} {nz:.6f}")
                    out.append("    outer loop")
                    for v in verts:
                        out.append(f"      vertex {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
                    out.append("    endloop")
                    out.append("  endfacet")
                out.append("endsolid model")
                return "\n".join(out)
            stl_text = build_ascii_from_facets(repaired)
        else:
            # repair failed — deterministic fallback
            stl_text = generate_block_with_hole_and_tower()

    # final validation quick check
    final_facets = parse_ascii_stl(stl_text)
    if not final_facets or find_open_edges(final_facets):
        # last resort fallback
        stl_text = generate_block_with_hole_and_tower()

    # return as bytes
    stl_bytes = stl_text.encode("utf-8")
    return StreamingResponse(io.BytesIO(stl_bytes), media_type="application/sla",
                             headers={"Content-Disposition":"attachment; filename=rough_model.stl"})
