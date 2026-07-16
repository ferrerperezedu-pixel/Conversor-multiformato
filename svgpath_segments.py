"""
Parser de trazados SVG que preserva la estructura real de curvas (M/L/C/Z), en vez de
aplanarlas a polilineas. Usado por pdf_writer.py para generar PDF vectorial de calidad
completa (splines reales, no facetadas), a diferencia de dxf_writer.py (que si aplana,
porque las entidades LWPOLYLINE de DXF no soportan curvas nativas de forma sencilla).

Las curvas Bezier son invariantes ante transformaciones afines: aplicar una transformacion
afin (traslacion+rotacion+escala+skew) a los PUNTOS DE CONTROL de una curva produce
exactamente la misma curva que se obtendria transformando cada punto de la curva ya
evaluada. Por eso basta con transformar los puntos de control -- no hace falta volver a
muestrear la curva.
"""

from svgpath_bbox import tokenize


def parse_path_segments(d):
    """Convierte un 'd' de SVG en una lista de segmentos canonicos:
        ('M', x, y)
        ('L', x, y)
        ('C', x1,y1, x2,y2, x,y)      -- curva cubica (control1, control2, punto final)
        ('Z',)
    H/V se convierten a L. Q/T (cuadraticas) se elevan a cubicas (C) de forma exacta.
    S se convierte a C reflejando el control anterior. A (arcos elipticos) se aproxima
    como una linea recta al punto final (limitacion conocida: ningun archivo de muestra
    usado en este proyecto contenia arcos, solo M/L/C/S/Z)."""
    tokens = tokenize(d)
    i = 0
    segments = []
    cx, cy = 0.0, 0.0
    start_x, start_y = 0.0, 0.0
    last_cmd = None
    prev_ctrl = None  # para reflejar en S/T

    def read_nums(n):
        nonlocal i
        vals = []
        for _ in range(n):
            vals.append(tokens[i][1]); i += 1
        return vals

    while i < len(tokens):
        ttype, tval = tokens[i]
        if ttype == 'cmd':
            cmd = tval; i += 1
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
            segments.append(('M', cx, cy))
            last_cmd = 'l' if rel else 'L'
            prev_ctrl = None

        elif upper == 'L':
            x, y = read_nums(2)
            if rel: x += cx; y += cy
            segments.append(('L', x, y))
            cx, cy = x, y
            last_cmd = cmd; prev_ctrl = None

        elif upper == 'H':
            x, = read_nums(1)
            if rel: x += cx
            segments.append(('L', x, cy))
            cx = x
            last_cmd = cmd; prev_ctrl = None

        elif upper == 'V':
            y, = read_nums(1)
            if rel: y += cy
            segments.append(('L', cx, y))
            cy = y
            last_cmd = cmd; prev_ctrl = None

        elif upper == 'C':
            x1,y1,x2,y2,x,y = read_nums(6)
            if rel:
                x1+=cx; y1+=cy; x2+=cx; y2+=cy; x+=cx; y+=cy
            segments.append(('C', x1,y1,x2,y2,x,y))
            prev_ctrl = (x2,y2); cx,cy = x,y
            last_cmd = cmd

        elif upper == 'S':
            x2,y2,x,y = read_nums(4)
            if rel:
                x2+=cx; y2+=cy; x+=cx; y+=cy
            if prev_ctrl:
                x1 = 2*cx - prev_ctrl[0]; y1 = 2*cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            segments.append(('C', x1,y1,x2,y2,x,y))
            prev_ctrl = (x2,y2); cx,cy = x,y
            last_cmd = cmd

        elif upper == 'Q':
            x1,y1,x,y = read_nums(4)
            if rel:
                x1+=cx; y1+=cy; x+=cx; y+=cy
            # elevacion exacta de cuadratica a cubica:
            # C1 = P0 + 2/3*(Q1-P0), C2 = P2 + 2/3*(Q1-P2)
            c1x = cx + 2/3*(x1-cx); c1y = cy + 2/3*(y1-cy)
            c2x = x + 2/3*(x1-x);   c2y = y + 2/3*(y1-y)
            segments.append(('C', c1x,c1y,c2x,c2y,x,y))
            prev_ctrl = (x1,y1); cx,cy = x,y
            last_cmd = cmd

        elif upper == 'T':
            x, y = read_nums(2)
            if rel: x+=cx; y+=cy
            if prev_ctrl:
                x1 = 2*cx - prev_ctrl[0]; y1 = 2*cy - prev_ctrl[1]
            else:
                x1, y1 = cx, cy
            c1x = cx + 2/3*(x1-cx); c1y = cy + 2/3*(y1-cy)
            c2x = x + 2/3*(x1-x);   c2y = y + 2/3*(y1-y)
            segments.append(('C', c1x,c1y,c2x,c2y,x,y))
            prev_ctrl = (x1,y1); cx,cy = x,y
            last_cmd = cmd

        elif upper == 'A':
            rx,ry,rot,laf,sf,x,y = read_nums(7)
            if rel: x+=cx; y+=cy
            # limitacion conocida: se aproxima como linea recta (ningun archivo de
            # muestra de este proyecto uso arcos elipticos)
            segments.append(('L', x, y))
            cx, cy = x, y
            last_cmd = cmd; prev_ctrl = None

        elif upper == 'Z':
            segments.append(('Z',))
            cx, cy = start_x, start_y
            last_cmd = cmd; prev_ctrl = None
        else:
            i += 1

    return segments
