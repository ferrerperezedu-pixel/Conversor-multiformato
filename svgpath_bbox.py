import re

TOKEN_RE = re.compile(r'([MLHVCSQTAZmlhvcsqtaz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)')

def tokenize(d):
    tokens = []
    for m in TOKEN_RE.finditer(d):
        if m.group(1):
            tokens.append(('cmd', m.group(1)))
        elif m.group(2):
            tokens.append(('num', float(m.group(2))))
    return tokens

def parse_path_points(d, samples_per_curve=12):
    """Devuelve una lista de puntos (x,y) que aproximan bien el trazado real (incluyendo curvas),
    suficiente para calcular un bounding box preciso."""
    tokens = tokenize(d)
    i = 0
    pts = []
    cx, cy = 0.0, 0.0       # posicion actual
    start_x, start_y = 0.0, 0.0
    last_cmd = None
    prev_ctrl = None  # para S/T reflejar el control anterior

    def read_nums(n):
        nonlocal i
        vals = []
        for _ in range(n):
            vals.append(tokens[i][1]); i += 1
        return vals

    while i < len(tokens):
        ttype, tval = tokens[i]
        if ttype == 'cmd':
            cmd = tval
            i += 1
        else:
            # numero repetido de comando anterior implicito
            cmd = last_cmd
        if cmd is None:
            break

        upper = cmd.upper()
        rel = cmd.islower()

        if upper == 'M':
            x, y = read_nums(2)
            if rel: x += cx; y += cy
            cx, cy = x, y
            start_x, start_y = cx, cy
            pts.append((cx, cy))
            last_cmd = 'l' if rel else 'L'  # tras M subsiguientes pares son L implicitos
            prev_ctrl = None

        elif upper == 'L':
            x, y = read_nums(2)
            if rel: x += cx; y += cy
            cx, cy = x, y
            pts.append((cx, cy))
            last_cmd = cmd
            prev_ctrl = None

        elif upper == 'H':
            x, = read_nums(1)
            if rel: x += cx
            cx = x
            pts.append((cx, cy))
            last_cmd = cmd
            prev_ctrl = None

        elif upper == 'V':
            y, = read_nums(1)
            if rel: y += cy
            cy = y
            pts.append((cx, cy))
            last_cmd = cmd
            prev_ctrl = None

        elif upper == 'C':
            x1,y1,x2,y2,x,y = read_nums(6)
            if rel:
                x1+=cx; y1+=cy; x2+=cx; y2+=cy; x+=cx; y+=cy
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**3*cx + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x
                by = mt**3*cy + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y
                pts.append((bx, by))
            prev_ctrl = (x2, y2)
            cx, cy = x, y
            last_cmd = cmd

        elif upper == 'S':
            x2,y2,x,y = read_nums(4)
            if rel:
                x2+=cx; y2+=cy; x+=cx; y+=cy
            if prev_ctrl:
                x1 = 2*cx - prev_ctrl[0]
                y1 = 2*cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**3*cx + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x
                by = mt**3*cy + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y
                pts.append((bx, by))
            prev_ctrl = (x2, y2)
            cx, cy = x, y
            last_cmd = cmd

        elif upper == 'Q':
            x1,y1,x,y = read_nums(4)
            if rel:
                x1+=cx; y1+=cy; x+=cx; y+=cy
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**2*cx + 2*mt*t*x1 + t**2*x
                by = mt**2*cy + 2*mt*t*y1 + t**2*y
                pts.append((bx, by))
            prev_ctrl = (x1, y1)
            cx, cy = x, y
            last_cmd = cmd

        elif upper == 'T':
            x, y = read_nums(2)
            if rel: x+=cx; y+=cy
            if prev_ctrl:
                x1 = 2*cx - prev_ctrl[0]
                y1 = 2*cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**2*cx + 2*mt*t*x1 + t**2*x
                by = mt**2*cy + 2*mt*t*y1 + t**2*y
                pts.append((bx, by))
            prev_ctrl = (x1, y1)
            cx, cy = x, y
            last_cmd = cmd

        elif upper == 'A':
            rx, ry, rot, laf, sf, x, y = read_nums(7)
            if rel: x += cx; y += cy
            # aproximacion cruda: solo extremos (suficiente para bbox estimado)
            pts.append((x, y))
            cx, cy = x, y
            last_cmd = cmd
            prev_ctrl = None

        elif upper == 'Z':
            cx, cy = start_x, start_y
            pts.append((cx, cy))
            last_cmd = cmd
            prev_ctrl = None
        else:
            i += 1  # comando desconocido, saltar

    return pts

def parse_path_subpaths(d, samples_per_curve=10):
    """Como parse_path_points, pero devuelve una lista de sub-trazados independientes
    (una lista de puntos por cada segmento que empieza con 'M'), junto con si cada uno
    se cerro explicitamente con 'Z'. Necesario para exportar a DXF, donde cada subpath
    (ej. contorno exterior + agujeros de una figura compuesta) debe ser su propia entidad,
    no una unica polilinea continua (que conectaria erroneamente piezas separadas)."""
    tokens = tokenize(d)
    i = 0
    subpaths = []  # lista de dicts {'points': [...], 'closed': bool}
    current = None
    cx, cy = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    last_cmd = None
    prev_ctrl = None

    def read_nums(n):
        nonlocal i
        vals = []
        for _ in range(n):
            vals.append(tokens[i][1]); i += 1
        return vals

    def ensure_current():
        nonlocal current
        if current is None:
            current = {'points': [], 'closed': False}
            subpaths.append(current)
        return current

    while i < len(tokens):
        ttype, tval = tokens[i]
        if ttype == 'cmd':
            cmd = tval
            i += 1
        else:
            cmd = last_cmd
        if cmd is None:
            break
        upper = cmd.upper()
        rel = cmd.islower()

        if upper == 'M':
            x, y = read_nums(2)
            if rel: x += cx; y += cy
            cx, cy = x, y
            start_x, start_y = cx, cy
            current = {'points': [(cx, cy)], 'closed': False}
            subpaths.append(current)
            last_cmd = 'l' if rel else 'L'
            prev_ctrl = None

        elif upper == 'L':
            x, y = read_nums(2)
            if rel: x += cx; y += cy
            cx, cy = x, y
            ensure_current()['points'].append((cx, cy))
            last_cmd = cmd; prev_ctrl = None

        elif upper == 'H':
            x, = read_nums(1)
            if rel: x += cx
            cx = x
            ensure_current()['points'].append((cx, cy))
            last_cmd = cmd; prev_ctrl = None

        elif upper == 'V':
            y, = read_nums(1)
            if rel: y += cy
            cy = y
            ensure_current()['points'].append((cx, cy))
            last_cmd = cmd; prev_ctrl = None

        elif upper == 'C':
            x1,y1,x2,y2,x,y = read_nums(6)
            if rel:
                x1+=cx; y1+=cy; x2+=cx; y2+=cy; x+=cx; y+=cy
            pts = ensure_current()['points']
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**3*cx + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x
                by = mt**3*cy + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y
                pts.append((bx, by))
            prev_ctrl = (x2, y2); cx, cy = x, y; last_cmd = cmd

        elif upper == 'S':
            x2,y2,x,y = read_nums(4)
            if rel:
                x2+=cx; y2+=cy; x+=cx; y+=cy
            if prev_ctrl:
                x1 = 2*cx - prev_ctrl[0]; y1 = 2*cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            pts = ensure_current()['points']
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**3*cx + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x
                by = mt**3*cy + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y
                pts.append((bx, by))
            prev_ctrl = (x2, y2); cx, cy = x, y; last_cmd = cmd

        elif upper == 'Q':
            x1,y1,x,y = read_nums(4)
            if rel:
                x1+=cx; y1+=cy; x+=cx; y+=cy
            pts = ensure_current()['points']
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**2*cx + 2*mt*t*x1 + t**2*x
                by = mt**2*cy + 2*mt*t*y1 + t**2*y
                pts.append((bx, by))
            prev_ctrl = (x1, y1); cx, cy = x, y; last_cmd = cmd

        elif upper == 'T':
            x, y = read_nums(2)
            if rel: x+=cx; y+=cy
            if prev_ctrl:
                x1 = 2*cx - prev_ctrl[0]; y1 = 2*cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            pts = ensure_current()['points']
            for t in [j/samples_per_curve for j in range(1, samples_per_curve+1)]:
                mt = 1-t
                bx = mt**2*cx + 2*mt*t*x1 + t**2*x
                by = mt**2*cy + 2*mt*t*y1 + t**2*y
                pts.append((bx, by))
            prev_ctrl = (x1, y1); cx, cy = x, y; last_cmd = cmd

        elif upper == 'A':
            rx, ry, rot, laf, sf, x, y = read_nums(7)
            if rel: x += cx; y += cy
            ensure_current()['points'].append((x, y))
            cx, cy = x, y; last_cmd = cmd; prev_ctrl = None

        elif upper == 'Z':
            cx, cy = start_x, start_y
            cur = ensure_current()
            cur['points'].append((cx, cy))
            cur['closed'] = True
            last_cmd = cmd; prev_ctrl = None
        else:
            i += 1

    return subpaths


def bbox_of_path(d):
    pts = parse_path_points(d)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)
