"""
Lector de PDF (y de archivos .ai modernos, que son PDF por dentro desde hace anos en
Illustrator) para el conversor multiformato.

A diferencia de escribir PDF (pdf_writer.py, donde nosotros decidimos que operadores
usar), aqui hay que INTERPRETAR el content stream de un PDF arbitrario: una lista de
operadores de dibujo tipo stack-machine (parecido a PostScript, pero mas simple y sin
control de flujo). Se apoya en pypdf solo para el CONTENEDOR (extraer paginas, resolver
streams comprimidos, leer Resources) -- la interpretacion del contenido del stream
(los propios operadores de dibujo) se hace aqui, ya que pypdf no interpreta graficos.

Operadores soportados (el subconjunto realmente usado por Illustrator/la mayoria de
exportadores vectoriales, confirmado inspeccionando archivos reales):
  Construccion de trazado: m, l, c, v, y, h, re
  Pintado:                 S, s, f, F, f*, B, B*, b, b*, n
  Estado grafico:          q, Q, cm, w
  Color:                   g, G, rg, RG, k, K, sc, scn, SC, SCN, cs, CS
  Texto:                   BT, ET, Tf, Tm, Td, TD, Tj, TJ, T*, '
  Capas (Optional Content Groups, usadas por Illustrator para las capas):
                            BDC, EMC (con /OC <tag> BDC)

No se interpreta: patrones, sombreados, formularios XObject anidados, ni fuentes
embebidas para reconstruir el contorno EXACTO del texto (ver mas abajo).
"""
import re
import math


def tokenize_content_stream(data):
    """Tokeniza un content stream de PDF en una lista de (tipo, valor):
    tipo='num' | 'name' (ej. /R7) | 'string' (texto entre parentesis o <hex>) |
    'array_start' | 'array_end' | 'op' (operador de dibujo)."""
    tokens = []
    i = 0
    n = len(data)
    while i < n:
        c = data[i:i+1]
        if c in b' \t\r\n\f\x00':
            i += 1
            continue
        if c == b'%':
            # comentario hasta fin de linea
            while i < n and data[i:i+1] not in b'\r\n':
                i += 1
            continue
        if c == b'/':
            j = i + 1
            while j < n and data[j:j+1] not in b' \t\r\n\f/[]()<>{}%':
                j += 1
            tokens.append(('name', data[i+1:j].decode('latin-1')))
            i = j
            continue
        if c == b'(':
            # cadena literal, respetando parentesis anidados y escapes
            depth = 1
            j = i + 1
            buf = bytearray()
            while j < n and depth > 0:
                ch = data[j:j+1]
                if ch == b'\\':
                    buf.append(data[j])
                    if j+1 < n:
                        buf.append(data[j+1])
                    j += 2
                    continue
                if ch == b'(':
                    depth += 1
                elif ch == b')':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                buf.append(data[j])
                j += 1
            tokens.append(('string', bytes(buf).decode('latin-1', errors='replace')))
            i = j
            continue
        if c == b'<' and data[i+1:i+2] != b'<':
            j = i + 1
            while j < n and data[j:j+1] != b'>':
                j += 1
            tokens.append(('string', data[i+1:j].decode('latin-1', errors='replace')))
            i = j + 1
            continue
        if data[i:i+2] == b'<<':
            # diccionario inline (ej. en BDC /OC << ... >>) -- lo saltamos como bloque opaco
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                if data[j:j+2] == b'<<':
                    depth += 1; j += 2
                elif data[j:j+2] == b'>>':
                    depth -= 1; j += 2
                else:
                    j += 1
            tokens.append(('dict', None))
            i = j
            continue
        if c == b'[':
            tokens.append(('array_start', None)); i += 1; continue
        if c == b']':
            tokens.append(('array_end', None)); i += 1; continue
        if c in b'{}':
            i += 1; continue
        # numero
        m = re.match(rb'[+-]?\d*\.?\d+', data[i:i+32])
        if m and (c.isdigit() or c in b'+-.'):
            tokens.append(('num', float(m.group(0))))
            i += m.end()
            continue
        # operador (letras, asterisco, comilla simple)
        m = re.match(rb"[A-Za-z\'\"\*]+", data[i:i+8])
        if m:
            tokens.append(('op', m.group(0).decode('latin-1')))
            i += m.end()
            continue
        i += 1  # caracter no reconocido, saltar
    return tokens


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mult(a, b):
    """Multiplica dos matrices PDF (a,b,c,d,e,f), aplicando b despues de a (b es la
    transformacion 'nueva' que se concatena sobre la CTM existente 'a')."""
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


class GraphicsState:
    def __init__(self):
        self.ctm = IDENTITY
        self.fill_color = (0.0, 0.0, 0.0)
        self.stroke_color = (0.0, 0.0, 0.0)
        self.line_width = 1.0

    def copy(self):
        gs = GraphicsState()
        gs.ctm = self.ctm
        gs.fill_color = self.fill_color
        gs.stroke_color = self.stroke_color
        gs.line_width = self.line_width
        return gs


def cmyk_to_rgb(c, m, y, k):
    r = 1 - min(1, c + k)
    g = 1 - min(1, m + k)
    b = 1 - min(1, y + k)
    return (r, g, b)


def rgb_to_hex(rgb):
    r, g, b = rgb
    return '#%02x%02x%02x' % (max(0, min(255, round(r*255))),
                               max(0, min(255, round(g*255))),
                               max(0, min(255, round(b*255))))


class ContentInterpreter:
    """Procesa los tokens de un content stream, manteniendo la pila de estado grafico
    (q/Q), la matriz de transformacion actual (cm), y construye trazados (subpaths)
    con las coordenadas YA TRANSFORMADAS a espacio absoluto de pagina -- asi el
    resultado no depende de cuantos 'cm' se hayan concatenado."""

    def __init__(self):
        self.gs_stack = []
        self.gs = GraphicsState()
        self.shapes = []       # {'subpaths':[[(x,y),...]], 'closed':[bool,...], 'fill':bool,'stroke':bool,'fill_color':hex,'stroke_color':hex,'layer':str}
        self.text_labels = []  # {'text':str,'x':float,'y':float,'font_size':float}
        self.current_subpaths = []
        self.current_point = (0.0, 0.0)
        self.subpath_start = (0.0, 0.0)
        self.layer_stack = ['Capa 1']
        self.text_matrix = IDENTITY
        self.text_line_matrix = IDENTITY
        self.font_size = 12.0
        self.in_text = False

    def current_layer(self):
        return self.layer_stack[-1] if self.layer_stack else 'Capa 1'

    def start_new_path(self):
        self.current_subpaths = []

    def moveto(self, x, y):
        px, py = apply_mat(self.gs.ctm, x, y)
        self.current_subpaths.append({'points': [(px, py)], 'closed': False})
        self.current_point = (x, y)
        self.subpath_start = (x, y)

    def lineto(self, x, y):
        if not self.current_subpaths:
            self.moveto(x, y)
            return
        px, py = apply_mat(self.gs.ctm, x, y)
        self.current_subpaths[-1]['points'].append((px, py))
        self.current_point = (x, y)

    def curveto(self, x1, y1, x2, y2, x3, y3, samples=12):
        if not self.current_subpaths:
            self.moveto(x1, y1)
        x0, y0 = self.current_point
        pts = self.current_subpaths[-1]['points']
        for i in range(1, samples+1):
            t = i / samples
            mt = 1 - t
            bx = mt**3*x0 + 3*mt**2*t*x1 + 3*mt*t**2*x2 + t**3*x3
            by = mt**3*y0 + 3*mt**2*t*y1 + 3*mt*t**2*y2 + t**3*y3
            px, py = apply_mat(self.gs.ctm, bx, by)
            pts.append((px, py))
        self.current_point = (x3, y3)

    def closepath(self):
        if self.current_subpaths:
            self.current_subpaths[-1]['closed'] = True
            self.current_point = self.subpath_start

    def rect(self, x, y, w, h):
        self.moveto(x, y)
        self.lineto(x+w, y)
        self.lineto(x+w, y+h)
        self.lineto(x, y+h)
        self.closepath()

    def finalize_path(self, fill, stroke, even_odd=False):
        if self.current_subpaths:
            self.shapes.append({
                'subpaths': self.current_subpaths,
                'fill': fill, 'stroke': stroke,
                'fill_color': rgb_to_hex(self.gs.fill_color),
                'stroke_color': rgb_to_hex(self.gs.stroke_color),
                'layer': self.current_layer(),
            })
        self.start_new_path()

    def run(self, tokens):
        stack = []
        i = 0
        while i < len(tokens):
            ttype, tval = tokens[i]
            if ttype != 'op':
                stack.append(tval)
                i += 1
                continue
            op = tval

            if op == 'q':
                self.gs_stack.append(self.gs.copy())
            elif op == 'Q':
                if self.gs_stack:
                    self.gs = self.gs_stack.pop()
            elif op == 'cm':
                a,b,c,d,e,f = stack[-6:]
                self.gs.ctm = mat_mult(self.gs.ctm, (a,b,c,d,e,f))
            elif op == 'w':
                self.gs.line_width = stack[-1]

            elif op in ('g',):
                v = stack[-1]; self.gs.fill_color = (v,v,v)
            elif op in ('G',):
                v = stack[-1]; self.gs.stroke_color = (v,v,v)
            elif op == 'rg':
                self.gs.fill_color = tuple(stack[-3:])
            elif op == 'RG':
                self.gs.stroke_color = tuple(stack[-3:])
            elif op == 'k':
                self.gs.fill_color = cmyk_to_rgb(*stack[-4:])
            elif op == 'K':
                self.gs.stroke_color = cmyk_to_rgb(*stack[-4:])
            elif op in ('sc', 'scn'):
                nums = [v for v in stack if isinstance(v, float)]
                if len(nums) >= 3:
                    self.gs.fill_color = tuple(nums[-3:])
                elif len(nums) == 1:
                    v = nums[-1]; self.gs.fill_color = (v,v,v)
            elif op in ('SC', 'SCN'):
                nums = [v for v in stack if isinstance(v, float)]
                if len(nums) >= 3:
                    self.gs.stroke_color = tuple(nums[-3:])
                elif len(nums) == 1:
                    v = nums[-1]; self.gs.stroke_color = (v,v,v)

            elif op == 'm':
                x, y = stack[-2:]; self.moveto(x, y)
            elif op == 'l':
                x, y = stack[-2:]; self.lineto(x, y)
            elif op == 'c':
                x1,y1,x2,y2,x3,y3 = stack[-6:]; self.curveto(x1,y1,x2,y2,x3,y3)
            elif op == 'v':
                x2,y2,x3,y3 = stack[-4:]; x0,y0 = self.current_point
                self.curveto(x0,y0,x2,y2,x3,y3)
            elif op == 'y':
                x1,y1,x3,y3 = stack[-4:]; self.curveto(x1,y1,x3,y3,x3,y3)
            elif op == 'h':
                self.closepath()
            elif op == 're':
                x,y,w,h = stack[-4:]; self.rect(x,y,w,h)

            elif op in ('S',):
                self.finalize_path(fill=False, stroke=True)
            elif op == 's':
                self.closepath(); self.finalize_path(fill=False, stroke=True)
            elif op in ('f', 'F'):
                self.finalize_path(fill=True, stroke=False)
            elif op == 'f*':
                self.finalize_path(fill=True, stroke=False, even_odd=True)
            elif op in ('B', 'B*'):
                self.finalize_path(fill=True, stroke=True, even_odd=(op=='B*'))
            elif op == 'b':
                self.closepath(); self.finalize_path(fill=True, stroke=True)
            elif op == 'b*':
                self.closepath(); self.finalize_path(fill=True, stroke=True, even_odd=True)
            elif op == 'n':
                self.finalize_path(fill=False, stroke=False)

            elif op == 'BT':
                self.in_text = True
                self.text_matrix = IDENTITY
                self.text_line_matrix = IDENTITY
            elif op == 'ET':
                self.in_text = False
            elif op == 'Tf':
                # /FontName size Tf -- el nombre viene como 'name' token, el tamano numerico
                nums = [v for v in stack if isinstance(v, float)]
                if nums:
                    self.font_size = nums[-1]
            elif op == 'Td':
                tx, ty = stack[-2:]
                self.text_line_matrix = mat_mult(self.text_line_matrix, (1,0,0,1,tx,ty))
                self.text_matrix = self.text_line_matrix
            elif op == 'TD':
                tx, ty = stack[-2:]
                self.text_line_matrix = mat_mult(self.text_line_matrix, (1,0,0,1,tx,ty))
                self.text_matrix = self.text_line_matrix
            elif op == 'Tm':
                a,b,c,d,e,f = stack[-6:]
                self.text_line_matrix = (a,b,c,d,e,f)
                self.text_matrix = self.text_line_matrix
            elif op == 'T*':
                self.text_line_matrix = mat_mult(self.text_line_matrix, (1,0,0,1,0,-self.font_size))
                self.text_matrix = self.text_line_matrix
            elif op in ('Tj', "'"):
                s = None
                for v in reversed(stack):
                    if isinstance(v, str):
                        s = v; break
                if s is not None:
                    full = mat_mult(self.gs.ctm, self.text_matrix)
                    x, y = apply_mat(full, 0, 0)
                    self.text_labels.append({'text': s, 'x': x, 'y': y, 'font_size': self.font_size,
                                              'layer': self.current_layer()})
            elif op == 'TJ':
                # array de strings/ajustes -- concatenar solo las cadenas de texto
                parts = [v for v in stack if isinstance(v, str)]
                if parts:
                    full = mat_mult(self.gs.ctm, self.text_matrix)
                    x, y = apply_mat(full, 0, 0)
                    self.text_labels.append({'text': ''.join(parts), 'x': x, 'y': y,
                                              'font_size': self.font_size, 'layer': self.current_layer()})

            elif op == 'BDC':
                # revisar si el tag de contenido opcional (OCG) trae un nombre reconocible;
                # si no, simplemente apilar una capa generica (no rompe el conteo BDC/EMC)
                name = None
                for v in reversed(stack):
                    if isinstance(v, str):
                        name = v; break
                self.layer_stack.append(name or self.current_layer())
            elif op == 'EMC':
                if len(self.layer_stack) > 1:
                    self.layer_stack.pop()

            stack = []
            i += 1
        return self.shapes, self.text_labels


def _resolve_ocg_names(page):
    """Resuelve el diccionario Resources/Properties (marcas de contenido opcional que usa
    Illustrator para las capas) a un mapa {tag_interno: nombre_real_de_capa}, ej.
    {'MC0': 'Cortar', 'MC1': 'Grabar'}. Sin esto, las capas solo tendrian el tag interno
    generico (MC0, MC1...) en vez del nombre que el usuario le puso en Illustrator."""
    names = {}
    try:
        resources = page.get('/Resources')
        if not resources:
            return names
        props = resources.get('/Properties')
        if not props:
            return names
        for tag, ref in props.items():
            try:
                ocg = ref.get_object()
                name = ocg.get('/Name')
                if name:
                    names[tag.lstrip('/')] = str(name)
            except Exception:
                continue
    except Exception:
        pass
    return names


def read_pdf_or_ai(path):
    """Punto de entrada: lee un PDF o un .ai moderno (PDF por dentro) y devuelve una
    estructura de proyecto compatible con el resto del motor (mismo formato que
    load_project(): {'canvases': [...], 'vector_lookup': {}, 'resources': {}, ...}).

    Cada pagina del PDF se convierte en un 'canvas' independiente (igual que un .xcs
    con varias paginas). Los trazados extraidos se representan como objetos PATH con
    el dPath ya en coordenadas ABSOLUTAS -- no hace falta replicar aqui la formula de
    transformacion (x,y,angle,scale,skew) de xTool: se usa el mismo truco de asignar
    x=y=ancla (ver pdf_shape_to_disp) para que el resto del motor (SVG/DXF/PDF writers,
    ya validados) siga funcionando sin cambios."""
    import pypdf
    reader = pypdf.PdfReader(path)
    canvases = []

    for page_idx, page in enumerate(reader.pages):
        ocg_names = _resolve_ocg_names(page)
        contents = page.get('/Contents')
        if contents is None:
            data = b''
        else:
            obj = contents.get_object()
            if isinstance(obj, list):
                data = b'\n'.join(o.get_object().get_data() for o in obj)
            else:
                data = obj.get_data()

        tokens = tokenize_content_stream(data)
        interp = ContentInterpreter()
        shapes, text_labels = interp.run(tokens)

        displays = []
        for shape in shapes:
            disp = pdf_shape_to_disp(shape, ocg_names)
            if disp:
                displays.append(disp)
        for label in text_labels:
            disp = pdf_text_to_disp(label, ocg_names)
            if disp:
                displays.append(disp)

        canvases.append({
            'id': f'page-{page_idx}',
            'title': f'pagina {page_idx+1}' if len(reader.pages) > 1 else None,
            'layerData': {}, 'groupData': {},
            'displays': displays,
            'material_profiles': {},
        })

    return {'canvases': canvases, 'vector_lookup': {}, 'resources': {},
            'source_version': 'pdf/ai', 'format': 'pdf-ai'}


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


def pdf_shape_to_disp(shape, ocg_names, id_counter=[0]):
    dpath = _subpaths_to_dpath(shape['subpaths'])
    if not dpath:
        return None
    all_pts = [p for sp in shape['subpaths'] for p in sp['points']]
    xs = [p[0] for p in all_pts]; ys = [p[1] for p in all_pts]
    anchor_x, anchor_y = min(xs), min(ys)

    is_fill = shape['fill']
    color = shape['fill_color'] if is_fill else shape['stroke_color']
    layer_tag = ocg_names.get(shape['layer'], shape['layer'])

    id_counter[0] += 1
    return {
        'id': f'pdfshape-{id_counter[0]}',
        'type': 'PATH',
        'x': anchor_x, 'y': anchor_y,
        'angle': 0, 'scale': {'x': 1, 'y': 1}, 'skew': {'x': 0, 'y': 0},
        'offsetX': 0, 'offsetY': 0,
        'width': max(xs) - anchor_x if xs else 0, 'height': max(ys) - anchor_y if ys else 0,
        'isFill': is_fill,
        'layerColor': color, 'layerTag': layer_tag,
        'fillRule': 'nonzero',
        'dPath': dpath,
    }


def pdf_text_to_disp(label, ocg_names, id_counter=[1000000]):
    """El texto de un PDF generico NO se convierte a contorno vectorial real (eso
    requeriria parsear el programa de la fuente embebida -- TrueType/Type1 -- para
    extraer los glifos, un problema mucho mas grande). En su lugar se representa como
    un objeto TEXT simple con el texto y posicion, para que al menos no se pierda la
    informacion (aparece como aviso/nota en vez de intentar dibujar letras exactas)."""
    id_counter[0] += 1
    layer_tag = ocg_names.get(label['layer'], label['layer'])
    return {
        'id': f'pdftext-{id_counter[0]}',
        'type': 'TEXT_LABEL',  # tipo especial: no es el TEXT de xTool (sin charJSONs)
        'text': label['text'],
        'x': label['x'], 'y': label['y'],
        'font_size': label['font_size'],
        'layerTag': layer_tag, 'layerColor': '#000000',
    }
