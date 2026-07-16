"""
Integracion con flujo LightBurn.

No generamos un .lbrn2 nativo (formato propietario de LightBurn, mal documentado
publicamente) sino que preparamos el SVG/DXF para que, al importarlos en LightBurn,
las capas queden claramente identificadas y listas para asignar Corte/Grabado sin
adivinar, mas un informe de problemas tipicos antes de enviarlo a la maquina.

Deteccion de operacion (dos niveles, de mas a menos fiable):
1. Perfil de material real (devices/device-*.json -> bindings -> profiles.json),
   disponible solo en formato .xs v2. El campo 'processingType' del perfil
   (VECTOR_CUTTING, VECTOR_ENGRAVING, FILL_VECTOR_ENGRAVING, COLOR_FILL_ENGRAVE,
   BITMAP_ENGRAVING) es la fuente de verdad: es literalmente el ajuste que el
   usuario configuro en xTool Creative Space para ese objeto.
2. Heuristica por 'isFill' (usada cuando no hay perfil: formato .xcs plano, o el
   objeto no tiene binding). Confirmada empiricamente en Sac_fleurs.xcs (capas
   isFill=True/False perfectamente separadas), pero es una aproximacion: se
   encontro un caso real (Round_Wooden_Rangoli) donde isFill sugeria CORTE pero
   el perfil real indicaba VECTOR_ENGRAVING -- por eso el perfil real, cuando
   existe, tiene prioridad sobre esta heuristica.
"""
import math

PROCESSING_TYPE_TO_OP = {
    'VECTOR_CUTTING': 'CORTE',
    'VECTOR_ENGRAVING': 'GRABADO',
    'FILL_VECTOR_ENGRAVING': 'GRABADO',
    'COLOR_FILL_ENGRAVE': 'GRABADO',
    'BITMAP_ENGRAVING': 'IMAGEN',
}


LAYER_NAME_TO_OP = {
    'cut': 'CORTE', 'corte': 'CORTE', 'cortar': 'CORTE',
    'score': 'GRABADO', 'engrave': 'GRABADO', 'grabado': 'GRABADO', 'grabar': 'GRABADO',
    'mark': 'GRABADO', 'marcar': 'GRABADO',
}


def infer_operation(disp, profile=None):
    if profile:
        op = PROCESSING_TYPE_TO_OP.get(profile.get('processing_type'))
        if op:
            return op
    t = disp.get('type')
    if t == 'BITMAP':
        return 'IMAGEN'
    # Convencion muy extendida en SVG genericos de laser (plantillas tipo Glowforge):
    # capas llamadas literalmente "Cut"/"Score"/"Engrave". Cuando el nombre de capa
    # coincide con esta convencion, es una senal mas fiable que la heuristica isFill
    # (se confirmo con un archivo real: un SVG con capa "Engrave" cuyos objetos tenian
    # isFill=False, que la heuristica sola habria clasificado como CORTE por error).
    layer_name = (disp.get('layerTag') or '').strip().lower()
    if layer_name in LAYER_NAME_TO_OP:
        return LAYER_NAME_TO_OP[layer_name]
    # Heuristica de respaldo (sin datos de perfil real ni nombre de capa reconocible)
    if disp.get('isFill', False):
        return 'GRABADO'
    return 'CORTE'


def format_profile_suffix(profile):
    """Devuelve un sufijo tipo '_P90_S6_R1' (potencia/velocidad/pasadas) para incluir
    en el nombre de capa, o cadena vacia si no hay datos de perfil."""
    if not profile:
        return ''
    power = profile.get('power')
    speed = profile.get('speed')
    repeat = profile.get('repeat')
    if power is None and speed is None:
        return ''
    parts = []
    if power is not None:
        parts.append(f"P{power}")
    if speed is not None:
        parts.append(f"S{speed}")
    if repeat is not None and repeat != 1:
        parts.append(f"R{repeat}")
    return '_' + '_'.join(parts) if parts else ''


def layer_export_name(layer_hex, operation, seq, profile_suffix=''):
    safe_hex = (layer_hex or '#000000').lstrip('#')
    return f"{seq:02d}_{operation}_{safe_hex}{profile_suffix}"


def resolve_layer_colors(displays, material_profiles=None):
    """LightBurn agrupa capas SOLO por color al importar SVG/DXF (las imagenes BITMAP son
    la excepcion: se importan como capa 'Imagen' independiente del color). Si un mismo color
    de capa se usa a la vez para CORTE y GRABADO vectorial (isFill mixto en objetos no-BITMAP),
    LightBurn los fusionaria en una unica capa, impidiendo asignar ajustes de laser distintos.

    Devuelve un dict {(hex_original, operacion): hex_final} - normalmente identidad, y solo
    remapea la operacion GRABADO a un color derivado (mismo tono, luminosidad reducida) cuando
    detecta esta colision real con CORTE en el mismo color."""
    material_profiles = material_profiles or {}
    ops_per_color = {}
    for disp in displays:
        if disp.get('type') == 'BITMAP':
            continue
        color = (disp.get('layerColor') or '#000000')
        op = infer_operation(disp, material_profiles.get(disp.get('id')))
        ops_per_color.setdefault(color, set()).add(op)

    mapping = {}
    for color, ops in ops_per_color.items():
        if 'CORTE' in ops and 'GRABADO' in ops:
            # colision real: oscurecer el color para GRABADO, manteniendo el CORTE original
            hexc = color.lstrip('#')
            r, g, b = int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16)
            factor = 0.55
            r2, g2, b2 = int(r * factor), int(g * factor), int(b * factor)
            derived = f"#{r2:02x}{g2:02x}{b2:02x}"
            mapping[(color, 'GRABADO')] = derived
            mapping[(color, 'CORTE')] = color
        else:
            for op in ops:
                mapping[(color, op)] = color
    return mapping


# ---------------------------------------------------------------------------
# Linter: problemas habituales antes de enviar a la maquina
# ---------------------------------------------------------------------------

def lint_canvas(canvas, vector_lookup, get_dpath_fn, parse_subpaths_fn, transform_point_fn, local_anchor_fn):
    """Analiza un canvas en busca de problemas tipicos de flujo laser:
    - trayectorias de CORTE 'casi cerradas' (gap pequeño relativo a su propio tamaño: sugiere
      un error de precision/redondeo del archivo original, NO una linea abierta intencional
      como una marca de registro o un detalle decorativo, que se ignoran deliberadamente)
    - objetos practicamente duplicados (mismo tipo+tamaño+posicion -> doble corte/grabado)
    - pares de objetos de CORTE con solapamiento MUY significativo y de tamaño comparable
      (posible corte redundante). Este chequeo es O(n^2) y en disenos muy densos (>150
      objetos) genera demasiados falsos positivos por proximidad normal entre piezas
      decorativas vecinas, asi que se omite con un aviso en su lugar.
    Devuelve un dict con listas de avisos, pensados para mostrar al usuario, no para
    'arreglar' automaticamente el archivo (una correccion automatica podria alterar el
    diseño sin que el usuario lo sepa)."""
    anchor_cache = {}
    warnings = {'open_cut_paths': [], 'possible_duplicates': [], 'overlapping_cuts': [], 'overlap_check_skipped': False}

    footprints = []  # (idx, disp, op, minx,miny,maxx,maxy)

    for idx, disp in enumerate(canvas['displays']):
        op = infer_operation(disp, canvas.get('material_profiles', {}).get(disp.get('id')))
        t = disp.get('type')
        anchor = local_anchor_fn(disp, vector_lookup, anchor_cache)

        # --- trayectorias 'casi cerradas' (solo relevante para CORTE, tipo PATH) ---
        if op == 'CORTE' and t == 'PATH':
            d = get_dpath_fn(disp, vector_lookup)
            if d:
                try:
                    subpaths = parse_subpaths_fn(d, samples_per_curve=6)
                except Exception:
                    subpaths = []
                for si, sp in enumerate(subpaths):
                    pts = sp['points']
                    if len(pts) < 4:
                        # muy pocos puntos: probablemente una marca/linea simple, no una
                        # figura que deba cerrar
                        continue
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    diag = math.dist((min(xs), min(ys)), (max(xs), max(ys)))
                    if diag < 0.5:
                        continue
                    dist_ends = math.dist(pts[0], pts[-1])
                    # solo es sospechoso si el hueco es PEQUEÑO relativo al tamaño de la
                    # propia figura (indica precision/redondeo, no una linea abierta real)
                    threshold = max(0.05, diag * 0.02)
                    if not sp['closed'] and 0.05 < dist_ends < threshold:
                        warnings['open_cut_paths'].append({
                            'display_id': disp.get('id'), 'index': idx, 'subpath': si,
                            'gap_mm': round(dist_ends, 3)
                        })

        # --- bbox transformado, para duplicados/solapamientos ---
        pts = []
        if t == 'PATH':
            d = get_dpath_fn(disp, vector_lookup)
            if d:
                try:
                    from svgpath_bbox import parse_path_points
                    pts = parse_path_points(d, samples_per_curve=4)
                except Exception:
                    pts = []
        if not pts:
            w, h = disp.get('width', 0), disp.get('height', 0)
            ax, ay = anchor
            pts = [(ax, ay), (ax + w, ay), (ax, ay + h), (ax + w, ay + h)]
        xs, ys = [], []
        for (px, py) in pts:
            fx, fy = transform_point_fn(px, py, disp, anchor)
            xs.append(fx); ys.append(fy)
        if xs:
            footprints.append((idx, disp, op, min(xs), min(ys), max(xs), max(ys)))

    # --- duplicados: mismo tipo+operacion+bbox casi identico (tolerancia 0.2mm) ---
    TOL = 0.2
    for i in range(len(footprints)):
        for j in range(i + 1, len(footprints)):
            idx_i, disp_i, op_i, minx_i, miny_i, maxx_i, maxy_i = footprints[i]
            idx_j, disp_j, op_j, minx_j, miny_j, maxx_j, maxy_j = footprints[j]
            if disp_i.get('type') != disp_j.get('type') or op_i != op_j:
                continue
            if (abs(minx_i - minx_j) < TOL and abs(miny_i - miny_j) < TOL and
                    abs(maxx_i - maxx_j) < TOL and abs(maxy_i - maxy_j) < TOL):
                warnings['possible_duplicates'].append({
                    'a_id': disp_i.get('id'), 'a_index': idx_i,
                    'b_id': disp_j.get('id'), 'b_index': idx_j,
                })

    # --- solapamiento MUY significativo ENTRE objetos de CORTE de tamaño comparable ---
    cuts = [f for f in footprints if f[2] == 'CORTE']
    if len(cuts) > 150:
        warnings['overlap_check_skipped'] = True
    else:
        for i in range(len(cuts)):
            for j in range(i + 1, len(cuts)):
                _, _, _, ax0, ay0, ax1, ay1 = cuts[i]
                _, _, _, bx0, by0, bx1, by1 = cuts[j]
                ox = max(0, min(ax1, bx1) - max(ax0, bx0))
                oy = max(0, min(ay1, by1) - max(ay0, by0))
                overlap_area = ox * oy
                area_a = (ax1 - ax0) * (ay1 - ay0)
                area_b = (bx1 - bx0) * (by1 - by0)
                if area_a == 0 or area_b == 0:
                    continue
                smaller, larger = min(area_a, area_b), max(area_a, area_b)
                # exigir ademas que ambas piezas sean de tamaño COMPARABLE (no una pequeña
                # dentro del hueco natural de una grande), para evitar falsos positivos en
                # disenos densos con piezas decorativas de tamaños muy distintos
                size_ratio = smaller / larger
                if overlap_area / smaller > 0.8 and size_ratio > 0.5:
                    warnings['overlapping_cuts'].append({
                        'a_id': cuts[i][1].get('id'), 'a_index': cuts[i][0],
                        'b_id': cuts[j][1].get('id'), 'b_index': cuts[j][0],
                        'overlap_pct': round(100 * overlap_area / smaller, 1),
                    })

    return warnings
