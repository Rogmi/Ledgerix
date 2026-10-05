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
    return f"{codigo} - ⚠️ fuera del catálogo PCGE"


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
    st.markdown("### 📝 Borrador para revisión humana")
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
                st.markdown(f"**Asiento #{i + 1} | 📝 {operacion.get('glosa') or 'sin glosa'}**")

            with col_check:
                aprobar = st.checkbox("Aprobar", value=True, key=f"chk_aprobar_{clave}")

            # --- FECHA: siempre editable. Si la IA no la encontró, se avisa y se
            # deja vacía; el usuario la ingresa. Nunca se rellena sola.
            if fecha_ia is None:
                st.warning("⚠️ FECHA NO ENCONTRADA EN EL DOCUMENTO. Ingresa la fecha real "
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
                st.info(f"↩️ Sin aprobar: este asiento se descartará{detalle}.")
            elif problemas:
                st.error("❌ Asiento #%d no se puede guardar: %s." % (i + 1, "; ".join(problemas)))
                bloqueos.append(f"Asiento #{i + 1}")
            else:
                st.success(f"✅ Partida doble cuadrada (S/ {total_debe:,.2f}) — listo para guardar")
                a_guardar.append({"fecha": fecha_elegida, "glosa": operacion.get("glosa") or "Asiento sin glosa",
                                  "movimientos": movimientos})

    st.markdown("---")
    if bloqueos:
        st.error("❌ No se puede guardar: corrige " + ", ".join(bloqueos)
                 + " o desmarca 'Aprobar' para descartarlos.")
    st.caption(f"{len(a_guardar)} asiento(s) aprobados y validados, listos para persistir.")

    if st.button("💾 Guardar Asientos Aprobados en BD", type="primary",
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
            st.success(f"✅ Se registraron {exitos} asientos contables en la Base de Datos.")
            st.balloons()
        for error in errores:
            st.error(f"❌ No se pudo registrar: {error}")
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


# 1. CONFIGURACIÓN DE LA PÁGINA (Debe ser la primera línea de código)

st.set_page_config(
    page_title="Ledgerix | Sistema Contable",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 2. BARRA LATERAL (NAVEGACIÓN CORPORATIVA)
st.sidebar.title("Ledgerix 📈")
st.sidebar.markdown("---")
menu = st.sidebar.radio(
    "Navegación del Sistema",
    ["📊 Dashboard Gerencial", "✍️ Registro de Transacciones", "📑 Estados Financieros"]
)
st.sidebar.markdown("---")
st.sidebar.caption("Ledgerix SaaS - Versión 1.0")

# 3. ENRUTAMIENTO DE MÓDULOS

if menu == "📊 Dashboard Gerencial":
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

elif menu == "✍️ Registro de Transacciones":
    st.title("Registro de Transacciones")
    st.markdown("Ingresa nuevos asientos contables mediante captura manual o el Asistente IA.")
    
    # Redujimos las pestañas a 2: Manual y el Motor Central IA
    tab_manual, tab_ia = st.tabs(["✍️ Ingreso Manual", "🤖 Asistente IA (Gemini)"])
    
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
                st.error("⚠️ La glosa es obligatoria.")
            else:
                detalles_asiento = []
                for index, row in df_editado.iterrows():
                    if pd.notna(row['Cuenta']): 
                        codigo_cuenta = row['Cuenta'].split(" - ")[0] 
                        val_debe = float(row['Debe']) if pd.notna(row['Debe']) else 0.0
                        val_haber = float(row['Haber']) if pd.notna(row['Haber']) else 0.0
                        
                        detalles_asiento.append({'cuenta': codigo_cuenta, 'debe': val_debe, 'haber': val_haber})
                        
                if len(detalles_asiento) < 2:
                    st.error("❌ El asiento debe tener al menos dos movimientos.")
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
        st.subheader("🤖 Asistente IA Contable (Gemini)")
        st.markdown("La Inteligencia Artificial analizará el contexto, identificará las cuentas del PCGE y calculará la partida doble automáticamente.")
        
        ia_metodo = st.radio(
            "Selecciona el método de captura:", 
            ["📝 Enunciado de Texto", "📂 Carga de Documentos (Excel, PDF, Word)", "🎙️ Dictado por Voz", "📸 Escáner Visual"], 
            horizontal=True
        )
        st.markdown("---")
        
        if ia_metodo == "📝 Enunciado de Texto":
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
                    with st.spinner("🤖 Gemini está analizando el caso aplicando el PCGE..."):
                        exito_ia, resultado_ia = ia.extraer_asiento_de_texto(enunciado_input)
                        
                        if not exito_ia:
                            st.error(f"❌ Error de procesamiento: {resultado_ia}")
                        elif not isinstance(resultado_ia, list) or not resultado_ia:
                            st.error("❌ La IA no devolvió partidas utilizables para este enunciado.")
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
        
        elif ia_metodo == "📂 Carga de Documentos (Excel, PDF, Word)":
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
                    st.caption(f"🧹 Se eliminaron {informe['control_eliminados']:,} caracteres de "
                               "control del documento antes de enviarlo a la IA.")
                if not texto_para_ia:
                    st.error("El documento no tiene texto legible. Si es un escaneo, "
                             "necesita OCR previo; si es un Excel, revisa que tenga contenido.")
                else:
                    st.caption(f"Documento listo para la IA: {informe['caracteres_enviados']:,} caracteres "
                               f"(original: {informe['caracteres_originales']:,}).")
                    if informe["recortado"]:
                        st.warning(f"⚠️ El documento supera el límite de {MAX_CHARS_PARA_IA:,} caracteres que "
                                   "acepta una petición de la API. Se enviará leído por bloques, en orden y "
                                   "sin descartar ningún contenido.")

                    # Mostramos un fragmento de lo que leyó el sistema
                    with st.expander("Ver texto limpio que se enviará a la IA"):
                        st.text_area("Texto enviado a la IA:", texto_para_ia[:1500] + "\n\n... (continúa)",
                                     height=200, disabled=True)

                    # 3. BOTÓN DE IA (el texto que se previsualiza es el que se envía)
                if st.button("✨ Generar Borrador con IA", type="primary", use_container_width=True):
                    with st.spinner("📄 Extrayendo filas del documento y armando la partida doble..."):
                        # Mandamos EXACTAMENTE el texto ya saneado y previsualizado
                        exito_ia, lote_operaciones = ia.analizar_excel_completo(texto_para_ia)
                        
                        if exito_ia:
                            # El lote crudo va al borrador comun: cada elemento es un
                            # asiento y sus partidas se conservan tal cual llegaron.
                            cargar_borrador(lote_operaciones, "documento cargado")
                            filas = sum(len(op["asiento"]) for op in lote_operaciones)
                            st.session_state.avisos_borrador = getattr(lote_operaciones, "avisos", [])
                            st.session_state.resumen_borrador = (
                                f"📄 {filas} fila(s) transcrita(s) en {len(lote_operaciones)} asiento(s)."
                            )
                            st.rerun()
                        else:
                            st.error(lote_operaciones)

        elif ia_metodo == "🎙️ Dictado por Voz":
            st.markdown("**Reconocimiento de Voz a Texto**")
            if st.button("🎤 Iniciar Grabación (Simulación)", type="secondary", use_container_width=True):
                st.warning("Próximamente: Integración del micrófono web.")
                
        elif ia_metodo == "📸 Escáner Visual":
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
                ["📂 Subir Archivo", "📸 Usar Cámara"],
                horizontal=True,
            )

            # Las condiciones comparan contra el VALOR de la opcion, no contra su
            # posicion, asi que invertir la lista no obliga a reordenar este bloque.
            imagen_final = None
            if origen_imagen == "📂 Subir Archivo":
                imagen_final = st.file_uploader("📂 Sube una imagen:", type=["png", "jpg", "jpeg"])
            elif origen_imagen == "📸 Usar Cámara":
                imagen_final = st.camera_input("📸 Tomar foto (Cámara)")

            if imagen_final:
                st.image(imagen_final, caption="Documento listo para analizar", width=350)

                if st.button("✨ Escanear Imagen y Extraer Asiento", type="primary", use_container_width=True):
                    with st.spinner("👁️ Llama 3.2 Vision está analizando el comprobante..."):
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
                                f"📸 {filas} partida(s) extraída(s) visualmente en {len(lote_operaciones)} asiento(s)."
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
            st.warning(f"⚠️ {aviso}")
        render_borrador(origen_borrador or "documento")

elif menu == "📑 Estados Financieros":
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
