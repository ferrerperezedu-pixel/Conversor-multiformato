#!/usr/bin/env python3
"""
XCS2SVG Converter Pro - Aplicacion de escritorio (Tkinter)

Interfaz grafica nativa para Windows que envuelve el motor de conversion ya validado
(converter.py, dxf_writer.py, lightburn.py, svgpath_bbox.py, conversion_worker.py).
Pensada para compilarse con PyInstaller como un unico .exe y empaquetarse con Inno
Setup como instalador.

Requisitos para EJECUTAR desde codigo fuente (no hacen falta si usas el .exe ya compilado):
    pip install tkinterdnd2

Si tkinterdnd2 no esta instalado, la aplicacion sigue funcionando igual mediante los
botones "Anadir archivos" / "Anadir carpeta" -- el arrastrar y soltar es una mejora
opcional, no un requisito.
"""
import os
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conversion_worker import run_conversion, find_source_files

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


APP_TITLE = "XCS2SVG Converter Pro"
APP_VERSION = "1.0.0"

# Paleta "blueprint" -- la misma identidad visual que la version web del conversor
# (azul de plano tecnico + acento rojo de corte + acento cian de grabado), para que
# ambas interfaces se sientan como el mismo producto.
THEME = {
    'bg':        '#0d2846',
    'bg_deep':   '#081b32',
    'panel':     '#123457',
    'panel_line':'#23517f',
    'ink':       '#eaf4ff',
    'ink_muted': '#7fa2c9',
    'line':      '#bfe4ff',
    'cut':       '#ff6b5e',
    'cut_dim':   '#7a3830',
    'engrave':   '#6fd1ff',
    'ok':        '#6fe3a8',
    'warn':      '#ffcf6b',
}


class XCS2SVGApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")
        self.root.geometry("760x620")
        self.root.minsize(680, 560)

        self.input_paths = []       # archivos y/o carpetas anadidos por el usuario
        self.queue_labels = []      # etiqueta base (sin marca de estado) por fila de la cola
        self.output_dir = os.path.join(os.path.expanduser("~"), "Documents", "XCS2SVG_Convertidos")
        self.msg_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread = None

        self._build_ui()
        self._poll_queue()

    # ---------------- construccion de la UI ----------------
    def _build_ui(self):
        T = THEME
        self.root.configure(bg=T['bg'])
        self._setup_style()
        pad = {'padx': 12, 'pady': 6}

        header = tk.Frame(self.root, bg=T['bg'])
        header.pack(fill='x', **pad)
        eyebrow = tk.Label(header, text="xTool Creative Space  →  SVG / DXF / PDF", bg=T['bg'], fg=T['engrave'],
                            font=('Consolas', 9, 'bold'))
        eyebrow.pack(anchor='w')
        title_lbl = tk.Label(header, text=APP_TITLE, bg=T['bg'], fg=T['ink'], font=('Segoe UI', 18, 'bold'))
        title_lbl.pack(anchor='w')
        tk.Label(header, text="Convierte proyectos .xcs / .xs a SVG, DXF y PDF listos para LightBurn.",
                 bg=T['bg'], fg=T['ink_muted'], font=('Segoe UI', 10)).pack(anchor='w')

        # ---- Zona de archivos ----
        files_frame = tk.LabelFrame(self.root, text=" Archivos y carpetas ", bg=T['panel'], fg=T['ink_muted'],
                                     font=('Segoe UI', 9, 'bold'), bd=1, relief='solid',
                                     highlightbackground=T['panel_line'])
        files_frame.pack(fill='both', expand=True, **pad)

        drop_hint = "Arrastra aqui archivos .xcs / .xs o carpetas completas" if HAS_DND else \
                    "Usa los botones de abajo para anadir archivos o carpetas"
        self.drop_label = tk.Label(files_frame, text=drop_hint, relief='groove', bd=2,
                                    bg=T['bg_deep'], fg=T['ink_muted'], height=3)
        self.drop_label.pack(fill='x', padx=8, pady=8)

        if HAS_DND:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind('<<Drop>>', self._on_drop)

        btn_row = tk.Frame(files_frame, bg=T['panel'])
        btn_row.pack(fill='x', padx=8)
        ttk.Button(btn_row, text="Anadir archivos...", command=self._pick_files, style='XCS.TButton').pack(side='left', padx=(0, 6))
        ttk.Button(btn_row, text="Anadir carpeta...", command=self._pick_folder, style='XCS.TButton').pack(side='left', padx=(0, 6))
        ttk.Button(btn_row, text="Quitar seleccionado", command=self._remove_selected, style='XCS.TButton').pack(side='left', padx=(0, 6))
        ttk.Button(btn_row, text="Vaciar lista", command=self._clear_list, style='XCS.TButton').pack(side='left')

        self.listbox = tk.Listbox(files_frame, height=8, selectmode='extended',
                                   bg=T['bg_deep'], fg=T['ink'], selectbackground=T['cut'],
                                   selectforeground=T['bg_deep'], bd=0, highlightthickness=1,
                                   highlightbackground=T['panel_line'], font=('Consolas', 9))
        self.listbox.pack(fill='both', expand=True, padx=8, pady=(8, 2))
        self.listbox.bind('<<ListboxSelect>>', self._on_queue_select)

        self.path_hint = tk.Label(files_frame, text="", bg=T['panel'], fg=T['ink_muted'],
                                   font=('Consolas', 8), anchor='w')
        self.path_hint.pack(fill='x', padx=8, pady=(0, 8))

        # ---- Destino ----
        dest_frame = tk.Frame(self.root, bg=T['bg'])
        dest_frame.pack(fill='x', **pad)
        tk.Label(dest_frame, text="Destino:", bg=T['bg'], fg=T['ink'], font=('Segoe UI', 9, 'bold')).pack(side='left')
        self.dest_var = tk.StringVar(value=self.output_dir)
        dest_entry = tk.Entry(dest_frame, textvariable=self.dest_var, bg=T['bg_deep'], fg=T['ink'],
                               insertbackground=T['ink'], relief='flat', highlightthickness=1,
                               highlightbackground=T['panel_line'], font=('Consolas', 9))
        dest_entry.pack(side='left', fill='x', expand=True, padx=8, ipady=3)
        ttk.Button(dest_frame, text="Cambiar...", command=self._pick_output_dir, style='XCS.TButton').pack(side='left')

        # ---- Opciones ----
        opt_frame = tk.LabelFrame(self.root, text=" Opciones ", bg=T['panel'], fg=T['ink_muted'],
                                   font=('Segoe UI', 9, 'bold'), bd=1, relief='solid')
        opt_frame.pack(fill='x', **pad)
        self.opt_dxf = tk.BooleanVar(value=True)
        self.opt_pdf = tk.BooleanVar(value=True)
        self.opt_preview = tk.BooleanVar(value=True)

        def mkcheck(text, var=None, state='normal'):
            cb = tk.Checkbutton(opt_frame, text=text, variable=var, state=state,
                                 bg=T['panel'], fg=T['ink'], selectcolor=T['bg_deep'],
                                 activebackground=T['panel'], activeforeground=T['ink'],
                                 disabledforeground=T['ink_muted'], font=('Segoe UI', 9),
                                 anchor='w')
            cb.pack(anchor='w', padx=8, pady=1)
            return cb

        mkcheck("Crear SVG", tk.BooleanVar(value=True), state='disabled')
        mkcheck("Crear DXF (LightBurn)", self.opt_dxf)
        mkcheck("Crear PDF (curvas reales + imagenes)", self.opt_pdf)
        mkcheck("Vista previa a color (piezas del mismo color)", self.opt_preview)
        mkcheck("Crear LBRN2 (proximamente)", state='disabled')
        mkcheck("Reparar archivos dañados (proximamente)", state='disabled')

        # ---- Boton convertir + progreso ----
        action_frame = tk.Frame(self.root, bg=T['bg'])
        action_frame.pack(fill='x', **pad)
        self.btn_convert = ttk.Button(action_frame, text="CONVERTIR", command=self._start_conversion, style='Accent.TButton')
        self.btn_convert.pack(side='left', ipadx=10, ipady=3)
        self.btn_cancel = ttk.Button(action_frame, text="Cancelar", command=self._cancel, state='disabled', style='XCS.TButton')
        self.btn_cancel.pack(side='left', padx=8)
        self.btn_open_output = ttk.Button(action_frame, text="Abrir carpeta de destino", command=self._open_output, style='XCS.TButton')
        self.btn_open_output.pack(side='right')

        self.progress = ttk.Progressbar(self.root, mode='determinate', style='XCS.Horizontal.TProgressbar')
        self.progress.pack(fill='x', **pad)

        # ---- Consola de estado ----
        console_frame = tk.LabelFrame(self.root, text=" Estado ", bg=T['panel'], fg=T['ink_muted'],
                                       font=('Segoe UI', 9, 'bold'), bd=1, relief='solid')
        console_frame.pack(fill='both', expand=True, **pad)
        self.console = tk.Text(console_frame, height=10, bg=T['bg_deep'], fg=T['line'],
                                font=('Consolas', 9), bd=0, highlightthickness=1,
                                highlightbackground=T['panel_line'], insertbackground=T['ink'])
        self.console.pack(fill='both', expand=True, padx=6, pady=6)
        self.console.tag_config('ok', foreground=T['ok'])
        self.console.tag_config('warn', foreground=T['warn'])
        self.console.tag_config('err', foreground=T['cut'])
        self.console.tag_config('info', foreground=T['line'])
        self.console.tag_config('muted', foreground=T['ink_muted'])
        self._log("Listo. Anade archivos o carpetas para empezar.", 'muted')

        if not HAS_DND:
            self._log("(Arrastrar y soltar no disponible: instala 'tkinterdnd2' para activarlo. "
                       "Los botones funcionan igual sin esto.)", 'muted')

    def _setup_style(self):
        T = THEME
        style = ttk.Style()
        # 'clam' es el unico theme base de ttk que respeta la personalizacion de color
        # de forma consistente en Windows/Linux/Mac (los temas nativos como 'vista'
        # ignoran casi todos los colores de fondo/texto que se intenten fijar).
        style.theme_use('clam')

        style.configure('XCS.TButton', background=T['panel'], foreground=T['ink'],
                         bordercolor=T['line'], focusthickness=0, font=('Segoe UI', 9))
        style.map('XCS.TButton', background=[('active', T['panel_line'])])

        style.configure('Accent.TButton', background=T['cut'], foreground='#1a0d0a',
                         font=('Segoe UI', 10, 'bold'), bordercolor=T['cut'])
        style.map('Accent.TButton',
                  background=[('disabled', T['cut_dim']), ('active', '#ff8478')],
                  foreground=[('disabled', '#9a7770')])

        style.configure('XCS.Horizontal.TProgressbar', troughcolor=T['bg_deep'],
                         background=T['cut'], bordercolor=T['panel_line'], lightcolor=T['cut'],
                         darkcolor=T['cut'])

    # ---------------- manejo de archivos ----------------
    def _pick_files(self):
        files = filedialog.askopenfilenames(
            title="Selecciona archivos .xcs / .xs",
            filetypes=[("Todos los soportados", "*.xcs *.xs *.svg *.pdf *.ai *.dxf"),
                       ("Proyectos xTool", "*.xcs *.xs"),
                       ("SVG / PDF / AI / DXF", "*.svg *.pdf *.ai *.dxf"),
                       ("Todos los archivos", "*.*")]
        )
        for f in files:
            if f not in self.input_paths:
                self._add_queue_row(f)

    def _pick_folder(self):
        folder = filedialog.askdirectory(title="Selecciona una carpeta")
        if folder and folder not in self.input_paths:
            self._add_queue_row(folder)

    def _add_queue_row(self, path):
        """Anade una fila a la cola. Solo se muestra el nombre (con extension, que ya
        indica el tipo) o el nombre de carpeta -- no la ruta completa, para que la lista
        sea legible incluso con rutas largas. La ruta completa se guarda en input_paths
        para el procesamiento real; queue_labels guarda la etiqueta base (sin marca de
        estado) para poder reescribirla despues con un check de 'convertido' o 'error'."""
        if os.path.isdir(path):
            label = os.path.basename(os.path.normpath(path)) + "  (carpeta)"
        else:
            label = os.path.basename(path)
        self.input_paths.append(path)
        self.queue_labels.append(label)
        self.listbox.insert('end', label)

    def _on_queue_select(self, event=None):
        selected = self.listbox.curselection()
        if not selected:
            self.path_hint.config(text="")
            return
        idx = selected[-1]
        if 0 <= idx < len(self.input_paths):
            self.path_hint.config(text=self.input_paths[idx])

    def _remove_selected(self):
        selected = list(self.listbox.curselection())
        for idx in reversed(selected):
            self.listbox.delete(idx)
            del self.input_paths[idx]
            del self.queue_labels[idx]

    def _clear_list(self):
        self.listbox.delete(0, 'end')
        self.input_paths = []
        self.queue_labels = []

    def _on_drop(self, event):
        # tkinterdnd2 entrega las rutas con posibles llaves si contienen espacios
        raw = self.root.tk.splitlist(event.data)
        for p in raw:
            if p not in self.input_paths:
                self._add_queue_row(p)

    def _mark_queue_status(self, owner_path, ok):
        """Marca visualmente (icono + color) la fila de la cola correspondiente a
        'owner_path' como convertida (verde, check) o con error (rojo, X)."""
        try:
            idx = self.input_paths.index(owner_path)
        except ValueError:
            return
        base_label = self.queue_labels[idx]
        icon = "\u2713" if ok else "\u2717"
        color = THEME['ok'] if ok else THEME['cut']
        self.listbox.delete(idx)
        self.listbox.insert(idx, f"{icon} {base_label}")
        self.listbox.itemconfig(idx, fg=color)

    def _pick_output_dir(self):
        folder = filedialog.askdirectory(title="Selecciona la carpeta de destino")
        if folder:
            self.dest_var.set(folder)

    def _open_output(self):
        out = self.dest_var.get()
        os.makedirs(out, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(out)
        elif sys.platform == 'darwin':
            os.system(f'open "{out}"')
        else:
            os.system(f'xdg-open "{out}"')

    # ---------------- logging ----------------
    def _log(self, msg, tag=None):
        self.console.insert('end', msg + "\n", tag or ())
        self.console.see('end')

    # ---------------- conversion ----------------
    def _start_conversion(self):
        if not self.input_paths:
            messagebox.showwarning(APP_TITLE, "Anade al menos un archivo o carpeta antes de convertir.")
            return
        output_dir = self.dest_var.get().strip()
        if not output_dir:
            messagebox.showwarning(APP_TITLE, "Indica una carpeta de destino.")
            return
        os.makedirs(output_dir, exist_ok=True)

        self.btn_convert.config(state='disabled')
        self.btn_cancel.config(state='normal')
        self.progress['value'] = 0
        self.stop_event.clear()

        # Restaurar las etiquetas de la cola a su estado base (sin check/cruz de una
        # conversion anterior), para que las marcas de esta nueva pasada sean claras.
        for idx, label in enumerate(self.queue_labels):
            self.listbox.delete(idx)
            self.listbox.insert(idx, label)
            self.listbox.itemconfig(idx, fg=THEME['ink'])

        options = {'dxf': self.opt_dxf.get(), 'pdf': self.opt_pdf.get(), 'preview': self.opt_preview.get()}
        self._log("=" * 50, 'muted')
        self._log("Iniciando conversion...", 'info')

        self.worker_thread = threading.Thread(
            target=run_conversion,
            args=(list(self.input_paths), output_dir, options, self.msg_queue, self.stop_event),
            daemon=True
        )
        self.worker_thread.start()

    def _cancel(self):
        self.stop_event.set()
        self._log("Cancelando tras el archivo actual...", 'warn')

    def _poll_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind == 'found':
                    self._log(f"Encontrados {item[1]} archivo(s) .xcs/.xs para procesar.", 'info')
                    self.progress['maximum'] = max(item[1], 1)
                elif kind == 'log':
                    self._log(item[1], item[2])
                elif kind == 'progress':
                    self.progress['value'] = item[1]
                elif kind == 'file_owner_result':
                    self._mark_queue_status(item[1], item[2])
                elif kind == 'done':
                    stats = item[1]
                    self._log("-" * 50, 'muted')
                    self._log(f"Conversion finalizada: {stats.get('success',0)} OK, "
                              f"{stats.get('failed',0)} con error, de {stats.get('total',0)} encontrados.", 'ok')
                    self.btn_convert.config(state='normal')
                    self.btn_cancel.config(state='disabled')
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)


def main():
    global HAS_DND
    root = None
    if HAS_DND:
        try:
            root = TkinterDnD.Tk()
        except Exception as e:
            # Si la libreria nativa tkdnd no se pudo cargar (p.ej. un empaquetado
            # incompleto con PyInstaller), no queremos que la aplicacion entera
            # se caiga: degradamos a una ventana normal sin arrastrar-y-soltar,
            # los botones "Anadir archivos/carpeta" siguen funcionando igual.
            HAS_DND = False
            root = None
    if root is None:
        root = tk.Tk()
    # El tema visual (paleta "blueprint") se aplica dentro de XCS2SVGApp._setup_style,
    # no aqui, para mantener toda la configuracion de estilo en un solo lugar.
    app = XCS2SVGApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
