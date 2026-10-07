import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

import streamlit as st
import pandas as pd
import logica as lg # Conectamos nuestro motor financiero
import database as bd # Esquema, catalogo PCGE e inicializacion de la base de datos
import reportes_pdf # Maquetacion de los PDF formales (Libro Diario, Libro Mayor, Estados)
import datetime
import ia_engine as ia
import transcripcion as voz
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
# PERIODO: UN SOLO MECANISMO PARA LIBROS Y ESTADOS FINANCIEROS
# ============================================================================
# Vive aqui, a nivel de modulo, y no dentro de la seccion de Libros Contables
# porque lo usan tres paginas distintas. Duplicarlo seria la forma mas rapida de
# que el Libro Diario y el Balance dejaran de aceptar el mismo rango un dia de
# estos, sin que ninguna prueba se-enterara.

def _periodo(rango):
    """
    Normaliza la salida de `st.date_input` a `(desde, hasta)`.

    Devuelve `(None, None)` mientras el usuario aun no ha elegido la fecha final:
    `date_input` entrega una tupla de un solo elemento en ese instante. Es el
    mismo contrato que usan los Libros Contables.
    """
    if isinstance(rango, (tuple, list)) and len(rango) == 2:
        return rango[0], rango[1]
    return None, None


def _sufijo_periodo(desde, hasta):
    """
    Sufijo de archivo con el periodo, para que dos descargas no se confundan.

    Sin periodo informado devuelve 'completo': el nombre del archivo debe decir
    que se exporto todo, nunca insinuar un corte que no se aplico.
    """
    def _iso(valor):
        return valor.isoformat() if hasattr(valor, "isoformat") else ""

    inicio, fin = _iso(desde), _iso(hasta)
    if inicio and fin:
        return f"{inicio}_{fin}"
    if fin:
        return f"hasta_{fin}"
    if inicio:
        return f"desde_{inicio}"
    return "completo"


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


# --------------------------------------------------------------------------- #
# IDENTIDAD DEL DOCUMENTO EXCEL: SOLO PARA NO DUPLICAR EN SILENCIO
# --------------------------------------------------------------------------- #
# La huella es el sha256 del TEXTO YA SANEADO del Excel, no de sus asientos: es la
# identidad exacta del documento procesado, no una heuristica por fecha, glosa,
# importe ni cuenta (que dos asientos coincidan en esos campos NO prueba que sean el
# mismo documento). Cuando la misma hoja vuelve al borrador despues de haber sido
# registrada, se avisa y se exige una confirmacion explicita antes de volver a
# guardarla. Sin huella (dictado, PDF, escaner o captura manual) no hay ninguna
# comprobacion: esos flujos no cambian.
def _ruta_documentos_registrados():
    return Path(lg.DB_PATH).with_name("documentos_registrados.json")


def _leer_documentos_registrados():
    ruta = _ruta_documentos_registrados()
    try:
        with open(ruta, "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
    except (OSError, ValueError):
        return set()
    return set(datos) if isinstance(datos, list) else set()


def _marcar_documento_registrado(huella):
    if not huella:
        return
    huellas = _leer_documentos_registrados()
    if huella in huellas:
        return
    huellas.add(huella)
    try:
        with open(_ruta_documentos_registrados(), "w", encoding="utf-8") as archivo:
            json.dump(sorted(huellas), archivo, ensure_ascii=False, indent=2)
    except OSError:
        # No poder persistir la huella no puede impedir un guardado ya realizado: a lo
        # sumo se volvera a avisar en la proxima pasada, que es el lado seguro.
        pass


def _huella_de_documento(texto):
    return hashlib.sha256((texto or "").encode("utf-8")).hexdigest()


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
    # La huella solo la vuelve a poner el flujo de Excel, despues de cargar el
    # borrador. Asi, un dictado, un PDF, un escaner o una captura manual nunca
    # heredan la identidad de un Excel anterior.
    st.session_state.pop("huella_documento", None)


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

    # Advertencia de reproceso: si este MISMO Excel ya fue registrado antes, no se
    # guarda en silencio. Se dice y se exige una confirmacion explicita, distinta de
    # la aprobacion de cada asiento, que aqui solo autoriza el duplicado.
    huella_documento = st.session_state.get("huella_documento")
    documento_ya_registrado = bool(huella_documento) and huella_documento in _leer_documentos_registrados()
    reproceso_confirmado = True
    if documento_ya_registrado:
        st.warning("Este mismo documento Excel ya fue registrado en la Base de Datos. "
                   "Si guardas otra vez, sus operaciones se registrarán de nuevo como un "
                   "duplicado. No se guarda nada salvo que lo confirmes.")
        reproceso_confirmado = st.checkbox(
            "Confirmo que quiero registrar este mismo documento otra vez",
            value=False,
            key=f"confirmar_reproceso_{borrador_id}",
        )

    if st.button("Guardar Asientos Aprobados en BD", type="primary",
                 disabled=bool(bloqueos) or not a_guardar or (documento_ya_registrado and not reproceso_confirmado)):
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

        if exitos:
            _marcar_documento_registrado(huella_documento)
        st.session_state.borrador_ia = None
        if exitos:
            st.success(f"Se registraron {exitos} asientos contables en la Base de Datos.")
            st.balloons()
        for error in errores:
            st.error(f"No se pudo registrar: {error}")
        time.sleep(1.5)
        st.rerun()


def _serializar_tabla(filas):
    """
    Convierte una tabla en filas de celdas, conservando la POSICIÓN de cada celda.

    Las celdas vacías se conservan como cadenas vacías y solo se descarta la fila
    que está enteramente vacía. Antes se eliminaban las celdas vacías, y eso
    borraba la información que dice en qué columna estaba escrito cada importe:
    en el libro contable del taller, la fila `39 Depreciación ACUMULADA -A`
    tiene su 135 en la columna HABER y la fila `68 Gastos de Depreciación G+`
    lo tiene en la columna DEBE. Al quitar las vacías, ambas quedaban idénticas
    (`... | 135`) y el importe perdía su lado, así que la IA ya no podía leer
    del documento en qué columna estaba y tenía que deducirlo de la notación
    (`-A`), que es justo lo que producía el 39 en el DEBE.

    La posición es la única fuente que el documento ofrece sobre el lado, y este
    lector es el único punto donde puede perderse: no se interpreta nada aquí,
    solo se transcribe la rejilla tal como está.

    Las filas se igualan de ancho con celdas vacías a la derecha porque la rejilla
    que ve la IA tiene que ser rectangular: si una fila llega con menos celdas que
    las demás, "el importe que está bajo HABER" no apunta a la misma columna en cada
    línea, que es exactamente la ambigüedad que hay que eliminar.
    """
    limpias = []
    for fila in filas:
        celdas = [str(celda).strip() if celda is not None else "" for celda in fila]
        if any(celdas):
            limpias.append(celdas)

    if not limpias:
        return limpias

    ancho = max(len(fila) for fila in limpias)
    return [fila + [""] * (ancho - len(fila)) for fila in limpias]


def _tabla_es_fiable(filas, texto_pagina):
    """
    Descarta las tablas ficticias: exige ≥3 filas, ≥2 columnas con CONTENIDO y que
    ninguna ficha de la tabla falte en el texto real de la página (control
    anti-truncamiento).

    Las columnas se cuentan con contenido y no con celdas, porque `_serializar_tabla`
    ya conserva las celdas vacías para no perder la posición de los importes. Una
    fila con una sola celda de texto y el resto vacías sigue siendo, para este
    control, una fila de una columna: contarla como de cuatro cambiaría qué páginas
    se leen como tabla y cuáles como texto maquetado.
    """
    if len(filas) < 3:
        return False
    if sum(1 for fila in filas if sum(1 for celda in fila if celda) >= 2) < len(filas) * 0.6:
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


# Orden estricto del ciclo contable: registro -> libros -> estados financieros -> análisis gerencial.
# Estos nombres son los que usan los `if menu == ...` del enrutamiento: no cambiarlos por separado.
MENU_OPCIONES = ["Inicio", "Registro de Transacciones", "Libros Contables", "Estados Financieros", "Dashboard Gerencial"]
MENU_ICONOS = ["house", "pen", "book", "file-earmark-spreadsheet", "graph-up"]  # Bootstrap Icons

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

# ============================================================================
# 2-bis. ARRANQUE: QUE LA BASE DE DATOS EXISTA Y ESTE LISTA
# ============================================================================
# `streamlit run app.py` tiene que funcionar sobre una instalacion limpia, sin que
# nadie tenga que acordarse de ejecutar `database.py` antes. Este es el UNICO punto
# del programa que abre la base para escribirla.
#
# Que estuviera aqui no es una comodidad: `logica.obtener_conexion()` es una
# `sqlite3.connect()` y eso, por si solo, CREA el archivo. Sobre una base inexistente
# la aplicacion se abria sin error y dejaba `datos/contabilidad.db` con 0 bytes, y
# recien al entrar a "Registro de Transacciones" saltaba `no such table: Cuentas`.
#
# La logica NO esta aqui: vive en `database.py`, que es la misma que ejecuta el
# script manual, para que no haya dos copias que puedan divergir. Esta llamada solo la
# invoca y avisa de lo que encontro.
#
# Va despues de `set_page_config` (que debe ser el primer comando de Streamlit) y
# antes del enrutamiento, que es donde la base se lee por primera vez.

@st.cache_resource(show_spinner=False)
def _asegurar_base_de_datos(ruta: str):
    """
    Inicializa la base de datos solo si hace falta. Nunca destruye datos.

    Devuelve lo que `database.inicializar_base_de_datos_si_es_necesario()` reporta:
    que estado encontro (inexistente, vacia, parcial, lista o inconsistente) y que
    hizo al respecto.

    La ruta llega por parametro y no se lee por dentro, a proposito: `logica.DB_PATH`
    es un nombre propio de `logica.py` (las pruebas lo sustituyen con `monkeypatch`),
    y leer una constante de modulo desde aqui escribiria en la base real durante las
    pruebas. Por la misma razon se le pasa `lg.DB_PATH` y no `bd.DB_PATH`.

    `@st.cache_resource` hace que el cuerpo corra UNA vez por sesion del servidor y
    que los reruns siguientes devuelvan el resultado cacheado, sin volver a tocar el
    disco. Si la funcion lanza, la excepcion no se cachea: el siguiente render vuelve
    a intentarlo.
    """
    return bd.inicializar_base_de_datos_si_es_necesario(ruta)


try:
    _resultado_bd = _asegurar_base_de_datos(lg.DB_PATH)
except Exception as _error_bd:  # noqa: BLE001 - la app no debe morir por un problema de arranque
    _resultado_bd = {
        "estado": bd.ERROR_ACCESO,
        "accion": bd.SIN_CAMBIOS,
        "ruta": lg.DB_PATH,
        "detalle": f"No se pudo preparar la base de datos: {_error_bd}",
        "tablas_creadas": [],
        "cuentas_insertadas": 0,
        "cuentas": 0,
        "asientos": 0,
        "detalles": 0,
    }

# Avisos de arranque. Solo se muestran los dos estados que son un problema real de la
# base (inaccesible, o con movimientos sin catalogo). Una base recien inicializada con
# el PCGE cargado y cero asientos es el estado normal de una instalacion limpia: se
# inicializa igual, pero en silencio, para que la portada no arranque con un aviso
# tecnico antes de que exista un solo dato.
if _resultado_bd["estado"] == bd.ERROR_ACCESO:
    # Sin acceso a la base ninguna pagina funciona: se dice claro y se detiene el
    # render en vez de dejar que cada consulta reviente con su propio traceback.
    st.error(f"Ledgerix no puede trabajar con la base de datos.\n\n{_resultado_bd['detalle']}")
    st.code(_resultado_bd["ruta"], language=None)
    st.stop()
elif _resultado_bd["estado"] == bd.INCONSISTENTE:
    # Movimientos sin catalogo. No se repara solo porque no hay forma de saber que
    # cuentas son las correctas; las demas paginas siguen Podiendo leerse.
    st.warning(
        "La base de datos esta incompleta y **no se ha modificado**.\n\n"
        f"{_resultado_bd['detalle']}\n\n"
        "Mientras tanto puedes consultar los libros y los estados financieros, pero no "
        "se pueden registrar asientos nuevos hasta que el catalogo de cuentas se complete."
    )

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
                        # Mandamos EXACTAMENTE el texto ya saneado y previsualizado.
                        # La extensión decide el prompt: solo una hoja de cálculo recibe
                        # el bloque que separa el CONTEXTO de las operaciones.
                        exito_ia, lote_operaciones = ia.analizar_excel_completo(
                            texto_para_ia, origen=extension)

                        if exito_ia:
                            # El lote crudo va al borrador comun: cada elemento es un
                            # asiento y sus partidas se conservan tal cual llegaron.
                            cargar_borrador(lote_operaciones, "documento cargado")
                            # La identidad exacta del Excel queda en la sesion para que
                            # el borrador pueda advertir si ese mismo documento ya fue
                            # registrado. Solo la hoja de calculo la recibe.
                            if extension in ("xlsx", "xls"):
                                st.session_state["huella_documento"] = _huella_de_documento(texto_para_ia)
                            filas = sum(len(op["asiento"]) for op in lote_operaciones)
                            st.session_state.avisos_borrador = getattr(lote_operaciones, "avisos", [])
                            st.session_state.resumen_borrador = (
                                f"{filas} fila(s) transcrita(s) en {len(lote_operaciones)} asiento(s)."
                            )
                            st.rerun()
                        else:
                            st.error(lote_operaciones)

        elif ia_metodo == "Dictado por Voz":
            st.markdown("**Reconocimiento de Voz a Texto (Groq Whisper)**")
            st.info("Graba tu dictado con el micrófono. El audio se transcribirá usando Groq y el texto resultante podrá revisarse antes de generar el asiento.")
            audio_grabado = st.audio_input("Grabar audio")
            if audio_grabado is not None:
                st.audio(audio_grabado, format="audio/wav")
                if st.button("Transcribir audio con Groq", type="secondary", use_container_width=True):
                    try:
                        audio_bytes=audio_grabado.getvalue()
                    except Exception:
                        try:
                            audio_bytes=audio_grabado.read() if hasattr(audio_grabado,"read") else b""
                        except Exception:
                            audio_bytes=b""
                    if not audio_bytes:
                        st.error("El audio está vacío.")
                    else:
                        with st.spinner("Transcribiendo audio con Groq..."):
                            exito_t, resultado_t=voz.transcribir_audio_bytes(audio_bytes)
                        if not exito_t:
                            st.error(resultado_t)
                        else:
                            st.session_state.texto_voz=resultado_t
                            st.rerun()
            texto_transcrito=st.session_state.get("texto_voz","")
            if texto_transcrito:
                st.text_area("Texto transcrito (editable para revisión)", key="texto_voz", height=120)
                col1,col2=st.columns(2)
                with col1:
                    if st.button("Analizar y generar borrador", type="primary", use_container_width=True):
                        texto_a_enviar=st.session_state.get("texto_voz","").strip()
                        if not texto_a_enviar:
                            st.error("El texto transcrito está vacío.")
                        else:
                            with st.spinner("La IA está analizando el dictado aplicando el PCGE..."):
                                # Mismo pipeline que PDF/Word: contrato multi-operacion
                                # (fecha + glosa + asiento por operacion), validacion de
                                # partida doble y normalizacion de fecha. La fecha que
                                # viaja al borrador es SOLO la escrita en la transcripcion
                                # y la glosa se deriva deterministicamente de cada
                                # oracion (ver ia.analizar_dictado).
                                exito_ia, resultado_ia=ia.analizar_dictado(texto_a_enviar)
                            if not exito_ia:
                                st.error(f"Error de procesamiento: {resultado_ia}")
                            elif not isinstance(resultado_ia,list) or not resultado_ia:
                                st.error("La IA no devolvió operaciones utilizables para este dictado.")
                            else:
                                # Una grabacion con varias transacciones genera varios
                                # asientos, cada uno con su fecha y su glosa: entran al
                                # borrador comun, igual que un PDF o un Excel.
                                cargar_borrador(list(resultado_ia), "dictado por voz")
                                st.session_state.avisos_borrador = getattr(resultado_ia, "avisos", [])
                                filas_voz = sum(
                                    len(op.get("asiento") or [])
                                    for op in resultado_ia if isinstance(op, dict)
                                )
                                st.session_state.resumen_borrador = (
                                    f"{filas_voz} partida(s) en {len(resultado_ia)} asiento(s)."
                                )
                                st.session_state.pop("texto_voz",None)
                                st.rerun()
                with col2:
                    if st.button("Limpiar texto transcrito", use_container_width=True):
                        st.session_state.pop("texto_voz",None)
                        st.rerun()


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
                    # Nombre de modelo generico a proposito: Groq retiro los llama-3.2
                    # vision y el modelo efectivo se configura por MODELO_VISION. Si la
                    # cuenta no tiene vision habilitada, `analizar_imagen_comprobante`
                    # devuelve un mensaje controlado y el resto del sistema sigue.
                    with st.spinner("El escáner visual está analizando la imagen..."):
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

elif menu == "Libros Contables":
    st.title("Libros Contables")
    st.markdown("Registros formales de las operaciones, base para la elaboración de los Estados Financieros.")

    # ------------------------------------------------------------------
    # Fuente de datos: la base de datos real. Los libros son consultas de SOLO
    # LECTURA sobre Asientos JOIN Detalles JOIN Cuentas (ver logica.py). No hay
    # datos de muestra, ni cuentas inventadas, ni numeración de asientos
    # construida en la interfaz: el N° de asiento es Asientos.id y la
    # denominación sale de Cuentas.descripcion.
    # ------------------------------------------------------------------
    _COLS_DIARIO = ["Fecha", "N° Asiento", "Glosa", "Cuenta PCGE", "Denominación", "Debe", "Haber"]
    _COLS_MAYOR = ["Fecha", "N° Asiento", "Cuenta PCGE", "Denominación", "Glosa", "Debe", "Haber", "Saldo"]

    _ETIQUETAS = {
        "fecha": "Fecha", "numero_asiento": "N° Asiento", "glosa": "Glosa",
        "cuenta": "Cuenta PCGE", "denominacion": "Denominación",
        "debe": "Debe", "haber": "Haber", "saldo": "Saldo",
    }

    def _presentar(df, columnas):
        """Lleva el DataFrame de logica.py a las etiquetas que ya consume la tabla."""
        if df.empty:
            return pd.DataFrame(columns=columnas)
        df = df.rename(columns=_ETIQUETAS)
        # La fecha llega como texto ISO de SQLite; se pasa a date para que la
        # columna se muestre con el formato-dd/mm/aaaa y no como texto crudo.
        df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.date
        return df[[c for c in columnas if c in df.columns]]

    def _soles(valor):
        return f"S/ {valor:,.2f}"

    def _fecha_texto(valor):
        """Fecha en dd/mm/aaaa para los encabezados de cada grupo."""
        try:
            return valor.strftime("%d/%m/%Y")
        except AttributeError:
            return str(valor)

    cfg_fecha = st.column_config.DateColumn("Fecha", format="DD/MM/YYYY", width="small")
    cfg_asiento = st.column_config.TextColumn("N° Asiento", width="small")
    cfg_glosa = st.column_config.TextColumn("Glosa", width="large")
    cfg_cuenta = st.column_config.TextColumn("Cuenta PCGE", width="small")
    cfg_denom = st.column_config.TextColumn("Denominación", width="medium")
    cfg_debe = st.column_config.NumberColumn("Debe (S/)", format="S/ %.2f", min_value=0, width="small")
    cfg_haber = st.column_config.NumberColumn("Haber (S/)", format="S/ %.2f", min_value=0, width="small")
    cfg_saldo = st.column_config.NumberColumn("Saldo (S/)", format="S/ %.2f", width="small")

    # El rango por defecto es el de los asientos realmente registrados, no el de hoy.
    _fecha_min, _fecha_max = lg.obtener_rango_fechas_asientos()
    _hay_asientos = _fecha_min is not None

    tab_diario, tab_mayor = st.tabs(["Libro Diario", "Libro Mayor"])

    # ============================ LIBRO DIARIO ============================
    with tab_diario:
        st.subheader("Libro Diario")
        st.caption(
            "Centralización de los asientos ya validados por el usuario en el motor de Partida Doble. "
            "Cada operación, ya sea capturada manualmente o interpretada por el Asistente IA, se lee aquí "
            "en orden cronológico con su cuenta del PCGE, importes al Debe y al Haber, y glosa explicativa. "
            "Es una consulta de la contabilidad registrada: no crea, no modifica y no recalcula asientos."
        )

        kpi_diario = st.container()

        if _hay_asientos:
            with st.container(border=True):
                st.markdown("**Filtros de consulta**")
                f1, f2, f3 = st.columns([2, 2, 3])
                with f1:
                    rango_d = st.date_input("Periodo", value=(_fecha_min, _fecha_max),
                                            format="DD/MM/YYYY", key="lc_diario_periodo")
                with f2:
                    cuenta_d = st.text_input("Cuenta PCGE", placeholder="Ej.: 10", key="lc_diario_cuenta")
                with f3:
                    glosa_d = st.text_input("Buscar en glosa", placeholder="Palabra clave de la operación",
                                            key="lc_diario_glosa")

            desde_d, hasta_d = _periodo(rango_d)
            df_diario = _presentar(
                lg.obtener_libro_diario(desde_d, hasta_d,
                                        cuenta_d.strip() or None, glosa_d.strip() or None),
                _COLS_DIARIO,
            )
        else:
            # Sin asientos no hay periodo ni filtros que propagar al PDF: se
            # dejan en None para que el contexto del documento diga 'historico'
            # en vez de inventar un rango. None tambien significa 'sin filtro',
            # que es lo que espera el generador: por eso el contexto se arma con
            # `(x or "")`, y no con `.strip()` sobre un valor que puede ser None.
            desde_d = hasta_d = cuenta_d = glosa_d = None
            df_diario = pd.DataFrame(columns=_COLS_DIARIO)
            st.info("Todavía no hay asientos registrados. El Libro Diario se alimenta de los asientos "
                    "aprobados desde 'Registro de Transacciones'.")

        total_debe_d = float(df_diario["Debe"].sum())
        total_haber_d = float(df_diario["Haber"].sum())
        diferencia_d = round(total_debe_d - total_haber_d, 2)

        with kpi_diario:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Asientos registrados", f"{df_diario['N° Asiento'].nunique()}",
                      help="Cantidad de asientos contabilizados en el conjunto filtrado.")
            m2.metric("Total Debe", _soles(total_debe_d), help="Suma de los importes al Debe del conjunto filtrado.")
            m3.metric("Total Haber", _soles(total_haber_d), help="Suma de los importes al Haber del conjunto filtrado.")
            m4.metric("Diferencia (Debe - Haber)", _soles(diferencia_d),
                      delta="Partida doble cuadrada" if diferencia_d == 0 else "Revisar el conjunto filtrado",
                      delta_color="off" if diferencia_d == 0 else "inverse",
help="Control de cuadre del conjunto filtrado: al ser cero, el Debe y el Haber "
                            "de lo seleccionado están equilibrados.")

        # --- EXPORTACION A PDF ---
        # El PDF se arma con `df_diario`, el MISMO DataFrame que se acaba de pintar
        # arriba: no se vuelve a consultar la base de datos, de modo que el
        # documento y la pantalla no pueden discrepar. El generador no decide
        # cuales son los asientos ni los importes, solo los maqueta.
        _pdf_diario = reportes_pdf.generar_pdf_libro_diario(
            df_diario,
            {
                "periodo": (desde_d, hasta_d),
                "cuenta": (cuenta_d or "").strip() or None,
                "glosa": (glosa_d or "").strip() or None,
                "total_asientos": int(df_diario["N° Asiento"].nunique()),
                "total_partidas": int(len(df_diario)),
            },
        )
        st.download_button(
            "Exportar PDF",
            data=_pdf_diario,
            file_name=f"libro_diario_{_sufijo_periodo(desde_d, hasta_d)}.pdf",
            mime="application/pdf",
            disabled=df_diario.empty,
            help=("Genera el Libro Diario en PDF con el periodo y los filtros que estas viendo. "
                  "Se deshabilita cuando el filtro no devuelve ningun asiento."),
        )

        # El Diario se lee asiento por asiento. La fecha y la glosa viven en la tabla
        # Asientos, o sea que son UNA por asiento y se repiten en cada una de sus
        # partidas: se muestran una sola vez en el encabezado y la tabla interna deja
        # de repetirlas, dejando solo el detalle contable del asiento.
        if df_diario.empty:
            with st.container(border=True):
                st.caption("No se encontraron registros con los filtros aplicados.")
        else:
            _COLS_DETALLE_D = ["Cuenta PCGE", "Denominación", "Debe", "Haber"]
            # Orden explicito para no depender del ORDER BY de SQLite al agrupar.
            _diario_ordenado = df_diario.sort_values(["Fecha", "N° Asiento"], kind="stable")
            for _numero_asiento, _partidas in _diario_ordenado.groupby("N° Asiento", sort=True):
                _cabecera = _partidas.iloc[0]
                with st.container(border=True):
                    st.markdown(
                        f"**Asiento #{_numero_asiento}** "
                        f"| {_fecha_texto(_cabecera['Fecha'])} "
                        f"| {_cabecera['Glosa']}"
                    )
                    st.dataframe(
                        _partidas[_COLS_DETALLE_D],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Cuenta PCGE": cfg_cuenta,
                            "Denominación": cfg_denom,
                            "Debe": cfg_debe,
                            "Haber": cfg_haber,
                        },
                    )

    # ============================= LIBRO MAYOR ============================
    with tab_mayor:
        st.subheader("Libro Mayor")
        st.caption(
            "Consolidación del Libro Diario por cuenta del PCGE. El sistema agrupa cada movimiento "
            "según la naturaleza contable de la cuenta (la misma que usan los Estados Financieros) "
            "y calcula el saldo acumulado de esa cuenta, "
            "generando la base directa y auditable para la elaboración de los Estados Financieros."
        )

        kpi_mayor = st.container()

        if _hay_asientos:
            _cuentas_disponibles = lg.obtener_cuentas_con_movimiento()
            _opciones_cuenta = ["Todas las cuentas"] + [
                f"{c} - {d}" for c, d in zip(_cuentas_disponibles["codigo"], _cuentas_disponibles["descripcion"])
            ]

            with st.container(border=True):
                st.markdown("**Selección de cuenta**")
                s1, s2 = st.columns([3, 2])
                with s1:
                    cuenta_m = st.selectbox("Cuenta PCGE", options=_opciones_cuenta, index=0,
                                            key="lc_mayor_cuenta",
                                            help="Seleccione una cuenta para consultar su movimiento y saldo acumulado.")
                with s2:
                    rango_m = st.date_input("Periodo", value=(_fecha_min, _fecha_max),
                                            format="DD/MM/YYYY", key="lc_mayor_periodo")

            desde_m, hasta_m = _periodo(rango_m)
            codigo_m = None if cuenta_m == "Todas las cuentas" else cuenta_m.split(" - ", 1)[0]
            df_mayor = _presentar(lg.obtener_libro_mayor(desde_m, hasta_m, codigo_m), _COLS_MAYOR)

            _naturaleza_m = "acreedora"
            if codigo_m is not None:
                _ficha = _cuentas_disponibles[_cuentas_disponibles["codigo"] == codigo_m]
                if not _ficha.empty:
                    _naturaleza_m = _ficha["naturaleza"].iloc[0]
        else:
            cuenta_m, codigo_m = "Todas las cuentas", None
            desde_m = hasta_m = _naturaleza_m = None
            df_mayor = pd.DataFrame(columns=_COLS_MAYOR)
            st.info("Todavía no hay asientos registrados, así que el Libro Mayor no tiene movimientos "
                    "que consolidar.")

        total_debe_m = float(df_mayor["Debe"].sum())
        total_haber_m = float(df_mayor["Haber"].sum())

        with kpi_mayor:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Cuentas con movimiento", f"{df_mayor['Cuenta PCGE'].nunique()}",
                      help="Cuentas del PCGE que registran operaciones en el periodo.")
            k2.metric("Total Debe", _soles(total_debe_m), help="Suma de los cargos del conjunto filtrado.")
            k3.metric("Total Haber", _soles(total_haber_m), help="Suma de los abonos del conjunto filtrado.")
            if codigo_m is not None and not df_mayor.empty:
                k4.metric(f"Saldo final ({_naturaleza_m})", _soles(float(df_mayor["Saldo"].iloc[-1])),
                          help="Saldo acumulado de la cuenta seleccionada, ya sea deudora o acreedora.")
            else:
                k4.caption("Elige una cuenta del listado para ver su saldo acumulado. "
                           "No se muestra un saldo único para 'Todas las cuentas': cada cuenta tiene "
                           "naturaleza propia y mezclarlas no produce un saldo contable.")

        # --- EXPORTACION A PDF ---
        # Mismo criterio que en el Diario: se exporta `df_mayor` tal cual se
        # muestra. El nombre de archivo incluye el periodo y el codigo de cuenta
        # cuando hay uno, para no sobrescribir la exportacion de otra cuenta.
        _pdf_mayor = reportes_pdf.generar_pdf_libro_mayor(
            df_mayor,
            {
                "periodo": (desde_m, hasta_m),
                "cuenta_texto": cuenta_m if codigo_m is not None else None,
                "naturaleza": _naturaleza_m if codigo_m is not None else None,
            },
        )
        _nombre_mayor = f"libro_mayor_{_sufijo_periodo(desde_m, hasta_m)}"
        if codigo_m is not None:
            _nombre_mayor += f"_cuenta_{codigo_m}"
        st.download_button(
            "Exportar PDF",
            data=_pdf_mayor,
            file_name=f"{_nombre_mayor}.pdf",
            mime="application/pdf",
            disabled=df_mayor.empty,
            help=("Genera el Libro Mayor en PDF con el periodo y la cuenta que estas viendo. "
                  "Se deshabilita cuando el filtro no devuelve ningun movimiento."),
        )

        # El Mayor se lee cuenta por cuenta. El codigo y la denominacion identifican la
        # cuenta, asi que van en el encabezado. En cambio la fecha, el numero de asiento
        # y la glosa SI se conservan en la tabla: son el detalle del movimiento, y el
        # saldo acumulado solo tiene sentido avanzando dentro de una misma cuenta.
        if df_mayor.empty:
            with st.container(border=True):
                st.caption("No se encontraron registros con los filtros aplicados.")
        else:
            _COLS_DETALLE_M = ["Fecha", "N° Asiento", "Glosa", "Debe", "Haber", "Saldo"]
            # Orden explicito por cuenta y, dentro de ella, cronologico: es el mismo
            # orden que ya aplica la consulta. Asi los grupos quedan contiguos y el
            # Saldo sigue siendo el acumulado de ESA cuenta.
            _mayor_ordenado = df_mayor.sort_values(
                ["Cuenta PCGE", "Fecha", "N° Asiento"], kind="stable"
            )
            for _codigo_cuenta, _movimientos in _mayor_ordenado.groupby("Cuenta PCGE", sort=True):
                _denominacion_cuenta = _movimientos["Denominación"].iloc[0]
                with st.container(border=True):
                    st.markdown(f"**Cuenta PCGE: {_codigo_cuenta} — {_denominacion_cuenta}**")
                    st.dataframe(
                        _movimientos[_COLS_DETALLE_M],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Fecha": cfg_fecha,
                            "N° Asiento": cfg_asiento,
                            "Glosa": cfg_glosa,
                            "Debe": cfg_debe,
                            "Haber": cfg_haber,
                            "Saldo": cfg_saldo,
                        },
                    )
            if codigo_m is None:
                st.caption("La columna Saldo es el acumulado propio de cada cuenta. "
                           "Selecciona una cuenta para ver su saldo final como indicador.")

elif menu == "Estados Financieros":
    st.title("Estados Financieros")
    st.markdown("Reportes automáticos basados en el Plan Contable General Empresarial (PCGE).")

    # --- PERIODO DEL ESTADO ---
    # Aqui el periodo no es un filtro cosmetico: decide que cifras son correctas.
    # Un Balance con Activos acumulados a la fecha de corte y Patrimonio tambien
    # acumulado, pero Ingresos y Gastos tomados de toda la historia, no cuadra
    # nunca. Por eso los Estados Financieros ahora piden el rango y separan el
    # saldo acumulado del movimiento del periodo.
    _ee_min, _ee_max = lg.obtener_rango_fechas_asientos()
    if _ee_min is not None:
        with st.container(border=True):
            st.markdown("**Periodo del estado**")
            ee1, ee2 = st.columns([2, 3])
            with ee1:
                rango_ee = st.date_input("Periodo", value=(_ee_min, _ee_max),
                                         format="DD/MM/YYYY", key="ee_periodo")
            with ee2:
                st.caption("Las cuentas de Activo, Pasivo y Patrimonio muestran el saldo acumulado a la fecha "
                           "de corte, y el Balance cierra con el resultado acumulado hasta esa misma fecha. El "
                           "Estado de Resultados, en cambio, muestra solo los movimientos del rango (elementos "
                           "6, 7, 8 y 9 del PCGE), porque su saldo historico acumulado no es el resultado del "
                           "periodo.")
        desde_ee, hasta_ee = _periodo(rango_ee)
    else:
        desde_ee = hasta_ee = None
        st.info("Todavía no hay asientos registrados, así que los Estados Financieros no tienen periodo "
                "que recortar. Se mostrara el acumulado historico disponible.")

    df_saldos = lg.obtener_saldos_cuentas(desde_ee, hasta_ee)

    # EL BALANCE NO SE ARMA CON `df_saldos`.
    # El balance es una foto a la fecha de corte, asi que su linea de resultado
    # necesita el resultado ACUMULADO hasta `hasta_ee` y no el del rango elegido: con
    # el rango, la utilidad generada antes del periodo se leia como cero en el
    # balance y la ecuacion Activo = Pasivo + Patrimonio se descuadraba sola.
    # `obtener_saldos_cuentas(None, fecha_hasta)` ya aplica el corte acumulado a
    # TODOS los elementos, resultados incluidos (ver `logica.py`), de modo que aqui no
    # hace falta una segunda regla de calculo: es la misma funcion con otro periodo.
    df_acumulado = lg.obtener_saldos_cuentas(None, hasta_ee)

    tab_balance, tab_resultados = st.tabs(["Balance General", "Estado de Resultados"])

    with tab_balance:
        st.subheader("Estado de Situación Financiera")
        st.info("Posición financiera a la fecha: lo que la empresa posee (Activos) frente a lo que debe y aporta "
                "(Pasivo y Patrimonio). Debe cumplirse Activo = Pasivo + Patrimonio.")

        if not df_saldos.empty:
            # Separamos las cuentas según el PCGE
            activos = df_saldos[df_saldos['elemento'].isin([1, 2, 3])]
            pasivos_patrimonio = df_saldos[df_saldos['elemento'].isin([4, 5])]

            # Resultado del periodo → para el Estado de Resultados
            ingresos_tot_per = df_saldos[df_saldos['elemento'] == 7]['saldo'].sum()
            gastos_tot_per  = df_saldos[df_saldos['elemento'].isin([6, 9])]['saldo'].sum()
            resultado_periodo = ingresos_tot_per - gastos_tot_per

            # Resultado acumulado hasta fecha de corte → para el Balance
            if df_acumulado is not None and not df_acumulado.empty:
                ingresos_acum = df_acumulado[df_acumulado['elemento'] == 7]['saldo'].sum()
                ingresos_acum = ingresos_acum + df_acumulado[df_acumulado['elemento'] == 8]['saldo'].sum()
                gastos_acum   = df_acumulado[df_acumulado['elemento'].isin([6, 9])]['saldo'].sum()
                resultado_acumulado = ingresos_acum - gastos_acum
            else:
                resultado_acumulado = 0.0
            col_activo, col_pasivo = st.columns(2)

            with col_activo:
                st.markdown("### Activos")
                st.dataframe(activos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.success(f"**Total Activos: S/ {activos['saldo'].sum():,.2f}**")

            with col_pasivo:
                st.markdown("### Pasivos y Patrimonio")
                st.dataframe(pasivos_patrimonio[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                if resultado_acumulado != 0:
                    st.caption(f"*Resultado acumulado hasta {hasta_ee.strftime('%d/%m/%Y') if hasattr(hasta_ee,'strftime') else hasta_ee}: S/ {resultado_acumulado:,.2f}*")

                total_p_y_p = pasivos_patrimonio['saldo'].sum() + resultado_acumulado
                st.error(f"**Total Pasivo + Patrimonio: S/ {total_p_y_p:,.2f}**")

            # El PDF se arma con `df_saldos`, el mismo DataFrame ya filtrado por
            # periodo que se esta mostrando, y con los subconjuntos que la pantalla
            # ya calculo. Cero consultas adicionales: asi el documento y la
            # pantalla no pueden discrepar por construccion.
            def _filas_cuenta(subconjunto):
                """`(codigo, descripcion, saldo)` de un subconjunto de df_saldos."""
                return [(r["codigo"], r["descripcion"], float(r["saldo"]))
                        for _, r in subconjunto.iterrows()]

            _pasivos = df_saldos[df_saldos['elemento'] == 4]
            _patrimonio = df_saldos[df_saldos['elemento'] == 5]
            _total_pasivos = round(float(_pasivos['saldo'].sum()), 2)
            _total_patrimonio = round(float(_patrimonio['saldo'].sum()), 2)

            if abs(resultado_acumulado) < 1e-9:
                etiqueta_utilidad = "SIN RESULTADO ACUMULADO"
            elif resultado_acumulado > 0:
                etiqueta_utilidad = "UTILIDAD ACUMULADA"
            else:
                etiqueta_utilidad = "PÉRDIDA ACUMULADA"

            _pdf_balance = reportes_pdf.generar_pdf_balance_general(
                df_acumulado,
                {
                    "periodo": (desde_ee, hasta_ee),
                    "total_activos": round(float(activos['saldo'].sum()), 2),
                    # Mismo importe que muestra el bloque de arriba, para que la
                    # ecuacion del PDF cierre con la cifra que ve el usuario.
                    "total_pasivo_patrimonio": round(total_p_y_p, 2),
                    "utilidad": float(resultado_acumulado),
                    "etiqueta_utilidad": etiqueta_utilidad,
                },
                bloques={
                    # El activo se desglosa por elemento del PCGE, igual que en la
                    # documentacion contable; cada subtotal es la suma de su bloque.
                    "activos": [
                        (lg.ETIQUETAS_ACTIVO.get(int(e), f"Activo {e}"),
                         _filas_cuenta(activos[activos['elemento'] == e]),
                         round(float(activos[activos['elemento'] == e]['saldo'].sum()), 2))
                        for e in sorted(activos['elemento'].unique())
                    ],
                    "pasivos": _filas_cuenta(_pasivos),
                    "patrimonio": _filas_cuenta(_patrimonio),
                    "total_pasivos": _total_pasivos,
                    "total_patrimonio": _total_patrimonio,
                },
            )
            st.download_button(
                "Exportar PDF",
                data=_pdf_balance,
                file_name=f"estado_situacion_financiera_{_sufijo_periodo(desde_ee, hasta_ee)}.pdf",
                mime="application/pdf",
                help="Genera el Estado de Situación Financiera en PDF con el periodo seleccionado.",
            )
        else:
            st.info("Aún no hay registros para procesar el Balance General.")

    with tab_resultados:
        st.subheader("Estado de Resultados Integrales")
        st.info("Resume los ingresos y gastos del periodo para determinar la utilidad o pérdida del ejercicio.")

        if not df_saldos.empty:
            ingresos_df = df_saldos[df_saldos['elemento'] == 7]
            gastos_df = df_saldos[df_saldos['elemento'].isin([6, 9])]

            col_ing, col_gas = st.columns(2)

            with col_ing:
                st.markdown("### Ingresos")
                if not ingresos_df.empty:
                    st.dataframe(ingresos_df[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.success(f"**Total Ingresos: S/ {ingresos_tot_per:,.2f}**")

            with col_gas:
                st.markdown("### Gastos")
                if not gastos_df.empty:
                    st.dataframe(gastos_df[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.error(f"**Total Gastos: S/ {gastos_tot_per:,.2f}**")

            st.markdown("---")
            color_utilidad = "normal" if resultado_periodo >= 0 else "inverse"
            st.metric(label="RESULTADO DEL EJERCICIO (Utilidad / Pérdida)", value=f"S/ {resultado_periodo:,.2f}", delta_color=color_utilidad)

            _pdf_resultados = reportes_pdf.generar_pdf_estado_resultados(
                df_saldos,
                {
                    "periodo": (desde_ee, hasta_ee),
                    "ingresos": [(r["codigo"], r["descripcion"], float(r["saldo"]))
                                 for _, r in ingresos_df.iterrows()],
                    "total_ingresos": float(ingresos_tot_per),
                    "gastos": [(r["codigo"], r["descripcion"], float(r["saldo"]))
                               for _, r in gastos_df.iterrows()],
                    "total_gastos": float(gastos_tot_per),
                    "resultado": float(resultado_periodo),
                },
            )
            st.download_button(
                "Exportar PDF",
                data=_pdf_resultados,
                file_name=f"estado_resultados_integrales_{_sufijo_periodo(desde_ee, hasta_ee)}.pdf",
                mime="application/pdf",
                help="Genera el Estado de Resultados Integrales en PDF con los ingresos y gastos del periodo.",
            )
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
    # Con la base recien inicializada todavia no hay saldo que calcular: las cuatro
    # tarjetas se pintan en cero, que es el valor neutro que corresponde, en vez de
    # dejar la cuarta sin valor definido y reventar la pagina con un NameError.
    utilidad_dashboard = 0.0

    if not df_saldos.empty:
        # Filtramos matemáticamente usando el campo 'elemento' del PCGE
        total_activos = df_saldos[df_saldos['elemento'].isin([1, 2, 3])]['saldo'].sum()
        total_pasivos = df_saldos[df_saldos['elemento'] == 4]['saldo'].sum()
        total_patrimonio = df_saldos[df_saldos['elemento'] == 5]['saldo'].sum()

        ingresos = df_saldos[df_saldos['elemento'] == 7]['saldo'].sum()
        gastos = df_saldos[df_saldos['elemento'].isin([6, 9])]['saldo'].sum()
        utilidad_dashboard = ingresos - gastos
    # Tarjetas de Métricas (KPIs)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(label="Activos Totales", value=f"S/ {total_activos:,.2f}")
    col2.metric(label="Pasivos Totales", value=f"S/ {total_pasivos:,.2f}")
    col3.metric(label="Patrimonio", value=f"S/ {total_patrimonio:,.2f}")
    col4.metric(label="Utilidad del Ejercicio", value=f"S/ {utilidad_dashboard:,.2f}")

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
