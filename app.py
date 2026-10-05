import re
import time
import unicodedata

import streamlit as st
import pandas as pd
import logica as lg # Conectamos nuestro motor financiero
import datetime
import ia_engine as ia
import pdfplumber
import docx
import requests
try:
    from streamlit_lottie import st_lottie
except ImportError:  # la app sigue funcionando aunque aún no se instale la librería
    st_lottie = None
from streamlit_option_menu import option_menu  # pip install streamlit-option-menu

if 'borrador_ia' not in st.session_state:
    st.session_state.borrador_ia = None
if 'borrador_id' not in st.session_state:
    st.session_state.borrador_id = 0

# --- MOTOR DE EXTRACCIÓN Y SANITIZACIÓN PARA LA IA ---

# Límite duro de caracteres. La cuenta Groq de este proyecto está en el tier
# gratuito (8000 tokens por minuto), así que un PDF de varias páginas no cabe:
# la API responde HTTP 413 y no hay asiento. Recortamos aquí, no después.
MAX_CHARS_PARA_IA = ia.MAX_CHARS_ENTRADA
MAX_PAGINAS_PDF = 12

AJUSTES_TABLA_BORDES = {"vertical_strategy": "lines", "horizontal_strategy": "lines",
                        "snap_tolerance": 4, "join_tolerance": 4, "intersection_tolerance": 6}
AJUSTES_TABLA_TEXTO = {"vertical_strategy": "text", "horizontal_strategy": "text",
                        "min_words_vertical": 2, "min_words_horizontal": 1, "text_x_tolerance": 2}

_ESPACIOS_EXCESIVOS = re.compile(r"[ \t]{6,}")
_SALTOS_EN_BLOQUE = re.compile(r"\n{3,}")
_CARACTERES_OCULTOS = re.compile("[\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060\ufeff]")
_FICHAS = re.compile(r"[\w.,]+", re.UNICODE)

# Una tabla solo se acepta si reproduce el texto real de la página. Sin este
# control, pdfplumber "detecta" tablas en párrafos corrientes y parte las palabras
# a media palabra, dejando importes como "2,500.00" convertidos en "2,5".
FIDELIDAD_MINIMA = 0.95


def sanitizar_texto_para_ia(texto):
    """
    Limpieza en dos capas que se ejecuta ANTES de llamar a la API:
      1. Sustituye los bytes de control (NUL \x00, biseles, campanas) y los
         glifos rotos que deja la extracción de PDF, que son la causa típica de
         que el modelo responda vacío o devuelva JSON truncado.
      2. Colapsa el relleno de espacios y los saltos en bloque que inflan el
         documento sin aportar información contable.
    NO recorta el documento. Un recorte por caracteres puede partir una fila de
     tabla por la mitad y fabricar una operación incompleta, y el tramo central de
     un libro contable es justamente donde están las operaciones. Si el texto no
     cabe en una petición, ia_engine lo divide en bloques por líneas, conserva el
     orden y no omite nada.
    Devuelve (texto_limpio, informe) donde informe describe lo que se hizo.
    """
    informe = {"caracteres_originales": 0, "caracteres_enviados": 0,
               "recortado": False, "control_eliminados": 0, "bloques": 1}

    if texto is None:
        return "", informe
    if not isinstance(texto, str):
        texto = str(texto)

    informe["caracteres_originales"] = len(texto)

    # 1. Normalización de saltos de línea y Unicode (NFKC unifica tildes y comillas).
    limpio = texto.replace("\r\n", "\n").replace("\r", "\n")
    limpio = unicodedata.normalize("NFKC", limpio)
    limpio = _CARACTERES_OCULTOS.sub("", limpio)

    # 2. Caracteres de control: se conservan solo tabulador y salto de línea.
    sin_control = "".join(c for c in limpio if c in "\n\t" or unicodedata.category(c) != "Cc")
    informe["control_eliminados"] = len(limpio) - len(sin_control)
    limpio = sin_control.replace("\ufffd", "")

    # 3. Relleno de espacios y saltos en bloque, conservando la sangría de las tablas.
    lineas = [_ESPACIOS_EXCESIVOS.sub("      ", linea).rstrip() for linea in limpio.split("\n")]
    limpio = _SALTOS_EN_BLOQUE.sub("\n\n", "\n".join(lineas)).strip()

    # 4. El texto sale COMPLETO. Solo se estima en cuántos bloques de entrada lo
    #    dividirá la IA; ningún carácter se descarta aquí.
    bloques = max(1, -(-len(limpio) // MAX_CHARS_PARA_IA))
    informe["bloques"] = bloques
    informe["recortado"] = bloques > 1

    informe["caracteres_enviados"] = len(limpio)
    return limpio, informe


def fecha_ia_a_objeto(fecha_ia):
    """
    Convierte la fecha transcrita por la IA en un objeto date. Devuelve None si
    está vacía o no es interpretable.

    NUNCA devuelve la fecha de hoy: una fecha ausente se queda ausente y el asiento
    queda pendiente de revisión humana. Ponerle una fecha inventada desplaza el
    asiento de período contable sin que nadie lo note.
    """
    if not fecha_ia:
        return None
    try:
        return datetime.datetime.strptime(str(fecha_ia).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ============================================================================
# HUMAN-IN-THE-LOOP: un único mecanismo de borrador para todo el sistema
# ============================================================================
# Todo lo que produzca la IA (documento o enunciado) y todo lo que.capture el
# usuario a mano entra en `st.session_state.borrador_ia` con la misma forma:
#
#     {"fecha": "AAAA-MM-DD" | "", "glosa": "texto", "asiento": [
#         {"cuenta": "20", "debe": 1500.0, "haber": 0.0}, ...]}
#
# `asiento` es SIEMPRE una lista de partidas: un asiento puede tener varias y el
# sistema no las agrupa ni las inventa. De ahi sale un unico `render_borrador()`
# que dibuja las tarjetas, deja editar fecha/cuenta/Debe/Haber, valida la partida
# doble y exige la aprobacion humana antes de tocar la base de datos.

TOLERANCIA_CENTIMOS_HIL = 0.01


def obtener_cuentas_pcge():
    """Catalogo del PCGE como etiquetas 'codigo - descripcion'. Fuente unica para
    el formulario manual y para los selectores de cuenta del borrador."""
    conn = lg.obtener_conexion()
    try:
        cuentas_df = pd.read_sql_query(
            "SELECT codigo || ' - ' || descripcion AS nombre_cuenta FROM Cuentas ORDER BY codigo", conn
        )
    finally:
        conn.close()
    return cuentas_df["nombre_cuenta"].tolist()


def codigos_del_catalogo(catalogo):
    return {etiqueta.split(" - ")[0].strip() for etiqueta in catalogo}


def _importe(valor):
    """
    Convierte lo que el humano escribio en la celda a float. Una celda vacia o no
    numerica es 0.0, nunca NaN: un NaN contaminaria la suma de la partida doble,
    haria que el descuadre no se detectara y llegaria a la base de datos.
    """
    numero = pd.to_numeric(valor, errors="coerce")
    return 0.0 if pd.isna(numero) else float(numero)


def etiqueta_de_cuenta(codigo, catalogo):
    """
    Traduce el codigo que entrega la IA ('20') a la etiqueta del catalogo
    ('20 - Mercaderias') que exige el selector. Es solo presentacion: si el codigo
    no existe en el catalogo se conserva igual, marcado, para que el humano lo
    vea y lo corrija. No se corrige ni se deduce ninguna cuenta.
    """
    codigo = str(codigo if codigo is not None else "").strip()
    for etiqueta in catalogo:
        if etiqueta.split(" - ")[0].strip() == codigo:
            return etiqueta
    return f"{codigo} - fuera del catálogo PCGE"


def cargar_borrador(asientos, origen):
    """
    Coloca asientos en el borrador y renueva su identidad. El identificador se
    usa en las claves de los widgets para que un borrador nuevo no herede los
    valores que el humano ya habia escrito en el anterior.
    """
    st.session_state.borrador_ia = list(asientos)
    st.session_state.borrador_id = st.session_state.get("borrador_id", 0) + 1
    st.session_state.origen_borrador = origen
    st.session_state.avisos_borrador = []
    st.session_state.resumen_borrador = ""


def render_borrador(origen):
    """
    Zona de revision humana unica: edicion, validacion, aprobacion y guardado.

    Reglas que este bloque garantiza:
      - Nada llega a la base de datos sin pasar por aqui.
      - Fecha, cuenta, Debe y Haber son editables a mano.
      - Una fecha que la IA no encontró se muestra como 'FECHA NO ENCONTRADA EN EL
        DOCUMENTO' y queda en blanco: el sistema no la rellena, espera al humano.
      - Un asiento descuadrado bloquea el guardado hasta que se corrija o se
        desmarque (desmarcar equivale a descartarlo).
      - Solo se guarda lo que el humano aprueba.
    """
    borrador = st.session_state.get("borrador_ia")
    if not borrador:
        return

    st.markdown("---")
    st.markdown("### Borrador para revisión humana")
    st.caption(f"Origen: {origen}. Ningún asiento se guarda sin tu aprobación explícita.")

    cat_edicion = obtener_cuentas_pcge()
    codigos_validos = codigos_del_catalogo(cat_edicion)
    borrador_id = st.session_state.get("borrador_id", 0)

    # Opciones del selector: el catalogo mas las cuentas que trajo la IA y que no
    # existen en el, para que nunca se pierdan en silencio.
    for operacion in borrador:
        for movimiento in operacion.get("asiento", []):
            etiqueta = etiqueta_de_cuenta(movimiento.get("cuenta"), cat_edicion)
            if etiqueta not in cat_edicion:
                cat_edicion.append(etiqueta)

    a_guardar, bloqueos = [], []

    for i, operacion in enumerate(borrador):
        fecha_ia = fecha_ia_a_objeto(operacion.get("fecha"))
        clave = f"{borrador_id}_{i}"

        with st.container(border=True):
            col_titulo, col_check = st.columns([0.85, 0.15])

            with col_titulo:
                st.markdown(f"**Asiento #{i + 1} | {operacion.get('glosa') or 'sin glosa'}**")

            with col_check:
                aprobar = st.checkbox("Aprobar", value=True, key=f"chk_aprobar_{clave}")

            # --- FECHA: siempre editable. Si la IA no la encontró, se avisa y se
            # deja vacía; el usuario la ingresa. Nunca se rellena sola.
            if fecha_ia is None:
                st.warning("FECHA NO ENCONTRADA EN EL DOCUMENTO. Ingresa la fecha real "
                           "para poder guardar este asiento.")
            fecha_elegida = st.date_input(
                "Fecha del asiento",
                value=fecha_ia,
                min_value=datetime.date(2000, 1, 1),
                key=f"fecha_{clave}",
            )

            # --- PARTIDAS: el mismo editor del ingreso manual (cuenta del PCGE,
            # Debe y Haber), reutilizado para que ambos flujos sean el mismo.
            df_movs = pd.DataFrame(operacion.get("asiento") or [])
            if df_movs.empty:
                df_movs = pd.DataFrame([{"cuenta": "", "debe": 0.0, "haber": 0.0}])
            for col in ("cuenta", "debe", "haber"):
                if col not in df_movs.columns:
                    df_movs[col] = "" if col == "cuenta" else 0.0
            df_movs["cuenta"] = [etiqueta_de_cuenta(c, cat_edicion) for c in df_movs["cuenta"]]

            df_editado = st.data_editor(
                df_movs,
                column_config={
                    "cuenta": st.column_config.SelectboxColumn(
                        "Cuenta Contable (PCGE)", options=cat_edicion, required=True, width="large"),
                    "debe": st.column_config.NumberColumn("Debe (S/)", min_value=0.0, format="%.2f"),
                    "haber": st.column_config.NumberColumn("Haber (S/)", min_value=0.0, format="%.2f"),
                },
                key=f"tbl_edicion_{clave}",
                use_container_width=True,
                num_rows="dynamic",
                hide_index=True,
            )

            # --- VALIDACION: solo se reporta. El sistema no corrige importes.
            movimientos = []
            for _, fila in df_editado.iterrows():
                cuenta = str(fila["cuenta"]).split(" - ")[0].strip()
                debe = _importe(fila["debe"])
                haber = _importe(fila["haber"])
                if cuenta:
                    movimientos.append({"cuenta": cuenta, "debe": debe, "haber": haber})

            total_debe = round(sum(m["debe"] for m in movimientos), 2)
            total_haber = round(sum(m["haber"] for m in movimientos), 2)
            descuadra = abs(total_debe - total_haber) > TOLERANCIA_CENTIMOS_HIL
            fuera_de_catalogo = [m["cuenta"] for m in movimientos if m["cuenta"] not in codigos_validos]

            problemas = []
            if len(movimientos) < 2:
                problemas.append("debe tener al menos dos partidas con cuenta")
            if fecha_elegida is None:
                problemas.append("no tiene fecha")
            if descuadra:
                problemas.append(f"está descuadrado (Debe S/ {total_debe:,.2f} ≠ Haber S/ {total_haber:,.2f})")
            if fuera_de_catalogo:
                problemas.append(f"usa cuentas fuera del catálogo PCGE: {', '.join(sorted(set(fuera_de_catalogo)))}")

            # Desmarcar equivale a descartar: un asiento que el humano no aprueba
            # no se guarda y, por lo tanto, tampoco bloquea al resto.
            if not aprobar:
                detalle = f" ({'; '.join(problemas)})" if problemas else ""
                st.info(f"Sin aprobar: este asiento se descartará{detalle}.")
            elif problemas:
                st.error("Asiento #%d no se puede guardar: %s." % (i + 1, "; ".join(problemas)))
                bloqueos.append(f"Asiento #{i + 1}")
            else:
                st.success(f"Partida doble cuadrada (S/ {total_debe:,.2f}) — listo para guardar")
                a_guardar.append({"fecha": fecha_elegida, "glosa": operacion.get("glosa") or "Asiento sin glosa",
                                  "movimientos": movimientos})

    st.markdown("---")
    if bloqueos:
        st.error("No se puede guardar: corrige " + ", ".join(bloqueos)
                 + " o desmarca 'Aprobar' para descartarlos.")
    st.caption(f"{len(a_guardar)} asiento(s) aprobados y validados, listos para persistir.")

    if st.button("Guardar Asientos Aprobados en BD", type="primary",
                 disabled=bool(bloqueos) or not a_guardar):
        exitos, errores = 0, []
        for asiento in a_guardar:
            try:
                exito_bd, msj = lg.registrar_asiento_completo(
                    asiento["fecha"], asiento["glosa"], asiento["movimientos"])
                if exito_bd:
                    exitos += 1
                else:
                    errores.append(msj)
            except ValueError as ve:
                errores.append(f"Asiento del {asiento['fecha']}: {ve}")
            except Exception as e:
                errores.append(f"Asiento del {asiento['fecha']}: {e}")

        st.session_state.borrador_ia = None
        if exitos:
            st.success(f"Se registraron {exitos} asientos contables en la Base de Datos.")
            st.balloons()
        for error in errores:
            st.error(f"No se pudo registrar: {error}")
        time.sleep(1.5)
        st.rerun()


def _serializar_tabla(filas):
    """Convierte una tabla en filas de celdas ya sin celdas vacías."""
    limpias = []
    for fila in filas:
        celdas = [str(celda).strip() for celda in fila if celda is not None and str(celda).strip()]
        if celdas:
            limpias.append(celdas)
    return limpias


def _tabla_es_fiable(filas, texto_pagina):
    """
    Descarta las tablas ficticias: exige ≥3 filas, ≥2 columnas y que ninguna ficha
    de la tabla falte en el texto real de la página (control anti-truncamiento).
    """
    if len(filas) < 3:
        return False
    if sum(1 for fila in filas if len(fila) >= 2) < len(filas) * 0.6:
        return False

    fichas_pagina = set(_FICHAS.findall((texto_pagina or "").lower()))
    if not fichas_pagina:
        return False
    fichas_tabla = set(_FICHAS.findall("\n".join(" ".join(fila) for fila in filas).lower()))
    if not fichas_tabla:
        return False
    return len(fichas_tabla & fichas_pagina) / len(fichas_tabla) >= FIDELIDAD_MINIMA


def _tablas_de_pagina(pagina):
    """Detecta tablas con líneas y, si falla, por posición de palabras."""
    for ajustes in (AJUSTES_TABLA_BORDES, AJUSTES_TABLA_TEXTO):
        try:
            tablas = [t for t in pagina.find_tables(table_settings=ajustes) if t.extract()]
        except Exception:
            continue
        if tablas:
            return tablas
    return []


def _fuera_de_las_tablas(objeto, cajas):
    """Permite quedarse solo con el texto que NO pertenece a ninguna tabla."""
    x0, top = objeto["x0"], objeto["top"]
    x1, bottom = objeto["x1"], objeto["bottom"]
    for cx0, ctop, cx1, cbottom in cajas:
        if x0 >= cx0 - 1 and x1 <= cx1 + 1 and top >= ctop - 1 and bottom <= cbottom + 1:
            return False
    return True


def extraer_texto_de_pdf(archivo):
    """
    Estructura el PDF por capas en vez de volcar texto plano: las tablas se
    serializan con separador '|' (conservan la alineación DEBE / HABER) y el
    resto de la página se lee aparte, para que la IA no reciba dos veces la misma
    fila mezclada en un solo bloque. Devuelve (texto, aviso).
    """
    bloques, paginas_sin_texto = [], []

    with pdfplumber.open(archivo) as pdf:
        total_paginas = len(pdf.pages)
        for indice, pagina in enumerate(pdf.pages[:MAX_PAGINAS_PDF], start=1):
            contenido_pagina = []
            texto_completo = pagina.extract_text(layout=True) or ""

            # Capa 1: tablas, solo si son fieles al texto real de la página.
            filas_tabla, cajas = [], []
            for tabla in _tablas_de_pagina(pagina):
                filas = _serializar_tabla(tabla.extract())
                if filas:
                    filas_tabla.extend(filas)
                    cajas.append(tabla.bbox)
            if filas_tabla and _tabla_es_fiable(filas_tabla, texto_completo):
                contenido_pagina.append("TABLA:")
                contenido_pagina.extend(" | ".join(fila) for fila in filas_tabla)
            else:
                filas_tabla, cajas = [], []

            # Capa 2: texto suelto, sin las zonas ya capturadas como tabla.
            try:
                texto_libre = (texto_completo if not cajas
                               else pagina.filter(lambda o: _fuera_de_las_tablas(o, cajas)).extract_text())
            except Exception:
                texto_libre = pagina.extract_text()
            if texto_libre:
                contenido_pagina.append(texto_libre.strip())

            texto_pagina = "\n".join(bloque for bloque in contenido_pagina if bloque).strip()
            if texto_pagina:
                bloques.append(f"=== PAGINA {indice} ===\n{texto_pagina}")
            else:
                paginas_sin_texto.append(indice)

    aviso = None
    if not bloques:
        aviso = ("El PDF no contiene texto extraíble: es un escaneo o una imagen. "
                 "Necesita pasar por OCR antes de enviarse a la IA.")
    elif paginas_sin_texto:
        aviso = (f"{len(paginas_sin_texto)} página(s) sin texto legible "
                 f"(pág. {', '.join(map(str, paginas_sin_texto[:8]))}). "
                 "Es probable que estén escaneadas como imagen.")
    if total_paginas > MAX_PAGINAS_PDF:
        aviso = ((aviso + " ") if aviso else "") + \
                f"Se leyeron las primeras {MAX_PAGINAS_PDF} de {total_paginas} páginas por límite de la API."
    return "\n\n".join(bloques), aviso


def extraer_texto_de_docx(archivo):
    """Lee párrafos y tablas del Word, en el mismo formato estructurado del PDF."""
    documento = docx.Document(archivo)
    bloques = [parrafo.text for parrafo in documento.paragraphs if parrafo.text.strip()]

    for indice, tabla in enumerate(documento.tables, start=1):
        filas = _serializar_tabla([[celda.text for celda in fila.cells] for fila in tabla.rows])
        if filas:
            bloques.append(f"TABLA {indice}:")
            bloques.extend(" | ".join(fila) for fila in filas)

    texto = "\n".join(bloques)
    return texto, None if texto.strip() else "El documento Word está vacío."


# Orden estricto del ciclo contable: captura -> estados financieros -> análisis gerencial.
# Estos nombres son los que usan los `if menu == ...` del enrutamiento: no cambiarlos por separado.
MENU_OPCIONES = ["Inicio", "Registro de Transacciones", "Estados Financieros", "Dashboard Gerencial"]
MENU_ICONOS = ["house", "pen", "file-earmark-spreadsheet", "graph-up"]  # Bootstrap Icons

if "menu_option" not in st.session_state:
    st.session_state["menu_option"] = 0  # índice de la opción activa del menú


def _sincronizar_menu(key: str):
    """on_change del menú: guarda el índice cuando el usuario hace clic en una opción."""
    st.session_state["menu_option"] = MENU_OPCIONES.index(st.session_state[key])


def _ir_a(destino: str):
    """Callback de los botones CTA: navegación programática del menú lateral."""
    st.session_state["menu_option"] = MENU_OPCIONES.index(destino)
    st.session_state["menu_destino"] = destino


def _ir_a_con_pista(destino: str):
    """Navega desde la portada y marca que debe mostrarse la pista del menú lateral (una sola vez)."""
    _ir_a(destino)
    st.session_state["mostrar_pista_menu"] = True


# URL pública del Lottie de la portada (analítica / finanzas). Reemplázala por la que elijas en lottiefiles.com
LOTTIE_URL = "https://assets2.lottiefiles.com/packages/lf20_qp1q7mct.json"
# Opcional y recomendado para la exposición: guarda el .json aquí y no dependerás de internet
LOTTIE_LOCAL = "assets/analitica.json"


@st.cache_data(show_spinner=False, ttl=3600)
def _cargar_lottie(url: str, ruta_local: str):
    """Carga el Lottie desde archivo local (si existe) o desde la URL. Devuelve None si falla."""
    import json
    import os
    try:
        if os.path.exists(ruta_local):
            with open(ruta_local, "r", encoding="utf-8") as f:
                return json.load(f)
        r = requests.get(url, timeout=5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# 1. CONFIGURACIÓN DE LA PÁGINA (Debe ser la primera línea de código)

st.set_page_config(
    page_title="Ledgerix | Sistema Contable",
    page_icon=":material/account_balance:",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 2. BARRA LATERAL (NAVEGACIÓN CORPORATIVA)
st.sidebar.title("Ledgerix")
st.sidebar.markdown("---")
with st.sidebar:
    seleccion = option_menu(
        menu_title=None,
        options=MENU_OPCIONES,
        icons=MENU_ICONOS,
        default_index=0,
        manual_select=st.session_state["menu_option"],  # permite que los botones CTA cambien la opción
        key="menu_nav",
        on_change=_sincronizar_menu,
        styles={
            "container": {"padding": "4px 0", "background-color": "transparent"},
            "icon": {"color": "#22D3EE", "font-size": "1.05rem"},
            "nav-link": {
                "font-size": "0.97rem", "text-align": "left", "margin": "3px 0",
                "padding": "10px 14px", "border-radius": "10px", "color": "#CBD5E1",
                "--hover-color": "rgba(34, 211, 238, 0.12)",
            },
            "nav-link-selected": {
                "background-color": "rgba(16, 185, 129, 0.16)", "color": "#FFFFFF",
                "font-weight": "600", "border-left": "3px solid #10B981",
            },
        },
    )
st.sidebar.markdown("---")
st.sidebar.caption("Ledgerix SaaS - Versión 1.0")

# Si la navegación fue programática (botón CTA), esa orden manda en este primer render;
# el componente alcanza el mismo estado en el rerun siguiente.
menu = st.session_state.pop("menu_destino", None) or seleccion

# 3. ENRUTAMIENTO DE MÓDULOS

# Pista de navegación: solo aparece al llegar desde un botón de la portada
if st.session_state.pop("mostrar_pista_menu", False):
    st.toast("Expande el menú lateral izquierdo para navegar por las demás secciones del sistema.")

if menu == "Inicio":
    # ---------- Iconos SVG de línea (sin emojis) ----------
    def _svg(interior: str, size: int = 28) -> str:
        return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
                f'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
                f'stroke-linejoin="round" aria-hidden="true">{interior}</svg>')

    ICO_SUBIR = _svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>')
    ICO_ARCHIVO = _svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>')
    ICO_APROBAR = _svg('<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>')
    ICO_CAPAS = _svg('<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>', 24)
    ICO_LIBRO = _svg('<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>', 24)
    ICO_AUDITOR = _svg('<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><polyline points="17 11 19 13 23 9"/>', 24)
    CONECTOR = ('<svg viewBox="0 0 56 12" width="56" height="12" fill="none" stroke="currentColor" '
                'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
                '<line class="lx-dash" x1="2" y1="6" x2="46" y2="6" stroke-dasharray="4 4"/>'
                '<polyline points="44,1.5 53,6 44,10.5"/></svg>')

    # ---------- Estilos (solo se inyectan en "Inicio") ----------
    st.markdown("""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');
        [data-testid="stMainBlockContainer"], .block-container {
            padding: 0 3rem 4rem 3rem !important;
            max-width: 100% !important;
        }
        header[data-testid="stHeader"] { background: transparent !important; }
        /* ===== HERO: fondo sólido + glow radial sutil ===== */
        .st-key-lx_hero {
            width: calc(100% + 6rem) !important;
            margin-left: -3rem;
            min-height: 94vh;
            padding: 6vh 3rem;
            gap: 1rem !important;
            justify-content: center;
            container-type: inline-size;
            container-name: lxhero;
            background:
                radial-gradient(ellipse 50% 38% at 50% 24%,
                    rgba(34,211,238,0.17) 0%, rgba(16,185,129,0.07) 42%, transparent 72%),
                #0b0f19;
        }
        .lx-brand {
            font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
            font-size: clamp(4rem, 11vw, 8.5rem);
            font-weight: 800;
            letter-spacing: -0.045em;
            line-height: 1;
            text-align: center;
            margin: 0;
            background: linear-gradient(180deg, #FFFFFF 40%, #A5F3FC 100%);
            -webkit-background-clip: text; background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: lx-rise .9s ease both;
        }
        .lx-tag {
            text-align: center; color: #94A3B8;
            font-size: clamp(1rem, 1.7vw, 1.3rem);
            line-height: 1.5;
            margin: 20px auto 0 auto; max-width: 760px;
            animation: lx-rise .9s ease .25s both;
        }
        .lx-how {
            text-align: center; margin: 52px 0 18px 0;
            font-size: .78rem; letter-spacing: .22em; text-transform: uppercase;
            color: #10B981; font-weight: 700;
            animation: lx-rise .8s ease .45s both;
        }
        /* ===== Túnel de pasos ===== */
        .lx-steps {
            display: flex; align-items: stretch; justify-content: center; gap: 18px;
            max-width: 980px; margin: 0 auto; padding: 8px 0 2.6rem 0;
        }
        .lx-step {
            flex: 1 1 0; min-width: 0; max-width: 270px;
            text-align: center; padding: 26px 20px; border-radius: 14px;
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(148,163,184,0.18);
            animation: lx-rise .8s ease both;
        }
        .lx-step.final { border-color: rgba(16,185,129,0.6); box-shadow: 0 0 24px rgba(16,185,129,0.12); }
        .lx-step-ico { color: #22D3EE; display: flex; justify-content: center; margin-bottom: 12px; }
        .lx-step-n { color: #10B981; font-size: .7rem; letter-spacing: .18em; font-weight: 700; text-transform: uppercase; }
        .lx-step-t { color: #F1F5F9; font-weight: 700; font-size: 1.02rem; margin-top: 6px; text-wrap: balance; }
        .lx-step-h { color: #94A3B8; font-size: .85rem; margin-top: 4px; line-height: 1.4; }
        .lx-conn { flex: 0 0 auto; align-self: center; padding: 0 4px; color: #22D3EE; opacity: .85; display: flex; animation: lx-rise .8s ease both; }
        .lx-conn svg { width: 40px; height: auto; }
        .lx-dash { animation: lx-flow 1.2s linear infinite; }
        /* ===== CTA principal (Streamlit) ===== */
        .st-key-cta_principal { animation: lx-rise .8s ease 1.5s both; }
        .st-key-cta_principal button {
            background: linear-gradient(90deg, #10B981, #22D3EE) !important;
            border: none !important; border-radius: 12px !important;
            min-height: 3.5rem; padding: .9rem 1.5rem;
            box-shadow: 0 10px 30px rgba(16,185,129,0.30);
            transition: transform .18s ease, box-shadow .18s ease, filter .18s ease;
            animation: lx-pulse 2.6s ease-out 2.3s infinite;
        }
        .st-key-cta_principal button p, .st-key-cta_principal button div {
            color: #0b0f19 !important; font-weight: 700 !important; font-size: 1.15rem !important;
        }
        .st-key-cta_principal button:hover {
            transform: translateY(-4px) scale(1.02);
            box-shadow: 0 16px 38px rgba(34,211,238,0.50);
            filter: brightness(1.08);
            animation: none;
        }
        .st-key-cta_principal button:active { transform: translateY(-1px); }
        /* ===== CTA secundario (discreto) ===== */
        .st-key-cta_secundario { animation: lx-rise .8s ease 1.7s both; }
        .st-key-cta_secundario button {
            background: transparent !important;
            border: 1px solid rgba(148,163,184,0.35) !important;
            border-radius: 12px !important;
            transition: border-color .18s ease, background .18s ease;
        }
        .st-key-cta_secundario button p, .st-key-cta_secundario button div { color: #CBD5E1 !important; }
        .st-key-cta_secundario button:hover {
            border-color: #22D3EE !important; background: rgba(34,211,238,0.08) !important;
        }
        /* ===== Contenido bajo el hero (compatible con tema claro/oscuro) ===== */
        .lx-section {
            font-size: .8rem; letter-spacing: .2em; text-transform: uppercase;
            color: #10B981; font-weight: 700; margin: 44px 0 14px 0;
        }
        .st-key-lx_lottie {
            border: 1px solid rgba(148,163,184,0.25); border-radius: 14px;
            padding: 12px; background: rgba(148,163,184,0.06);
        }
        .lx-pillar {
            display: flex; align-items: center; gap: 16px; padding: 16px 18px; margin-bottom: 12px;
            border: 1px solid rgba(148,163,184,0.25); border-radius: 12px;
            background: rgba(148,163,184,0.06);
        }
        .lx-pillar-ico {
            flex: 0 0 auto; width: 48px; height: 48px; display: grid; place-items: center;
            border-radius: 10px; color: #10B981; background: rgba(16,185,129,0.12);
        }
        .lx-pillar-t { font-weight: 700; font-size: 1.05rem; }
        .lx-pillar-d { opacity: .75; font-size: .95rem; }
        .lx-footer {
            max-width: 780px; margin: 64px auto 0 auto; padding: 0 12px 12px 12px;
            text-align: center; opacity: .6;
            font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
            font-size: .8rem; font-weight: 400; letter-spacing: .03em; line-height: 1.8;
            text-wrap: balance;
        }
        .lx-footer::before {
            content: ""; display: block; width: 56px; height: 2px; margin: 0 auto 20px auto;
            border-radius: 2px; background: linear-gradient(90deg, #10B981, #22D3EE);
        }
        /* Gráfico SVG animado (respaldo si no carga el Lottie) */
        .lx-chart { display: flex; align-items: center; justify-content: center; min-height: 240px; color: #22D3EE; }
        .lx-bar {
            fill: #10B981; transform-box: fill-box; transform-origin: bottom;
            animation: lx-grow 1.8s ease-in-out infinite alternate;
        }
        @keyframes lx-rise  { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
        @keyframes lx-flow  { to { stroke-dashoffset: -16; } }
        @keyframes lx-grow  { from { transform: scaleY(.35); } to { transform: scaleY(1); } }
        @keyframes lx-pulse {
            0%   { box-shadow: 0 10px 30px rgba(16,185,129,0.30), 0 0 0 0 rgba(34,211,238,0.45); }
            70%  { box-shadow: 0 10px 30px rgba(16,185,129,0.30), 0 0 0 16px rgba(34,211,238,0); }
            100% { box-shadow: 0 10px 30px rgba(16,185,129,0.30), 0 0 0 0 rgba(34,211,238,0); }
        }
        /* Las reglas responsivas del hero se miden contra su propio ancho (no el de la ventana),
           porque el menú lateral le quita espacio al contenido. */
        @container lxhero (max-width: 820px) {
            .lx-steps { gap: 10px; }
            .lx-step { padding: 22px 12px; }
            .lx-step-t { font-size: .92rem; }
            .lx-step-h { font-size: .78rem; }
            .lx-conn svg { width: 28px; }
        }
        @container lxhero (max-width: 560px) {
            .lx-steps { flex-direction: column; align-items: center; gap: 6px; }
            .lx-step { flex: none; width: 100%; max-width: 340px; }
            .lx-conn { transform: rotate(90deg); margin: 2px 0; }
            .lx-conn svg { width: 36px; }
        }
        @media (max-width: 768px) {
            [data-testid="stMainBlockContainer"], .block-container { padding: 0 1rem 3rem 1rem !important; }
            .st-key-lx_hero { width: calc(100% + 2rem) !important; margin-left: -1rem; padding: 5vh 1rem; }
        }
        @media (prefers-reduced-motion: reduce) {
            .lx-brand, .lx-tag, .lx-how, .lx-step, .lx-conn, .lx-dash, .lx-bar,
            .st-key-cta_principal, .st-key-cta_principal button, .st-key-cta_secundario { animation: none !important; }
        }
    </style>
    """, unsafe_allow_html=True)

    # ---------- HERO: logo -> pasos -> botón ----------
    with st.container(key="lx_hero"):
        st.markdown(f"""
        <div class="lx-brand">Ledgerix</div>
        <div class="lx-tag">El motor de inteligencia artificial para la gestión financiera y contable.</div>
        <div class="lx-how">Cómo funciona</div>
        <div class="lx-steps">
            <div class="lx-step" style="animation-delay:.6s">
                <div class="lx-step-ico">{ICO_SUBIR}</div>
                <div class="lx-step-n">Paso 1</div>
                <div class="lx-step-t">Captura de Datos (IA o Manual)</div>
                <div class="lx-step-h">Texto, PDF, imagen o voz</div>
            </div>
            <div class="lx-conn" style="animation-delay:.75s">{CONECTOR}</div>
            <div class="lx-step" style="animation-delay:.9s">
                <div class="lx-step-ico">{ICO_APROBAR}</div>
                <div class="lx-step-n">Paso 2</div>
                <div class="lx-step-t">Validación de Partida Doble</div>
                <div class="lx-step-h">Debe = Haber y PCGE</div>
            </div>
            <div class="lx-conn" style="animation-delay:1.05s">{CONECTOR}</div>
            <div class="lx-step final" style="animation-delay:1.2s">
                <div class="lx-step-ico">{ICO_ARCHIVO}</div>
                <div class="lx-step-n">Paso 3</div>
                <div class="lx-step-t">Generación de EEFF</div>
                <div class="lx-step-h">Balance y Estado de Resultados</div>
            </div>
        </div>
        """, unsafe_allow_html=True)

        _, col_cta, _ = st.columns([1, 1.5, 1])
        with col_cta:
            st.button("Iniciar Ciclo Contable  →", key="cta_principal", type="primary",
                      use_container_width=True, on_click=_ir_a_con_pista, args=("Registro de Transacciones",))
            st.button("Ver Dashboard Gerencial", key="cta_secundario",
                      use_container_width=True, on_click=_ir_a_con_pista, args=("Dashboard Gerencial",))

    # ---------- Propuesta de valor: Lottie + 3 frases de una línea ----------
    st.markdown('<div class="lx-section">Por qué Ledgerix</div>', unsafe_allow_html=True)

    GRAFICO_ANIMADO = """
    <div class="lx-chart">
        <svg viewBox="0 0 160 110" width="100%" height="220" aria-hidden="true">
            <line x1="10" y1="100" x2="150" y2="100" stroke="currentColor" stroke-opacity=".35" stroke-width="1.2"/>
            <rect class="lx-bar" x="20" y="60" width="16" height="40" rx="2" style="animation-delay:0s"/>
            <rect class="lx-bar" x="48" y="45" width="16" height="55" rx="2" style="animation-delay:.2s"/>
            <rect class="lx-bar" x="76" y="52" width="16" height="48" rx="2" style="animation-delay:.4s"/>
            <rect class="lx-bar" x="104" y="28" width="16" height="72" rx="2" style="animation-delay:.6s"/>
            <rect class="lx-bar" x="132" y="14" width="16" height="86" rx="2" style="animation-delay:.8s"/>
        </svg>
    </div>
    """

    col_anim, col_pilares = st.columns([1, 1.7], gap="large", vertical_alignment="center")
    with col_anim:
        with st.container(key="lx_lottie"):
            animacion = _cargar_lottie(LOTTIE_URL, LOTTIE_LOCAL) if st_lottie else None
            if animacion:
                st_lottie(animacion, height=260, loop=True, quality="medium", key="lottie_analitica")
            else:
                st.markdown(GRAFICO_ANIMADO, unsafe_allow_html=True)
    with col_pilares:
        st.markdown(f"""
        <div class="lx-pillar">
            <div class="lx-pillar-ico">{ICO_CAPAS}</div>
            <div><div class="lx-pillar-t">Extracción multimodal</div>
            <div class="lx-pillar-d">Texto, documentos, imagen y voz en un solo flujo.</div></div>
        </div>
        <div class="lx-pillar">
            <div class="lx-pillar-ico">{ICO_LIBRO}</div>
            <div><div class="lx-pillar-t">Rigor financiero</div>
            <div class="lx-pillar-d">Partida Doble y PCGE validados en cada asiento.</div></div>
        </div>
        <div class="lx-pillar">
            <div class="lx-pillar-ico">{ICO_AUDITOR}</div>
            <div><div class="lx-pillar-t">Human-in-the-Loop</div>
            <div class="lx-pillar-d">Nada se guarda sin la aprobación de un contador.</div></div>
        </div>
        """, unsafe_allow_html=True)

    # ---------- KPIs ----------
    st.markdown('<div class="lx-section">Impacto proyectado</div>', unsafe_allow_html=True)
    k1, k2, k3 = st.columns(3)
    k1.metric("Tiempo de registro", "−80%", help="Reducción estimada frente a la captura manual de asientos.")
    k2.metric("Asientos descuadrados", "0", help="La validación de Partida Doble (Debe = Haber) bloquea cualquier asiento desbalanceado.")
    k3.metric("Fuentes de captura", "4", help="Texto, documentos (PDF y Word), imagen y voz.")
    st.caption("Cifras proyectadas con base en pruebas internas del prototipo; sujetas a validación con datos reales.")

    st.markdown(
        '<div class="lx-footer">Una iniciativa del Grupo 7 para el curso de Sistema y Gestión Financiera, '
        'a cargo del profesor MBA John Valle Santos - Universidad Nacional de Ingeniería (UNI).</div>',
        unsafe_allow_html=True)

elif menu == "Registro de Transacciones":
    st.title("Registro de Transacciones")
    st.markdown("Ingresa nuevos asientos contables mediante captura manual o el Asistente IA.")

    # Redujimos las pestañas a 2: Manual y el Motor Central IA
    tab_manual, tab_ia = st.tabs(["Ingreso Manual", "Asistente IA (Gemini)"])

    with tab_manual:
        st.subheader("Ingreso Manual de Asientos")
        st.info("Captura el asiento y envíalo al borrador. Allí lo apruebas y recién "
                "entonces se guarda en la base de datos.")

        lista_cuentas = obtener_cuentas_pcge()

        if "form_key" not in st.session_state:
            st.session_state.form_key = 0

        with st.form(f"form_asiento_manual_{st.session_state.form_key}", clear_on_submit=False):
            col1, col2 = st.columns([1, 3])
            with col1:
                fecha_input = st.date_input("Fecha de la transacción",min_value=datetime.date(2000, 1, 1))
            with col2:
                glosa_input = st.text_input("Glosa / Descripción de la operación", placeholder="Ej. Por el aporte de capital inicial")

            st.markdown("**Detalle del Asiento (Partida Doble)**")

            df_inicial = pd.DataFrame([{"Cuenta": None, "Debe": 0.0, "Haber": 0.0} for _ in range(2)])
            df_editado = st.data_editor(
                df_inicial,
                column_config={
                    "Cuenta": st.column_config.SelectboxColumn("Cuenta Contable (PCGE)", options=lista_cuentas, required=True, width="large"),
                    "Debe": st.column_config.NumberColumn("Debe (S/)", min_value=0.0, format="%.2f"),
                    "Haber": st.column_config.NumberColumn("Haber (S/)", min_value=0.0, format="%.2f")
                },
                num_rows="dynamic",
                use_container_width=True
            )

            submit_btn = st.form_submit_button("Enviar al borrador", type="primary")

        if submit_btn:
            if not glosa_input:
                st.error("La glosa es obligatoria.")
            else:
                detalles_asiento = []
                for index, row in df_editado.iterrows():
                    if pd.notna(row['Cuenta']):
                        codigo_cuenta = row['Cuenta'].split(" - ")[0]
                        val_debe = float(row['Debe']) if pd.notna(row['Debe']) else 0.0
                        val_haber = float(row['Haber']) if pd.notna(row['Haber']) else 0.0

                        detalles_asiento.append({'cuenta': codigo_cuenta, 'debe': val_debe, 'haber': val_haber})

                if len(detalles_asiento) < 2:
                    st.error("El asiento debe tener al menos dos movimientos.")
                else:
                    # El asiento manual entra al MISMO borrador que la IA: no se
                    # escribe nada en la base de datos todavía.
                    cargar_borrador([{
                        "fecha": fecha_input.isoformat(),
                        "glosa": glosa_input,
                        "asiento": detalles_asiento,
                    }], "ingreso manual")
                    st.session_state.form_key += 1
                    st.rerun()

    with tab_ia:
        st.subheader("Asistente IA Contable (Gemini)")
        st.markdown("La Inteligencia Artificial analizará el contexto, identificará las cuentas del PCGE y calculará la partida doble automáticamente.")

        ia_metodo = st.radio(
            "Selecciona el método de captura:",
            ["Enunciado de Texto", "Carga de Documentos (Excel, PDF, Word)", "Dictado por Voz", "Escáner Visual"],
            horizontal=True
        )
        st.markdown("---")

        if ia_metodo == "Enunciado de Texto":
            st.info("Pega aquí el caso del profesor (incluso copiando celdas de Excel). "
                    "La IA Arma la partida doble y el resultado pasa al borrador: "
                    "lo revisas, lo corriges y lo apruebas antes de guardarse.")

            # La fecha la elige el usuario. Si la deja en la fecha por defecto de
            # Streamlit, el borrador la mostrará como dato a confirmar.
            fecha_ia = st.date_input("Fecha de la transacción", min_value=datetime.date(2000, 1, 1))

            enunciado_input = st.text_area(
                "Enunciado contable:",
                placeholder="Ej: Se compra 50,000 de mercadería al contado...",
                height=100
            )

            if st.button("Analizar y Generar Borrador", type="primary", use_container_width=True):
                if enunciado_input:
                    with st.spinner("Gemini está analizando el caso aplicando el PCGE..."):
                        exito_ia, resultado_ia = ia.extraer_asiento_de_texto(enunciado_input)

                        if not exito_ia:
                            st.error(f"Error de procesamiento: {resultado_ia}")
                        elif not isinstance(resultado_ia, list) or not resultado_ia:
                            st.error("La IA no devolvió partidas utilizables para este enunciado.")
                        else:
                            # Un enunciado es UN asiento con las partidas que la IA
                            # entrego. No se agrupa ni se reordena nada.
                            glosa_corta = (enunciado_input[:45] + '...') if len(enunciado_input) > 45 else enunciado_input
                            cargar_borrador([{
                                "fecha": fecha_ia.isoformat(),
                                "glosa": glosa_corta,
                                "asiento": resultado_ia,
                            }], "enunciado de texto")
                            st.rerun()
                else:
                    st.error("Por favor, ingresa un enunciado.")

        elif ia_metodo == "Carga de Documentos (Excel, PDF, Word)":
            st.info("Sube tu archivo. La IA extraerá los datos y abrirá un Espacio de Trabajo (Borrador) para que revises y corrijas antes de guardar.")

            # Ampliamos los tipos de archivo permitidos
            archivo_doc = st.file_uploader("Selecciona el documento", type=["xlsx", "xls", "pdf", "docx"])

            if archivo_doc is not None:
                # 1. ENRUTADOR DE EXTRACCIÓN DE TEXTO
                extension = archivo_doc.name.split('.')[-1].lower()
                texto_crudo, aviso_extraccion, error_extraccion = "", None, None

                with st.spinner(f"Extrayendo texto del archivo .{extension}..."):
                    try:
                        if extension in ['xlsx', 'xls']:
                            df_crudo = pd.read_excel(archivo_doc, header=None).dropna(how='all').dropna(axis=1, how='all')
                            texto_crudo = df_crudo.to_csv(index=False, header=False, na_rep="")

                        elif extension == 'pdf':
                            texto_crudo, aviso_extraccion = extraer_texto_de_pdf(archivo_doc)

                        elif extension == 'docx':
                            texto_crudo, aviso_extraccion = extraer_texto_de_docx(archivo_doc)

                    except Exception as e:
                        error_extraccion = f"No se pudo leer el archivo .{extension}: {e}"

                if error_extraccion:
                    st.error(error_extraccion)
                elif aviso_extraccion:
                    st.warning(aviso_extraccion)

                # 2. LIMPIEZA RIGUROSA ANTES DE LA IA (control nulos, saltos y largo)
                texto_para_ia, informe = sanitizar_texto_para_ia(texto_crudo)

                if informe["control_eliminados"]:
                    st.caption(f"Se eliminaron {informe['control_eliminados']:,} caracteres de "
                               "control del documento antes de enviarlo a la IA.")
                if not texto_para_ia:
                    st.error("El documento no tiene texto legible. Si es un escaneo, "
                             "necesita OCR previo; si es un Excel, revisa que tenga contenido.")
                else:
                    st.caption(f"Documento listo para la IA: {informe['caracteres_enviados']:,} caracteres "
                               f"(original: {informe['caracteres_originales']:,}).")
                    if informe["recortado"]:
                        st.warning(f"El documento supera el límite de {MAX_CHARS_PARA_IA:,} caracteres que "
                                   "acepta una petición de la API. Se enviará leído por bloques, en orden y "
                                   "sin descartar ningún contenido.")

                    # Mostramos un fragmento de lo que leyó el sistema
                    with st.expander("Ver texto limpio que se enviará a la IA"):
                        st.text_area("Texto enviado a la IA:", texto_para_ia[:1500] + "\n\n... (continúa)",
                                     height=200, disabled=True)

                    # 3. BOTÓN DE IA (el texto que se previsualiza es el que se envía)
                if st.button("Generar Borrador con IA", type="primary", use_container_width=True):
                    with st.spinner("Extrayendo filas del documento y armando la partida doble..."):
                        # Mandamos EXACTAMENTE el texto ya saneado y previsualizado
                        exito_ia, lote_operaciones = ia.analizar_excel_completo(texto_para_ia)

                        if exito_ia:
                            # El lote crudo va al borrador comun: cada elemento es un
                            # asiento y sus partidas se conservan tal cual llegaron.
                            cargar_borrador(lote_operaciones, "documento cargado")
                            filas = sum(len(op["asiento"]) for op in lote_operaciones)
                            st.session_state.avisos_borrador = getattr(lote_operaciones, "avisos", [])
                            st.session_state.resumen_borrador = (
                                f"{filas} fila(s) transcrita(s) en {len(lote_operaciones)} asiento(s)."
                            )
                            st.rerun()
                        else:
                            st.error(lote_operaciones)

        elif ia_metodo == "Dictado por Voz":
            st.markdown("**Reconocimiento de Voz a Texto**")
            if st.button("Iniciar Grabación (Simulación)", type="secondary", use_container_width=True):
                st.warning("Próximamente: Integración del micrófono web.")

        elif ia_metodo == "Escáner Visual":
            st.markdown("**Visión Artificial y OCR para Comprobantes / Casos**")
            st.info("Puedes tomar una foto directamente o subir una imagen guardada. La IA leerá los datos espaciales y generará los asientos.")

            # ORIGEN DE LA IMAGEN COMO RADIO, NO COMO COLUMNAS.
            # `st.camera_input` abre el stream de la camara en cuanto se monta el widget:
            # dentro de un `st.columns` ambos widgets coexisten, la camara se encendia
            # sola al entrar a la pestana y el usuario tenia que denegar el permiso del
            # navegador para poder usar la subida de archivo. El radio hace que el widget
            # de camara no exista en el arbol hasta que el usuario lo elige.
            # La opcion por defecto es SUBIR ARCHIVO: es la unica que no toca el
            # hardware. La camara queda como segunda opcion, nunca como la primera.
            origen_imagen = st.radio(
                "Origen de la imagen",
                ["Subir Archivo", "Usar Cámara"],
                horizontal=True,
            )

            # Las condiciones comparan contra el VALOR de la opcion, no contra su
            # posicion, asi que invertir la lista no obliga a reordenar este bloque.
            imagen_final = None
            if origen_imagen == "Subir Archivo":
                imagen_final = st.file_uploader("Sube una imagen:", type=["png", "jpg", "jpeg"])
            elif origen_imagen == "Usar Cámara":
                imagen_final = st.camera_input("Tomar foto (Cámara)")

            if imagen_final:
                st.image(imagen_final, caption="Documento listo para analizar", width=350)

                if st.button("Escanear Imagen y Extraer Asiento", type="primary", use_container_width=True):
                    with st.spinner("Llama 3.2 Vision está analizando el comprobante..."):
                        # getvalue() entrega los bytes crudos: es lo que espera el motor
                        # visual. read() con decodificacion daria texto y no una imagen.
                        imagen_bytes = imagen_final.getvalue()
                        exito_ia, lote_operaciones = ia.analizar_imagen_comprobante(imagen_bytes)

                        if exito_ia:
                            # Reutilizamos el mecanismo de borrador estándar: la foto entra
                            # por el mismo camino de revisión humana que el texto y el Excel,
                            # sin atajos y sin saltarse la validación de partida doble.
                            cargar_borrador(lote_operaciones, "escáner visual")
                            filas = sum(len(op["asiento"]) for op in lote_operaciones)
                            st.session_state.avisos_borrador = getattr(lote_operaciones, "avisos", [])
                            st.session_state.resumen_borrador = (
                                f"{filas} partida(s) extraída(s) visualmente en {len(lote_operaciones)} asiento(s)."
                            )
                            st.rerun()
                        else:
                            st.error(lote_operaciones)

    # ZONA DE REVISION HUMANA UNICA. Se dibuja una sola vez al final de la pagina
    # para que el ingreso manual, el enunciado de texto y el documento/upload
    # terminen exactamente en el mismo mecanismo: borrador -> edicion ->
    # validacion -> aprobacion -> base de datos.
    origen_borrador = st.session_state.get("origen_borrador")
    if st.session_state.get("borrador_ia"):
        if st.session_state.get("resumen_borrador"):
            st.caption(st.session_state["resumen_borrador"])
        for aviso in st.session_state.get("avisos_borrador", []):
            st.warning(f"{aviso}")
        render_borrador(origen_borrador or "documento")

elif menu == "Estados Financieros":
    st.title("Estados Financieros")
    st.markdown("Reportes automáticos basados en el Plan Contable General Empresarial (PCGE).")

    df_saldos = lg.obtener_saldos_cuentas()

    tab_balance, tab_resultados = st.tabs(["Balance General", "Estado de Resultados"])

    with tab_balance:
        st.subheader("Estado de Situación Financiera")

        if not df_saldos.empty:
            # Separamos las cuentas según el PCGE
            activos = df_saldos[df_saldos['elemento'].isin([1, 2, 3])]
            pasivos_patrimonio = df_saldos[df_saldos['elemento'].isin([4, 5])]

            # Calculamos la utilidad del periodo para cuadrar el balance
            ingresos_tot = df_saldos[df_saldos['elemento'] == 7]['saldo'].sum()
            gastos_tot = df_saldos[df_saldos['elemento'].isin([6, 9])]['saldo'].sum()
            utilidad = ingresos_tot - gastos_tot

            col_activo, col_pasivo = st.columns(2)

            with col_activo:
                st.markdown("### Activos")
                st.dataframe(activos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.success(f"**Total Activos: S/ {activos['saldo'].sum():,.2f}**")

            with col_pasivo:
                st.markdown("### Pasivos y Patrimonio")
                st.dataframe(pasivos_patrimonio[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                if utilidad != 0:
                    st.caption(f"*Utilidad del Ejercicio a distribuir: S/ {utilidad:,.2f}*")

                total_p_y_p = pasivos_patrimonio['saldo'].sum() + utilidad
                st.error(f"**Total Pasivo + Patrimonio: S/ {total_p_y_p:,.2f}**")

        else:
            st.info("Aún no hay registros para procesar el Balance General.")

    with tab_resultados:
        st.subheader("Estado de Resultados Integrales")

        if not df_saldos.empty:
            ingresos = df_saldos[df_saldos['elemento'] == 7]
            gastos = df_saldos[df_saldos['elemento'].isin([6, 9])]

            col_ing, col_gas = st.columns(2)

            with col_ing:
                st.markdown("### Ingresos")
                if not ingresos.empty:
                    st.dataframe(ingresos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.success(f"**Total Ingresos: S/ {ingresos_tot:,.2f}**")

            with col_gas:
                st.markdown("### Gastos")
                if not gastos.empty:
                    st.dataframe(gastos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.error(f"**Total Gastos: S/ {gastos_tot:,.2f}**")

            st.markdown("---")
            color_utilidad = "normal" if utilidad >= 0 else "inverse"
            st.metric(label="RESULTADO DEL EJERCICIO (Utilidad / Pérdida)", value=f"S/ {utilidad:,.2f}", delta_color=color_utilidad)
        else:
            st.info("Aún no hay registros de ingresos o gastos para procesar.")

elif menu == "Dashboard Gerencial":
    st.title("Dashboard Gerencial")
    st.markdown("Visión general del estado financiero en tiempo real.")

    # Llamamos a nuestro motor de lógica
    df_saldos = lg.obtener_saldos_cuentas()

    total_activos = 0.0
    total_pasivos = 0.0
    total_patrimonio = 0.0
    utilidad = 0.0

    if not df_saldos.empty:
        # Filtramos matemáticamente usando el campo 'elemento' del PCGE
        total_activos = df_saldos[df_saldos['elemento'].isin([1, 2, 3])]['saldo'].sum()
        total_pasivos = df_saldos[df_saldos['elemento'] == 4]['saldo'].sum()
        total_patrimonio = df_saldos[df_saldos['elemento'] == 5]['saldo'].sum()

        ingresos = df_saldos[df_saldos['elemento'] == 7]['saldo'].sum()
        gastos = df_saldos[df_saldos['elemento'].isin([6, 9])]['saldo'].sum()
        utilidad = ingresos - gastos

    # Tarjetas de Métricas (KPIs)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(label="Activos Totales", value=f"S/ {total_activos:,.2f}")
    col2.metric(label="Pasivos Totales", value=f"S/ {total_pasivos:,.2f}")
    col3.metric(label="Patrimonio", value=f"S/ {total_patrimonio:,.2f}")
    col4.metric(label="Utilidad del Ejercicio", value=f"S/ {utilidad:,.2f}")

    st.markdown("---")
    st.subheader("Saldos Actuales por Cuenta")

    if not df_saldos.empty:
        # Mostramos una tabla estilizada con los datos reales
        st.dataframe(
            df_saldos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No hay transacciones registradas todavía.")
