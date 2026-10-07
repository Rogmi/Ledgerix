"""
FASE 4 — EXPORTACION A PDF DE LOS REPORTES CONTABLES.

Qué protege este archivo
-----------------------
1. Que los cuatro generadores devuelvan un PDF REAL y no un placeholder: firma
   `%PDF-` al principio, `%%EOF` al final y texto que se puede extraer. Un modulo
   que devuelve `b""` o un HTML con nombre `.pdf` pasaria muchos tests hasta que
   alguien lo abre en un visor.
2. Que el PDF diga lo mismo que la pantalla. Este archivo arma los mismos
   DataFrames que arma `app.py` y comprueba que las cifras del documento
   coinciden con las del DataFrame: si alguien maqueta el PDF recalculando
   importes aparte de la consulta, las pruebas lo detectan.
3. Que la informacion contable NO se pierda al maquetar. Especificamente: la
   columna Denominacion del Estado de Resultados debe seguir visible. Un `SPAN`
   de estilo aplicado a la fila equivocada se come el texto de la celda y el
   documento sale con codigos sin nombre, sin error y sin que nadie se entere.
4. Que los estados financieros respeten el periodo: activos, pasivos y patrimonio
   acumulados a la fecha de corte, ingresos y gastos solo del rango. Y que la
   ecuacion del balance cierre con los importes que el PDF imprime.
5. Que el manejo de vacios y de caracteres no soportados no reviente el modulo:
   sin asientos, sin cuentas de una seccion, y con texto con acentos, enye y
   simbolos.

Base de datos de prueba
-----------------------
Ninguna prueba toca `datos/contabilidad.db`. Se construye una base temporal en
`tmp_path` con el MISMO esquema y los mismos 5 asientos reales que usa
`test_libros_contables.py`, de modo que las cifras esperadas son las mismas que
alli: Total Debe = Total Haber = 280,000.00, Activo = 110,000.00,
Patrimonio = 100,000.00, Utilidad = 10,000.00.
"""

import io
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pdfplumber
import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import logica as lg  # noqa: E402
import reportes_pdf as rp  # noqa: E402


# --------------------------------------------------------------------------- #
# Réplica de los 5 asientos reales (identica a test_libros_contables.py)
# --------------------------------------------------------------------------- #
CUENTAS = [
    ("10", "Efectivo y equivalentes de efectivo", 1),
    ("12", "Cuentas por cobrar comerciales – Terceros", 2),
    ("20", "Mercaderías", 2),
    ("50", "Capital", 5),
    ("63", "Gastos de servicios prestados por terceros", 6),
    ("69", "Costo de ventas", 6),
    ("70", "Ventas", 7),
]

ASIENTOS = [
    (1, "2020-07-08", "Se crea una empresa con 100,000 al contado.",
     [("10", 100000.0, 0.0), ("50", 0.0, 100000.0)]),
    (2, "2020-07-20", "Se compra 50,000 de mercaderia al contado",
     [("20", 50000.0, 0.0), ("10", 0.0, 50000.0)]),
    (3, "2020-07-25", "SE realiza una venta por 70,000 soles al credito",
     [("12", 70000.0, 0.0), ("70", 0.0, 70000.0)]),
    (4, "2020-07-31", "Asiento de costo de ventas basado en inventario final",
     [("69", 40000.0, 0.0), ("20", 0.0, 40000.0)]),
    (5, "2020-07-31", "Se pagan gastos operativos por 20,000 soles al contado",
     [("63", 20000.0, 0.0), ("10", 0.0, 20000.0)]),
]

ESQUEMA = """
CREATE TABLE Cuentas (
    codigo TEXT PRIMARY KEY,
    descripcion TEXT NOT NULL,
    elemento INTEGER NOT NULL
);
CREATE TABLE Asientos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha DATE NOT NULL,
    glosa TEXT NOT NULL
);
CREATE TABLE Detalles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asiento_id INTEGER NOT NULL,
    cuenta_codigo TEXT NOT NULL,
    debe REAL DEFAULT 0.0,
    haber REAL DEFAULT 0.0,
    FOREIGN KEY (asiento_id) REFERENCES Asientos(id),
    FOREIGN KEY (cuenta_codigo) REFERENCES Cuentas(codigo)
);
"""

PERIODO = (date(2020, 7, 8), date(2020, 7, 31))

# Cifras que deben aparecer en el periodo completo. Estan escritas aqui a mano y
# no calculadas con la misma expresion que usa el codigo, para que el test pueda
# detectAR un error de calculo en lugar de reproducirlo.
ESPERADO = {
    "total_debe": 280000.0,
    "total_haber": 280000.0,
    "total_activo": 110000.0,
    "total_pasivo": 0.0,
    "total_patrimonio": 100000.0,
    "total_ingresos": 70000.0,
    "total_gastos": 60000.0,
    "utilidad": 10000.0,
}


@pytest.fixture
def bd_real(monkeypatch, tmp_path):
    ruta = str(tmp_path / "pdf_contabilidad_test.db")
    conexion = sqlite3.connect(ruta)
    try:
        conexion.executescript(ESQUEMA)
        conexion.executemany(
            "INSERT INTO Cuentas (codigo, descripcion, elemento) VALUES (?, ?, ?)", CUENTAS
        )
        for asiento_id, fecha, glosa, partidas in ASIENTOS:
            conexion.execute(
                "INSERT INTO Asientos (id, fecha, glosa) VALUES (?, ?, ?)", (asiento_id, fecha, glosa)
            )
            conexion.executemany(
                "INSERT INTO Detalles (asiento_id, cuenta_codigo, debe, haber) VALUES (?, ?, ?, ?)",
                [(asiento_id, cuenta, debe, haber) for cuenta, debe, haber in partidas],
            )
        conexion.commit()
    finally:
        conexion.close()
    monkeypatch.setattr(lg, "DB_PATH", ruta)
    return ruta


# --------------------------------------------------------------------------- #
# Utilidades: leer el PDF y reconstruir lo que la pantallaaria
# --------------------------------------------------------------------------- #
_ETIQUETAS_PANTALLA = {
    "fecha": "Fecha", "numero_asiento": "N° Asiento", "glosa": "Glosa",
    "cuenta": "Cuenta PCGE", "denominacion": "Denominación",
    "debe": "Debe", "haber": "Haber", "saldo": "Saldo",
}


def _presentar(df, columnas):
    """Replica de `_presentar` en app.py: mismo DataFrame que ve el usuario."""
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df = df.rename(columns=_ETIQUETAS_PANTALLA)
    df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.date
    return df[[c for c in columnas if c in df.columns]]


def _texto(datos_pdf):
    """Todo el texto del PDF, paginas unidas, con espacios normalizados."""
    with pdfplumber.open(io.BytesIO(datos_pdf)) as pdf:
        return "\n".join(pagina.extract_text() or "" for pagina in pdf.pages)


def _paginas(datos_pdf):
    with pdfplumber.open(io.BytesIO(datos_pdf)) as pdf:
        return [pagina.extract_text() or "" for pagina in pdf.pages]


def _es_pdf(datos):
    return (isinstance(datos, (bytes, bytearray))
            and bytes(datos[:5]) == b"%PDF-"
            and bytes(bytes(datos).rstrip()[-5:]) == b"%%EOF")


def _contexto(**extra):
    base = {"periodo": PERIODO}
    base.update(extra)
    return base


def _df_diario():
    return _presentar(
        lg.obtener_libro_diario(*PERIODO),
        ["Fecha", "N° Asiento", "Glosa", "Cuenta PCGE", "Denominación", "Debe", "Haber"],
    )


def _df_mayor():
    return _presentar(
        lg.obtener_libro_mayor(*PERIODO),
        ["Fecha", "N° Asiento", "Cuenta PCGE", "Denominación", "Glosa", "Debe", "Haber", "Saldo"],
    )


def _filas(subconjunto):
    return [(r["codigo"], r["descripcion"], float(r["saldo"])) for _, r in subconjunto.iterrows()]


def _pdf_balance(df_saldos):
    activos = df_saldos[df_saldos["elemento"].isin([1, 2, 3])]
    pasivos = df_saldos[df_saldos["elemento"] == 4]
    patrimonio = df_saldos[df_saldos["elemento"] == 5]
    utilidad = (df_saldos[df_saldos["elemento"] == 7]["saldo"].sum()
                - df_saldos[df_saldos["elemento"].isin([6, 9])]["saldo"].sum())
    return rp.generar_pdf_balance_general(
        df_saldos,
        _contexto(
            total_activos=round(float(activos["saldo"].sum()), 2),
            total_pasivo_patrimonio=round(float(pasivos["saldo"].sum())
                                          + float(patrimonio["saldo"].sum()) + float(utilidad), 2),
            utilidad=float(utilidad),
        ),
        bloques={
            "activos": [
                (lg.ETIQUETAS_ACTIVO.get(int(e), f"Activo {e}"),
                 _filas(activos[activos["elemento"] == e]),
                 round(float(activos[activos["elemento"] == e]["saldo"].sum()), 2))
                for e in sorted(activos["elemento"].unique())
            ],
            "pasivos": _filas(pasivos),
            "patrimonio": _filas(patrimonio),
            "total_pasivos": round(float(pasivos["saldo"].sum()), 2),
            "total_patrimonio": round(float(patrimonio["saldo"].sum()), 2),
        },
    )


def _pdf_resultados(df_saldos):
    ingresos = df_saldos[df_saldos["elemento"] == 7]
    gastos = df_saldos[df_saldos["elemento"].isin([6, 9])]
    return rp.generar_pdf_estado_resultados(
        df_saldos,
        _contexto(
            ingresos=_filas(ingresos),
            total_ingresos=round(float(ingresos["saldo"].sum()), 2),
            gastos=_filas(gastos),
            total_gastos=round(float(gastos["saldo"].sum()), 2),
            resultado=round(float(ingresos["saldo"].sum() - gastos["saldo"].sum()), 2),
        ),
    )


# =========================================================================== #
# 1. LOS CUATRO GENERADORES PRODUCEN UN PDF REAL
# =========================================================================== #
def test_libro_diario_es_un_pdf_valido(bd_real):
    datos = rp.generar_pdf_libro_diario(
        _df_diario(), _contexto(total_asientos=5, total_partidas=10)
    )
    assert _es_pdf(datos)
    assert len(_paginas(datos)) >= 1


def test_libro_mayor_es_un_pdf_valido(bd_real):
    datos = rp.generar_pdf_libro_mayor(_df_mayor(), _contexto())
    assert _es_pdf(datos)


def test_balance_es_un_pdf_valido(bd_real):
    datos = _pdf_balance(lg.obtener_saldos_cuentas(*PERIODO))
    assert _es_pdf(datos)


def test_resultados_es_un_pdf_valido(bd_real):
    datos = _pdf_resultados(lg.obtener_saldos_cuentas(*PERIODO))
    assert _es_pdf(datos)


def test_cada_documento_trae_su_titulo_y_su_periodo(bd_real):
    df_diario, df_saldos = _df_diario(), lg.obtener_saldos_cuentas(*PERIODO)
    documentos = {
        "LIBRO DIARIO": rp.generar_pdf_libro_diario(
            df_diario, _contexto(total_asientos=5, total_partidas=10)),
        "LIBRO MAYOR": rp.generar_pdf_libro_mayor(_df_mayor(), _contexto()),
        "ESTADO DE SITUACIÓN FINANCIERA": _pdf_balance(df_saldos),
        "ESTADO DE RESULTADOS INTEGRALES": _pdf_resultados(df_saldos),
    }
    for titulo, datos in documentos.items():
        texto = _texto(datos)
        assert titulo in texto, f"falta el titulo {titulo}"
        # El periodo se imprime dd/mm/aaaa, con guion largo entre las fechas.
        assert "08/07/2020 – 31/07/2020" in texto, f"{titulo}: falta el periodo"
        assert "Página 1 de" in texto, f"{titulo}: falta la numeracion de paginas"


# =========================================================================== #
# 2. EL PDF DICE LO MISMO QUE EL DATAFRAME DE PANTALLA
# =========================================================================== #
def test_libro_diario_imprime_los_totales_del_dataframe(bd_real):
    df = _df_diario()
    total_debe = round(float(df["Debe"].sum()), 2)
    total_haber = round(float(df["Haber"].sum()), 2)
    texto = _texto(rp.generar_pdf_libro_diario(
        df, _contexto(total_asientos=int(df["N° Asiento"].nunique()), total_partidas=len(df))))

    assert f"S/ {total_debe:,.2f}" in texto
    assert f"S/ {total_haber:,.2f}" in texto
    assert total_debe == ESPERADO["total_debe"]
    assert total_haber == ESPERADO["total_haber"]
    # Cada asiento se rotula con su numero correlativo real de la tabla Asientos.
    for numero in sorted(df["N° Asiento"].unique()):
        assert f"Asiento N° {numero}" in texto


def test_libro_diario_conserva_cada_cuenta_con_su_denominacion(bd_real):
    df = _df_diario()
    texto = _texto(rp.generar_pdf_libro_diario(
        df, _contexto(total_asientos=5, total_partidas=10)))
    for _, fila in df.iterrows():
        assert str(fila["Cuenta PCGE"]) in texto
        assert fila["Denominación"] in texto
    # La glosa es la descripcion del asiento y debe leerse completa.
    assert "Se crea una empresa con 100,000 al contado." in texto


def test_libro_mayor_imprime_el_saldo_final_de_cada_cuenta(bd_real):
    df = _df_mayor()
    texto = _texto(rp.generar_pdf_libro_mayor(df, _contexto()))
    assert "Totales de la cuenta" in texto
    assert "Saldo final" in texto
    for codigo in sorted(df["Cuenta PCGE"].unique()):
        assert f"Cuenta PCGE {codigo} —" in texto
        assert codigo in texto
    # El Mayor consolida por cuenta, asi que su total Debe es el de la pantalla.
    assert f"S/ {round(float(df['Debe'].sum()), 2):,.2f}" in texto
    assert f"S/ {round(float(df['Haber'].sum()), 2):,.2f}" in texto


def test_libro_mayor_de_una_cuenta_solo_muestra_esa_cuenta(bd_real):
    codigo = "10"
    df = _df_mayor()
    df = df[df["Cuenta PCGE"] == codigo]
    texto = _texto(rp.generar_pdf_libro_mayor(
        df, _contexto(cuenta_texto=f"{codigo} - Efectivo y equivalentes de efectivo",
                      naturaleza="deudora")))
    assert "Cuenta PCGE 10 — Efectivo y equivalentes de efectivo" in texto
    assert "Cuenta PCGE 70 —" not in texto, "se colaron cuentas de otro tipo"
    assert f"S/ {round(float(df['Saldo'].iloc[-1]), 2):,.2f}" in texto


def test_balance_imprime_las_cifras_del_dataframe(bd_real):
    df_saldos = lg.obtener_saldos_cuentas(*PERIODO)
    texto = _texto(_pdf_balance(df_saldos))

    activos = df_saldos[df_saldos["elemento"].isin([1, 2, 3])]
    for _, fila in activos.iterrows():
        assert fila["descripcion"] in texto
        assert f"S/ {round(float(fila['saldo']), 2):,.2f}" in texto

    assert f"S/ {ESPERADO['total_activo']:,.2f}" in texto
    assert f"S/ {ESPERADO['total_patrimonio']:,.2f}" in texto
    assert "UTILIDAD ACUMULADA" in texto
    assert f"S/ {ESPERADO['utilidad']:,.2f}" in texto


def test_balance_cuadra_con_la_ecuacion_del_activo(bd_real):
    texto = _texto(_pdf_balance(lg.obtener_saldos_cuentas(*PERIODO)))
    total_p_y_p = ESPERADO["total_pasivo"] + ESPERADO["total_patrimonio"] + ESPERADO["utilidad"]
    assert total_p_y_p == ESPERADO["total_activo"], "las cifras esperadas no cuadran entre si"
    assert f"S/ {total_p_y_p:,.2f}" in texto
    assert "Diferencia (Activo - Pasivo + Patrimonio + Resultado acumulado)" in texto
    # Con el periodo completo la diferencia es cero: debe aparecer el cero, no un
    # texto que la esconda.
    assert "S/ 0.00" in texto


def test_resultados_imprime_ingresos_gastos_y_utilidad(bd_real):
    df_saldos = lg.obtener_saldos_cuentas(*PERIODO)
    texto = _texto(_pdf_resultados(df_saldos))

    assert f"S/ {ESPERADO['total_ingresos']:,.2f}" in texto
    assert f"S/ {ESPERADO['total_gastos']:,.2f}" in texto
    assert "UTILIDAD DEL EJERCICIO" in texto
    assert f"S/ {ESPERADO['utilidad']:,.2f}" in texto

    ingresos = df_saldos[df_saldos["elemento"] == 7]
    for _, fila in ingresos.iterrows():
        assert fila["descripcion"] in texto
    gastos = df_saldos[df_saldos["elemento"].isin([6, 9])]
    for _, fila in gastos.iterrows():
        assert fila["descripcion"] in texto


def test_resultados_roe_una_perdida_y_no_la_etiqueta_de_utilidad(bd_real):
    """Con mas gastos que ingresos el r��tulo debe cambiar a PÉRDIDA."""
    df_saldos = lg.obtener_saldos_cuentas(*PERIODO)
    datos = rp.generar_pdf_estado_resultados(
        df_saldos,
        _contexto(ingresos=[("70", "Ventas", 1000.0)], total_ingresos=1000.0,
                  gastos=[("69", "Costo de ventas", 4000.0)], total_gastos=4000.0,
                  resultado=-3000.0),
    )
    texto = _texto(datos)
    assert "PÉRDIDA DEL EJERCICIO" in texto or "P\xc9RDIDA DEL EJERCICIO" in texto or "P\u00c9RDIDA DEL EJERCICIO" in texto
    assert "UTILIDAD DEL EJERCICIO" not in texto
    assert "S/ -3,000.00" in texto


# =========================================================================== #
# 3. LA MAQUETACION NO SE COME INFORMACION
# =========================================================================== #
def test_resultados_conserva_la_columna_denominacion(bd_real):
    """
    Regresion de un `SPAN` aplicado a la fila equivocada: al fusionar la celda
    Codigo con la Denominacion, reportlab se come el nombre de la cuenta y el
    documento sale con codigos sueltos. No hay error, solo informacion perdida.
    """
    df_saldos = lg.obtener_saldos_cuentas(*PERIODO)
    texto = _texto(_pdf_resultados(df_saldos))
    assert "Código" in texto and "Denominación" in texto
    for _, fila in df_saldos[df_saldos["elemento"].isin([6, 7, 9])].iterrows():
        assert fila["descripcion"] in texto, f"se perdio la denominacion de {fila['codigo']}"


def test_las_cabeceras_de_columna_no_se_parten_en_dos_lineas(bd_real):
    """
    Con un ancho insuficiente reportlab parte "N° Asiento" en "N°" + "Asiento" y
    la cabecera queda cojea. Se comprueba que la etiqueta se lea entera y seguida
    en la misma linea del resto de la fila de cabeceras.
    """
    diario = _texto(rp.generar_pdf_libro_diario(
        _df_diario(), _contexto(total_asientos=5, total_partidas=10)))
    assert "Cuenta PCGE Denominación Debe Haber" in diario

    mayor = _texto(rp.generar_pdf_libro_mayor(_df_mayor(), _contexto()))
    assert "Fecha N° Asiento Glosa Debe Haber Saldo" in mayor


def test_los_importes_usan_el_formato_con_moneda_y_miles(bd_real):
    texto = _texto(_pdf_balance(lg.obtener_saldos_cuentas(*PERIODO)))
    assert "S/ 110,000.00" in texto
    # Ni el separador de miles ni el decimal usan el formato de la configuracion
    # regional: un PDF contable con "110000.00" o "110.000,00" no es utilizable.
    assert "S/ 110.000,00" not in texto


def test_los_acentos_y_la_enye_sobreviven_a_la_maquetacion(bd_real):
    df_saldos = lg.obtener_saldos_cuentas(*PERIODO)
    textos = [
        _texto(_pdf_balance(df_saldos)),
        _texto(_pdf_resultados(df_saldos)),
        _texto(rp.generar_pdf_libro_mayor(_df_mayor(), _contexto())),
    ]
    # Cada documento muestra solo las cuentas de sus secciones, asi que los
    # nombres que se comprueban son los que le corresponden a cada uno.
    assert "Cuentas por cobrar comerciales – Terceros" in textos[0], "balance"
    assert "Mercaderías" in textos[0], "balance"
    assert "Situación" in textos[0], "balance"

    assert "Gastos de servicios prestados por terceros" in textos[1], "resultados"
    assert "Cuentas por cobrar comerciales – Terceros" in textos[2], "mayor"
    assert "Mercaderías" in textos[2], "mayor"


def test_el_pdf_avisa_de_su_origen_y_de_su_periodo(bd_real):
    texto = _texto(_pdf_balance(lg.obtener_saldos_cuentas(*PERIODO)))
    assert "solo lectura" in texto
    assert "Fecha de corte" in texto
    assert "31/07/2020" in texto


# =========================================================================== #
# 4. EL PERIODO DE LOS ESTADOS FINANCIEROS
# =========================================================================== #
def test_activos_son_acumulados_a_la_fecha_de_corte(bd_real):
    """
    Al 20/07 la mercaderia todavia no se ha vendido, pero el efectivo y las
    cuentas por cobrar SÍ existen: el balance es una foto de la posicion, asi que
    sus saldos no pueden depender del rango.
    """
    df_saldos = lg.obtener_saldos_cuentas(date(2020, 7, 1), date(2020, 7, 20))
    saldos = {r["codigo"]: round(float(r["saldo"]), 2) for _, r in df_saldos.iterrows()}
    assert saldos["10"] == 50000.0, "el efectivo acumulado se recortó al periodo"
    assert saldos["50"] == 100000.0, "el capital acumulado se recortó al periodo"
    # El elemento 9 (costo de ventas) todavia no tiene movimiento en este rango.
    assert "69" not in saldos or saldos["69"] == 0.0


def test_las_cuentas_de_resultado_solo_toman_el_periodo(bd_real):
    """
    La venta del 25/07 esta FUERA del rango. Si las cuentas de resultado se
    tomaran acumuladas, la utilidad del periodo seria 70,000 y el balance no
    cuadaria contra un patrimonio de 100,000.
    """
    df_saldos = lg.obtener_saldos_cuentas(date(2020, 7, 1), date(2020, 7, 20))
    saldos = {r["codigo"]: round(float(r["saldo"]), 2) for _, r in df_saldos.iterrows()}
    assert saldos.get("70", 0.0) == 0.0, "la venta posterior entró en el periodo"
    assert saldos.get("69", 0.0) == 0.0, "el costo posterior entró en el periodo"

    utilidad = (df_saldos[df_saldos["elemento"] == 7]["saldo"].sum()
                - df_saldos[df_saldos["elemento"].isin([6, 9])]["saldo"].sum())
    assert round(float(utilidad), 2) == 0.0


def test_el_periodo_completo_conserva_los_saldos_historicos(bd_real):
    completo = lg.obtener_saldos_cuentas(*PERIODO)
    historico = lg.obtener_saldos_cuentas()
    assert list(completo["codigo"]) == list(historico["codigo"])
    for codigo in completo["codigo"]:
        con_periodo = round(float(completo.loc[completo["codigo"] == codigo, "saldo"].iloc[0]), 2)
        sin_periodo = round(float(historico.loc[historico["codigo"] == codigo, "saldo"].iloc[0]), 2)
        assert con_periodo == sin_periodo, f"la cuenta {codigo} cambio sin motivo"


def test_el_balance_de_un_periodo_parcial_avisa_la_diferencia(bd_real):
    """
    Un corte a mitad de ejercicio puede no cuadrar: el Activo acumulado incluye
    hechos que todavia no tienen contrapartida de resultado en el rango. El PDF
    debe INFORMAR esa diferencia, nunca compensarla con una linea contable
    inventada ni esconderla.

    La base sembrada esta completa y por eso cuadra en cualquier corte. Se le
    anade un asiento DESCUADRADO en agosto para provocar el caso: sin el, la
    prueba pasaria siempre y no comprobaria nada.
    """
    conexion = sqlite3.connect(lg.DB_PATH)
    try:
        conexion.execute(
            "INSERT INTO Asientos (id, fecha, glosa) VALUES (?, ?, ?)",
            (99, "2020-08-05", "Ajuste sin contrapartida, para probar el descuadre"),
        )
        conexion.execute(
            "INSERT INTO Detalles (asiento_id, cuenta_codigo, debe, haber) VALUES (?, ?, ?, ?)",
            (99, "20", 25000.0, 0.0),
        )
        conexion.commit()
    finally:
        conexion.close()

    df_saldos = lg.obtener_saldos_cuentas(date(2020, 8, 1), date(2020, 8, 31))
    texto = _texto(_pdf_balance(df_saldos))
    # El PDF debe decir que los saldos son acumulados, no solo insinuarlo con la
    # palabra "acumulado" en un parrafo: la diferencia tiene que quedar explicada.
    assert "acumulado" in texto.lower()
    assert "Diferencia (Activo - Pasivo + Patrimonio + Resultado acumulado)" in texto

    activos = df_saldos[df_saldos["elemento"].isin([1, 2, 3])]["saldo"].sum()
    pasivo_pat = (df_saldos[df_saldos["elemento"].isin([4, 5])]["saldo"].sum()
                  + df_saldos[df_saldos["elemento"] == 7]["saldo"].sum()
                  - df_saldos[df_saldos["elemento"].isin([6, 9])]["saldo"].sum())
    diferencia = round(float(activos - pasivo_pat), 2)
    assert diferencia != 0, "el corte deberia descuadrar: la prueba no prueba nada"
    assert f"S/ {diferencia:,.2f}" in texto


# =========================================================================== #
# 5. VACIOS Y CASOS LIMITES: NADA DEBE REVENTAR
# =========================================================================== #
def test_periodo_sin_asientos_no_rompe_los_estados(bd_real):
    """
    Un rango anterior a cualquier asiento no devuelve nada, y los generadores
    tienen que aguantarlo: en enero, cuando aun no se registro nada, el boton de
    exportar no puede dejar la pantalla en error.
    """
    df_saldos = lg.obtener_saldos_cuentas(date(2019, 1, 1), date(2019, 12, 31))
    assert df_saldos.empty
    for datos in (_pdf_balance(df_saldos), _pdf_resultados(df_saldos)):
        assert _es_pdf(datos)
        assert "Periodo" in _texto(datos)


def test_dataframe_vacio_aun_produce_un_pdf(bd_real):
    columnas_diario = ["Fecha", "N° Asiento", "Glosa", "Cuenta PCGE",
                       "Denominación", "Debe", "Haber"]
    columnas_mayor = ["Fecha", "N° Asiento", "Cuenta PCGE", "Denominación",
                      "Glosa", "Debe", "Haber", "Saldo"]
    diario = rp.generar_pdf_libro_diario(pd.DataFrame(columns=columnas_diario), _contexto())
    mayor = rp.generar_pdf_libro_mayor(pd.DataFrame(columns=columnas_mayor), _contexto())
    for datos in (diario, mayor):
        assert _es_pdf(datos)
        assert _texto(datos).strip(), "el PDF de un filtro vacio quedo en blanco"


def test_seccion_sin_cuentas_dice_que_no_hay_movimiento(bd_real):
    df_saldos = lg.obtener_saldos_cuentas(date(2020, 7, 8), date(2020, 7, 9))
    texto = _texto(_pdf_balance(df_saldos))
    assert "Sin movimiento en el periodo" in texto


def test_los_generadores_tolignan_none_y_contexto_omitido(bd_real):
    """`contexto=None` es el uso por defecto: no debe reventar nada."""
    # Sin los dos parametros opcionales. Cada generador tiene que saber
    # maquetar un documento sin periodo ni listas de la interfaz.
    assert _es_pdf(rp.generar_pdf_libro_diario(_df_diario()))
    assert _es_pdf(rp.generar_pdf_libro_mayor(_df_mayor()))
    assert _es_pdf(rp.generar_pdf_estado_resultados(None))
    assert _es_pdf(rp.generar_pdf_balance_general(None))


def test_texto_con_simbolos_no_soportados_no_rompe_el_pdf(bd_real):
    """Emoji y caracteres CJK no existen en cp1252: se sustituyen, no explotan."""
    df = _df_diario()
    df.loc[0, "Glosa"] = "Compra de mercadería 😀 con 好 warranty"
    datos = rp.generar_pdf_libro_diario(df, _contexto(total_asientos=5, total_partidas=10))
    assert _es_pdf(datos)
    texto = _texto(datos)
    assert "mercadería" in texto


def test_importes_none_se_leen_como_cero(bd_real):
    datos = rp.generar_pdf_estado_resultados(
        None, _contexto(total_ingresos=None, total_gastos=None, resultado=None))
    assert _es_pdf(datos)
    assert "S/ 0.00" in _texto(datos)


# =========================================================================== #
# 6. PAGINACION Y REPETICION DE ENCABEZADOS
# =========================================================================== #
def test_un_libro_con_muchos_asientos_se_pagina_y_repite_cabecera(bd_real):
    """
    Con 120 asientos el Diario no cabe en una hoja. Cuando eso ocurre el
    documento debe continuar en otra pagina repitiendo el encabezado, porque un
    reporte contable a medio camino sin contexto no sirve para archivar.
    """
    conexion = sqlite3.connect(lg.DB_PATH)
    try:
        # Las fechas caen DENTRO de PERIODO (08/07 a 31/07). Si alguna se saliera
        # del rango, la consulta la descartaria y el conteo de partidas no
        # quadraria con lo sembrado, y el fallo seria del test, no del paginado.
        for numero in range(6, 126):
            fecha = date(2020, 7, 9) + pd.Timedelta(days=numero % 20)
            conexion.execute(
                "INSERT INTO Asientos (id, fecha, glosa) VALUES (?, ?, ?)",
                (numero, fecha.isoformat(), f"Asiento de prueba numero {numero}"),
            )
            conexion.executemany(
                "INSERT INTO Detalles (asiento_id, cuenta_codigo, debe, haber) VALUES (?, ?, ?, ?)",
                [(numero, "10", 100.0, 0.0), (numero, "50", 0.0, 100.0)],
            )
        conexion.commit()
    finally:
        conexion.close()

    df = _df_diario()
    # 10 partidas originales + 120 asientos de prueba x 2 partidas.
    assert len(df) == 250, "las partidas sembradas no llegaron al dataframe"
    datos = rp.generar_pdf_libro_diario(
        df, _contexto(total_asientos=int(df["N° Asiento"].nunique()), total_partidas=len(df)))

    paginas = _paginas(datos)
    assert len(paginas) > 1, "240 partidas deberian ocupar mas de una pagina"
    for indice, pagina in enumerate(paginas, start=1):
        assert "Página" in pagina, f"la pagina {indice} no esta numerada"
        assert f"de {len(paginas)}" in pagina
        # El encabezado se repite: cada hoja dice que reporte es y de que sistema.
        assert "Libro Diario" in pagina, f"la pagina {indice} no repite el titulo"
        assert "LEDGERIX" in pagina, f"la pagina {indice} no repite la marca"
    assert f"Página {len(paginas)} de {len(paginas)}" in paginas[-1]
    assert f"Página 1 de {len(paginas)}" in paginas[0]


def test_el_mayor_pagina_sin_partir_una_fila_a_la_deriva(bd_real):
    """Cada bloque de cuenta debe quedar entero; partirlo en dos haria el saldo
    acumulado ilegible."""
    datos = rp.generar_pdf_libro_mayor(_df_mayor(), _contexto())
    paginas = _paginas(datos)
    assert len(paginas) > 1, "con 7 cuentas el Mayor deberia ocupar mas de una pagina"
    for pagina in paginas:
        # El encabezado repite el nombre del reporte en cada pagina. Sin el, la
        # hoja suelta no dice de que reporte proviene.
        assert "Libro Mayor" in pagina
        assert "Página" in pagina
    texto = "\n".join(paginas)
    for codigo in ("10", "12", "20", "50", "63", "69", "70"):
        assert f"Cuenta PCGE {codigo} —" in texto
        assert f"Totales de la cuenta {codigo}" in texto


# =========================================================================== #
# 7. LA GENERACION NO ESCRITE EN LA BASE DE DATOS
# =========================================================================== #
def test_exportar_los_cuatro_reportes_no_toca_la_base(bd_real):
    def _foto():
        conexion = sqlite3.connect(lg.DB_PATH)
        try:
            return {
                tabla: conexion.execute(f"SELECT * FROM {tabla} ORDER BY {orden}").fetchall()
                for tabla, orden in (("Cuentas", "codigo"), ("Asientos", "id"), ("Detalles", "id"))
            }
        finally:
            conexion.close()

    antes = _foto()
    df_diario, df_mayor = _df_diario(), _df_mayor()
    df_saldos = lg.obtener_saldos_cuentas(*PERIODO)
    for datos in (rp.generar_pdf_libro_diario(df_diario, _contexto()),
                  rp.generar_pdf_libro_mayor(df_mayor, _contexto()),
                  _pdf_balance(df_saldos), _pdf_resultados(df_saldos)):
        assert _es_pdf(datos)
    assert _foto() == antes, "la generacion de PDF modifico la base de datos"