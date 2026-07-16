"""
Escritor DXF minimalista (formato AC1015 / AutoCAD 2000, entidad LWPOLYLINE).

Se eligio implementar esto a mano (en vez de una libreria como ezdxf) porque el entorno
de ejecucion no tiene acceso a internet para instalar paquetes. El formato DXF es texto
plano y esta bien documentado.

CAMBIO IMPORTANTE: la primera version de este escritor usaba la entidad POLYLINE clasica
(con sub-entidades VERTEX + SEQEND), valida desde DXF R12. Un usuario reporto que los
archivos se veian "desordenados" al importarlos en su software de laser, mientras que el
SVG (generado con una geometria identica, verificada matematicamente) se veia perfecto.
Esto apunta a un problema de INTERPRETACION de esa entidad clasica por parte del software
importador, no a un error en las coordenadas. Se cambio a LWPOLYLINE (la entidad estandar
desde AutoCAD 2000/R2000, "AC1015"), que es una unica entidad autocontenida (no depende de
sub-entidades encadenadas) y tiene un soporte mucho mas consistente en software CAD/laser
moderno, incluido LightBurn.

Entidades usadas:
- LWPOLYLINE para trazados (PATH) y rectangulos (RECT)
- CIRCLE para elipses cuando rx==ry; poligono LWPOLYLINE cuando rx!=ry (LWPOLYLINE no
  soporta arcos elipticos nativos de forma sencilla en escritura manual)
- Capas (LAYER) creadas dinamicamente segun el color/layerTag del proyecto original,
  con un Color Index (ACI) aproximado al color RGB mas cercano de la paleta basica.

LIMITACION CONOCIDA: los objetos BITMAP (imagenes raster) NO se exportan a DXF, ya que
los formatos DXF usados en flujos de corte/grabado laser son fundamentalmente vectoriales.
"""
import math

ACI_PALETTE = {
    1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255),
    5: (0, 0, 255), 6: (255, 0, 255), 7: (255, 255, 255), 8: (128, 128, 128),
}


def closest_aci(hex_color):
    hex_color = (hex_color or '#000000').lstrip('#')
    if len(hex_color) != 6:
        return 7
    r = int(hex_color[0:2], 16); g = int(hex_color[2:4], 16); b = int(hex_color[4:6], 16)
    if (r, g, b) in [(0, 0, 0), (255, 255, 255)]:
        return 7
    best_idx, best_dist = 7, float('inf')
    for idx, (pr, pg, pb) in ACI_PALETTE.items():
        dist = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if dist < best_dist:
            best_dist, best_idx = dist, idx
    return best_idx


class DXFWriter:
    def __init__(self):
        self.layers = {}
        self.entities = []

    def add_layer(self, name, hex_color):
        safe_name = (name or 'DEFAULT').replace('#', 'L_')
        if safe_name not in self.layers:
            self.layers[safe_name] = closest_aci(hex_color)
        return safe_name

    def add_polyline(self, layer_name, points, closed=False):
        if len(points) < 2:
            return
        flags = 1 if closed else 0
        lines = ["0", "LWPOLYLINE", "8", layer_name,
                 "100", "AcDbEntity", "100", "AcDbPolyline",
                 "90", str(len(points)), "70", str(flags)]
        for (x, y) in points:
            lines.append("10"); lines.append(f"{x:.6f}")
            lines.append("20"); lines.append(f"{y:.6f}")
        self.entities.append("\n".join(lines))

    def add_circle(self, layer_name, cx, cy, radius):
        lines = ["0", "CIRCLE", "8", layer_name, "100", "AcDbEntity", "100", "AcDbCircle",
                 "10", f"{cx:.6f}", "20", f"{cy:.6f}", "30", "0.0", "40", f"{radius:.6f}"]
        self.entities.append("\n".join(lines))

    def write(self, path):
        header = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1015", "0", "ENDSEC"]

        tables = ["0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER",
                  "70", str(len(self.layers) if self.layers else 1)]
        if not self.layers:
            self.layers['DEFAULT'] = 7
        for name, aci in self.layers.items():
            tables += ["0", "LAYER", "2", name, "70", "0", "62", str(aci), "6", "CONTINUOUS"]
        tables += ["0", "ENDTAB", "0", "ENDSEC"]

        entities_section = ["0", "SECTION", "2", "ENTITIES"]
        for e in self.entities:
            entities_section.append(e)
        entities_section += ["0", "ENDSEC"]

        eof = ["0", "EOF"]

        full = "\n".join(header + tables + entities_section + eof)
        with open(path, 'w', encoding='ascii', errors='replace') as f:
            f.write(full)
