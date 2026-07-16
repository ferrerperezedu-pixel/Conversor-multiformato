# XCS2SVG Converter Pro — Como generar el instalador (.exe)

Este entorno donde trabajamos es Linux, así que no puedo generar aquí mismo un `.exe`
de Windows fiable (PyInstaller necesita compilar en la misma plataforma de destino,
no permite "cross-compilar" con garantías desde Linux). Lo que sí te dejo aquí es
**todo el proyecto listo y ya probado** — solo falta el paso final de compilación,
para el que tienes dos caminos. Elige el que prefieras.

---

## Opción A — Sin usar tu propio PC con Windows (recomendada)

Usa GitHub Actions: un servicio gratuito que compila el proyecto en una máquina
Windows en la nube automáticamente.

1. Crea un repositorio nuevo en [github.com](https://github.com) (puede ser privado).
2. Sube **todo el contenido de esta carpeta** (`desktop_app/`) a ese repositorio,
   incluyendo la carpeta oculta `.github/`.
3. Ve a la pestaña **Actions** de tu repositorio en GitHub.
4. Debería aparecer un flujo llamado **"Compilar XCS2SVG Converter (Windows)"**.
   Si no se ejecutó solo, pulsa **"Run workflow"**.
5. Espera unos 3-5 minutos a que termine (verás una marca verde ✓).
6. Entra en esa ejecución terminada y baja hasta **"Artifacts"**. Ahí encontrarás:
   - `XCS2SVG_Converter_Setup` → el instalador completo (`XCS2SVG_Converter_Setup.exe`)
   - `XCS2SVG_Converter_exe_suelto` → el `.exe` sin instalador, por si prefieres
     usarlo directamente sin instalar nada.
7. Descarga el `.zip` del artefacto, descomprímelo, y ahí está tu instalador.

No necesitas tener Windows en ningún momento para este camino.

---

## Opción B — Compilar en tu propio PC con Windows

Si tienes acceso a un PC con Windows:

1. Instala [Python 3.10 o superior](https://www.python.org/downloads/) (marca la
   casilla "Add Python to PATH" durante la instalación).
2. Instala [Inno Setup Compiler](https://jrsoftware.org/isdl.php) (gratuito).
3. Copia toda esta carpeta (`desktop_app/`) a tu PC Windows.
4. Abre una terminal (`cmd` o PowerShell) dentro de esa carpeta y ejecuta:
   ```
   build.bat
   ```
   Esto instala las dependencias y compila `dist\XCS2SVG_Converter.exe`.
5. Abre `installer.iss` con Inno Setup Compiler y pulsa **Compile** (o `Ctrl+F9`).
6. El instalador final aparecerá en `Output\XCS2SVG_Converter_Setup.exe`.

---

## Qué hace el instalador

- Instala la aplicación en `Archivos de programa\XCS2SVG Converter`.
- Crea una entrada en el **Menú Inicio**.
- Ofrece crear un acceso directo en el Escritorio (opcional, casilla en el instalador).
- Incluye un desinstalador estándar (aparece en "Aplicaciones y características" de Windows).

## Qué hace la aplicación (`gui.py`)

- Interfaz con la misma identidad visual "blueprint" que la versión web (azul de plano
  técnico + acento rojo de corte + acento cian de grabado).
- Arrastra y suelta archivos `.xcs`/`.xs` o carpetas completas (usa `tkinterdnd2`;
  si por algún motivo no está disponible en tu compilación, los botones "Añadir
  archivos"/"Añadir carpeta" funcionan igual).
- Convierte a **SVG + DXF + PDF** (casillas independientes para activar cada uno):
  - SVG y PDF conservan las curvas Bézier reales.
  - DXF usa `LWPOLYLINE` (aplana las curvas a polígonos, ya que ese formato no soporta
    splines nativas de forma sencilla) — pensado para LightBurn/corte láser.
  - PDF también incrusta las imágenes `BITMAP` del proyecto original como imágenes reales
    (no solo el contorno vectorial), algo que el DXF omite deliberadamente.
- Vista previa a color automática y aviso de problemas típicos (LightBurn), usando
  **exactamente el mismo motor** (`converter.py`, `dxf_writer.py`, `pdf_writer.py`,
  `lightburn.py`, `svgpath_bbox.py`, `svgpath_segments.py`) ya validado contra 7
  archivos reales en esta conversación.
- Casillas para LBRN2 y reparación de archivos dañados están presentes pero
  deshabilitadas ("próximamente") — funciones aún no implementadas.

## Formatos de entrada soportados

Ademas de los proyectos nativos de xTool (`.xcs`, `.xs`), el conversor ahora tambien
puede LEER (ademas de escribir) estos formatos genericos de otros programas:

- **SVG generico** (Illustrator, Inkscape, etc.) -- detecta capas por nombre de grupo
  (`Cut`/`Score`/`Engrave`, convencion muy extendida en la comunidad laser).
- **PDF generico** y **`.ai` moderno** (Illustrator guarda `.ai` como PDF desde hace
  anos) -- interpreta el content stream de PDF (m/l/c/re/S/f/cm...) reconstruyendo
  los trazados en coordenadas absolutas.

Pendientes (dan un error claro, no fallan en silencio):
- **DXF generico** (de otros programas, no el que genera este conversor) -- los DXF
  reales suelen usar splines (curvas NURBS) que necesitan matematica adicional.
- **`.ai` antiguo / PostScript** -- formato de lenguaje de programacion completo,
  mucho mas complejo que el `.ai` moderno (que es PDF).

## Archivos de este paquete

```
desktop_app/
├── gui.py                  <- interfaz grafica (Tkinter, tema "blueprint")
├── conversion_worker.py    <- logica de conversion (sin dependencias de UI)
├── converter.py            <- motor de conversion (ya validado)
├── dxf_writer.py
├── pdf_writer.py           <- generador de PDF (curvas reales + imagenes)
├── pdf_reader.py           <- lector de PDF/.ai moderno
├── svg_reader.py           <- lector de SVG generico
├── svgpath_segments.py     <- parser de curvas SVG sin aplanar (para el PDF)
├── svgpath_bbox.py         <- parser de curvas SVG (usado por varios lectores)
├── lightburn.py
├── icon.ico / icon.png     <- icono de la aplicacion
├── requirements.txt        <- dependencias Python (tkinterdnd2, pyinstaller, Pillow, pypdf)
├── build.bat               <- script de compilacion para Windows
├── installer.iss           <- script de Inno Setup
└── .github/workflows/build.yml  <- compilacion automatica en la nube
```

## Ya validado antes de llegar aqui

La lógica de conversión (`conversion_worker.py`) se probó de forma headless
(sin interfaz gráfica) contra los 7 archivos reales de este proyecto: 7/7
convertidos correctamente, 9 páginas × 3 formatos (SVG+DXF+PDF) = 35 archivos,
575 objetos — mismos resultados exactos que en todas las pruebas anteriores de
esta conversación. Los 9 PDF se validaron estructuralmente con `pypdf` (todos
abren sin error) y visualmente renderizándolos a imagen con `pdftoppm` (curvas
correctas, imagen incrustada visible en `tortue_tribal`). Lo único que NO se ha
podido probar en este entorno es la ventana de Tkinter en sí (no hay entorno
gráfico ni tkinter instalado aquí), así que esa parte visual conviene que la
compruebes tú al abrir la app.
