"""
Lector de SVG generico (no el que nosotros mismos generamos, sino cualquier SVG externo:
de Illustrator, Inkscape, etc.) para el conversor multiformato.

Reutiliza el mismo parser de trazados (svgpath_bbox.parse_path_subpaths) que ya usamos
para leer los 'dPath' de xTool, ya que es exactamente el mismo lenguaje de paths SVG
(spec identico). La parte nueva aqui es interpretar el arbol DOM del SVG: grupos
anidados <g>, sus atributos 'transform', y los distintos tipos de forma (<path>,
<polygon>, <polyline>, <rect>, <circle>, <ellipse>, <line>, <text>).

Convencion de capas detectada en archivos reales: los grupos de nivel superior con
un 'id' (ej. <g id="Cut">, <g id="Score">, <g id="Engrave">) se usan como nombre de
capa -- convencion muy extendida en la comunidad de corte laser (viene de plantillas
tipo Glowforge: Cut=negro, Score=rojo, Engrave=azul).
"""
import re
import math
import xml.etree.ElementTree as ET
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from svgpath_bbox import parse_path_subpaths

SVG_NS = '{http://www.w3.org/2000/svg}'

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mult(a, b):
    """Igual que en pdf_reader.py: aplica b despues de a (b es la transformacion mas
    interna/nueva que se concatena sobre el CTM existente)."""
    a0,a1,a2,a3,a4,a5 = a
    b0,b1,b2,b3,b4,b5 = b
    return (
        b0*a0 + b1*a2, b0*a1 + b1*a3,
        b2*a0 + b3*a2, b2*a1 + b3*a3,
        b4*a0 + b5*a2 + a4, b4*a1 + b5*a3 + a5,
    )


def apply_mat(m, x, y):
    a,b,c,d,e,f = m
    return (a*x + c*y + e, b*x + d*y + f)


def parse_transform(s):
    """Convierte un atributo transform="translate(...) rotate(...) ..." de SVG en una
    matriz (a,b,c,d,e,f) compuesta, en el mismo orden en que SVG los aplica (izquierda
    a derecha = de fuera hacia adentro)."""
    if not s:
        return IDENTITY
    m = IDENTITY
    for name, args in re.findall(r'(\w+)\s*\(([^)]*)\)', s):
        nums = [float(v) for v in re.split(r'[,\s]+', args.strip()) if v]
        if name == 'translate':
            tx = nums[0]; ty = nums[1] if len(nums) > 1 else 0
            fn = (1, 0, 0, 1, tx, ty)
        elif name == 'scale':
            sx = nums[0]; sy = nums[1] if len(nums) > 1 else sx
            fn = (sx, 0, 0, sy, 0, 0)
        elif name == 'rotate':
            ang = math.radians(nums[0])
            cos_a, sin_a = math.cos(ang), math.sin(ang)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                fn = mat_mult(mat_mult((1,0,0,1,cx,cy), (cos_a,sin_a,-sin_a,cos_a,0,0)), (1,0,0,1,-cx,-cy))
            else:
                fn = (cos_a, sin_a, -sin_a, cos_a, 0, 0)
        elif name == 'skewX':
            fn = (1, 0, math.tan(math.radians(nums[0])), 1, 0, 0)
        elif name == 'skewY':
            fn = (1, math.tan(math.radians(nums[0])), 0, 1, 0, 0)
        elif name == 'matrix':
            fn = tuple(nums[:6])
        else:
            continue
        m = mat_mult(m, fn)
    return m


def parse_color(value, current_color='#000000'):
    """Extrae un color hex de un valor de atributo 'fill'/'stroke' (o de una propiedad
    dentro de un atributo style="fill:#rrggbb;..."). Ignora 'none'/'currentColor'/
    gradientes o patrones (url(#...)) devolviendo None para que el llamador decida."""
    if not value or value in ('none', 'transparent'):
        return None
    value = value.strip()
    if value.startswith('#'):
        if len(value) == 4:  # #rgb corto
            r,g,b = value[1],value[2],value[3]
            return f'#{r}{r}{g}{g}{b}{b}'
        return value[:7]
    m = re.match(r'rgb\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)', value)
    if m:
        r,g,b = (int(x) for x in m.groups())
        return f'#{r:02x}{g:02x}{b:02x}'
    if value == 'currentColor':
        return current_color
    return None  # url(#...), nombre de color CSS no numerico, etc.


def parse_style_attr(style):
    """Convierte style="fill:#fff;stroke:#000;stroke-width:2" en un dict."""
    out = {}
    if not style:
        return out
    for part in style.split(';'):
        if ':' in part:
            k, v = part.split(':', 1)
            out[k.strip()] = v.strip()
    return out


class SvgReadState:
    def __init__(self):
        self.ctm = IDENTITY
        self.fill = '#000000'
        self.stroke = None
        self.has_explicit_fill = False
        self.layer = 'Capa 1'

    def copy(self):
        s = SvgReadState()
        s.ctm, s.fill, s.stroke = self.ctm, self.fill, self.stroke
        s.has_explicit_fill, s.layer = self.has_explicit_fill, self.layer
        return s


def _get_style(el, style_dict, key, default=None):
    return style_dict.get(key, el.get(key, default))


def read_svg(path):
    """Punto de entrada: lee un SVG generico y devuelve la misma estructura de proyecto
    que el resto de lectores ({'canvases': [...], ...}), con un unico canvas."""
    tree = ET.parse(path)
    root = tree.getroot()

    displays = []
    id_counter = [0]

    def walk(el, state):
        tag = el.tag.replace(SVG_NS, '')
        local_state = state.copy()

        transform_attr = el.get('transform')
        if transform_attr:
            local_state.ctm = mat_mult(local_state.ctm, parse_transform(transform_attr))

        style = parse_style_attr(el.get('style'))
        fill_val = _get_style(el, style, 'fill')
        stroke_val = _get_style(el, style, 'stroke')
        if fill_val is not None:
            local_state.fill = parse_color(fill_val, local_state.fill)
            local_state.has_explicit_fill = fill_val not in ('none',)
        if stroke_val is not None:
            local_state.stroke = parse_color(stroke_val, local_state.stroke)

        if tag == 'g':
            gid = el.get('id')
            if gid and not re.match(r'^(g|path|polygon)\d*$', gid, re.IGNORECASE):
                # Solo se adopta como nombre de capa si parece un nombre real (Cut,
                # Score, Engrave...), no un id autogenerado tipo 'g12' de Illustrator.
                local_state.layer = gid
            for child in el:
                walk(child, local_state)
            return

        if tag in ('path', 'polygon', 'polyline', 'rect', 'circle', 'ellipse', 'line'):
            _emit_shape(el, tag, local_state, displays, id_counter)
        elif tag == 'text':
            _emit_text(el, local_state, displays, id_counter)
        else:
            for child in el:
                walk(child, local_state)

    for child in root:
        walk(child, SvgReadState())

    return {'canvases': [{'id': 'svg', 'title': None, 'layerData': {}, 'groupData': {},
                           'displays': displays, 'material_profiles': {}}],
            'vector_lookup': {}, 'resources': {}, 'source_version': 'svg', 'format': 'svg-generic'}


def _shape_local_points(el, tag):
    """Devuelve la geometria de la forma en su PROPIO sistema local (antes de aplicar
    el CTM), como lista de subpaths [{'points':[(x,y),...], 'closed':bool}]."""
    if tag == 'path':
        d = el.get('d', '')
        return parse_path_subpaths(d, samples_per_curve=10) if d else []

    if tag in ('polygon', 'polyline'):
        pts_attr = el.get('points', '')
        nums = [float(v) for v in re.split(r'[,\s]+', pts_attr.strip()) if v]
        pts = list(zip(nums[0::2], nums[1::2]))
        return [{'points': pts, 'closed': tag == 'polygon'}] if pts else []

    if tag == 'rect':
        x = float(el.get('x', 0)); y = float(el.get('y', 0))
        w = float(el.get('width', 0)); h = float(el.get('height', 0))
        return [{'points': [(x,y),(x+w,y),(x+w,y+h),(x,y+h)], 'closed': True}]

    if tag == 'line':
        x1,y1 = float(el.get('x1',0)), float(el.get('y1',0))
        x2,y2 = float(el.get('x2',0)), float(el.get('y2',0))
        return [{'points': [(x1,y1),(x2,y2)], 'closed': False}]

    if tag in ('circle', 'ellipse'):
        cx = float(el.get('cx', 0)); cy = float(el.get('cy', 0))
        if tag == 'circle':
            rx = ry = float(el.get('r', 0))
        else:
            rx = float(el.get('rx', 0)); ry = float(el.get('ry', 0))
        n = 48
        pts = [(cx + rx*math.cos(2*math.pi*k/n), cy + ry*math.sin(2*math.pi*k/n)) for k in range(n+1)]
        return [{'points': pts, 'closed': True}]

    return []


def _subpaths_to_dpath(subpaths):
    parts = []
    for sp in subpaths:
        pts = sp['points']
        if not pts:
            continue
        parts.append(f"M{pts[0][0]:.4f},{pts[0][1]:.4f}")
        for (x, y) in pts[1:]:
            parts.append(f"L{x:.4f},{y:.4f}")
        if sp.get('closed'):
            parts.append("Z")
    return " ".join(parts)


def _emit_shape(el, tag, state, displays, id_counter):
    local_subpaths = _shape_local_points(el, tag)
    if not local_subpaths:
        return
    # transformar cada punto por el CTM acumulado -> coordenadas absolutas de pagina
    abs_subpaths = []
    for sp in local_subpaths:
        abs_pts = [apply_mat(state.ctm, x, y) for (x, y) in sp['points']]
        abs_subpaths.append({'points': abs_pts, 'closed': sp.get('closed', False)})

    dpath = _subpaths_to_dpath(abs_subpaths)
    if not dpath:
        return
    all_pts = [p for sp in abs_subpaths for p in sp['points']]
    xs = [p[0] for p in all_pts]; ys = [p[1] for p in all_pts]
    anchor_x, anchor_y = min(xs), min(ys)

    is_fill = state.has_explicit_fill and state.fill is not None
    color = state.fill if is_fill else (state.stroke or state.fill or '#000000')

    id_counter[0] += 1
    displays.append({
        'id': f'svgshape-{id_counter[0]}',
        'type': 'PATH',
        'x': anchor_x, 'y': anchor_y,
        'angle': 0, 'scale': {'x': 1, 'y': 1}, 'skew': {'x': 0, 'y': 0},
        'offsetX': 0, 'offsetY': 0,
        'width': max(xs)-anchor_x, 'height': max(ys)-anchor_y,
        'isFill': is_fill,
        'layerColor': color, 'layerTag': state.layer,
        'fillRule': 'nonzero',
        'dPath': dpath,
    })


def _emit_text(el, state, displays, id_counter):
    text = ''.join(el.itertext()).strip()
    if not text:
        return
    x = float(el.get('x', 0) or 0)
    y = float(el.get('y', 0) or 0)
    ax, ay = apply_mat(state.ctm, x, y)
    fs = el.get('font-size', '12')
    try:
        fs = float(re.sub(r'[^\d.]', '', fs) or 12)
    except ValueError:
        fs = 12
    id_counter[0] += 1
    displays.append({
        'id': f'svgtext-{id_counter[0]}', 'type': 'TEXT_LABEL',
        'text': text, 'x': ax, 'y': ay, 'font_size': fs,
        'layerTag': state.layer, 'layerColor': state.fill or '#000000',
    })
