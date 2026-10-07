"""
EXCEL: CONTEXTO CONTABLE QUE NO ES UNA OPERACION, Y EL ASIENTO QUE SE DERIVA.

Que falla antes de este archivo
-------------------------------
La hoja de calculo del taller trae cuatro operaciones escritas y, aparte, una frase que
OBSERVA un saldo: "En el inventario se observa un saldo final de 10,000 soles al cierre
de mes". Esa frase no es una operacion contable, pero el extractor la transcribia como si
lo fuera y el libro acababa con un asiento de mas que repetia una economia ya escrita.

Que comprueba este archivo
--------------------------
1. La hoja produce las cinco operaciones que corresponden: las cuatro escritas y el
   asiento de costo de ventas que el inventario permite determinar.
2. La frase de inventario NO aparece como asiento independiente.
3. El asiento derivado lleva los importes del caso (0 + 50,000 - 10,000 = 40,000).
4. Queda marcado para revision, con los supuestos escritos.
5. No se inventa la fecha que el documento no da.
6. Un unico guardado escribe exactamente los cinco asientos y sus diez partidas.
7. Los reruns normales de Streamlit no vuelven a guardar el mismo borrador.

Ninguna prueba de este archivo llama a la API: el cliente de Groq se sustituye por un
doble. Ninguna escribe en `datos/contabilidad.db`: las que llegan a la base usan una
temporal.
"""

import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Ninguna prueba llama a la API: el cliente se sustituye por un doble, pero la clave
# debe existir para que el import de `ia_engine` no reviente.
os.environ.setdefault("GROQ_API_KEY", "clave-de-pruebas")

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import database as bd  # noqa: E402
import ia_engine as ia  # noqa: E402
import logica as lg  # noqa: E402


# --------------------------------------------------------------------------- #
# El caso académico, escrito como llega desde la lectura del Excel
# --------------------------------------------------------------------------- #
TEXTO_EXCEL = """Fecha | Glosa | Cuenta | Debe | Haber
2020-07-08 | Se crea una empresa con 100,000 al contado | 10 Caja (efectivo) | 100000 |
2020-07-08 | Se crea una empresa con 100,000 al contado | 50 Capital | | 100000
2020-07-20 | Se compra 50,000 de mercaderia al contado | 20 Inventarios | 50000 |
2020-07-20 | Se compra 50,000 de mercaderia al contado | 10 Caja (efectivo) | | 50000
2020-07-25 | SE realiza una venta por 70,000 soles al credito | 12 Cuentas por cobrar | 70000 |
2020-07-25 | SE realiza una venta por 70,000 soles al credito | 70 Ventas | | 70000
2020-07-31 | Se pagan gastos operativos por 20,000 soles al contado | 63 Gastos de servicios | 20000 |
2020-07-31 | Se pagan gastos operativos por 20,000 soles al contado | 10 Caja (efectivo) | | 20000
En el inventario se observa un saldo final de 10,000 soles al cierre de mes.
"""

FRASE_DE_INVENTARIO = "se observa un saldo final"

CUATRO_ESCRITAS = [
    {
        "fecha": "2020-07-08",
        "glosa": "Se crea una empresa con 100,000 al contado.",
        "asiento": [
            {"cuenta": "10", "debe": 100000, "haber": 0},
            {"cuenta": "50", "debe": 0, "haber": 100000},
        ],
    },
    {
        "fecha": "2020-07-20",
        "glosa": "Se compra 50,000 de mercaderia al contado",
        "asiento": [
            {"cuenta": "20", "debe": 50000, "haber": 0},
            {"cuenta": "10", "debe": 0, "haber": 50000},
        ],
    },
    {
        "fecha": "2020-07-25",
        "glosa": "SE realiza una venta por 70,000 soles al credito",
        "asiento": [
            {"cuenta": "12", "debe": 70000, "haber": 0},
            {"cuenta": "70", "debe": 0, "haber": 70000},
        ],
    },
    {
        "fecha": "2020-07-31",
        "glosa": "Se pagan gastos operativos por 20,000 soles al contado",
        "asiento": [
            {"cuenta": "63", "debe": 20000, "haber": 0},
            {"cuenta": "10", "debe": 0, "haber": 20000},
        ],
    },
]

PARTIDAS_DE_LAS_CUATRO_ESCRITAS = {
    ("10", 100000.0, 0.0), ("50", 0.0, 100000.0),
    ("20", 50000.0, 0.0), ("10", 0.0, 50000.0),
    ("12", 70000.0, 0.0), ("70", 0.0, 70000.0),
    ("63", 20000.0, 0.0), ("10", 0.0, 20000.0),
}


def respuesta_del_extractor(contexto, operaciones):
    """El JSON que devuelve el extractor siguiendo el bloque de Excel."""
    return json.dumps({"contexto": contexto, "operaciones": operaciones}, ensure_ascii=False)


# Lo que el extractor responde en el caso del taller: las cuatro operaciones escritas, y
# el saldo que OBSERVA declarado aparte como contexto. El asiento de costo de ventas no
# lo escribe nadie: lo deriva el sistema.
RESPUESTA_DEL_EXTRACTOR = respuesta_del_extractor(
    [{"tipo": "inventario_final", "importe": 10000}], CUATRO_ESCRITAS
)


# --------------------------------------------------------------------------- #
# Doble del cliente de Groq
# --------------------------------------------------------------------------- #
class _MensajeFalso:
    def __init__(self, contenido):
        self.content = contenido
        self.reasoning = ""


class _EleccionFalsa:
    def __init__(self, contenido, finish_reason):
        self.message = _MensajeFalso(contenido)
        self.finish_reason = finish_reason


class _RespuestaFalsa:
    def __init__(self, contenido, finish_reason="stop"):
        self.choices = [_EleccionFalsa(contenido, finish_reason)]


class _ExtractorFalso:
    """Sustituye a `ia.cliente` y devuelve siempre la misma respuesta."""

    def __init__(self, contenido):
        self.contenido = contenido
        self.llamadas = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **parametros):
        self.llamadas.append(parametros)
        return _RespuestaFalsa(self.contenido)


def leer(monkeypatch, contenido=RESPUESTA_DEL_EXTRACTOR, origen="xlsx", texto=TEXTO_EXCEL):
    """Corre el flujo real con el extractor sustituido y devuelve las operaciones."""
    extractor = _ExtractorFalso(contenido)
    monkeypatch.setattr(ia, "cliente", extractor)

    exito, resultado = ia.analizar_excel_completo(texto, origen=origen)
    assert exito, f"el flujo real fallo con un doble valido: {resultado}"
    return list(resultado), extractor, resultado


def partidas(operaciones):
    return {(m["cuenta"], m["debe"], m["haber"]) for o in operaciones for m in o["asiento"]}


def derivada(operaciones):
    """El asiento que el sistema dedujo (no el que el documento escribe)."""
    found = [o for o in operaciones if str(o.get("glosa", "")).startswith("DERIVADO")]
    assert len(found) == 1, f"se esperaba 1 asiento derivado y hay {len(found)}"
    return found[0]


# =========================================================================== #
# 1. La hoja produce las operaciones que corresponden
# =========================================================================== #
def test_el_excel_produce_las_cinco_operaciones_del_caso(monkeypatch):
    operaciones, _, _ = leer(monkeypatch)

    assert len(operaciones) == 5, (
        f"la hoja debe producir 5 operaciones (4 escritas + 1 derivada) y produjo "
        f"{len(operaciones)}"
    )

    # Las cuatro escritas llegan intactas: mismo orden, misma fecha, mismo glosa.
    fechas_escritas = [o["fecha"] for o in operaciones[:4]]
    assert fechas_escritas == ["2020-07-08", "2020-07-20", "2020-07-25", "2020-07-31"]
    glossas_escritas = [o["glosa"] for o in operaciones[:4]]
    assert glossas_escritas == [o["glosa"] for o in CUATRO_ESCRITAS]

    assert PARTIDAS_DE_LAS_CUATRO_ESCRITAS <= partidas(operaciones), (
        "alguna de las cuatro operaciones escritas se perdio, se agrupo o se modifico"
    )
    # Y todas las cinco cuadran: la derivada tambien, aunque venga marcada.
    for operacion in operaciones:
        debe = round(sum(m["debe"] for m in operacion["asiento"]), 2)
        haber = round(sum(m["haber"] for m in operacion["asiento"]), 2)
        assert debe == haber, f"'{operacion['glosa']}' no cuadra: {debe} vs {haber}"


def test_el_envoltorio_en_un_array_de_un_elemento_tambien_separa_el_contexto(monkeypatch):
    """
    La API real no siempre devuelve el objeto {"contexto": ..., "operaciones": ...}
    desnudo: tambien lo envuelve en un array de un solo elemento. Los dos formatos son
    el mismo contrato; si el envoltorio no se desenvuelve, el flujo lo lee como una
    operacion plana sin cuenta y el documento entero se cae.
    """
    envuelto = json.dumps(
        [{
            "contexto": [{"tipo": "inventario_final", "importe": 10000}],
            "operaciones": CUATRO_ESCRITAS,
        }],
        ensure_ascii=False,
    )

    operaciones, _, resultado = leer(monkeypatch, contenido=envuelto)

    assert len(operaciones) == 5
    assert resultado.contexto == [{"tipo": "inventario_final", "importe": 10000}]
    assert [(m["cuenta"], m["debe"], m["haber"]) for m in derivada(operaciones)["asiento"]] == [
        ("69", 40000.0, 0.0),
        ("20", 0.0, 40000.0),
    ]


def test_el_dict_con_la_clave_context_es_el_mismo_contrato(monkeypatch):
    """
    Una lectura real de Groq escribio la clave `context` en lugar de `contexto`. Sin ese
    alias el contexto se perdia, no habia inventario final y el costo de ventas no salia.
    """
    alias = json.dumps({
        "context": [{"tipo": "inventario_final", "importe": 10000}],
        "operaciones": CUATRO_ESCRITAS,
    }, ensure_ascii=False)

    operaciones, _, resultado = leer(monkeypatch, contenido=alias)

    assert resultado.contexto == [{"tipo": "inventario_final", "importe": 10000}]
    assert len(operaciones) == 5, "el contexto con la clave traducida debe permitir derivar el costo"
    assert [(m["cuenta"], m["debe"], m["haber"]) for m in derivada(operaciones)["asiento"]] == [
        ("69", 40000.0, 0.0), ("20", 0.0, 40000.0),
    ]


def test_el_envoltorio_anidado_al_final_del_array_no_duplica_las_operaciones(monkeypatch):
    """
    Forma real: un array que abren las operaciones planas y CIERRA el envoltorio con las
    mismas operaciones duplicadas adentro. El envoltorio vale por el array completo: si se
    leyeran los dos juegos de golpe, el libro escribiria dos veces cada economia.
    """
    duplicado = json.dumps(
        list(CUATRO_ESCRITAS) + [{
            "contexto": [{"tipo": "inventario_final", "importe": 10000}],
            "operaciones": CUATRO_ESCRITAS,
        }],
        ensure_ascii=False,
    )

    operaciones, _, resultado = leer(monkeypatch, contenido=duplicado)

    assert len(operaciones) == 5, (
        f"el envoltorio duplico las operaciones: {len(operaciones)} en vez de 5"
    )
    assert [o["glosa"] for o in operaciones[:4]] == [o["glosa"] for o in CUATRO_ESCRITAS]
    assert resultado.contexto == [{"tipo": "inventario_final", "importe": 10000}]


def test_el_envoltorio_anidado_al_inicio_del_array_tambien_se_separa(monkeypatch):
    """La misma forma con el envoltorio al principio se lee igual y nada se pierde."""
    al_inicio = json.dumps(
        [
            {
                "contexto": [{"tipo": "inventario_final", "importe": 10000}],
                "operaciones": CUATRO_ESCRITAS,
            },
        ] + list(CUATRO_ESCRITAS[2:]),
        ensure_ascii=False,
    )

    operaciones, _, _ = leer(monkeypatch, contenido=al_inicio)

    assert len(operaciones) == 5
    assert [o["glosa"] for o in operaciones[:4]] == [o["glosa"] for o in CUATRO_ESCRITAS]


def test_el_asiento_derivado_tiene_los_importes_del_caso(monkeypatch):
    """0 de inventario inicial + 50,000 de compras - 10,000 de inventario final."""
    operaciones, _, _ = leer(monkeypatch)

    derivacion = derivada(operaciones)

    assert [(m["cuenta"], m["debe"], m["haber"]) for m in derivacion["asiento"]] == [
        ("69", 40000.0, 0.0),
        ("20", 0.0, 40000.0),
    ], "el costo derivado no es 69 al DEBE por 40,000 contra 20 al HABER por 40,000"

    # Las cuentas salen del plan de cuentas, no de un numero escrito a mano.
    assert ia._cuenta_por_naturaleza(6, "costo de ventas") == "69"
    assert ia._cuenta_por_naturaleza(2, "mercaderia") == "20"


def test_las_cuentas_del_asiento_derivado_vienen_del_catalogo(monkeypatch):
    """
    Si el plan de cuentas del ejercicio usara otras cuentas, el sistema debe derivar el
    mismo costo sobre ellas. Es la prueba de que "69 al DEBE y 20 al HABER" NO es una
    regla: son las cuentas que el catálogo dice que cumplen ese papel.
    """
    monkeypatch.setattr(ia, "CATALOGO_PCGE", (
        ("23", "Mercaderia en transito", 2),
        ("79", "Costo de ventas", 6),
    ))
    compras = respuesta_del_extractor(
        [{"tipo": "inventario_final", "importe": 10000}],
        [{
            "fecha": "2020-07-20",
            "glosa": "Se compra mercaderia al contado",
            "asiento": [
                {"cuenta": "23", "debe": 50000, "haber": 0},
                {"cuenta": "10", "debe": 0, "haber": 50000},
            ],
        }],
    )

    operaciones, _, _ = leer(monkeypatch, contenido=compras)

    assert [(m["cuenta"], m["debe"], m["haber"]) for m in derivada(operaciones)["asiento"]] == [
        ("79", 40000.0, 0.0),
        ("23", 0.0, 40000.0),
    ], "las cuentas del asiento derivado tienen que salir del plan de cuentas"


# =========================================================================== #
# 2. La frase de inventario no es un asiento
# =========================================================================== #
def test_la_frase_de_inventario_no_es_un_asiento_independiente(monkeypatch):
    operaciones, _, resultado = leer(monkeypatch)

    glosas = [str(o.get("glosa", "")).lower() for o in operaciones]
    assert not [g for g in glosas if FRASE_DE_INVENTARIO in g], (
        f"la frase que OBSERVA el saldo final se transcribio como asiento: {glosas}"
    )

    # El saldo no se perdio: se conserva como contexto, que es lo que permite derivarlo.
    assert resultado.contexto == [{"tipo": "inventario_final", "importe": 10000}]

    # Y el unico asiento que lo menciona es el derivado, con la marca a la vista.
    assert derivada(operaciones)["glosa"].startswith("DERIVADO / REVISAR")


def test_una_frase_con_importes_sigue_siendo_contexto_y_no_una_operacion(monkeypatch):
    """
    Que haya un saldo no convierte la frase en operacion. Si el extractor la escribe
    igual como operacion, el costo derivado no puede salir de ahi: el saldo ya esta
    contado en las compras del documento.
    """
    operaciones, _, _ = leer(monkeypatch, contenido=respuesta_del_extractor(
        [{"tipo": "inventario_final", "importe": 10000}],
        CUATRO_ESCRITAS[1:2] + [{
            "fecha": "2020-07-31",
            "glosa": "En el inventario se observa un saldo final de 10,000 soles",
            "asiento": [
                {"cuenta": "20", "debe": 0, "haber": 10000},
                {"cuenta": "69", "debe": 10000, "haber": 0},
            ],
        }],
    ))

    assert [(m["cuenta"], m["debe"], m["haber"]) for m in derivada(operaciones)["asiento"]] == [
        ("69", 40000.0, 0.0),
        ("20", 0.0, 40000.0),
    ], "la frase del inventario se conto como compras y contamino el costo derivado"


# =========================================================================== #
# 3-5. Revision, supuestos y fecha
# =========================================================================== #
def test_el_asiento_derivado_queda_marcado_para_revision(monkeypatch):
    operaciones, _, _ = leer(monkeypatch)

    derivacion = derivada(operaciones)

    assert derivacion.get("revisar") is True, (
        "un asiento que el sistema dedujo y el documento no escribe va siempre marcado "
        "para revision humana"
    )
    incertidumbres = derivacion.get("incertidumbres", "")
    assert "inventario inicial" in incertidumbres, (
        "el supuesto de inventario inicial = 0 debe quedar escrito, no escondido"
    )
    assert "fecha" in incertidumbres, "la falta de fecha de cierre debe quedar escrita"


def test_no_se_inventa_la_fecha_del_asiento_derivado(monkeypatch):
    """
    El documento dice "al cierre de mes" y no dice de que mes. El asiento derivado llega
    SIN fecha para que la complete quien revisa: la fecha de hoy seria inventarla.
    """
    operaciones, _, _ = leer(monkeypatch)

    assert derivada(operaciones)["fecha"] == "", (
        "el documento no da una fecha de cierre inequivoca: el asiento derivado tiene "
        "que llegar sin fecha"
    )
    assert operaciones[0]["fecha"] == "2020-07-08", "las fechas escritas si se leen del documento"


def test_si_el_documento_da_la_fecha_de_cierre_se_usa_esa(monkeypatch):
    """Con fecha inequivoca en el contexto, se usa esa y se deja de avisar por ella."""
    operaciones, _, _ = leer(monkeypatch, contenido=respuesta_del_extractor(
        [{"tipo": "inventario_final", "importe": 10000, "fecha": "31/07/2020"}],
        CUATRO_ESCRITAS[1:2],
    ))

    derivacion = derivada(operaciones)
    assert derivacion["fecha"] == "2020-07-31"
    assert "fecha" not in derivacion["incertidumbres"]


# =========================================================================== #
# Cuando NO se puede determinar con seguridad, no se inventa
# =========================================================================== #
def test_sin_compras_no_se_deriva_ningun_asiento(monkeypatch):
    """Sin compras del periodo no hay costo de ventas que calcular."""
    operaciones, _, resultado = leer(monkeypatch, contenido=respuesta_del_extractor(
        [{"tipo": "inventario_final", "importe": 10000}], CUATRO_ESCRITAS[0:1],
    ))

    assert len(operaciones) == 1, "sin compras no hay nada que derivar"
    assert [a for a in resultado.avisos if "no hay costo de ventas que derivar" in a]


def test_si_el_inventario_no_baja_no_se_deriva_ningun_asiento(monkeypatch):
    """Compras por debajo del inventario final significa devoluciones: no hay costo."""
    operaciones, _, resultado = leer(monkeypatch, contenido=respuesta_del_extractor(
        [{"tipo": "inventario_final", "importe": 80000}], CUATRO_ESCRITAS[1:2],
    ))

    assert len(operaciones) == 1
    assert [a for a in resultado.avisos if "no sale mercadería" in a]


# =========================================================================== #
# El resto de los origenes no cambian
# =========================================================================== #
def test_el_pdf_no_deriva_nada_aunque_ia_declare_contexto(monkeypatch):
    """
    El contexto y la derivacion son de la hoja de calculo. Un PDF entrega el prompt
    base y, aunque viniera un `contexto`, el sistema no deriva nada: el PDF se lee como
    siempre.
    """
    operaciones, extractor, _ = leer(monkeypatch, origen="pdf")

    assert len(operaciones) == 4, "el PDF debe producir solo lo que el documento escribe"
    assert not [o for o in operaciones if str(o.get("glosa", "")).startswith("DERIVADO")]

    sistema = extractor.llamadas[0]["messages"][0]["content"]
    assert sistema == ia.instrucciones_agente_excel, "el PDF recibio algo distinto del prompt base"


def test_el_escaneo_visual_no_deriva_nada(monkeypatch):
    operaciones, _, _ = leer(monkeypatch, origen="", texto="imagen escaneada")
    assert len(operaciones) == 4
    assert not [o for o in operaciones if str(o.get("glosa", "")).startswith("DERIVADO")]


# =========================================================================== #
# 6-7. El borrador completo: un guardado y ningun rerun de mas
# =========================================================================== #
@pytest.fixture
def base_temporal(tmp_path, monkeypatch):
    """
    Una base limpia con el PCGE, en un archivo temporal. `datos/contabilidad.db` no se
    abre: el borrador de estas pruebas se escribe en la copia, no en la evidencia.
    """
    ruta = str(tmp_path / "contabilidad_de_prueba.db")
    bd.inicializar_base_de_datos_si_es_necesario(ruta)
    monkeypatch.setattr(lg, "DB_PATH", ruta)
    return ruta


def _contar(ruta, tabla):
    conexion = sqlite3.connect(ruta)
    try:
        return conexion.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
    finally:
        conexion.close()


def _libro(ruta):
    """(fecha, glosa, [(cuenta, debe, haber), ...]) de cada asiento, en orden."""
    conexion = sqlite3.connect(ruta)
    try:
        filas = conexion.execute(
            "SELECT a.id, a.fecha, a.glosa, d.cuenta_codigo, d.debe, d.haber"
            " FROM Asientos a JOIN Detalles d ON d.asiento_id = a.id ORDER BY a.id, d.id"
        ).fetchall()
    finally:
        conexion.close()

    asientos = []
    for asiento_id, fecha, glosa, cuenta, debe, haber in filas:
        if not asientos or asientos[-1][0] != asiento_id:
            asientos.append((asiento_id, fecha, glosa, []))
        asientos[-1][3].append((cuenta, debe, haber))
    return [(a[1], a[2], a[3]) for a in asientos]


def _app_de_prueba():
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(RAIZ / "app.py"), default_timeout=90)


def _borrador_por_sesion(at, operaciones):
    at.session_state["menu_option"] = 1  # Registro de Transacciones
    at.session_state["borrador_ia"] = operaciones
    at.session_state["borrador_id"] = 1
    at.session_state["origen_borrador"] = "documento cargado"
    at.session_state["avisos_borrador"] = []
    at.session_state["resumen_borrador"] = ""
    return at


def _boton_de_guardado(at):
    for boton in at.button:
        if boton.label.startswith("Guardar Asientos Aprobados"):
            return boton
    raise AssertionError("no se encontro el boton de guardado en el borrador")


def _fechas_del_borrador(at):
    """Solo las del borrador: la pagina tiene otros selectores de fecha."""
    return [w for w in at.date_input if w.label == "Fecha del asiento"]


FECHAS_DE_LAS_CUATRO = [
    datetime.date(2020, 7, 8),
    datetime.date(2020, 7, 20),
    datetime.date(2020, 7, 25),
    datetime.date(2020, 7, 31),
]


def test_un_unico_guardado_escribe_los_cinco_asientos(base_temporal, monkeypatch):
    """Del Excel a la base: cinco asientos y diez partidas, en una sola aprobacion."""
    operaciones, _, _ = leer(monkeypatch)

    at = _borrador_por_sesion(_app_de_prueba(), operaciones)
    at.run()

    # El derivado llega sin fecha: el borrador avisa y no deja aprobarlo hasta que el
    # humano la escriba. Eso es lo que impide que el sistema ponga una fecha cualquiera.
    assert any("FECHA NO ENCONTRADA" in w.value for w in at.warning), (
        "el asiento derivado tiene que llegar pidiendo fecha, no con una fecha puesta"
    )
    fechas = _fechas_del_borrador(at)
    assert len(fechas) == 5, f"el borrador deberia pintar 5 asientos y pinto {len(fechas)}"
    assert _boton_de_guardado(at).disabled, "con una fecha sin completar no se puede guardar"

    # Las cuatro escritas ya traen la fecha del documento.
    for indice, esperada in enumerate(FECHAS_DE_LAS_CUATRO):
        assert fechas[indice].value == esperada
    assert fechas[4].value is None, "el asiento derivado debe llegar sin fecha"

    # Quien revisa la completa y guarda una sola vez.
    fechas[4].set_value(datetime.date(2020, 7, 31))
    at.run()
    _boton_de_guardado(at).click().run()

    assert _contar(base_temporal, "Asientos") == 5, (
        f"un solo guardado debe dejar 5 asientos y dejo {_contar(base_temporal, 'Asientos')}"
    )
    assert _contar(base_temporal, "Detalles") == 10

    libro = _libro(base_temporal)
    assert len(libro) == 5
    for indice, (fecha, glosa, _) in enumerate(libro[:4]):
        assert fecha == FECHAS_DE_LAS_CUATRO[indice].isoformat()
        assert glosa == CUATRO_ESCRITAS[indice]["glosa"]

    fecha_derivada, glosa_derivada, partidas_derivadas = libro[4]
    assert fecha_derivada == "2020-07-31"
    assert glosa_derivada.startswith("DERIVADO / REVISAR")
    assert partidas_derivadas == [("69", 40000.0, 0.0), ("20", 0.0, 40000.0)]

    # La frase que observaba el saldo no aparece en el libro como glosa propia.
    for _, glosa, _ in libro:
        assert "se observa un saldo" not in glosa.lower(), (
            f"la frase de inventario llego al libro como glosa: {glosa}"
        )


def test_los_reruns_no_vuelven_a_guardar_el_mismo_borrador(base_temporal, monkeypatch):
    """
    Un rerun de Streamlit vuelve a dibujar la pagina. Como el borrador se consume al
    guardar, el rerun no tiene nada que guardar: la base no crece.
    """
    operaciones, _, _ = leer(monkeypatch)

    at = _borrador_por_sesion(_app_de_prueba(), operaciones)
    at.run()
    fechas = _fechas_del_borrador(at)
    fechas[4].set_value(datetime.date(2020, 7, 31))
    at.run()

    _boton_de_guardado(at).click().run()
    assert _contar(base_temporal, "Asientos") == 5

    for _ in range(3):
        at.run()

    assert _contar(base_temporal, "Asientos") == 5, (
        "un rerun volvio a guardar el borrador: los asientos se duplicaron solos"
    )
    assert _contar(base_temporal, "Detalles") == 10
    assert not at.session_state.get("borrador_ia"), "el borrador deberia quedar consumido"


# =========================================================================== #
# 8. Normalizacion determinista de las cuentas que la IA INFIRIO (solo Excel)
# =========================================================================== #
def _gasto_inferido(cuenta):
    return {
        "fecha": "2020-07-31",
        "glosa": "Se pagan gastos operativos por 20,000 soles al contado",
        "asiento": [
            {"cuenta": cuenta, "debe": 20000, "haber": 0},
            {"cuenta": "10", "debe": 0, "haber": 20000},
        ],
    }


@pytest.mark.parametrize("inferida", ["70", "68", "74", "30"])
def test_el_gasto_operativo_se_remapea_a_la_cuenta_de_gasto_del_catalogo(inferida):
    """
    En las lecturas reales la IA nunca acierta el gasto del caso: escribio 70, 68, 74 y 30.
    Como la glosa declara un gasto y la partida en tela esta en el DEBE (el lado de un
    gasto, elemento 6, segun logica.naturaleza_de_elemento), el codigo se reemplaza por la
    cuenta de gasto generico del catalogo y el asiento queda marcado para revision.
    """
    operacion = _gasto_inferido(inferida)
    avisos = ia._normalizar_cuentas_excel([operacion])

    assert [(m["cuenta"], m["debe"], m["haber"]) for m in operacion["asiento"]] == [
        ("63", 20000.0, 0.0), ("10", 0.0, 20000.0),
    ], "el gasto operativo mal leido debe cerrar en 63 al DEBE contra 10 al HABER"
    assert operacion["revisar"] is True
    assert any("reemplazo por la 63" in aviso for aviso in avisos)


def test_el_gasto_bien_leido_no_se_toca():
    operacion = _gasto_inferido("63")
    avisos = ia._normalizar_cuentas_excel([operacion])
    assert not avisos
    assert "revisar" not in operacion


def test_la_venta_bien_leida_no_se_marca_para_revision():
    venta = {
        "glosa": "SE realiza una venta por 70,000 soles al credito",
        "asiento": [
            {"cuenta": "12", "debe": 70000, "haber": 0},
            {"cuenta": "70", "debe": 0, "haber": 70000},
        ],
    }
    avisos = ia._normalizar_cuentas_excel([venta])
    assert not avisos
    assert "revisar" not in venta


def test_la_venta_invertida_no_se_inventa_y_queda_para_revision():
    """
    Una lectura real invirtio la venta: 10 al DEBE y 12 al HABER. Como el record de la
    venta (elemento 7, acreedora) falta y no hay regla de venta escrita a mano, la capa no
    corrige nada: el asiento llega marcado para revision humana.
    """
    invertida = {
        "glosa": "SE realiza una venta por 70,000 soles al credito",
        "asiento": [
            {"cuenta": "10", "debe": 70000, "haber": 0},
            {"cuenta": "12", "debe": 0, "haber": 70000},
        ],
    }
    avisos = ia._normalizar_cuentas_excel([invertida])

    assert [m["cuenta"] for m in invertida["asiento"]] == ["10", "12"], (
        "la venta invertida no se remapea: no se sabe con que seguridad corregirla"
    )
    assert invertida["revisar"] is True
    assert any("no se puede confirmar" in aviso for aviso in avisos)


def test_un_gasto_con_dos_partidas_en_tela_no_se_remapea():
    """Dos partidas en el DEBE del gasto: no se sabe cual reemplazar y no se inventa."""
    dudoso = {
        "glosa": "Se pagan gastos operativos por 20,000 soles al contado",
        "asiento": [
            {"cuenta": "70", "debe": 20000, "haber": 0},
            {"cuenta": "12", "debe": 20000, "haber": 0},
            {"cuenta": "10", "debe": 0, "haber": 40000},
        ],
    }
    avisos = ia._normalizar_cuentas_excel([dudoso])

    assert [m["cuenta"] for m in dudoso["asiento"]] == ["70", "12", "10"]
    assert dudoso["revisar"] is True
    assert any("no se puede confirmar" in aviso for aviso in avisos)


def test_la_normalizacion_sale_del_catalogo_no_de_un_numero_fijo(monkeypatch):
    """No hay 63 ni 70 escritos a mano: la cuenta sale del plan de cuentas del ejercicio."""
    monkeypatch.setattr(ia, "CATALOGO_PCGE", (
        ("65", "Otros gastos de gestion y servicios prestados por terceros", 6),
        ("10", "Efectivo y equivalentes de efectivo", 1),
    ))
    operacion = _gasto_inferido("70")
    ia._normalizar_cuentas_excel([operacion])
    assert [(m["cuenta"], m["debe"], m["haber"]) for m in operacion["asiento"]] == [
        ("65", 20000.0, 0.0), ("10", 0.0, 20000.0),
    ]


def test_la_compra_bien_leida_no_se_toca():
    compra = {
        "glosa": "Se compra 50,000 de mercaderia al contado",
        "asiento": [
            {"cuenta": "20", "debe": 50000, "haber": 0},
            {"cuenta": "10", "debe": 0, "haber": 50000},
        ],
    }
    avisos = ia._normalizar_cuentas_excel([compra])
    assert not avisos
    assert "revisar" not in compra


def test_gastaron_no_se_confunde_con_gastos():
    """La deteccion es por palabra completa: no es una regla de substring."""
    assert ia._concepto_de_glosa("Se gastaron los ahorros del mes") is None


def test_el_pdf_no_pasa_por_la_normalizacion_determinista(monkeypatch):
    """Solo la hoja de calculo infiere cuentas: el PDF conserva lo que leyo la IA."""
    mal_escritas = [{
        "fecha": "2020-07-31",
        "glosa": "Se pagan gastos operativos por 20,000 soles al contado",
        "asiento": [
            {"cuenta": "70", "debe": 20000, "haber": 0},
            {"cuenta": "10", "debe": 0, "haber": 20000},
        ],
    }]
    contenido = json.dumps({"contexto": [], "operaciones": mal_escritas}, ensure_ascii=False)

    operaciones, _, _ = leer(monkeypatch, contenido=contenido, origen="pdf")

    gasto = [o for o in operaciones if "gastos" in str(o.get("glosa", "")).lower()][0]
    assert [(m["cuenta"], m["debe"], m["haber"]) for m in gasto["asiento"]] == [
        ("70", 20000.0, 0.0), ("10", 0.0, 20000.0),
    ], "el PDF no pasa por la normalizacion determinista de Excel"


def test_el_flujo_real_normaliza_el_gasto_y_deriva_el_costo(monkeypatch):
    """Del texto real de la hoja: el gasto mal leido cierra en 63 y se deriva 69/20."""
    mal_escritas = [
        {
            "fecha": "2020-07-08",
            "glosa": "Se crea una empresa con 100,000 al contado.",
            "asiento": [
                {"cuenta": "10", "debe": 100000, "haber": 0},
                {"cuenta": "50", "debe": 0, "haber": 100000},
            ],
        },
        {
            "fecha": "2020-07-20",
            "glosa": "Se compra 50,000 de mercaderia al contado",
            "asiento": [
                {"cuenta": "20", "debe": 50000, "haber": 0},
                {"cuenta": "10", "debe": 0, "haber": 50000},
            ],
        },
        {
            "fecha": "2020-07-25",
            "glosa": "SE realiza una venta por 70,000 soles al credito",
            "asiento": [
                {"cuenta": "12", "debe": 70000, "haber": 0},
                {"cuenta": "70", "debe": 0, "haber": 70000},
            ],
        },
        {
            "fecha": "2020-07-31",
            "glosa": "Se pagan gastos operativos por 20,000 soles al contado",
            "asiento": [
                {"cuenta": "70", "debe": 20000, "haber": 0},
                {"cuenta": "10", "debe": 0, "haber": 20000},
            ],
        },
    ]
    contenido = respuesta_del_extractor(
        [{"tipo": "inventario_final", "importe": 10000}], mal_escritas
    )

    operaciones, _, resultado = leer(monkeypatch, contenido=contenido)

    assert len(operaciones) == 5, "la normalizacion y la derivacion deben cerrar en 5 asientos"
    gasto = operaciones[3]
    assert [(m["cuenta"], m["debe"], m["haber"]) for m in gasto["asiento"]] == [
        ("63", 20000.0, 0.0), ("10", 0.0, 20000.0),
    ]
    assert gasto["revisar"] is True
    assert [(m["cuenta"], m["debe"], m["haber"]) for m in derivada(operaciones)["asiento"]] == [
        ("69", 40000.0, 0.0), ("20", 0.0, 40000.0),
    ]
    assert [a for a in resultado.avisos if "reemplazo por la 63" in a]