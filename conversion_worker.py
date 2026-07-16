"""
XCS2SVG Converter Pro - logica de conversion (sin dependencias de interfaz grafica)

Separado de gui.py deliberadamente para poder probarse de forma independiente sin
necesitar tkinter instalado (util en entornos de CI/pruebas automatizadas donde no
hay entorno grafico disponible).
"""
import os
import traceback
from converter import convert_project_to_svg


SUPPORTED_EXTENSIONS = ('.xcs', '.xs', '.svg', '.pdf', '.ai', '.dxf')


def find_source_files(paths):
    """Acepta una lista de rutas (archivos y/o carpetas) y devuelve la lista final
    de archivos a procesar, recorriendo carpetas recursivamente. Incluye tanto los
    formatos nativos de xTool (.xcs/.xs) como los formatos multiformato de entrada
    (.svg, .pdf, .ai, .dxf) -- algunos de estos ultimos (DXF generico, .ai PostScript
    antiguo) aun no estan implementados y dan un error claro al intentarlos, pero se
    incluyen en la busqueda para que el usuario vea ese mensaje en vez de que el
    archivo simplemente se ignore en silencio."""
    result = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    if f.lower().endswith(SUPPORTED_EXTENSIONS):
                        result.append(os.path.join(root, f))
        elif os.path.isfile(p) and p.lower().endswith(SUPPORTED_EXTENSIONS):
            result.append(p)
    seen = set()
    unique = []
    for f in result:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def run_conversion(input_paths, output_dir, options, msg_queue, stop_event):
    """Ejecuta la conversion completa. Envia mensajes a msg_queue para que la UI
    (en el hilo principal) los muestre sin bloquear la ventana.

    Cada archivo de origen obtiene su PROPIA SUBCARPETA de salida (nombrada igual que
    el archivo), en vez de volcar SVG+DXF+PDF+vistas previa+informes de todos los
    archivos juntos y sueltos en la misma carpeta -- evita la saturacion de archivos
    que se detecto al usar el conversor con varios proyectos a la vez.

    Mensajes enviados (tuplas):
      ('found', n)                      - se encontraron n archivos para procesar
      ('log', texto, tag)               - linea de log, tag en {'info','ok','warn','err','muted'}
      ('progress', i, total)            - progreso actual
      ('file_owner_result', owner, ok)  - un archivo/carpeta de la cola de entrada
                                           termino (ok=True/False), para que la UI
                                           marque esa fila como convertida
      ('done', stats_dict)              - conversion terminada (o cancelada)
    """
    source_files = find_source_files(input_paths)
    msg_queue.put(('found', len(source_files)))

    if not source_files:
        msg_queue.put(('done', {'total': 0, 'success': 0, 'failed': 0}))
        return

    common_base = os.path.commonpath([os.path.dirname(f) for f in source_files]) \
        if len(source_files) > 1 else os.path.dirname(source_files[0])

    # Para poder informar a la UI que fila de la cola (archivo o carpeta tal cual el
    # usuario lo anadio) corresponde a cada archivo fuente ya expandido.
    def owner_of(src_path):
        for p in input_paths:
            if os.path.isdir(p) and (src_path == p or src_path.startswith(os.path.join(p, ''))):
                return p
            if os.path.isfile(p) and src_path == p:
                return p
        return src_path

    owner_had_error = {}  # owner_path -> bool (True si algun archivo bajo el fallo)

    stats = {'total': len(source_files), 'success': 0, 'failed': 0, 'pages': 0, 'objects': 0}

    for i, src_path in enumerate(source_files, start=1):
        if stop_event.is_set():
            msg_queue.put(('log', f"Cancelado por el usuario en el archivo {i}/{len(source_files)}.", 'warn'))
            break

        rel_dir = os.path.relpath(os.path.dirname(src_path), common_base)
        base_name = os.path.splitext(os.path.basename(src_path))[0]
        # Subcarpeta propia para este archivo (evita mezclar los SVG/DXF/PDF/vistas
        # previa de distintos proyectos sueltos en la misma carpeta de salida)
        out_subdir = os.path.join(output_dir, rel_dir, base_name) if rel_dir != '.' \
            else os.path.join(output_dir, base_name)
        os.makedirs(out_subdir, exist_ok=True)
        out_svg_path = os.path.join(out_subdir, base_name + '.svg')

        owner = owner_of(src_path)
        msg_queue.put(('log', f"[{i}/{len(source_files)}] {os.path.basename(src_path)} ...", 'info'))
        try:
            results, fmt, ver = convert_project_to_svg(src_path, out_svg_path, formats=options)
            n_pages = len(results)
            n_objects = sum(r['stats']['objects'] for r in results)
            stats['success'] += 1
            stats['pages'] += n_pages
            stats['objects'] += n_objects
            msg_queue.put(('log', f"    OK - {n_pages} pagina(s), {n_objects} objetos (formato {fmt} v{ver})", 'ok'))

            for r in results:
                lint = r['lint']
                warn_count = len(lint['open_cut_paths']) + len(lint['possible_duplicates']) + len(lint['overlapping_cuts'])
                if warn_count:
                    msg_queue.put(('log', f"      Aviso LightBurn: {warn_count} elemento(s) a revisar en '{r['title'] or base_name}'", 'warn'))
                if r.get('pdf_stats') and r['pdf_stats'].get('error'):
                    msg_queue.put(('log', f"      PDF no generado: {r['pdf_stats']['error']}", 'warn'))
                if r.get('has_material_data'):
                    msg_queue.put(('log', f"      Ajustes de material reales detectados (potencia/velocidad) -> incluidos en el nombre de capa", 'ok'))
            owner_had_error.setdefault(owner, False)
        except Exception as e:
            stats['failed'] += 1
            msg_queue.put(('log', f"    ERROR: {e}", 'err'))
            msg_queue.put(('log', traceback.format_exc(), 'muted'))
            owner_had_error[owner] = True

        msg_queue.put(('progress', i, len(source_files)))
        msg_queue.put(('file_owner_result', owner, not owner_had_error.get(owner, False)))

    msg_queue.put(('done', stats))
