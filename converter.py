#!/usr/bin/env python3
"""
XCS2SVG Converter - Fase 1 (prototipo funcional)
Convierte proyectos .xcs (JSON plano) y .xs (ZIP v2) de xTool Creative Space a SVG.

Soporta:
- .xcs plano (v1.6.x, v1.7.x, v2.2.x): dPath embebido
- .xs v2 (directory format): buckets de vectores por hash SHA-256
- Tipos: PATH, CIRCLE, RECT
- Transformaciones: x, y, angle, scale (skew soportado pero sin muestras para validar)
- Capas por color (layerTag) -> <g> con id/color

NOTA IMPORTANTE SOBRE LA TRANSFORMACION:
El campo offsetX/offsetY del JSON NO es fiable como ancla de posicionamiento: su semantica
parece variar (o ser metadata obsoleta/interna) segun la version del archivo. Se probo
empiricamente en varios proyectos reales y en algunos casos coincidia aproximadamente con
la esquina superior izquierda del bounding box crudo del path, pero en otros (mismo tipo de
objeto, version distinta) el valor era completamente distinto en escala y signo, produciendo
posiciones absurdas (miles de mm) al usarlo directamente.

En su lugar, este conversor calcula el bounding box real del propio dPath (parseando la
geometria, incluidas curvas) y usa esa esquina como ancla local. Esto es autoconsistente y
no depende de la version del formato. La formula final es:

    punto_final = translate(x,y) . rotate(angle) . scale(sx,sy) . translate(-bbox_minx,-bbox_miny) . punto_crudo

Esto fue validado contra 6 proyectos reales de distintas versiones (1.6.6, 1.7.24, 2.2.23,
2.0.0-xs) comprobando que el "footprint" final de cada proyecto cae en rangos de tamano
fisico plausibles (decenas de cm), no en rangos absurdos (metros).

LIMITACION CONOCIDA: no se ha podido verificar visualmente con el software original
(xTool Creative Space / LightBurn) que el resultado sea pixel-perfecto; se ha validado
solo por consistencia numerica y de orden de magnitud. Se recomienda abrir los SVG
generados y compararlos visualmente contra el proyecto original.
"""
import json, zipfile, math, sys, os, base64, io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from svgpath_bbox import parse_path_points, parse_path_subpaths
from dxf_writer import DXFWriter
from svgpath_segments import parse_path_segments
from pdf_writer import PDFWriter
import lightburn

MM_TO_PT = 72.0 / 25.4


def expand_text_objects(displays):
    """xTool guarda el texto (type='TEXT') con los contornos vectoriales YA CALCULADOS
    por caracter en el campo 'charJSONs': cada entrada es, en la practica, un objeto
    PATH completo (mismos campos x/y/angle/scale/skew/offsetX/offsetY/dPath/fillRule/
    isFill/layerColor que cualquier otro display), con posicion ABSOLUTA ya resuelta
    (no relativa al objeto TEXT padre). Esto se confirmo con un archivo real que
    contenia texto: el numero de entradas en charJSONs coincidia exactamente con los
    caracteres visibles del texto (los saltos de linea no generan entrada), y las
    coordenadas x avanzaban progresivamente caracter a caracter.

    Gracias a esto, no hace falta escribir un renderizador de texto/fuentes propio:
    basta con 'expandir' cada objeto TEXT en sus propios PATH ya resueltos, ANTES de
    que el resto del motor (SVG/DXF/PDF/lint) procese los displays. Asi el resto del
    codigo no necesita saber que el texto existio como tal."""
    expanded = []
    for disp in displays:
        if disp.get('type') == 'TEXT' and disp.get('charJSONs'):
            expanded.extend(disp['charJSONs'])
        else:
            expanded.append(disp)
    return expanded


# ---------------------------------------------------------------------------
# Carga de proyectos (ambos formatos)
# ---------------------------------------------------------------------------

def load_project(path):
    with open(path, 'rb') as f:
        head = f.read(1024)
    if head.lstrip(b'\xef\xbb\xbf').startswith(b'%PDF'):
        from pdf_reader import read_pdf_or_ai
        return read_pdf_or_ai(path)
    if head.startswith(b'%!PS') or head.startswith(b'%!Adobe'):
        raise NotImplementedError(
            "Este archivo es PostScript / .ai antiguo (formato pre-PDF de Illustrator). "
            "Todavia no esta soportado -- es un formato mas complejo (un lenguaje de "
            "programacion completo, no solo datos). Ver conversacion para el plan.")
    if b'<svg' in head:
        from svg_reader import read_svg
        return read_svg(path)
    if path.lower().endswith('.dxf') or (b'SECTION' in head and b'HEADER' in head):
        raise NotImplementedError(
            "La lectura de archivos DXF genericos todavia no esta implementada. Los "
            "DXF reales (no generados por este programa) suelen usar splines (curvas "
            "NURBS) que requieren matematica adicional para leerse con fidelidad -- "
            "ver conversacion para el plan. De momento este programa SI puede ESCRIBIR "
            "DXF (para LightBurn), solo no leerlo de vuelta.")
    if zipfile.is_zipfile(path):
        return load_xs_v2(path)
    else:
        return load_xcs_flat(path)


def load_xcs_flat(path):
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    canvases = []
    for c in d.get('canvas', []):
        canvases.append({
            'id': c.get('id'),
            'title': c.get('title'),
            'layerData': c.get('layerData', {}),
            'groupData': c.get('groupData', {}),
            'displays': expand_text_objects(c.get('displays', [])),
            'material_profiles': {},  # el formato .xcs plano no incluye perfiles de material
        })
    return {'canvases': canvases, 'vector_lookup': {}, 'resources': {}, 'source_version': d.get('version', 'unknown'), 'format': 'xcs-flat'}


def load_xs_v2(path):
    vector_lookup = {}
    canvases = []
    resources = {}
    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        for n in names:
            if n.startswith('vectors/svg/data-') and n.endswith('.json'):
                bucket = json.loads(z.read(n))
                vector_lookup.update(bucket.get('entries', {}))

        for n in names:
            if n.startswith('resources/') and not n.endswith('/'):
                resources[n] = z.read(n)

        # --- Perfiles de material (potencia/velocidad/pasadas) ---
        # profiles.json define los AJUSTES (id -> tipo de proceso + potencia/velocidad/etc).
        # devices/device-*.json define los ENLACES: que objetos (displayIds) usan que perfil,
        # organizados por canvas. IMPORTANTE: estos enlaces acumulan referencias a displayIds
        # de ediciones anteriores que ya no existen en el diseño actual (se descubrio al
        # inspeccionar datos reales) -- hay que filtrar solo los que siguen existiendo.
        profiles = {}
        if 'profiles.json' in names:
            try:
                profiles = json.loads(z.read('profiles.json')).get('profiles', {})
            except Exception:
                profiles = {}

        raw_bindings_by_canvas = {}  # canvas_id -> lista de (profile_id, [display_ids])
        for n in names:
            if n.startswith('devices/') and n.endswith('.json'):
                try:
                    dev = json.loads(z.read(n))
                except Exception:
                    continue
                for canvas_id, proc in dev.get('processing', {}).items():
                    for mode_name, mode_data in proc.get('modes', {}).items():
                        for b in mode_data.get('bindings', []):
                            raw_bindings_by_canvas.setdefault(canvas_id, []).append(
                                (b.get('baseProfileId'), b.get('displayIds', [])))

        canvas_meta_files = [n for n in names if n.startswith('canvases/') and n.endswith('.json') and n.count('/') == 1]
        for cmf in canvas_meta_files:
            cmeta = json.loads(z.read(cmf))
            cid = cmeta['id']
            displays = []
            chunk_indexes = cmeta.get('chunkLayout', {}).get('chunkIndexes', [0])
            for idx in chunk_indexes:
                dfile = f'canvases/{cid}/displays-{idx}.json'
                if dfile in names:
                    dchunk = json.loads(z.read(dfile))
                    displays.extend(dchunk.get('displays', []))

            current_ids = set(d.get('id') for d in displays)
            material_profiles = {}
            for profile_id, display_ids in raw_bindings_by_canvas.get(cid, []):
                prof = profiles.get(profile_id)
                if not prof:
                    continue
                for did in display_ids:
                    if did in current_ids:
                        values = dict(prof.get('values', {}))
                        values['processing_type'] = prof.get('processingType')
                        material_profiles[did] = values

            # Si un objeto TEXT tenia un perfil de material vinculado, propagarlo a cada
            # uno de sus glifos expandidos (ver expand_text_objects) para que no se pierda
            # el ajuste de potencia/velocidad al convertir el texto en trazados PATH.
            for disp in displays:
                if disp.get('type') == 'TEXT' and disp.get('id') in material_profiles:
                    parent_profile = material_profiles[disp['id']]
                    for glyph in disp.get('charJSONs', []):
                        material_profiles[glyph['id']] = parent_profile

            canvases.append({
                'id': cid,
                'title': cmeta.get('title'),
                'layerData': cmeta.get('layerData', {}),
                'groupData': cmeta.get('groupData', {}),
                'displays': expand_text_objects(displays),
                'material_profiles': material_profiles,
            })

        proj = {}
        if 'project.json' in names:
            proj = json.loads(z.read('project.json'))

    return {'canvases': canvases, 'vector_lookup': vector_lookup, 'resources': resources,
            'source_version': proj.get('version', 'unknown'), 'format': 'xs-v2'}


def get_dpath(display, vector_lookup):
    if 'dPath' in display and display['dPath']:
        return display['dPath']
    ref = display.get('vectorRef')
    if ref:
        return vector_lookup.get(ref.get('vectorHash'))
    return None


# ---------------------------------------------------------------------------
# Transformacion geometrica
# ---------------------------------------------------------------------------

def local_anchor(disp, vector_lookup, cache):
    """Ancla local (minx,miny) usada como origen antes de escalar/rotar.
    Para PATH: esquina superior-izq. del bbox REAL del propio dPath (calculado por nosotros).
    Para CIRCLE/RECT: (0,0), ya que esos elementos se dibujan ya centrados/anclados en el
    origen local en convert_display_to_svg_element."""
    key = disp.get('id')
    if key in cache:
        return cache[key]
    t = disp.get('type')
    anchor = (0.0, 0.0)
    if t == 'PATH':
        d = get_dpath(disp, vector_lookup)
        if d:
            try:
                pts = parse_path_points(d, samples_per_curve=8)
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                anchor = (min(xs), min(ys))
            except Exception:
                anchor = (0.0, 0.0)
    cache[key] = anchor
    return anchor


def build_transform(disp, anchor):
    x = disp.get('x', 0)
    y = disp.get('y', 0)
    angle = disp.get('angle', 0)
    scale = disp.get('scale', {'x': 1, 'y': 1})
    skew = disp.get('skew', {'x': 0, 'y': 0})
    ax, ay = anchor

    # skew.x/skew.y vienen en RADIANES (se observo un valor exacto de PI = 3.141593 en la
    # practica), pero SVG skewX()/skewY() esperan GRADOS. Sin esta conversion, un valor de
    # PI se interpretaria como ~3.14 grados (un shear pequeno no deseado) en lugar del
    # resultado correcto (skew de 180 grados = identidad, tan(180)=0), usado por la app
    # de origen como parte de la codificacion de imagenes volteadas/espejadas.
    skew_x_deg = math.degrees(skew.get('x', 0))
    skew_y_deg = math.degrees(skew.get('y', 0))

    parts = [f"translate({x},{y})"]
    if angle:
        parts.append(f"rotate({angle})")
    if skew_x_deg or skew_y_deg:
        parts.append(f"skewX({skew_x_deg}) skewY({skew_y_deg})")
    parts.append(f"scale({scale.get('x',1)},{scale.get('y',1)})")
    parts.append(f"translate({-ax},{-ay})")
    return " ".join(parts)


def transform_point(px, py, disp, anchor):
    x = disp.get('x', 0)
    y = disp.get('y', 0)
    angle = math.radians(disp.get('angle', 0))
    scale = disp.get('scale', {'x': 1, 'y': 1})
    skew = disp.get('skew', {'x': 0, 'y': 0})
    ax, ay = anchor

    qx, qy = (px - ax) * scale.get('x', 1), (py - ay) * scale.get('y', 1)
    # skew (mismo criterio que build_transform: valores en radianes, se usan tal cual
    # porque math.tan ya trabaja en radianes)
    skx, sky = skew.get('x', 0), skew.get('y', 0)
    if skx or sky:
        qx, qy = qx + math.tan(skx) * qy, qy + math.tan(sky) * qx
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    rx = qx * cos_a - qy * sin_a
    ry = qx * sin_a + qy * cos_a
    return rx + x, ry + y


def get_image_data_uri(disp, resources):
    """Devuelve un data-URI base64 para un objeto BITMAP. Soporta las DOS formas en que
    xTool almacena imagenes:
    - formato .xs (ZIP): 'resourcePath' referencia un archivo dentro del ZIP (resources).
    - formato .xcs plano: la imagen viene YA como data-URI completo en el campo 'base64'
      (se descubrio con un archivo real: 'data:image/png;base64,...' listo para usar)."""
    if disp.get('base64'):
        # ya es un data-URI completo, listo para usar directamente
        return disp['base64']
    import base64
    rpath = disp.get('resourcePath')
    if rpath and rpath in resources:
        raw = resources[rpath]
        fmt = disp.get('format', 'png')
        b64 = base64.b64encode(raw).decode('ascii')
        return f"data:image/{fmt};base64,{b64}"
    return None


def color_hex(disp, key='layerColor'):
    c = disp.get(key)
    if isinstance(c, str) and c.startswith('#'):
        return c
    return '#000000'


# ---------------------------------------------------------------------------
# Generacion de elementos SVG
# ---------------------------------------------------------------------------

def convert_display_to_svg_element(disp, vector_lookup, anchor_cache, resources=None, override_color=None, profile=None):
    resources = resources or {}
    t = disp.get('type')
    anchor = local_anchor(disp, vector_lookup, anchor_cache)
    transform = build_transform(disp, anchor)
    stroke_color = override_color or color_hex(disp, 'layerColor')
    is_fill = disp.get('isFill', False)
    fill_attr = stroke_color if is_fill else 'none'
    stroke_attr = stroke_color if not is_fill else 'none'
    fill_rule = disp.get('fillRule', 'nonzero')
    op = lightburn.infer_operation(disp, profile)

    if t == 'PATH':
        d = get_dpath(disp, vector_lookup)
        if not d:
            return f"<!-- PATH {disp.get('id')} sin dPath (posible dato dañado) -->"
        # hairline (0.3mm) para CORTE: suficientemente fino para la convencion laser, pero
        # visible en una vista previa normal (0.05mm resultaba invisible en disenos grandes,
        # ej. un mandala de 410mm, ya que el stroke-width en SVG es proporcional al viewBox,
        # no un tamano de pixel fijo -- 0.05 unidades en un viewBox de cientos de mm equivale
        # a una fraccion de pixel, practicamente invisible al renderizar)
        sw = 0.3 if not is_fill else 0.3
        return (f'<path d="{d}" transform="{transform}" '
                f'fill="{fill_attr}" stroke="{stroke_attr}" stroke-width="{sw}" '
                f'fill-rule="{fill_rule}" data-id="{disp.get("id")}" data-op="{op}"/>')

    elif t == 'CIRCLE':
        w = disp.get('width', 0)
        h = disp.get('height', 0)
        sw = 0.3
        return (f'<ellipse cx="0" cy="0" rx="{w/2}" ry="{h/2}" transform="{transform}" '
                f'fill="{fill_attr}" stroke="{stroke_attr}" stroke-width="{sw}" data-id="{disp.get("id")}" data-op="{op}"/>')

    elif t == 'RECT':
        w = disp.get('width', 0)
        h = disp.get('height', 0)
        sw = 0.3
        return (f'<rect x="0" y="0" width="{w}" height="{h}" transform="{transform}" '
                f'fill="{fill_attr}" stroke="{stroke_attr}" stroke-width="{sw}" data-id="{disp.get("id")}" data-op="{op}"/>')

    elif t == 'BITMAP':
        data_uri = get_image_data_uri(disp, resources)
        if not data_uri:
            return f"<!-- BITMAP {disp.get('id')} sin recurso de imagen resoluble -->"
        # anchor para BITMAP es (0,0): el contenido raster se dibuja desde su propia esquina
        # superior izquierda en unidades de pixel "crudas" (originWidth/originHeight), y la
        # transformacion (que incluye 'scale') ya lo reduce al tamano final en mm.
        ow = disp.get('originWidth') or disp.get('width', 0)
        oh = disp.get('originHeight') or disp.get('height', 0)
        opacity = disp.get('opacity', 1)
        return (f'<image href="{data_uri}" x="0" y="0" width="{ow}" height="{oh}" '
                f'transform="{transform}" opacity="{opacity}" data-id="{disp.get("id")}"/>')

    elif t == 'TEXT_LABEL':
        # Texto extraido de un PDF/AI generico: no se reconstruyo como contorno vectorial
        # real (requeriria parsear la fuente embebida), asi que se representa como un
        # <text> SVG normal en la posicion/tamano detectados. Fiel para VER el contenido;
        # no fiel al 100% para CORTAR ese texto tal cual (usar el original para eso).
        txt = disp.get('text', '').replace('&', '&amp;').replace('<', '&lt;')
        fs = disp.get('font_size', 12)
        x, y = disp.get('x', 0), disp.get('y', 0)
        return (f'<text x="{x}" y="{y}" font-size="{fs}" fill="{color_hex(disp)}" '
                f'data-id="{disp.get("id")}">{txt}</text>')

    else:
        return f"<!-- Tipo no soportado aun: {t} (id={disp.get('id')}) -->"


# ---------------------------------------------------------------------------
# Conversion completa de proyecto
# ---------------------------------------------------------------------------

PREVIEW_PALETTE = ['#e6194b','#3cb44b','#ffe119','#4363d8','#f58231','#911eb4','#46f0f0','#f032e6',
                   '#bcf60c','#fabebe','#008080','#e6beff','#9a6324','#fffac8','#800000','#aaffc3',
                   '#808000','#ffd8b1','#000075','#808080']


def generate_preview_svg(canvas, vector_lookup, resources, output_path):
    """Genera un SVG de 'vista previa' con cada objeto en un color distinto (paleta ciclica)
    y numerado, independiente de los colores/capas reales del proyecto. Es puramente una
    ayuda de control de calidad visual: en disenos con muchas piezas que comparten el mismo
    color de capa (frecuente en patrones de corte monocromos), las lineas superpuestas se
    vuelven dificiles de distinguir a simple vista aunque la geometria sea correcta. Este
    archivo NO esta pensado para importar en LightBurn (usar el SVG/DXF normal para eso)."""
    anchor_cache = {}
    elems = []
    minx=miny=maxx=maxy=None
    for idx, disp in enumerate(canvas['displays']):
        color = PREVIEW_PALETTE[idx % len(PREVIEW_PALETTE)]
        elems.append(convert_display_to_svg_element(disp, vector_lookup, anchor_cache, resources, override_color=color))
        x, y = disp.get('x', 0), disp.get('y', 0)
        elems.append(f'<text x="{x}" y="{y}" font-size="{max(2, (disp.get("width",10))*0.08)}" fill="{color}">{idx}</text>')

        anchor = local_anchor(disp, vector_lookup, anchor_cache)
        t = disp.get('type')
        pts = []
        if t == 'PATH':
            d = get_dpath(disp, vector_lookup)
            if d:
                try:
                    pts = parse_path_points(d, samples_per_curve=4)
                except Exception:
                    pts = []
        if not pts:
            w, h = disp.get('width', 0), disp.get('height', 0)
            ax, ay = anchor
            pts = [(ax, ay), (ax + w, ay), (ax, ay + h), (ax + w, ay + h)]
        for (px, py) in pts:
            fx, fy = transform_point(px, py, disp, anchor)
            minx = fx if minx is None else min(minx, fx)
            maxx = fx if maxx is None else max(maxx, fx)
            miny = fy if miny is None else min(miny, fy)
            maxy = fy if maxy is None else max(maxy, fy)

    if minx is None:
        minx, miny, maxx, maxy = 0, 0, 100, 100
    margin = max((maxx - minx), (maxy - miny)) * 0.03 + 1
    minx -= margin; miny -= margin; maxx += margin; maxy += margin
    vb_w, vb_h = max(maxx - minx, 1), max(maxy - miny, 1)

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{minx:.3f} {miny:.3f} {vb_w:.3f} {vb_h:.3f}" '
           f'width="{vb_w:.1f}" height="{vb_h:.1f}">\n'
           f'<!-- VISTA PREVIA de control de calidad -- colores no reales, NO importar en LightBurn -->\n'
           + "\n".join(elems) + "\n</svg>")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(svg)


def convert_canvas_to_svg(canvas, vector_lookup, resources, output_svg_path, source_name, fmt, version):
    """Convierte UN canvas (pagina) a un SVG independiente."""
    layer_groups = {}
    layer_meta = {}  # nombre_capa -> (color_final, operacion)
    layer_profiles = {}  # nombre_capa -> lista de perfiles (para detectar si son uniformes)
    anchor_cache = {}
    stats = {'objects': 0, 'groups': set(), 'layers': set(), 'unsupported_types': set(), 'missing_paths': 0, 'missing_images': 0}

    material_profiles = canvas.get('material_profiles', {})
    color_map = lightburn.resolve_layer_colors(canvas['displays'], material_profiles)

    SUPPORTED = ('PATH', 'CIRCLE', 'RECT', 'BITMAP')
    for disp in canvas['displays']:
        stats['objects'] += 1
        layer = disp.get('layerTag', '#000000')
        stats['layers'].add(layer)
        if disp.get('groupTag'):
            stats['groups'].add(disp.get('groupTag'))
        t = disp.get('type')
        if t not in SUPPORTED:
            stats['unsupported_types'].add(t)
        if t == 'PATH' and not get_dpath(disp, vector_lookup):
            # Un PATH sin dPath y con ancho/alto cero es casi siempre un caracter espacio
            # (glifo de texto expandido, ver expand_text_objects) -- no es un dano real,
            # asi que no se cuenta como aviso para evitar falsas alarmas.
            if disp.get('width', 0) != 0 or disp.get('height', 0) != 0:
                stats['missing_paths'] += 1
        if t == 'BITMAP' and not get_image_data_uri(disp, resources):
            stats['missing_images'] += 1

        profile = material_profiles.get(disp.get('id'))
        op = lightburn.infer_operation(disp, profile)
        original_color = disp.get('layerColor', '#000000')
        final_color = color_map.get((original_color, op), original_color) if t != 'BITMAP' else original_color
        elem = convert_display_to_svg_element(disp, vector_lookup, anchor_cache, resources, override_color=final_color, profile=profile)

        group_key = f"{final_color}__{op}"
        layer_groups.setdefault(group_key, []).append(elem)
        layer_meta[group_key] = (final_color, op)
        layer_profiles.setdefault(group_key, []).append(profile)

    body = ""
    for seq, (group_key, elems) in enumerate(layer_groups.items(), start=1):
        final_color, op = layer_meta[group_key]
        # Si TODOS los objetos de esta capa comparten exactamente el mismo perfil de
        # material (potencia/velocidad/pasadas), se incluye en el nombre de la capa --
        # asi se ve directamente en LightBurn sin tener que abrir un informe aparte.
        profs = layer_profiles[group_key]
        suffix = ''
        if profs and all(p == profs[0] for p in profs):
            suffix = lightburn.format_profile_suffix(profs[0])
        layer_name = lightburn.layer_export_name(final_color, op, seq, suffix)
        body += f'<g id="{layer_name}" data-layer-color="{final_color}" data-operation="{op}">\n' + "\n".join(elems) + "\n</g>\n"

    minx, miny, maxx, maxy = None, None, None, None
    for disp in canvas['displays']:
        t = disp.get('type')
        anchor = local_anchor(disp, vector_lookup, anchor_cache)
        pts = []
        if t == 'PATH':
            d_raw = get_dpath(disp, vector_lookup)
            if d_raw:
                try:
                    pts = parse_path_points(d_raw, samples_per_curve=6)
                except Exception:
                    pts = []
        if not pts:
            w, h = disp.get('width', 0), disp.get('height', 0)
            ax, ay = anchor
            pts = [(ax, ay), (ax + w, ay), (ax, ay + h), (ax + w, ay + h)]
        for (px, py) in pts:
            fx, fy = transform_point(px, py, disp, anchor)
            minx = fx if minx is None else min(minx, fx)
            maxx = fx if maxx is None else max(maxx, fx)
            miny = fy if miny is None else min(miny, fy)
            maxy = fy if maxy is None else max(maxy, fy)

    if minx is None:
        minx, miny, maxx, maxy = 0, 0, 100, 100
    margin = max((maxx - minx), (maxy - miny)) * 0.03 + 1
    minx -= margin; miny -= margin; maxx += margin; maxy += margin
    vb_w = max(maxx - minx, 1)
    vb_h = max(maxy - miny, 1)

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{minx:.3f} {miny:.3f} {vb_w:.3f} {vb_h:.3f}" width="{vb_w:.1f}" height="{vb_h:.1f}">
<!-- Generado por XCS2SVG Converter - Fuente: {source_name} - Pagina/canvas: {canvas.get('title')} (formato {fmt}, version {version}) -->
{body}
</svg>'''

    with open(output_svg_path, 'w', encoding='utf-8') as f:
        f.write(svg)

    return stats


def convert_canvas_to_dxf(canvas, vector_lookup, output_dxf_path):
    """Convierte UN canvas (pagina) a un archivo DXF (R12) independiente.
    NOTA: los objetos BITMAP se omiten (ver dxf_writer.py); esto es intencional, ya que
    DXF en flujos de laser/CNC es un formato vectorial, no de imagen.

    NOTA SOBRE EL EJE Y: el proyecto original (y por tanto nuestro transform_point/SVG)
    usa la convencion Y-hacia-abajo, propia de sistemas tipo Pixi.js/canvas/SVG. DXF y el
    software CAD/laser en general usan la convencion matematica estandar Y-hacia-arriba.
    Sin invertir el signo de Y, el dibujo aparece reflejado verticalmente (lo cual, para
    formas sin fuerte asimetria, se percibe facilmente como "girado 180 grados"). Por eso
    se invierte aqui el signo de Y en el punto FINAL ya transformado (no se toca la formula
    de transformacion en si, que ya esta validada visualmente contra el SVG)."""
    dxf = DXFWriter()
    anchor_cache = {}
    dxf_stats = {'exported': 0, 'skipped_bitmap': 0}
    material_profiles = canvas.get('material_profiles', {})
    color_map = lightburn.resolve_layer_colors(canvas['displays'], material_profiles)
    layer_seq = {}

    def to_dxf(pt):
        return (pt[0], -pt[1])

    def profile_of(disp):
        return material_profiles.get(disp.get('id'))

    # Pre-pasada: agrupar por (color_final, operacion) para saber si todos los objetos
    # de esa capa comparten el mismo perfil de material (y poder incluirlo en el nombre)
    group_profiles = {}
    for disp in canvas['displays']:
        profile = profile_of(disp)
        op = lightburn.infer_operation(disp, profile)
        original_color = disp.get('layerColor', '#000000')
        final_color = color_map.get((original_color, op), original_color) if disp.get('type') != 'BITMAP' else original_color
        group_profiles.setdefault((final_color, op), []).append(profile)

    layer_name_cache = {}
    def get_layer_name(disp):
        profile = profile_of(disp)
        op = lightburn.infer_operation(disp, profile)
        original_color = disp.get('layerColor', '#000000')
        final_color = color_map.get((original_color, op), original_color) if disp.get('type') != 'BITMAP' else original_color
        key = (final_color, op)
        if key not in layer_seq:
            layer_seq[key] = len(layer_seq) + 1
        if key not in layer_name_cache:
            profs = group_profiles[key]
            suffix = lightburn.format_profile_suffix(profs[0]) if profs and all(p == profs[0] for p in profs) else ''
            layer_name_cache[key] = lightburn.layer_export_name(final_color, op, layer_seq[key], suffix)
        return dxf.add_layer(layer_name_cache[key], final_color)

    for disp in canvas['displays']:
        t = disp.get('type')
        layer_name = get_layer_name(disp)
        anchor = local_anchor(disp, vector_lookup, anchor_cache)

        if t == 'PATH':
            d_raw = get_dpath(disp, vector_lookup)
            if not d_raw:
                continue
            try:
                subpaths = parse_path_subpaths(d_raw, samples_per_curve=10)
            except Exception:
                continue
            for sp in subpaths:
                pts_final = [to_dxf(transform_point(px, py, disp, anchor)) for (px, py) in sp['points']]
                dxf.add_polyline(layer_name, pts_final, closed=sp['closed'])
            dxf_stats['exported'] += 1

        elif t == 'CIRCLE':
            w, h = disp.get('width', 0), disp.get('height', 0)
            if abs(w - h) < 1e-6:
                cx, cy = to_dxf(transform_point(0, 0, disp, anchor))
                radius = (w / 2) * disp.get('scale', {'x': 1}).get('x', 1)
                dxf.add_circle(layer_name, cx, cy, radius)
            else:
                n = 72
                pts_local = [((w/2) * math.cos(2*math.pi*k/n), (h/2) * math.sin(2*math.pi*k/n)) for k in range(n+1)]
                pts_final = [to_dxf(transform_point(px, py, disp, anchor)) for (px, py) in pts_local]
                dxf.add_polyline(layer_name, pts_final, closed=True)
            dxf_stats['exported'] += 1

        elif t == 'RECT':
            w, h = disp.get('width', 0), disp.get('height', 0)
            pts_local = [(0, 0), (w, 0), (w, h), (0, h)]
            pts_final = [to_dxf(transform_point(px, py, disp, anchor)) for (px, py) in pts_local]
            dxf.add_polyline(layer_name, pts_final, closed=True)
            dxf_stats['exported'] += 1

        elif t == 'BITMAP':
            dxf_stats['skipped_bitmap'] += 1
        elif t == 'TEXT_LABEL':
            pass  # de momento no se exporta a DXF (ver convert_canvas_to_svg para el SVG)

    dxf.write(output_dxf_path)
    return dxf_stats


# ---------------------------------------------------------------------------
# Exportacion a PDF (curvas Bezier reales + imagenes BITMAP incrustadas)
# ---------------------------------------------------------------------------

def _image_placement_matrix(disp, minx, maxy):
    """Calcula la matriz PDF (a,b,c,d,e,f) que situa la imagen (su 'cuadrado unidad')
    en la posicion/rotacion/escala finales correctas, evaluando numericamente la
    transformacion YA VALIDADA (transform_point) en 3 puntos de referencia, en vez de
    re-derivar el algebra de matrices a mano (mas seguro dado el historial de bugs de
    transformacion de este proyecto: reutilizar codigo ya probado es preferible a
    reimplementar la misma matematica de una forma nueva sin verificar)."""
    ow = disp.get('originWidth') or disp.get('width', 0) or 1
    oh = disp.get('originHeight') or disp.get('height', 0) or 1
    anchor = (0.0, 0.0)

    def to_pdf_pt(local_pt):
        mx, my = transform_point(local_pt[0], local_pt[1], disp, anchor)
        return ((mx - minx) * MM_TO_PT, (maxy - my) * MM_TO_PT)

    origin = to_pdf_pt((0, oh))   # esquina que sera (0,0) de la imagen en PDF (abajo-izq)
    p_u = to_pdf_pt((ow, oh))     # eje horizontal de la imagen
    p_v = to_pdf_pt((0, 0))       # eje vertical de la imagen
    a, b = p_u[0]-origin[0], p_u[1]-origin[1]
    c, d = p_v[0]-origin[0], p_v[1]-origin[1]
    e, f = origin
    return (a, b, c, d, e, f)


def convert_canvas_to_pdf(canvas, vector_lookup, resources, output_pdf_path):
    """Convierte UN canvas a PDF vectorial. A diferencia del DXF, aqui se preservan las
    curvas Bezier reales (operador 'c' de PDF) y se incrustan las imagenes BITMAP como
    XObjects reales (decodificadas via Pillow para obtener pixeles RGB crudos)."""
    from PIL import Image

    anchor_cache = {}
    minx, miny, maxx, maxy = None, None, None, None
    for disp in canvas['displays']:
        t = disp.get('type')
        anchor = local_anchor(disp, vector_lookup, anchor_cache)
        pts = []
        if t == 'PATH':
            d_raw = get_dpath(disp, vector_lookup)
            if d_raw:
                try:
                    pts = parse_path_points(d_raw, samples_per_curve=6)
                except Exception:
                    pts = []
        if not pts:
            w, h = disp.get('width', 0), disp.get('height', 0)
            ax, ay = anchor
            pts = [(ax, ay), (ax + w, ay), (ax, ay + h), (ax + w, ay + h)]
        for (px, py) in pts:
            fx, fy = transform_point(px, py, disp, anchor)
            minx = fx if minx is None else min(minx, fx)
            maxx = fx if maxx is None else max(maxx, fx)
            miny = fy if miny is None else min(miny, fy)
            maxy = fy if maxy is None else max(maxy, fy)
    if minx is None:
        minx, miny, maxx, maxy = 0, 0, 100, 100
    margin = max(maxx-minx, maxy-miny) * 0.03 + 1
    minx -= margin; miny -= margin; maxx += margin; maxy += margin

    width_pt = (maxx - minx) * MM_TO_PT
    height_pt = (maxy - miny) * MM_TO_PT
    pdf = PDFWriter(width_pt, height_pt)

    def to_pdf(pt):
        x, y = pt
        return ((x - minx) * MM_TO_PT, (maxy - y) * MM_TO_PT)

    stats = {'exported': 0, 'images_embedded': 0, 'images_skipped': 0}
    img_seq = 0

    for disp in canvas['displays']:
        t = disp.get('type')
        anchor = local_anchor(disp, vector_lookup, anchor_cache)
        color_hex_str = disp.get('layerColor') or '#000000'
        hexc = color_hex_str.lstrip('#')
        rgb = tuple(int(hexc[i:i+2], 16)/255.0 for i in (0, 2, 4)) if len(hexc) == 6 else (0, 0, 0)
        is_fill = disp.get('isFill', False)

        if t == 'PATH':
            d_raw = get_dpath(disp, vector_lookup)
            if not d_raw:
                continue
            try:
                segments = parse_path_segments(d_raw)
            except Exception:
                continue
            pdf.set_color(rgb, stroke=not is_fill)
            pdf.set_line_width(0.3)
            for seg in segments:
                if seg[0] == 'M':
                    x, y = to_pdf(transform_point(seg[1], seg[2], disp, anchor))
                    pdf.move_to(x, y)
                elif seg[0] == 'L':
                    x, y = to_pdf(transform_point(seg[1], seg[2], disp, anchor))
                    pdf.line_to(x, y)
                elif seg[0] == 'C':
                    x1, y1 = to_pdf(transform_point(seg[1], seg[2], disp, anchor))
                    x2, y2 = to_pdf(transform_point(seg[3], seg[4], disp, anchor))
                    x3, y3 = to_pdf(transform_point(seg[5], seg[6], disp, anchor))
                    pdf.curve_to(x1, y1, x2, y2, x3, y3)
                elif seg[0] == 'Z':
                    pdf.close_path()
            pdf.paint(fill=is_fill, stroke=not is_fill)
            stats['exported'] += 1

        elif t == 'CIRCLE':
            w, h = disp.get('width', 0), disp.get('height', 0)
            rx, ry = w/2, h/2
            k = 0.5522847498
            local_pts = [
                ('M', rx, 0),
                ('C', rx, ry*k, rx*k, ry, 0, ry),
                ('C', -rx*k, ry, -rx, ry*k, -rx, 0),
                ('C', -rx, -ry*k, -rx*k, -ry, 0, -ry),
                ('C', rx*k, -ry, rx, -ry*k, rx, 0),
                ('Z',)
            ]
            pdf.set_color(rgb, stroke=not is_fill)
            for seg in local_pts:
                if seg[0] == 'M':
                    x, y = to_pdf(transform_point(seg[1], seg[2], disp, anchor))
                    pdf.move_to(x, y)
                elif seg[0] == 'C':
                    x1,y1 = to_pdf(transform_point(seg[1], seg[2], disp, anchor))
                    x2,y2 = to_pdf(transform_point(seg[3], seg[4], disp, anchor))
                    x3,y3 = to_pdf(transform_point(seg[5], seg[6], disp, anchor))
                    pdf.curve_to(x1,y1,x2,y2,x3,y3)
                elif seg[0] == 'Z':
                    pdf.close_path()
            pdf.paint(fill=is_fill, stroke=not is_fill)
            stats['exported'] += 1

        elif t == 'RECT':
            w, h = disp.get('width', 0), disp.get('height', 0)
            pdf.set_color(rgb, stroke=not is_fill)
            pts_local = [(0, 0), (w, 0), (w, h), (0, h)]
            x0, y0 = to_pdf(transform_point(*pts_local[0], disp, anchor))
            pdf.move_to(x0, y0)
            for lp in pts_local[1:]:
                x, y = to_pdf(transform_point(*lp, disp, anchor))
                pdf.line_to(x, y)
            pdf.close_path()
            pdf.paint(fill=is_fill, stroke=not is_fill)
            stats['exported'] += 1

        elif t == 'BITMAP':
            raw_bytes = None
            if disp.get('base64'):
                try:
                    b64_data = disp['base64'].split(',', 1)[1] if ',' in disp['base64'] else disp['base64']
                    raw_bytes = base64.b64decode(b64_data)
                except Exception:
                    raw_bytes = None
            else:
                rpath = disp.get('resourcePath')
                if rpath and rpath in resources:
                    raw_bytes = resources[rpath]  # ya son bytes crudos (ver load_xs_v2)

            if raw_bytes:
                try:
                    im = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
                    img_seq += 1
                    name = f"Im{img_seq}"
                    pdf.add_image(name, im.width, im.height, im.tobytes())
                    a, b, c, d, e, f = _image_placement_matrix(disp, minx, maxy)
                    pdf.content_ops.append("q")
                    pdf.content_ops.append(f"{a:.4f} {b:.4f} {c:.4f} {d:.4f} {e:.4f} {f:.4f} cm")
                    pdf.content_ops.append(f"/{name} Do")
                    pdf.content_ops.append("Q")
                    stats['images_embedded'] += 1
                except Exception:
                    stats['images_skipped'] += 1
            else:
                stats['images_skipped'] += 1

    pdf.write(output_pdf_path)
    return stats


def write_material_report(canvas, output_path):
    """Genera un informe legible (.txt) con los ajustes de material reales (potencia,
    velocidad, pasadas, tipo de proceso) detectados en el proyecto original, agrupados
    por capa/color. Solo tiene contenido util para el formato .xs v2 (el .xcs plano no
    incluye esta informacion). Si no hay datos, NO se escribe ningun archivo (evita
    saturar la carpeta de salida con notas vacias de "no hay datos")."""
    material_profiles = canvas.get('material_profiles', {})
    if not material_profiles:
        return False

    lines = [f"Ajustes de material detectados -- {canvas.get('title') or '(sin titulo)'}", "=" * 60, ""]

    # Agrupar objetos por perfil identico, para no repetir la misma linea 500 veces
    groups = {}  # (processing_type, power, speed, repeat) -> [layerColor, ...]
    covered = 0
    for disp in canvas['displays']:
        profile = material_profiles.get(disp.get('id'))
        if not profile:
            continue
        covered += 1
        key = (profile.get('processing_type'), profile.get('power'), profile.get('speed'), profile.get('repeat'))
        groups.setdefault(key, set()).add(disp.get('layerColor', '#000000'))

    total = len(canvas['displays'])
    lines.append(f"Objetos con perfil de material detectado: {covered} / {total}")
    lines.append("")
    for (ptype, power, speed, repeat), colors in sorted(groups.items(), key=lambda kv: str(kv[0])):
        op = lightburn.PROCESSING_TYPE_TO_OP.get(ptype, ptype or '?')
        lines.append(f"- {op} ({ptype}): potencia={power}  velocidad={speed}mm/s  pasadas={repeat}")
        lines.append(f"    color(es) de capa: {', '.join(sorted(colors))}")
    if covered < total:
        lines.append("")
        lines.append(f"AVISO: {total - covered} objeto(s) sin perfil de material vinculado "
                      f"(se uso la heuristica isFill para esos).")

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    return True


def convert_project_to_svg(project_path, output_svg_path, formats=None):
    """Convierte un proyecto completo. Si tiene MAS DE UN canvas (pagina), genera un archivo
    por canvas (sufijo _canvasN) en lugar de fusionarlos en un unico SVG, ya que cada canvas
    tiene su propio sistema de coordenadas independiente (ej. distintos grosores de material,
    distintas variantes de un mismo diseno). Fusionarlos causaria amontonamiento de piezas
    de paginas distintas en el mismo espacio.

    'formats' controla que archivos adicionales generar ademas del SVG (que siempre se
    genera): {'dxf': bool, 'pdf': bool, 'preview': bool}. Por defecto, todos activos.

    Cada elemento de 'results' es un diccionario (no una tupla posicional) con las claves:
    svg_path, dxf_path, pdf_path, title, stats, dxf_stats, pdf_stats, lint, preview_svg,
    preview_dxf. Usar diccionarios en vez de tuplas evita errores de desempaquetado al
    anadir nuevos formatos (como el propio PDF que se anadio despues del diseno original)."""
    if formats is None:
        formats = {'dxf': True, 'pdf': True, 'preview': True}

    data = load_project(project_path)
    source_name = os.path.basename(project_path)
    base, ext = os.path.splitext(output_svg_path)

    def process_canvas(canvas, svg_path):
        stats = convert_canvas_to_svg(canvas, data['vector_lookup'], data.get('resources', {}), svg_path,
                                       source_name, data['format'], data['source_version'])
        dxf_path, dxf_stats = None, None
        if formats.get('dxf', True):
            dxf_path = os.path.splitext(svg_path)[0] + '.dxf'
            dxf_stats = convert_canvas_to_dxf(canvas, data['vector_lookup'], dxf_path)
        pdf_path, pdf_stats = None, None
        if formats.get('pdf', True):
            pdf_path = os.path.splitext(svg_path)[0] + '.pdf'
            try:
                pdf_stats = convert_canvas_to_pdf(canvas, data['vector_lookup'], data.get('resources', {}), pdf_path)
            except ImportError:
                # Pillow no disponible: el PDF se omite en vez de romper toda la conversion
                pdf_path, pdf_stats = None, {'error': 'Pillow no instalado'}
        lint = lightburn.lint_canvas(canvas, data['vector_lookup'], get_dpath, parse_path_subpaths, transform_point, local_anchor)
        preview_svg, preview_dxf = (None, None)
        if formats.get('preview', True):
            preview_svg, preview_dxf = _maybe_generate_preview(canvas, data, svg_path)

        material_report_path = os.path.splitext(svg_path)[0] + '__ajustes_material.txt'
        has_material_data = write_material_report(canvas, material_report_path)

        return {
            'svg_path': svg_path, 'dxf_path': dxf_path, 'pdf_path': pdf_path,
            'title': canvas.get('title'), 'stats': stats, 'dxf_stats': dxf_stats,
            'pdf_stats': pdf_stats, 'lint': lint,
            'preview_svg': preview_svg, 'preview_dxf': preview_dxf,
            'material_report_path': material_report_path,
            'has_material_data': has_material_data,
        }

    results = []
    if len(data['canvases']) <= 1:
        canvas = data['canvases'][0] if data['canvases'] else {'displays': [], 'title': None}
        results.append(process_canvas(canvas, output_svg_path))
    else:
        for canvas in data['canvases']:
            safe_title = (canvas.get('title') or canvas.get('id', 'canvas')).replace(' ', '_').replace('/', '-')
            out_path = f"{base}__{safe_title}{ext}"
            results.append(process_canvas(canvas, out_path))

    return results, data['format'], data['source_version']


def generate_preview_dxf(canvas, vector_lookup, output_path):
    """Version DXF de generate_preview_svg: cada objeto en su propia capa numerada con un
    color ACI distinto (paleta ciclica), para poder distinguir visualmente piezas que en el
    archivo real comparten el mismo color/capa. NO pensado para produccion/LightBurn real,
    solo control de calidad visual."""
    from dxf_writer import DXFWriter
    dxf = DXFWriter()
    anchor_cache = {}

    def to_dxf(pt):
        return (pt[0], -pt[1])

    for idx, disp in enumerate(canvas['displays']):
        color = PREVIEW_PALETTE[idx % len(PREVIEW_PALETTE)]
        layer_name = dxf.add_layer(f"{idx:02d}_{color.lstrip('#')}", color)
        anchor = local_anchor(disp, vector_lookup, anchor_cache)
        t = disp.get('type')

        if t == 'PATH':
            d_raw = get_dpath(disp, vector_lookup)
            if not d_raw:
                continue
            try:
                subpaths = parse_path_subpaths(d_raw, samples_per_curve=10)
            except Exception:
                continue
            for sp in subpaths:
                pts_final = [to_dxf(transform_point(px, py, disp, anchor)) for (px, py) in sp['points']]
                dxf.add_polyline(layer_name, pts_final, closed=sp['closed'])
        elif t == 'CIRCLE':
            w, h = disp.get('width', 0), disp.get('height', 0)
            if abs(w - h) < 1e-6:
                cx, cy = to_dxf(transform_point(0, 0, disp, anchor))
                radius = (w / 2) * disp.get('scale', {'x': 1}).get('x', 1)
                dxf.add_circle(layer_name, cx, cy, radius)
            else:
                n = 72
                pts_local = [((w/2)*math.cos(2*math.pi*k/n), (h/2)*math.sin(2*math.pi*k/n)) for k in range(n+1)]
                pts_final = [to_dxf(transform_point(px, py, disp, anchor)) for (px, py) in pts_local]
                dxf.add_polyline(layer_name, pts_final, closed=True)
        elif t == 'RECT':
            w, h = disp.get('width', 0), disp.get('height', 0)
            pts_local = [(0, 0), (w, 0), (w, h), (0, h)]
            pts_final = [to_dxf(transform_point(px, py, disp, anchor)) for (px, py) in pts_local]
            dxf.add_polyline(layer_name, pts_final, closed=True)
        # BITMAP: omitido, igual que en la exportacion normal

    dxf.write(output_path)


def _maybe_generate_preview(canvas, data, svg_output_path):
    """Genera vistas previa a color (SVG y DXF) SOLO si detecta que muchos objetos (>=6)
    comparten el mismo color de capa, caso en el que las lineas superpuestas resultan
    dificiles de distinguir a simple vista en el archivo real (monocromo) -- esto aplica
    igual de forma independiente al SVG y al DXF, ya que cada formato necesita su propia
    version a color (un cambio de color en el SVG no afecta al DXF ni viceversa)."""
    from collections import Counter
    counts = Counter(d.get('layerColor', '#000000') for d in canvas['displays'])
    if not counts or max(counts.values()) < 6:
        return None, None
    preview_svg_path = os.path.splitext(svg_output_path)[0] + '__VISTA_PREVIA_colores.svg'
    generate_preview_svg(canvas, data['vector_lookup'], data.get('resources', {}), preview_svg_path)
    preview_dxf_path = os.path.splitext(svg_output_path)[0] + '__VISTA_PREVIA_colores.dxf'
    generate_preview_dxf(canvas, data['vector_lookup'], preview_dxf_path)
    return preview_svg_path, preview_dxf_path


if __name__ == '__main__':
    src = sys.argv[1]
    dst = sys.argv[2]
    results, fmt, ver = convert_project_to_svg(src, dst)
    print(f"Formato detectado: {fmt} (version {ver})")
    if len(results) > 1:
        print(f"AVISO - Proyecto con {len(results)} canvases/paginas independientes -> se generaron {len(results)} conjuntos SVG+DXF+PDF separados")
    for r in results:
        stats, lint = r['stats'], r['lint']
        print(f"--- Pagina: {r['title'] or '(sin titulo)'} ---")
        print(f"  Objetos: {stats['objects']}  Grupos: {len(stats['groups'])}  Capas: {stats['layers']}")
        if stats['unsupported_types']:
            print(f"  AVISO - Tipos no soportados aun: {stats['unsupported_types']}")
        if stats['missing_paths']:
            print(f"  AVISO - {stats['missing_paths']} paths sin dPath resoluble (posible dano)")
        if stats.get('missing_images'):
            print(f"  AVISO - {stats['missing_images']} imagenes BITMAP sin recurso resoluble")
        print(f"  SVG generado: {r['svg_path']}")
        if r['dxf_path']:
            ds = r['dxf_stats']
            print(f"  DXF generado: {r['dxf_path']}  (entidades exportadas: {ds['exported']}, BITMAP omitidos: {ds['skipped_bitmap']})")
        if r['pdf_path']:
            ps = r['pdf_stats']
            print(f"  PDF generado: {r['pdf_path']}  (elementos exportados: {ps.get('exported')}, imagenes incrustadas: {ps.get('images_embedded')})")
        elif r['pdf_stats'] and r['pdf_stats'].get('error'):
            print(f"  AVISO - PDF no generado: {r['pdf_stats']['error']}")
        if r.get('has_material_data'):
            print(f"  Informe de ajustes de material (potencia/velocidad/pasadas): {r['material_report_path']}")
        if r['preview_svg']:
            print(f"  INFO - Muchos objetos comparten el mismo color de capa; vistas previa a color generadas:")
            print(f"      SVG: {r['preview_svg']}")
            print(f"      DXF: {r['preview_dxf']}")
        print(f"  --- Informe LightBurn ---")
        if lint['open_cut_paths']:
            print(f"  AVISO - {len(lint['open_cut_paths'])} trayectorias de CORTE abiertas (podrian dejar una pestana sin cortar):")
            for w in lint['open_cut_paths'][:10]:
                print(f"      objeto #{w['index']} (id={w['display_id'][:8]}) subpath {w['subpath']}: hueco de {w['gap_mm']}mm")
        if lint['possible_duplicates']:
            print(f"  AVISO - {len(lint['possible_duplicates'])} posibles objetos duplicados (mismo tipo/tamano/posicion):")
            for w in lint['possible_duplicates'][:10]:
                print(f"      objeto #{w['a_index']} y #{w['b_index']}")
        if lint['overlapping_cuts']:
            print(f"  AVISO - {len(lint['overlapping_cuts'])} pares de CORTE con solapamiento muy significativo:")
            for w in lint['overlapping_cuts'][:10]:
                print(f"      objeto #{w['a_index']} y #{w['b_index']}: {w['overlap_pct']}% solapado")
        if lint.get('overlap_check_skipped'):
            print(f"  INFO - Demasiados objetos de CORTE para analisis de solapamiento par-a-par; se omitio (revisar visualmente)")
        if not any([lint['open_cut_paths'], lint['possible_duplicates'], lint['overlapping_cuts'], lint.get('overlap_check_skipped')]):
            print(f"  Sin problemas detectados.")
