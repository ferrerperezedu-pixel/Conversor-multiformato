"""
Escritor PDF minimalista (formato PDF 1.4, generado a mano).

Se eligio implementar esto sin libreria externa (reportlab, etc.) por el mismo motivo
que dxf_writer.py: no hay acceso a internet en el entorno de desarrollo para instalar
paquetes, y el formato PDF basico (objetos indirectos + content stream con operadores
de dibujo) esta bien documentado y es directo de generar a mano para el caso de uso de
"un dibujo vectorial por pagina".

A diferencia del DXF, aqui SI se preservan las curvas Bezier reales (operador "c" de
PDF), ya que las curvas son invariantes ante transformaciones afines: transformar los
puntos de control de una curva produce la misma curva que transformarla ya evaluada.
Esto da mayor fidelidad que el DXF (que aplana a poligonos porque LWPOLYLINE no soporta
splines nativas) si el destino es Illustrator, Inkscape u otro editor vectorial.

Tambien se embebe el contenido BITMAP (imagenes raster) como XObject de imagen real,
algo que el DXF omite deliberadamente (ver dxf_writer.py).

Unidades: se trabaja internamente en puntos PDF (1/72 pulgada). La conversion desde
mm la hace quien llama a este modulo (ver converter.py: convert_canvas_to_pdf).
"""
import zlib


class PDFWriter:
    def __init__(self, width_pt, height_pt):
        self.width = width_pt
        self.height = height_pt
        self.objects = []  # lista de bytes por objeto indirecto (indice+1 = numero de objeto)
        self.content_ops = []  # operadores de dibujo acumulados para el content stream
        self.images = {}  # nombre XObject -> (obj_num se resuelve al final)
        self._image_defs = []  # (name, width_px, height_px, raw_rgb_bytes)

    # ---------------- operadores de dibujo ----------------
    def set_color(self, rgb, stroke=True):
        r, g, b = rgb
        op = 'RG' if stroke else 'rg'
        self.content_ops.append(f"{r:.4f} {g:.4f} {b:.4f} {op}")

    def set_line_width(self, w):
        self.content_ops.append(f"{w:.4f} w")

    def move_to(self, x, y):
        self.content_ops.append(f"{x:.3f} {y:.3f} m")

    def line_to(self, x, y):
        self.content_ops.append(f"{x:.3f} {y:.3f} l")

    def curve_to(self, x1, y1, x2, y2, x3, y3):
        self.content_ops.append(f"{x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} {x3:.3f} {y3:.3f} c")

    def close_path(self):
        self.content_ops.append("h")

    def paint(self, fill, stroke):
        if fill and stroke:
            self.content_ops.append("B")
        elif fill:
            self.content_ops.append("f")
        elif stroke:
            self.content_ops.append("S")
        else:
            self.content_ops.append("n")

    def draw_image(self, name, x, y, w, h):
        """Dibuja una imagen previamente registrada con add_image, en la posicion y
        tamano indicados (en puntos PDF), usando 'q/Q' para aislar la matriz de
        transformacion de este bloque del resto del contenido."""
        self.content_ops.append("q")
        self.content_ops.append(f"{w:.3f} 0 0 {h:.3f} {x:.3f} {y:.3f} cm")
        self.content_ops.append(f"/{name} Do")
        self.content_ops.append("Q")

    def add_image(self, name, width_px, height_px, raw_rgb_bytes):
        """Registra una imagen RGB cruda (sin comprimir) de width_px x height_px x 3 bytes,
        para ser referenciada luego con draw_image."""
        self._image_defs.append((name, width_px, height_px, raw_rgb_bytes))

    # ---------------- ensamblado del PDF ----------------
    def write(self, path):
        objs = []  # cada elemento: bytes del objeto COMPLETO "N 0 obj ... endobj"

        def add_obj(body_bytes):
            n = len(objs) + 1
            objs.append((n, body_bytes))
            return n

        # 1: Catalog, 2: Pages, 3: Page (se enlazan mas abajo con numeros reales)
        catalog_num = add_obj(b"")  # placeholder, se rellena despues
        pages_num = add_obj(b"")
        page_num = add_obj(b"")

        # Content stream (comprimido con Flate para reducir tamano)
        content_str = "\n".join(self.content_ops)
        content_bytes = content_str.encode('latin-1', errors='replace')
        compressed = zlib.compress(content_bytes)
        content_obj = (f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n").encode('latin-1') + \
                      compressed + b"\nendstream"
        content_num = add_obj(content_obj)

        # XObjects de imagen (si hay)
        image_obj_nums = {}
        for name, w_px, h_px, raw in self._image_defs:
            comp = zlib.compress(raw)
            img_dict = (f"<< /Type /XObject /Subtype /Image /Width {w_px} /Height {h_px} "
                        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                        f"/Length {len(comp)} >>\nstream\n").encode('latin-1') + comp + b"\nendstream"
            num = add_obj(img_dict)
            image_obj_nums[name] = num

        xobject_entries = " ".join(f"/{name} {num} 0 R" for name, num in image_obj_nums.items())
        resources = f"<< /ProcSet [/PDF] /XObject << {xobject_entries} >> >>"

        # Rellenar los objetos base ahora que conocemos los numeros reales
        objs[catalog_num-1] = (catalog_num, f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode('latin-1'))
        objs[pages_num-1] = (pages_num, f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 >>".encode('latin-1'))
        objs[page_num-1] = (page_num, (
            f"<< /Type /Page /Parent {pages_num} 0 R "
            f"/MediaBox [0 0 {self.width:.3f} {self.height:.3f}] "
            f"/Resources {resources} "
            f"/Contents {content_num} 0 R >>"
        ).encode('latin-1'))

        # Ensamblar bytes finales con tabla xref valida
        header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"  # comentario binario estandar (marca no-ascii)
        buf = bytearray()
        buf += header
        offsets = [0] * (len(objs) + 1)  # offsets[0] no se usa (obj 0 reservado)

        for num, body in objs:
            offsets[num] = len(buf)
            buf += f"{num} 0 obj\n".encode('latin-1')
            buf += body
            buf += b"\nendobj\n"

        xref_offset = len(buf)
        buf += f"xref\n0 {len(objs)+1}\n".encode('latin-1')
        buf += b"0000000000 65535 f \n"
        for num in range(1, len(objs)+1):
            buf += f"{offsets[num]:010d} 00000 n \n".encode('latin-1')

        buf += (f"trailer\n<< /Size {len(objs)+1} /Root {catalog_num} 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF").encode('latin-1')

        with open(path, 'wb') as f:
            f.write(bytes(buf))
