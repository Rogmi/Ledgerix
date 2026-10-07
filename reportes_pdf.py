"""
REPORTES PDF DE LEDGERIX — maquetacion de los documentos contables formales.

Que hace este modulo
-------------------
Construye, en memoria, los cuatro documentos que el sistema puede exportar:
Libro Diario, Libro Mayor, Estado de Situacion Financiera y Estado de Resultados
Integrales. Los documentos tienen estructura propia: no son una captura de la
interfaz de Streamlit, ni una impresion de un DataFrame.

Que NO hace, y por que
----------------------
Este modulo es deliberadamente tonto. No importa Streamlit, no abre SQLite, no
ejecuta SQL y no contiene regla contable alguna:

  * no lee la base de datos, porque los datos llegan ya filtrados en el
    DataFrame que la pantalla esta pintando. Que el PDF y la pantalla digan lo
    mismo no es una coincidencia: es que salen del mismo objeto;
  * no clasifica cuentas por elemento ni decide que es activo y que es pasivo,
    porque esa regla vive en `logica.py` y en la interfaz. Aqui solo se.maquetan
    las filas que le entregan, en el orden en que las entregan;
  * no recalcula saldos ni cuadres. El `Saldo` del Libro Mayor y los totales del
    Libro Diario llegan calculados por la logica contable y se imprimen tal cual.

Recibe DataFrames y un diccionario de contexto ya resuelto, y devuelve `bytes`.

Decisiones de diseno
--------------------
FUENTE. Las tipografias base de reportlab (Helvetica) se dibujan con la
codificacion WinAnsi (cp1252), que cubre de sobra el castellano del catalogo
PCGE: acentos, enye, ene,atia, simbolos y hasta el guion largo. Comprobado
contra el texto extraido del PDF generado. No hace falta registrar ninguna
fuente TrueType adicional, ni anadir una dependencia por eso.

Lo que si hace falta es `_pdf_texto`: una glosa puede traer un emoji o un
caracter chino entered a mano y reportlab lo dibujaria como un gluco roto.
Todo texto pasa por ahi y se degrada a '?', que es preferible a un garbage.

PAGINACION. Se usa `LongTable` y no `Table`: `LongTable` reparte las filas entre
paginas y, con `repeatRows`, repite la cabecera en cada una. En el Libro Diario
el encabezado del asiento (numero, fecha y glosa) es una fila `SPAN` de la misma
tabla, no un parrafo suelto encima: asi viaja con el detalle y se repite al
saltar de pagina. Un asiento partido entre dos hojas conserva su contexto.

PAGINAS "X de Y". reportlab no sabe cuantas paginas saldran hasta que las ha
dibujado todas. `_CanvasNumerado` guarda cada pagina y reproduce el pie al
final, cuando el total ya se conoce.
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ============================================================================
# IDENTIDAD DEL DOCUMENTO
# ============================================================================
MARCA = "LEDGERIX"
DESCRIPCION_MARCA = "Sistema Contable"
CABECERA_DERECHA = "Grupo 7 · Sistema y Gestión Financiera · UNI"

# ============================================================================
# PALETA
# ============================================================================
# Centralizada a proposito. La interfaz es oscura (`#0B0F19`), pero un PDF se
# imprime y se fotocopia en blanco y negro: aqui el fondo es blanco, el texto
# casi negro y el acento es el verde primario del proyecto. El color nunca es
# el unico portador de significado: una utilidad y una perdida tambien se
# distinguen por su rótulo.
PALETA = {
    "tinta": colors.HexColor("#0F172A"),
    "tinta_suave": colors.HexColor("#64748B"),
    "acento": colors.HexColor("#10B981"),
    "acento_oscuro": colors.HexColor("#047857"),
    "cabecera": colors.HexColor("#F1F5F9"),
    "franja": colors.HexColor("#E2E8F0"),
    "fila_alterna": colors.HexColor("#F8FAFC"),
    "linea": colors.HexColor("#CBD5E1"),
    "alerta": colors.HexColor("#B91C1C"),
    "alerta_fondo": colors.HexColor("#FEF2F2"),
    "papel": colors.white,
}

# ============================================================================
# PAGINA Y TIPOGRAFIA
# ============================================================================
MARGEN_IZQUIERDO = 18 * mm
MARGEN_DERECHO = 18 * mm
MARGEN_SUPERIOR = 26 * mm
MARGEN_INFERIOR = 22 * mm

ANCHO_PAGINA, ALTO_PAGINA = A4
ANCHO_UTIL = ANCHO_PAGINA - MARGEN_IZQUIERDO - MARGEN_DERECHO

FUENTE = "Helvetica"
FUENTE_NEGRITA = "Helvetica-Bold"

# Una glosa puede venir de un documento escaneado y medir cientos de caracteres.
# Dentro de una celda eso produce una fila mas alta que la pagina y reportlab
# lanza "Flowable too large". Se acota con un tope generoso: ninguna glosa real
# de un asiento contable llega aqui, y si llega, se marca el corte con puntos.
GLOSA_MAX_CARACTERES = 480

_ESTILOS = None


def _estilos():
    """Estilos `Paragraph` del sistema. Se construyen una vez y se reutilizan."""
    global _ESTILOS
    if _ESTILOS is not None:
        return _ESTILOS

    comun = dict(fontName=FUENTE, fontSize=8.5, leading=10.5,
                 textColor=PALETA["tinta"], spaceBefore=0, spaceAfter=0)

    _ESTILOS = {
        "titulo": ParagraphStyle("titulo", fontName=FUENTE_NEGRITA, fontSize=17, leading=20,
                                 textColor=PALETA["tinta"], spaceAfter=1.5 * mm),
        "subtitulo": ParagraphStyle("subtitulo", fontName=FUENTE, fontSize=9.5, leading=12.5,
                                    textColor=PALETA["tinta_suave"], spaceAfter=4.5 * mm),
        "seccion": ParagraphStyle("seccion", fontName=FUENTE_NEGRITA, fontSize=9.6, leading=12,
                                  textColor=PALETA["acento_oscuro"],
                                  spaceBefore=4.5 * mm, spaceAfter=2.5 * mm),
        "asiento": ParagraphStyle("asiento", fontName=FUENTE_NEGRITA, fontSize=8.6, leading=11,
                                  textColor=PALETA["tinta"], spaceBefore=0, spaceAfter=0),
        "cabecera": ParagraphStyle("cabecera", fontName=FUENTE_NEGRITA, fontSize=8, leading=10,
                                   textColor=PALETA["tinta"], spaceBefore=0, spaceAfter=0),
        "cabecera_num": ParagraphStyle("cabecera_num", fontName=FUENTE_NEGRITA, fontSize=8,
                                       leading=10, textColor=PALETA["tinta"],
                                       alignment=TA_RIGHT, spaceBefore=0, spaceAfter=0),
        "etiqueta": ParagraphStyle("etiqueta", fontName=FUENTE_NEGRITA, fontSize=7.6, leading=9.5,
                                   textColor=PALETA["tinta_suave"], spaceBefore=0, spaceAfter=0),
        "valor": ParagraphStyle("valor", fontName=FUENTE, fontSize=8.6, leading=11,
                                textColor=PALETA["tinta"], spaceBefore=0, spaceAfter=0),
        "celda": ParagraphStyle("celda", **comun, splitLongWords=1),
        "vacia": ParagraphStyle("vacia", fontName=FUENTE, fontSize=8, leading=10,
                                textColor=PALETA["tinta_suave"], spaceBefore=0, spaceAfter=0),
        "moneda": ParagraphStyle("moneda", **comun, alignment=TA_RIGHT),
        "moneda_fuerte": ParagraphStyle("moneda_fuerte", fontName=FUENTE_NEGRITA, fontSize=8.5,
                                        leading=10.5, textColor=PALETA["tinta"],
                                        alignment=TA_RIGHT, spaceBefore=0, spaceAfter=0),
        "total_texto": ParagraphStyle("total_texto", fontName=FUENTE_NEGRITA, fontSize=8.6,
                                      leading=11, textColor=PALETA["tinta"],
                                      spaceBefore=0, spaceAfter=0),
        "alerta": ParagraphStyle("alerta", fontName=FUENTE_NEGRITA, fontSize=8.6, leading=11,
                                 textColor=PALETA["alerta"], spaceBefore=0, spaceAfter=0),
        "nota": ParagraphStyle("nota", fontName=FUENTE, fontSize=7.6, leading=9.8,
                               textColor=PALETA["tinta_suave"], alignment=TA_JUSTIFY,
                               spaceBefore=1.5 * mm, spaceAfter=0),
    }
    return _ESTILOS


# ============================================================================
# FORMATO
# ============================================================================
def _pdf_texto(valor):
    """
    Deja el texto en algo que Helvetica pueda dibujar.

    Las tipografias base de reportlab escriben en cp1252. Todo lo que el
    castellano del PCGE usa cabe (acentos, enye, ene, atia, guion largo), pero
    un emoji o un caracter chino en una glosa no cabe: reportlab no lanza error,
    simplemente pinta un gluco roto. Aqui se sustituye por '?', que al menos se
    ve que hay algo ahi.
    """
    if valor is None:
        return ""
    try:
        if valor != valor:  # NaN, sea del tipo que sea
            return ""
    except (TypeError, ValueError):
        pass
    return str(valor).encode("cp1252", "replace").decode("cp1252")


def _a_float(valor):
    """
    Convierte a float sin dejar pasar NaN.

    Un NaN en una suma de importes se propaga en silencio a todos los totales y
    el PDF acaba mostrando `S/ nan`. Una celda vacia o no numerica es 0.0.
    """
    if valor is None:
        return 0.0
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if numero != numero else numero


def _moneda(valor):
    """`S/ 1,234.56`. Formato de importe del sistema, identico al de la pantalla."""
    return f"S/ {_a_float(valor):,.2f}"


def _fecha(valor):
    """`dd/mm/aaaa`. Acepta `date`, `datetime` o el texto ISO que entrega SQLite."""
    if valor is None or valor == "":
        return ""
    if isinstance(valor, str):
        try:
            valor = datetime.strptime(valor.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return _pdf_texto(valor)
    try:
        return valor.strftime("%d/%m/%Y")
    except (AttributeError, ValueError):
        return _pdf_texto(valor)


def _fecha_hora(valor):
    return valor.strftime("%d/%m/%Y %H:%M")


def _periodo(contexto):
    """`(desde, hasta)` siempre como tupla, aunque el contexto venga sin periodo."""
    periodo = (contexto or {}).get("periodo") or (None, None)
    if isinstance(periodo, (list, tuple)) and len(periodo) == 2:
        return periodo[0], periodo[1]
    return (periodo, None)


def _periodo_texto(contexto):
    """`dd/mm/aaaa - dd/mm/aaaa`, o el texto que corresponda si no hay periodo."""
    desde, hasta = _periodo(contexto)
    desde, hasta = _fecha(desde), _fecha(hasta)
    if desde and hasta:
        return f"{desde} – {hasta}"
    if hasta:
        return f"Acumulado hasta {hasta}"
    if desde:
        return f"Desde {desde}"
    return "Todo el histórico registrado"


def _entero(valor):
    try:
        return f"{int(valor):,}"
    except (TypeError, ValueError):
        return _pdf_texto(valor)


def _texto_largo(valor, limite=GLOSA_MAX_CARACTERES):
    """Acota un texto de celda y deja constancia del corte."""
    texto = _pdf_texto(valor).strip()
    if len(texto) <= limite:
        return texto
    return texto[:limite].rstrip() + " […]"


def _importe(valor):
    """Celda de importe ya formateada, alineada a la derecha."""
    return Paragraph(_moneda(valor), _estilos()["moneda"])


def _importe_fuerte(valor):
    return Paragraph(_moneda(valor), _estilos()["moneda_fuerte"])


def _vacia(texto):
    """Celda de texto que explica que no hay filas, sin fingir que hay un total."""
    return Paragraph(_pdf_texto(texto), _estilos()["vacia"])


def _nombre_de_estilo(celda):
    """
    Nombre del estilo de una celda, o None si la celda no es un `Paragraph`.

    Las tablas de los estados se arman mezclando celdas `Paragraph` y cadenas
    vacias (un `SPAN` necesita que la celda exista). Preguntar por el estilo sin
    este filtro lanzaria `AttributeError` sobre las cadenas.
    """
    return celda.style.name if isinstance(celda, Paragraph) else None


# ============================================================================
# CANVAS Y PAGINADO
# ============================================================================
class _CanvasNumerado(canvas.Canvas):
    """
    Canvas que difiere el pie de pagina hasta conocer el total de paginas.

    "Página 3 de ?" no sirve. reportlab no sabe cuantas paginas va a producir
    hasta que ya las dibujó todas, asi que esta clase guarda el estado de cada
    pagina en `showPage` y lo reproduce en `save`, ya con el total conocido.
    """

    def __init__(self, *args, **kwargs):
        pie = kwargs.pop("pie", "")
        marca_tiempo = kwargs.pop("marca_tiempo", None)
        # El titulo viaja en el canvas porque el encabezado se dibuja desde
        # `onLaterPages`, que solo recibe (canvas, doc) y no el contexto del
        # reporte. Sin el, la pagina 2 de un Libro Mayor de 30 paginas no dice
        # de que reporte es: el papel suelto no se puede archivar ni identificar.
        self._titulo_documento = kwargs.pop("titulo", "")
        self._pie_documento = pie
        self._marca_tiempo = marca_tiempo or datetime.now()
        self._margen_izquierdo = MARGEN_IZQUIERDO
        self._ancho_pagina = ANCHO_PAGINA
        super().__init__(*args, **kwargs)
        self._paginas = []

    def showPage(self):
        self._paginas.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._paginas) or 1
        for estado in self._paginas:
            self.__dict__.update(estado)
            _dibujar_pie(self, total, self._pie_documento, self._marca_tiempo)
            super().showPage()
        super().save()


def _dibujar_encabezado(cnv, doc):
    """Encabezado discreto, repetido en todas las paginas."""
    cnv.saveState()
    y = ALTO_PAGINA - 15 * mm
    cnv.setStrokeColor(PALETA["linea"])
    cnv.setLineWidth(0.6)
    cnv.line(MARGEN_IZQUIERDO, y, ANCHO_PAGINA - MARGEN_DERECHO, y)

    cnv.setFont(FUENTE_NEGRITA, 7.4)
    cnv.setFillColor(PALETA["tinta"])
    cnv.drawString(MARGEN_IZQUIERDO, y + 2.6 * mm, MARCA)
    cnv.setFont(FUENTE, 7.4)
    cnv.setFillColor(PALETA["tinta_suave"])
    # El desplazamiento del subtitulo sale del ancho REAL de la marca, no de una
    # estimacion por numero de letras: "LEDGERIX" y "Ledgerix" ocupan distinto.
    cnv.drawString(MARGEN_IZQUIERDO + cnv.stringWidth(MARCA, FUENTE_NEGRITA, 7.4) + 1.6 * mm,
                   y + 2.6 * mm, DESCRIPCION_MARCA)

    cnv.setFont(FUENTE, 7)
    cnv.drawRightString(ANCHO_PAGINA - MARGEN_DERECHO, y + 2.6 * mm, CABECERA_DERECHA)

    # Nombre del reporte en la esquina opuesta. Se dibuja tambien en la pagina 1,
    # donde el titulo grande ya aparece en el cuerpo, para que el encabezado sea
    # identico en todas las hojas y el papel suelto se pueda identificar.
    titulo = getattr(cnv, "_titulo_documento", "")
    if titulo:
        cnv.setFont(FUENTE_NEGRITA, 7.4)
        cnv.setFillColor(PALETA["tinta_suave"])
        cnv.drawRightString(ANCHO_PAGINA - MARGEN_DERECHO, y - 4.4 * mm, titulo)
    cnv.restoreState()


def _dibujar_pie(cnv, total, pie_documento, marca_tiempo):
    """Pie de pagina con el folio real y la hora de emision."""
    cnv.saveState()
    y = MARGEN_INFERIOR - 12 * mm
    cnv.setStrokeColor(PALETA["linea"])
    cnv.setLineWidth(0.6)
    cnv.line(MARGEN_IZQUIERDO, y, ANCHO_PAGINA - MARGEN_DERECHO, y)

    cnv.setFont(FUENTE, 7)
    cnv.setFillColor(PALETA["tinta_suave"])
    cnv.drawString(MARGEN_IZQUIERDO, y - 3.8 * mm, pie_documento)
    cnv.drawCentredString(ANCHO_PAGINA / 2, y - 3.8 * mm,
                          f"Emitido el {_fecha_hora(marca_tiempo)}")
    cnv.setFont(FUENTE_NEGRITA, 7.4)
    cnv.setFillColor(PALETA["tinta"])
    cnv.drawRightString(ANCHO_PAGINA - MARGEN_DERECHO, y - 3.8 * mm,
                        f"Página {cnv.getPageNumber()} de {total}")
    cnv.restoreState()


def _construir_pdf(elementos, titulo_pdf, pie):
    """Ensambla el documento en un `BytesIO` y devuelve los bytes."""
    buffer = io.BytesIO()
    marca_tiempo = datetime.now()

    documento = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGEN_IZQUIERDO,
        rightMargin=MARGEN_DERECHO,
        topMargin=MARGEN_SUPERIOR,
        bottomMargin=MARGEN_INFERIOR,
        title=titulo_pdf,
        author=f"{MARCA} · {DESCRIPCION_MARCA}",
        subject=titulo_pdf,
        creator=MARCA,
    )

    def _canvas(*args, **kwargs):
        return _CanvasNumerado(*args, pie=pie, titulo=titulo_pdf,
                               marca_tiempo=marca_tiempo, **kwargs)

    documento.build(elementos,
                    onFirstPage=_dibujar_encabezado,
                    onLaterPages=_dibujar_encabezado,
                    canvasmaker=_canvas)
    return buffer.getvalue()


# ============================================================================
# PIEZAS COMUNES
# ============================================================================
def _titulo(titulo, subtitulo):
    est = _estilos()
    piezas = [Paragraph(_pdf_texto(titulo), est["titulo"])]
    if subtitulo:
        piezas.append(Paragraph(_pdf_texto(subtitulo), est["subtitulo"]))
    return piezas


def _seccion(texto):
    return Paragraph(_pdf_texto(texto), _estilos()["seccion"])


def _nota(texto):
    return Paragraph(_pdf_texto(texto), _estilos()["nota"])


def _tabla_contexto(pares):
    """
    Bloque de contexto en dos columnas (concepto / valor).

    Es lo que da al documento su encuadre: que periodo cubre, que filtro se
    aplico y cuantas filas contiene. Sin esto el PDF seria una tabla suelta
    sin ninguna manera de saber que esta mirando.
    """
    est = _estilos()
    filas = [[Paragraph(_pdf_texto(etiqueta), est["etiqueta"]),
              Paragraph(_pdf_texto(valor), est["valor"])]
             for etiqueta, valor in pares if valor is not None]

    tabla = Table(filas, colWidths=[ANCHO_UTIL * 0.27, ANCHO_UTIL * 0.73], hAlign="LEFT")
    tabla.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.1 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.1 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, PALETA["franja"]),
    ]))
    return tabla


def _tabla_resumen(debe, haber, diferencia, etiqueta_diferencia="Diferencia"):
    """Franja de tres importes: Total Debe, Total Haber y su diferencia."""
    est = _estilos()
    cabecera = [Paragraph("Total Debe", est["cabecera"]),
                Paragraph("Total Haber", est["cabecera"]),
                Paragraph(etiqueta_diferencia, est["cabecera"])]
    valores = [_importe_fuerte(debe), _importe_fuerte(haber), _importe_fuerte(diferencia)]

    tabla = Table([cabecera, valores], colWidths=[ANCHO_UTIL / 3.0] * 3, hAlign="LEFT")
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PALETA["cabecera"]),
        ("BOX", (0, 0), (-1, -1), 0.5, PALETA["linea"]),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, PALETA["linea"]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.4 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.4 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.4 * mm),
    ]))
    return tabla


def _aviso_descuadre(diferencia):
    """Caja de advertencia cuando Debe y Haber no coinciden."""
    est = _estilos()
    texto = (
        f"DESCUADRE: el Debe y el Haber del conjunto seleccionado difieren en "
        f"{_moneda(abs(diferencia))}. El documento reproduce fielmente los importes "
        f"registrados en la base de datos; la diferencia no ha sido corregida ni "
        f"ocultada por este reporte."
    )
    tabla = Table([[Paragraph(_pdf_texto(texto), est["alerta"])]],
                  colWidths=[ANCHO_UTIL], hAlign="LEFT")
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALETA["alerta_fondo"]),
        ("BOX", (0, 0), (-1, -1), 0.7, PALETA["alerta"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
    ]))
    return tabla


def _vacio(mensaje):
    """Documento sin datos: contexto completo y aviso, nunca una excepcion."""
    est = _estilos()
    caja = Table([[Paragraph(_pdf_texto(mensaje), est["nota"])]],
                 colWidths=[ANCHO_UTIL], hAlign="LEFT")
    caja.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PALETA["cabecera"]),
        ("BOX", (0, 0), (-1, -1), 0.5, PALETA["linea"]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
    ]))
    return caja


def _estilo_tabla(base):
    """Estilo comun de las tablas de detalle: rejilla discreta y filas alternas."""
    comandos = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.1 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.1 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, PALETA["franja"]),
        ("BOX", (0, 0), (-1, -1), 0.5, PALETA["linea"]),
    ]
    for fila in range(1, len(base)):
        if fila % 2 == 0:
            comandos.append(("BACKGROUND", (0, fila), (-1, fila), PALETA["fila_alterna"]))
    return TableStyle(comandos)


# ============================================================================
# 1. LIBRO DIARIO
# ============================================================================
# "Cuenta PCGE" y "N° Asiento" tienen que caber en una sola linea a 8pt: si el
# ancho no alcanza, reportlab las parte en dos y la cabecera queda cojea.
COLS_DIARIO = (0.145, 0.485, 0.185, 0.185)


def _tabla_asiento(numero, fecha, glosa, partidas):
    """
    Tabla de un asiento: encabezado `SPAN` + cabecera de columnas + partidas + total.

    El encabezado va DENTRO de la tabla a proposito. Si se pusiera como un
    parrafo encima, un asiento partido entre dos paginas perderia su numero y su
    glosa en la segunda hoja, que es justo el contexto que hace falta para
    entender las partidas que siguen.
    """
    est = _estilos()
    titulo_asiento = (
        f"Asiento N° {numero}"
        f"     |     {_fecha(fecha) if fecha else 'sin fecha'}"
        f"     |     {_texto_largo(glosa, 300)}"
    )

    filas = [
        [Paragraph(_pdf_texto(titulo_asiento), est["asiento"]), "", "", ""],
        [Paragraph("Cuenta PCGE", est["cabecera"]),
         Paragraph("Denominación", est["cabecera"]),
         Paragraph("Debe", est["cabecera_num"]),
         Paragraph("Haber", est["cabecera_num"])],
    ]

    total_debe = total_haber = 0.0
    for partida in partidas:
        debe = _a_float(partida.get("Debe"))
        haber = _a_float(partida.get("Haber"))
        total_debe += debe
        total_haber += haber
        filas.append([
            Paragraph(_pdf_texto(partida.get("Cuenta PCGE")), est["celda"]),
            Paragraph(_texto_largo(partida.get("Denominación")), est["celda"]),
            _importe(debe),
            _importe(haber),
        ])

    filas.append([
        Paragraph("Total del asiento", est["total_texto"]), "",
        _importe_fuerte(total_debe), _importe_fuerte(total_haber),
    ])

    tabla = LongTable(filas, colWidths=[ANCHO_UTIL * f for f in COLS_DIARIO],
                      repeatRows=2, splitByRow=1, hAlign="LEFT")
    estilo = _estilo_tabla(filas)
    estilo.add("SPAN", (0, 0), (-1, 0))
    estilo.add("BACKGROUND", (0, 0), (-1, 0), PALETA["cabecera"])
    estilo.add("LINEBELOW", (0, 0), (-1, 0), 0.7, PALETA["acento"])
    estilo.add("BACKGROUND", (0, 1), (-1, 1), PALETA["cabecera"])
    estilo.add("TOPPADDING", (0, 0), (-1, 0), 1.6 * mm)
    estilo.add("BOTTOMPADDING", (0, 0), (-1, 0), 1.6 * mm)
    estilo.add("SPAN", (0, -1), (1, -1))
    estilo.add("BACKGROUND", (0, -1), (-1, -1), PALETA["franja"])
    estilo.add("LINEABOVE", (0, -1), (-1, -1), 0.7, PALETA["tinta_suave"])
    tabla.setStyle(estilo)
    return tabla


def generar_pdf_libro_diario(df, contexto=None):
    """
    Libro Diario en PDF, a partir del mismo DataFrame que pinta la pantalla.

    Parametros
    ----------
    df : DataFrame con las columnas que ya usa la pantalla: "Fecha",
        "N° Asiento", "Glosa", "Cuenta PCGE", "Denominación", "Debe", "Haber".
    contexto : dict con "periodo" (tupla desde/hasta), "cuenta", "glosa",
        "total_asientos", "total_partidas", "total_debe", "total_haber".

    Devuelve los `bytes` del PDF. Un DataFrame vacio produce un documento con el
    contexto y un aviso, no una excepcion.
    """
    contexto = dict(contexto or {})
    df = df if df is not None else pd.DataFrame()

    elementos = _titulo(
        "LIBRO DIARIO",
        "Detalle cronológico de los asientos contables registrados, agrupados por número de asiento.")

    if "N° Asiento" in df.columns and not df.empty:
        total_debe = float(df["Debe"].sum())
        total_haber = float(df["Haber"].sum())
        total_asientos = int(df["N° Asiento"].nunique())
    else:
        total_debe = total_haber = 0.0
        total_asientos = 0

    diferencia = round(total_debe - total_haber, 2)

    elementos += [
        _tabla_contexto([
            ("Periodo", _periodo_texto(contexto)),
            ("Cuenta PCGE", contexto.get("cuenta") or "Todas las cuentas"),
            ("Búsqueda en glosa", contexto.get("glosa") or "Sin filtro"),
            ("Asientos", _entero(total_asientos)),
            ("Partidas", _entero(contexto.get("total_partidas", 0 if df.empty else len(df)))),
            ("Origen de los datos", "Base de datos contable · consultas de solo lectura"),
        ]),
        Spacer(1, 3 * mm),
        _seccion("Resumen del conjunto seleccionado"),
        _tabla_resumen(total_debe, total_haber, diferencia),
    ]

    if diferencia != 0:
        elementos += [Spacer(1, 2.5 * mm), _aviso_descuadre(diferencia)]

    if df.empty:
        elementos += [Spacer(1, 5 * mm), _seccion("Detalle de asientos"),
                      _vacio("No se encontraron asientos que cumplan los filtros seleccionados.")]
        return _construir_pdf(elementos, "Libro Diario", f"{MARCA} · Libro Diario")

    elementos.append(_seccion("Detalle de asientos"))

    # El orden lo fija la consulta (cronológico, y dentro del asiento el orden de
    # las partidas). Se reordena igual que en pantalla para que ambos documentos
    # cuenten la historia en el mismo orden.
    ordenado = df.sort_values(["Fecha", "N° Asiento"], kind="stable")
    for numero, partidas in ordenado.groupby("N° Asiento", sort=True):
        cabecera = partidas.iloc[0]
        elementos.append(_tabla_asiento(numero, cabecera.get("Fecha"),
                                        cabecera.get("Glosa"),
                                        partidas.to_dict("records")))
        elementos.append(Spacer(1, 2.6 * mm))

    elementos += [
        _seccion("Totales generales"),
        _tabla_resumen(total_debe, total_haber, diferencia, "Diferencia final"),
        _nota(
            "Cifras en soles. El N° de asiento es el correlativo real de la base de datos "
            "(tabla Asientos) y la denominación de cada cuenta procede del catálogo PCGE "
            "(tabla Cuentas). Este documento es una proyección de solo lectura: no crea, "
            "modifica ni recalcula ningún asiento."
        ),
    ]
    if diferencia != 0:
        elementos.append(_aviso_descuadre(diferencia))

    return _construir_pdf(elementos, "Libro Diario", f"{MARCA} · Libro Diario")


# ============================================================================
# 2. LIBRO MAYOR
# ============================================================================
COLS_MAYOR = (0.125, 0.115, 0.33, 0.1433, 0.1434, 0.1433)


def _tabla_cuenta(codigo, denominacion, naturaleza, movimientos):
    """Tabla de una cuenta: cabecera, movimientos con Saldo y pie de la cuenta."""
    est = _estilos()
    encabezado = f"Cuenta PCGE {codigo} — {denominacion}"
    if naturaleza:
        encabezado += f"     |     Naturaleza: {naturaleza}"

    filas = [
        [Paragraph(_pdf_texto(encabezado), est["asiento"]), "", "", "", "", ""],
        [Paragraph("Fecha", est["cabecera"]),
         Paragraph("N° Asiento", est["cabecera_num"]),
         Paragraph("Glosa", est["cabecera"]),
         Paragraph("Debe", est["cabecera_num"]),
         Paragraph("Haber", est["cabecera_num"]),
         Paragraph("Saldo", est["cabecera_num"])],
    ]

    total_debe = total_haber = 0.0
    saldo_final = 0.0
    for movimiento in movimientos:
        debe = _a_float(movimiento.get("Debe"))
        haber = _a_float(movimiento.get("Haber"))
        total_debe += debe
        total_haber += haber
        # El Saldo se imprime TAL COMO LLEGA. Es una funcion de ventana
        # particionada por cuenta en SQL (`logica.obtener_libro_mayor`); aqui
        # solo se copia el ultimo valor para el pie de la cuenta.
        saldo_final = _a_float(movimiento.get("Saldo"))
        filas.append([
            Paragraph(_fecha(movimiento.get("Fecha")), est["celda"]),
            Paragraph(_pdf_texto(movimiento.get("N° Asiento")), est["celda"]),
            Paragraph(_texto_largo(movimiento.get("Glosa")), est["celda"]),
            _importe(debe),
            _importe(haber),
            _importe(saldo_final),
        ])

    filas.append([
        Paragraph(f"Totales de la cuenta {codigo}", est["total_texto"]), "", "", "",
        _importe_fuerte(total_debe), _importe_fuerte(total_haber),
    ])
    filas.append([
        Paragraph("Saldo final", est["total_texto"]), "", "", "", "",
        _importe_fuerte(saldo_final),
    ])

    tabla = LongTable(filas, colWidths=[ANCHO_UTIL * f for f in COLS_MAYOR],
                      repeatRows=2, splitByRow=1, hAlign="LEFT")
    estilo = _estilo_tabla(filas)
    estilo.add("SPAN", (0, 0), (-1, 0))
    estilo.add("BACKGROUND", (0, 0), (-1, 0), PALETA["cabecera"])
    estilo.add("LINEBELOW", (0, 0), (-1, 0), 0.7, PALETA["acento"])
    estilo.add("BACKGROUND", (0, 1), (-1, 1), PALETA["cabecera"])
    estilo.add("TOPPADDING", (0, 0), (-1, 0), 1.6 * mm)
    estilo.add("BOTTOMPADDING", (0, 0), (-1, 0), 1.6 * mm)
    estilo.add("SPAN", (0, -2), (2, -2))
    estilo.add("SPAN", (0, -1), (4, -1))
    estilo.add("BACKGROUND", (0, -2), (-1, -1), PALETA["franja"])
    estilo.add("LINEABOVE", (0, -2), (-1, -2), 0.7, PALETA["tinta_suave"])
    estilo.add("LINEABOVE", (0, -1), (-1, -1), 0.4, PALETA["linea"])
    tabla.setStyle(estilo)
    return tabla


def generar_pdf_libro_mayor(df, contexto=None):
    """
    Libro Mayor en PDF, a partir del mismo DataFrame que pinta la pantalla.

    El columna `Saldo` se imprime tal como llega. No se recalcula aqui: el saldo
    acumulado por cuenta es una funcion de ventana particionada en SQL
    (`logica.obtener_libro_mayor`), y recalcularlo en Python produciria dos
    cifras distintas para el mismo dato si alguna vez divergieran.
    """
    contexto = dict(contexto or {})
    df = df if df is not None else pd.DataFrame()

    elementos = _titulo(
        "LIBRO MAYOR",
        "Movimientos consolidados por cuenta del PCGE, con el saldo acumulado de cada una.")

    total_cuentas = 0 if df.empty else int(df["Cuenta PCGE"].nunique())
    total_movimientos = 0 if df.empty else len(df)

    elementos += [
        _tabla_contexto([
            ("Periodo", _periodo_texto(contexto)),
            ("Cuenta seleccionada", contexto.get("cuenta_texto") or "Todas las cuentas"),
            ("Naturaleza", contexto.get("naturaleza") or "Se muestra el saldo propio de cada cuenta"),
            ("Cuentas con movimiento", _entero(total_cuentas)),
            ("Movimientos", _entero(total_movimientos)),
            ("Origen de los datos", "Base de datos contable · consultas de solo lectura"),
        ]),
    ]

    if df.empty:
        elementos += [Spacer(1, 5 * mm), _seccion("Movimientos por cuenta"),
                      _vacio("No se encontraron movimientos que cumplan los filtros seleccionados.")]
        return _construir_pdf(elementos, "Libro Mayor", f"{MARCA} · Libro Mayor")

    # El resumen solo aporta cuando hay más de una cuenta: con una sola, repetir
    # sus totales al principio y al final es ruido.
    if total_cuentas > 1:
        elementos += [Spacer(1, 1 * mm), _seccion("Resumen por cuenta"), _tabla_resumen_mayor(df)]

    elementos.append(_seccion("Movimientos por cuenta"))

    ordenado = df.sort_values(["Cuenta PCGE", "Fecha", "N° Asiento"], kind="stable")
    for codigo, movimientos in ordenado.groupby("Cuenta PCGE", sort=True):
        primero = movimientos.iloc[0]
        elementos.append(_tabla_cuenta(codigo, primero.get("Denominación"),
                                       contexto.get("naturaleza") if total_cuentas == 1 else None,
                                       movimientos.to_dict("records")))
        elementos.append(Spacer(1, 2.6 * mm))

    if total_cuentas > 1:
        elementos.append(
            _nota(
                "El importe de la columna Saldo es el acumulado propio de cada cuenta y no se "
                "reinicia entre cuentas. No existe un saldo único para el conjunto: cada "
                "cuenta tiene naturaleza propia y mezclarlas no produce una cifra contable."
            )
        )

    return _construir_pdf(elementos, "Libro Mayor", f"{MARCA} · Libro Mayor")


def _tabla_resumen_mayor(df):
    """Una fila por cuenta: Total Debe, Total Haber y Saldo final."""
    est = _estilos()
    filas = [[Paragraph("Cuenta", est["cabecera"]),
              Paragraph("Denominación", est["cabecera"]),
              Paragraph("Total Debe", est["cabecera_num"]),
              Paragraph("Total Haber", est["cabecera_num"]),
              Paragraph("Saldo final", est["cabecera_num"])]]

    total_debe = total_haber = 0.0
    for codigo, grupo in df.groupby("Cuenta PCGE", sort=True):
        debe = float(grupo["Debe"].sum())
        haber = float(grupo["Haber"].sum())
        # El saldo final es el ultimo acumulado de ESA cuenta, no un promedio
        # ni una suma de saldos.
        saldo_final = float(grupo.sort_values(["Fecha", "N° Asiento"], kind="stable")["Saldo"].iloc[-1])
        total_debe += debe
        total_haber += haber
        filas.append([
            Paragraph(_pdf_texto(codigo), est["celda"]),
            Paragraph(_texto_largo(grupo.iloc[0].get("Denominación")), est["celda"]),
            _importe(debe),
            _importe(haber),
            _importe(saldo_final),
        ])

    filas.append([Paragraph("Totales del conjunto", est["total_texto"]), "",
                  _importe_fuerte(total_debe), _importe_fuerte(total_haber), ""])

    tabla = LongTable(filas,
                      colWidths=[ANCHO_UTIL * f for f in (0.09, 0.45, 0.1533, 0.1533, 0.1534)],
                      repeatRows=1, splitByRow=1, hAlign="LEFT")
    estilo = _estilo_tabla(filas)
    estilo.add("BACKGROUND", (0, 0), (-1, 0), PALETA["cabecera"])
    estilo.add("SPAN", (0, -1), (1, -1))
    estilo.add("BACKGROUND", (0, -1), (-1, -1), PALETA["franja"])
    estilo.add("LINEABOVE", (0, -1), (-1, -1), 0.7, PALETA["tinta_suave"])
    tabla.setStyle(estilo)
    return tabla


# ============================================================================
# 3. ESTADO DE SITUACION FINANCIERA
# ============================================================================
COLS_ESTADO = (0.11, 0.65, 0.24)


def _filas_de_cuentas(cuentas):
    """`(codigo, descripcion, saldo)` -> las tres celdas de una fila de cuenta."""
    est = _estilos()
    return [[Paragraph(_pdf_texto(codigo), est["celda"]),
             Paragraph(_texto_largo(descripcion), est["celda"]),
             _importe(saldo)]
            for codigo, descripcion, saldo in (cuentas or [])]


def _tabla_estado_financiero(bloques, total_activos, total_pasivo_patrimonio,
                             utilidad, etiqueta_utilidad, diferencia_cuadre):
    """
    Estado de Situacion Financiera: Activo, Patrimonio y el resultado ACUMULADO.

    `bloques` es una lista de `(etiqueta_de_seccion, filas_de_cuentas)`. Las filas
    de cada cuenta y los subtotales de cada bloque los resuelve la interfaz: este
    modulo no sabe que un elemento 1 es activo. `utilidad` es el resultado acumulado
    a la fecha de corte, no el del periodo, y `etiqueta_utilidad` ya viene resuelto
    segun su signo.
    """
    est = _estilos()
    anchos = [ANCHO_UTIL * f for f in COLS_ESTADO]

    filas = []

    def _seccion_tabla(texto):
        filas.append([Paragraph(_pdf_texto(texto), est["seccion"]), "", ""])

    def _cabecera():
        filas.append([Paragraph("Código", est["cabecera"]),
                      Paragraph("Denominación", est["cabecera"]),
                      Paragraph("Saldo", est["cabecera_num"])])

    def _subtotal(texto, importe):
        filas.append([Paragraph(_pdf_texto(texto), est["total_texto"]), "",
                      _importe_fuerte(importe)])

    def _sin_cuentas():
        """Aviso de seccion sin cuentas. Se marca para poder extenderlo a 3 columnas."""
        filas.append([_vacia("Sin movimiento en el periodo"), "", ""])

    # --- ACTIVO (con sus subtotales por elemento) ---
    for etiqueta, cuentas, subtotal in bloques["activos"]:
        _seccion_tabla(etiqueta)
        _cabecera()
        filas.extend(_filas_de_cuentas(cuentas))
        if not cuentas:
            _sin_cuentas()
        _subtotal(f"Subtotal · {etiqueta}", subtotal)

    filas.append([Paragraph("TOTAL ACTIVO", est["total_texto"]), "", _importe_fuerte(total_activos)])

    # --- PASIVO ---
    _seccion_tabla("PASIVO")
    _cabecera()
    filas.extend(_filas_de_cuentas(bloques["pasivos"]))
    if not bloques["pasivos"]:
        _sin_cuentas()
    _subtotal("TOTAL PASIVO", bloques["total_pasivos"])

    # --- PATRIMONIO ---
    _seccion_tabla("PATRIMONIO")
    _cabecera()
    filas.extend(_filas_de_cuentas(bloques["patrimonio"]))
    if not bloques["patrimonio"]:
        _sin_cuentas()
    _subtotal("TOTAL PATRIMONIO", bloques["total_patrimonio"])

    # --- RESULTADO Y CIERRE ---
    # El Balance es una foto a la fecha de corte, asi que la linea de resultado es
    # ACUMULADA hasta esa fecha y no el movimiento del rango elegido. El rotulo lo
    # recibe resuelto el llamador; aqui solo se maqueta.
    _seccion_tabla("RESULTADO ACUMULADO")
    filas.append([Paragraph(_pdf_texto(etiqueta_utilidad), est["total_texto"]),
                  Paragraph("Resultado acumulado hasta la fecha de corte", est["celda"]),
                  _importe_fuerte(utilidad)])

    filas.append([Paragraph("TOTAL PASIVO + PATRIMONIO + RESULTADO", est["total_texto"]), "",
                  _importe_fuerte(total_pasivo_patrimonio)])

    tabla = LongTable(filas, colWidths=anchos, repeatRows=1, splitByRow=1, hAlign="LEFT")
    estilo = _estilo_tabla(filas)
    for indice, fila in enumerate(filas):
        nombre = _nombre_de_estilo(fila[0])
        if nombre == "seccion":
            estilo.add("SPAN", (0, indice), (-1, indice))
            estilo.add("BACKGROUND", (0, indice), (-1, indice), PALETA["cabecera"])
            estilo.add("TOPPADDING", (0, indice), (-1, indice), 2 * mm)
            estilo.add("BOTTOMPADDING", (0, indice), (-1, indice), 2 * mm)
        elif nombre == "cabecera":
            estilo.add("BACKGROUND", (0, indice), (-1, indice), PALETA["cabecera"])
        elif nombre == "vacia":
            estilo.add("SPAN", (0, indice), (-1, indice))
        elif _nombre_de_estilo(fila[2]) == "moneda_fuerte":
            # Fila de total o subtotal: la etiqueta ocupa las dos primeras columnas.
            estilo.add("SPAN", (0, indice), (1, indice))
            estilo.add("BACKGROUND", (0, indice), (-1, indice), PALETA["franja"])
            estilo.add("LINEABOVE", (0, indice), (-1, indice), 0.7, PALETA["tinta_suave"])

    tabla.setStyle(estilo)

    comprobacion = _tabla_comprobacion(total_activos, total_pasivo_patrimonio, diferencia_cuadre)
    return [tabla, Spacer(1, 3 * mm), comprobacion]


def _tabla_comprobacion(total_activos, total_pasivo_patrimonio, diferencia_cuadre):
    """La ecuacion del balance, mostrada y con su diferencia a la vista."""
    est = _estilos()
    cuadra = abs(diferencia_cuadre) < 0.005

    filas = [
        [Paragraph("Comprobación", est["cabecera"]),
         Paragraph("Importe", est["cabecera_num"])],
        [Paragraph("Total Activo", est["celda"]), _importe(total_activos)],
        [Paragraph("Total Pasivo + Patrimonio + Resultado acumulado", est["celda"]),
         _importe(total_pasivo_patrimonio)],
    ]
    filas.append([
        # Guion ASCII, no U+2212: Helvetica no lo tiene en cp1252 y `_pdf_texto`
        # lo convertiria en "?", dejando la ecuacion del balance ilegible.
        Paragraph("Diferencia (Activo - Pasivo + Patrimonio + Resultado acumulado)", est["total_texto"]),
        _importe_fuerte(diferencia_cuadre),
    ])

    tabla = Table(filas, colWidths=[ANCHO_UTIL * 0.68, ANCHO_UTIL * 0.32], hAlign="LEFT")
    estilo = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.3 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("BACKGROUND", (0, 0), (-1, 0), PALETA["cabecera"]),
        ("BOX", (0, 0), (-1, -1), 0.5, PALETA["linea"]),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, PALETA["franja"]),
        ("LINEABOVE", (0, -1), (-1, -1), 0.7, PALETA["tinta_suave"]),
        ("BACKGROUND", (0, -1), (-1, -1),
         PALETA["fila_alterna"] if cuadra else PALETA["alerta_fondo"]),
    ]
    tabla.setStyle(TableStyle(estilo))
    return tabla


def generar_pdf_balance_general(df_saldos, contexto=None, bloques=None):
    """
    Estado de Situacion Financiera (Balance General) en PDF.

    `df_saldos` es el DataFrame de `logica.obtener_saldos_cuentas(None, fecha_hasta)`
    YA recortado a la fecha de corte: el balance es una foto de la posicion, asi que
    sus cuentas y su resultado son ACUMULADOS hasta esa fecha y no el movimiento del
    rango elegido. `bloques` lleva las filas de cada seccion y sus subtotales,
    resueltos por la interfaz con las reglas contables existentes.
    """
    contexto = dict(contexto or {})
    bloques = dict(bloques or {})

    elementos = _titulo(
        "ESTADO DE SITUACIÓN FINANCIERA",
        "Balance General · posición de las cuentas del Plan Contable General "
        "Empresarial (PCGE) a la fecha de corte del periodo seleccionado.")

    total_activos = _a_float(contexto.get("total_activos"))
    total_pasivo_patrimonio = _a_float(contexto.get("total_pasivo_patrimonio"))
    utilidad = _a_float(contexto.get("utilidad"))
    diferencia_cuadre = round(total_activos - total_pasivo_patrimonio, 2)

    # El rotulo lo decide el signo, no una palabra fija. Imprimir siempre "UTILIDAD
    # DEL EJERCICIO" rotula como utilidad una perdida, y el color no salva la
    # lectura en una impresion en blanco y negro: lo que la resuelve es la palabra.
    # En cero no se inventa un "-0.00": se dice que no hay resultado.
    if contexto.get("etiqueta_utilidad"):
        etiqueta_utilidad = contexto["etiqueta_utilidad"]
    elif utilidad > 0:
        etiqueta_utilidad = "UTILIDAD ACUMULADA"
    elif utilidad < 0:
        etiqueta_utilidad = "PÉRDIDA ACUMULADA"
    else:
        etiqueta_utilidad = "SIN RESULTADO ACUMULADO"

    elementos += [
        _tabla_contexto([
            ("Periodo", _periodo_texto(contexto)),
            ("Fecha de corte", _fecha(_periodo(contexto)[1])),
            ("Cuentas con saldo", _entero(0 if df_saldos is None or df_saldos.empty else len(df_saldos))),
            ("Base del resultado",
             "Resultado acumulado hasta la fecha de corte (cuentas de resultado, "
             "elementos 6, 7, 8 y 9)"),
            ("Norma contable", "PCGE (Perú) · grupos por elemento del catálogo"),
            ("Origen de los datos", "Base de datos contable · consultas de solo lectura"),
        ]),
        Spacer(1, 1 * mm),
    ]

    if df_saldos is None or df_saldos.empty:
        elementos += [Spacer(1, 3 * mm), _seccion("Estado"),
                      _vacio("El periodo seleccionado no tiene saldos para elaborar el estado.")]
        return _construir_pdf(elementos, "Estado de Situación Financiera",
                              f"{MARCA} · Estado de Situación Financiera")

    elementos += _tabla_estado_financiero(
        bloques, total_activos, total_pasivo_patrimonio, utilidad,
        etiqueta_utilidad,
        diferencia_cuadre,
    )

    nota_cierre = (
        "Los saldos del Activo, del Pasivo y del Patrimonio son ACUMULADOS a la fecha de corte: un "
        "activo registrado antes del periodo sigue presente, porque el balance es una foto de la "
        "posición y no se reinicia. El importe de la línea de resultado es, por la misma razón, el "
        "resultado ACUMULADO hasta la fecha de corte, y no el movimiento del periodo seleccionado."
    )
    nota_cierre += (
        " Ese resultado acumulado se obtiene de los movimientos registrados hasta esa fecha en las "
        "cuentas de resultado del catálogo (elementos 6, 7, 8 y 9 del PCGE). LX7 no tiene asientos "
        "de cierre: no existe ningún traslado del resultado a las cuentas de patrimonio, así que el "
        "importe permanece en las cuentas de resultado y todavía no figura dentro del Patrimonio "
        "que se presenta más arriba."
    )
    if diferencia_cuadre != 0:
        nota_cierre += (
            f" La diferencia de {_moneda(abs(diferencia_cuadre))} no proviene del resultado "
            "acumulado, que ya está incluido en la línea anterior: proviene de partidas cuya "
            "contrapartida no está registrada hasta la fecha de corte, o de asientos descuadrados. "
            "Se informa en lugar de compensarse con una línea contable inexistente."
        )
    elementos.append(_nota(nota_cierre))

    return _construir_pdf(elementos, "Estado de Situación Financiera",
                          f"{MARCA} · Estado de Situación Financiera")


# ============================================================================
# 4. ESTADO DE RESULTADOS INTEGRALES
# ============================================================================
def _tabla_resultados(titulo, cuentas, total, etiqueta_total):
    est = _estilos()
    filas = [
        [Paragraph(_pdf_texto(titulo), est["seccion"]), "", ""],
        [Paragraph("Código", est["cabecera"]),
         Paragraph("Denominación", est["cabecera"]),
         Paragraph("Importe", est["cabecera_num"])],
    ]
    filas.extend(_filas_de_cuentas(cuentas))
    if not cuentas:
        filas.append([_vacia("Sin movimiento en el periodo"), "", ""])
    filas.append([Paragraph(_pdf_texto(etiqueta_total), est["total_texto"]), "", _importe_fuerte(total)])

    tabla = LongTable(filas, colWidths=[ANCHO_UTIL * f for f in COLS_ESTADO],
                      repeatRows=1, splitByRow=1, hAlign="LEFT")
    estilo = _estilo_tabla(filas)
    for indice, fila in enumerate(filas):
        nombre = _nombre_de_estilo(fila[0])
        if nombre == "seccion":
            estilo.add("SPAN", (0, indice), (-1, indice))
            estilo.add("BACKGROUND", (0, indice), (-1, indice), PALETA["cabecera"])
            estilo.add("TOPPADDING", (0, indice), (-1, indice), 2 * mm)
            estilo.add("BOTTOMPADDING", (0, indice), (-1, indice), 2 * mm)
        elif nombre == "cabecera":
            estilo.add("BACKGROUND", (0, indice), (-1, indice), PALETA["cabecera"])
        elif nombre == "vacia":
            estilo.add("SPAN", (0, indice), (-1, indice))
        elif _nombre_de_estilo(fila[2]) == "moneda_fuerte":
            # SOLO la fila de total extiende la etiqueta sobre la denominacion.
            # Un SPAN en una fila de datos se tragaria el nombre de la cuenta.
            estilo.add("SPAN", (0, indice), (1, indice))
            estilo.add("BACKGROUND", (0, indice), (-1, indice), PALETA["franja"])
            estilo.add("LINEABOVE", (0, indice), (-1, indice), 0.7, PALETA["tinta_suave"])
    tabla.setStyle(estilo)
    return tabla


def generar_pdf_estado_resultados(df_saldos, contexto=None):
    """
    Estado de Resultados Integrales en PDF.

    `df_saldos` viene ya filtrado por el periodo: aqui solo se presentan las
    listas de ingresos y gastos que le entrega la interfaz y su total, más la
    diferencia entre ambos, que es el resultado del ejercicio.

    El resultado se rotula como UTILIDAD o como PÉRDIDA. El color no es el
    unico portador del signo: en una impresión en blanco y negro, un rojo y un
    verde se leen igual, así que el rótulo es lo que decide.
    """
    contexto = dict(contexto or {})
    total_ingresos = _a_float(contexto.get("total_ingresos"))
    total_gastos = _a_float(contexto.get("total_gastos"))
    resultado = _a_float(contexto.get("resultado", round(total_ingresos - total_gastos, 2)))
    es_utilidad = resultado >= 0
    etiqueta_resultado = "UTILIDAD DEL EJERCICIO" if es_utilidad else "PÉRDIDA DEL EJERCICIO"

    elementos = _titulo(
        "ESTADO DE RESULTADOS INTEGRALES",
        "Ingresos y gastos del periodo seleccionado, según el Plan Contable General Empresarial (PCGE).")

    elementos += [
        _tabla_contexto([
            ("Periodo", _periodo_texto(contexto)),
            ("Cuentas con saldo", _entero(0 if df_saldos is None or df_saldos.empty else len(df_saldos))),
            ("Base del resultado", "Movimientos del periodo seleccionado, no acumulados históricos"),
            ("Origen de los datos", "Base de datos contable · consultas de solo lectura"),
        ]),
        Spacer(1, 1 * mm),
        _tabla_resultados("INGRESOS", contexto.get("ingresos", []), total_ingresos,
                          "TOTAL INGRESOS"),
        Spacer(1, 3 * mm),
        _tabla_resultados("GASTOS", contexto.get("gastos", []), total_gastos, "TOTAL GASTOS"),
        Spacer(1, 4 * mm),
        _cierre_resultados(total_ingresos, total_gastos, resultado, etiqueta_resultado, es_utilidad),
        _nota(
            "El importe de Resultado del Ejercicio es la diferencia entre el Total de Ingresos y el "
            "Total de Gastos del periodo seleccionado. Los saldos del Activo, Pasivo y Patrimonio "
            "no se presentan aquí porque son acumulados a la fecha de corte y figuran en el Estado "
            "de Situación Financiera."
        ),
    ]

    return _construir_pdf(elementos, "Estado de Resultados Integrales",
                          f"{MARCA} · Estado de Resultados Integrales")


def _cierre_resultados(total_ingresos, total_gastos, resultado, etiqueta_resultado, es_utilidad):
    """Franja de cierre: Ingresos, menos Gastos, igual a Utilidad o Pérdida."""
    est = _estilos()
    filas = [
        [Paragraph("Total Ingresos", est["celda"]), _importe(total_ingresos)],
        [Paragraph("(menos) Total Gastos", est["celda"]), _importe(-abs(total_gastos))],
        [Paragraph(_pdf_texto(etiqueta_resultado), est["total_texto"]),
         _importe_fuerte(resultado)],
    ]
    tabla = Table(filas, colWidths=[ANCHO_UTIL * 0.68, ANCHO_UTIL * 0.32], hAlign="LEFT")
    estilo = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("BOX", (0, 0), (-1, -1), 0.6, PALETA["linea"]),
        ("LINEBELOW", (0, 0), (-1, 1), 0.4, PALETA["franja"]),
        ("LINEABOVE", (0, 2), (-1, 2), 0.9, PALETA["acento"]),
        ("BACKGROUND", (0, 2), (-1, 2),
         PALETA["cabecera"] if es_utilidad else PALETA["alerta_fondo"]),
        ("TEXTCOLOR", (0, 2), (-1, 2), PALETA["tinta"] if es_utilidad else PALETA["alerta"]),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]
    tabla.setStyle(TableStyle(estilo))
    return tabla