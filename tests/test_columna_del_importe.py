"""
LA COLUMNA DEL IMPORTE NO SE PIERDE AL LEER EL PDF  (regresion de la depreciacion)

Que falla y por que existe este archivo
---------------------------------------
El libro de contabilidad del taller escribe cada importe DEBAJO de una columna rotulada
(IZQUIERDO / DERECHO = CARGO / ABONO = DEBE / HABER). Esa posicion es lo unico que el
documento dice sobre el LADO del importe.

En el asiento de depreciacion el 135 de la 39 esta en la columna HABER y el 135 de
la 68 esta en la columna DEBE:

    ['30/09/2009 39 Depreciacion ACUMULADA -A', '', '135']   <- 135 en HABER
    ['68 Gastos de Depreciacion G+',           '135', '']    <- 135 en DEBE

`_serializar_tabla` quitaba las celdas vacias antes de devolver la fila, y con eso las
dos quedaban identicas ("... | 135"): el importe se quedaba sin columna. Como ademas
el prompt prohibia deducir la columna, al modelo no le quedaba mas que sacar el lado
del signo "-A", y por eso la 39 caia al DEBE y el asiento no cuadraba.

Este archivo fija las DOS mitades de la correccion:
  1. La lectura conserva la posicion de los importes (no borra las celdas vacias).
  2. El prompt manda leer la COLUMNA del importe y sigue prohibiendo sacar el lado
     del SIGNO, que es el error que produjo los tres fallos anteriores.
Y sigue exigiendo que Python NO corrija el lado: el arreglo es de lectura, no una regla
que invierta columnas a posteriori.
"""

import ast
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("GROQ_API_KEY", "clave-de-pruebas")

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia_engine as ia  # noqa: E402

FUENTE_APP = (RAIZ / "app.py").read_text(encoding="utf-8")
ARBOL_APP = ast.parse(FUENTE_APP)

# Las filas del PDF tal como las entrega pdfplumber, con sus celdas vacias.
FILAS_LIBRO = [
    ["30/09/2009 39 Depreciacion ACUMULADA -A", "", "135"],
    ["", None, "", None],
    ["68 Gastos de Depreciacion G+", "135", "", None],
]


def _cargar_de_app(*nombres):
    """
    Ejecuta en un namespace limpio solo las funciones pedidas de `app.py`.

    Importar `app` entero levanta la interfaz de Streamlit en modo bare y ensucia la
    salida de las pruebas. Estas funciones son puras y no dependen de Streamlit, asi
    que se cargan desde el fuente real: si alguien las borra del archivo, la prueba
    falla aqui en vez de dar un falso OK.
    """
    espacio = {"re": re}
    pedidos = set(nombres)
    for nodo in ARBOL_APP.body:
        if isinstance(nodo, ast.FunctionDef):
            declarados = [nodo.name]
        elif isinstance(nodo, ast.Assign):
            declarados = [t.id for t in nodo.targets if isinstance(t, ast.Name)]
        else:
            continue
        if not pedidos.intersection(declarados):
            continue
        exec(compile(ast.unparse(nodo), "app.py", "exec"), espacio)

    faltan = pedidos - set(espacio)
    assert not faltan, f"app.py ya no define: {', '.join(sorted(faltan))}"
    return espacio


def _columna_del_importe(celdas):
    """Indice de la celda con el importe, o None si la fila no trae importe."""
    for indice, celda in enumerate(celdas):
        if celda and re.fullmatch(r"[\d.,]+", celda):
            return indice
    return None


def _plano(texto):
    """El prompt esta envuelto a 90 columnas: las frases se buscan sin saltos de linea."""
    return " ".join(texto.split())


# =========================================================================== #
# 1. La lectura del PDF conserva la columna del importe
# =========================================================================== #
def test_la_serializacion_conserva_la_columna_del_importe():
    espacio = _cargar_de_app("_serializar_tabla")
    filas = espacio["_serializar_tabla"](FILAS_LIBRO)

    # La fila totalmente vacia del libro si se descarta; las dos con contenido, no.
    assert len(filas) == 2, "solo se descarta la fila enteramente vacia"
    assert len({len(fila) for fila in filas}) == 1, (
        "la rejilla tiene que ser rectangular: si una fila llega con menos celdas, "
        "'el importe que esta bajo HABER' apunta a una columna distinta en cada linea"
    )
    assert all(len(fila) == 4 for fila in filas), "las cuatro celdas del libro se conservan"


def test_la_depreciacion_y_el_gasto_siguen_en_columnas_distintas():
    """La 39 y la 68 deben seguir distinguiendose: es el asiento que se rompia."""
    espacio = _cargar_de_app("_serializar_tabla")
    filas = espacio["_serializar_tabla"](FILAS_LIBRO)

    linea_39, linea_68 = filas
    assert "39" in linea_39[0] and linea_39[1] == "" and linea_39[2] == "135"
    assert "68" in linea_68[0] and linea_68[1] == "135" and linea_68[2] == ""

    columna_39 = _columna_del_importe(linea_39)
    columna_68 = _columna_del_importe(linea_68)
    assert columna_39 == 2, "el importe de la 39 debe quedar en la columna HABER"
    assert columna_68 == 1, "el importe de la 68 debe quedar en la columna DEBE"
    assert columna_39 != columna_68, "las dos lineas son indistinguibles: el lado se perdio"


def test_la_sola_fila_vacia_se_descarta():
    espacio = _cargar_de_app("_serializar_tabla")
    filas = espacio["_serializar_tabla"]([["", None], ["cuenta", "1,000"]])
    assert filas == [["cuenta", "1,000"]]


def test_la_seleccion_de_capa_no_cambio_por_conservar_las_vacias():
    """
    Que `_serializar_tabla` conserve las vacias no puede cambiar QUE PAGINAS se leen
    como tabla y cuales como texto maquetado: el criterio de fiabilidad cuenta columnas
    CON CONTENIDO. Si empieza a contar celdas, una pagina con muchas filas de glosa
    pasaria a leerse como tabla y su texto cambiaria entero.
    """
    espacio = _cargar_de_app(
        "_serializar_tabla", "_tabla_es_fiable", "_FICHAS", "FIDELIDAD_MINIMA"
    )
    fiable = espacio["_tabla_es_fiable"]

    texto = (
        "01/04/2009 10 Caja (efectivo) A+ 10,000 50 Patrimonio (Capital social) PAT+ 10,000 "
        "20 Inventarios A+ 4,000 10 Caja (efectivo) A- 4,000"
    )
    # Dos filas con dos celdas con contenido de cuatro: 2/4 = 0.5, menor que 0.6.
    filas = espacio["_serializar_tabla"]([
        ["Glosa: aporte de capital"],
        ["01/04/2009 10 Caja (efectivo) A+", "10,000", ""],
        ["Glosa: venta al contado"],
        ["10 Caja (efectivo) A+", "3,000", ""],
    ])
    assert sum(1 for fila in filas if sum(1 for celda in fila if celda) >= 2) == 2
    assert not fiable(filas, texto), "sigue descartando la tabla con poca estructura"

    # Con estructura suficiente, la misma pagina si es fiable.
    filas_buenas = espacio["_serializar_tabla"]([
        ["10 Caja (efectivo) A+", "10,000", ""],
        ["50 Patrimonio (Capital social) PAT+", "", "10,000"],
        ["20 Inventarios A+", "4,000", ""],
        ["10 Caja (efectivo) A-", "", "4,000"],
    ])
    assert fiable(filas_buenas, texto)


# =========================================================================== #
# 2. El prompt manda leer la COLUMNA del importe y no el SIGNO
# =========================================================================== #
def test_el_prompt_dice_que_la_columna_del_importe_manda():
    criterio = _plano(ia.CRITERIO_DEBE_HABER)
    assert "LA COLUMNA DEL IMPORTE sí es una columna" in criterio
    assert "una celda VACÍA intercalada no es un importe, pero la CUENTA" in criterio
    assert "el importe se lee en la columna en la que está escrito" in criterio
    assert 'una línea "39 ... -A" cuyo importe está en la columna que ya calibraste como HABER va al HABER' in criterio
    assert "El signo explica el efecto; la columna explica el lado." in criterio


def test_el_prompt_sigue_prohibiendo_sacar_el_lado_del_signo():
    """La regla que evita los tres fallos anteriores no se toca: el signo no es columna."""
    criterio = _plano(ia.CRITERIO_DEBE_HABER)
    assert "EL SIGNO nunca es una columna" in criterio
    assert '"+" en cargo ni un "-" en abono' in criterio
    assert 'No saques el lado de un "+" o de un "-"' in criterio
    assert "PROHIBIDO" in criterio, "sigue vigente la lista de equivalencias prohibidas"


def test_el_prompt_ya_no_prohibe_leer_la_posicion_del_importe():
    criterio = _plano(ia.CRITERIO_DEBE_HABER)
    assert "NO deduzcas la columna por la posición" not in criterio
    assert "la posición, las tabulaciones, las sangrías ni la clase de la cuenta" not in criterio, (
        "esa frase es la que prohibia leer la columna del importe"
    )


def test_el_prompt_calibra_las_columnas_con_el_documento():
    """
    La pagina 2 del libro no rotula sus columnas: los rotulos (IZQUIERDO/DERECHO,
    CARGO/ABONO, DEBE/HABER) estan en la pagina 1. Por eso la calibracion tiene que
    poder apoyarse en las filas de notacion inequivoca de la propia pagina, y no solo
    en un rotulo que puede no estar delante.
    """
    criterio = _plano(ia.CRITERIO_DEBE_HABER)
    assert "CALIBRA LAS COLUMNAS antes de fijar el lado" in criterio
    assert "Los rótulos pueden estar en otra página del mismo libro" in criterio
    assert "Si NO hay rótulo a la vista, deduce la correspondencia con las filas cuya" in criterio
    assert "un ACTIVO que AUMENTA (10 Caja A+, 20 Inventarios A+) lleva su importe en la columna del DEBE" in criterio
    assert "un ACTIVO que DISMINUYE o un INGRESO que AUMENTA" in criterio
    assert "lo llevan en la del HABER" in criterio
    assert "aplícala a TODAS las filas de la página aunque su notación parezca contradecirla" in criterio


def test_la_calibracion_no_prohíbe_las_contras_cuando_el_ingreso_aumenta():
    """
    "Todo lo que aumenta va al DEBE" es FALSO y es la trampa de este asiento: el 70
    Ingresos V+ esta en la columna del HABER y la 39 tambien, aunque las dos "aumenten".
    La calibracion tiene que nombrar los dos casos que no admiten duda, no una regla
    general de aumento.
    """
    criterio = _plano(ia.CRITERIO_DEBE_HABER)
    assert 'NO uses "todo lo que aumenta va al DEBE"' in criterio
    assert "un ingreso que aumenta va al HABER, igual que una contra-activo que acumula" in criterio


def test_la_calibracion_no_depende_de_la_clase_de_la_cuenta():
    """Calibrar con 'activo que aumenta' es un hecho contable, no una clase -> columna."""
    criterio = _plano(ia.CRITERIO_DEBE_HABER)
    assert "calibra con el documento, no con la clase de la cuenta" in criterio
    assert "nunca por la clase de la cuenta ni por la sangría ni por el código" in criterio


def test_lo_que_ya_se_sabia_de_la_39_sigue_estar():
    """Reescribir la seccion E no puede borrar el criterio contable de la 39."""
    criterio = _plano(ia.CRITERIO_DEBE_HABER)
    assert "contra-activo (19, 29, 36, 39)" in criterio
    assert "DEPRECIACIÓN: el gasto del periodo (68) va al DEBE y la depreciación acumulada (39) va al" in criterio
    assert "la 39 es correctora del activo y ACREEDORA" in criterio


def test_el_prompt_no_fija_columnas_por_clase_de_cuenta():
    """La correccion no puede colarse como una equivalencia de clase -> columna."""
    for prompt in (ia.instrucciones_agente_excel, ia.instrucciones_contador):
        assert "SIEMPRE van al DEBE" not in prompt
        assert "SIEMPRE van al HABER" not in prompt
        assert "DINÁMICA DE LAS CUENTAS" not in prompt


def test_el_criterio_sigue_estando_en_los_dos_prompts():
    assert ia.CRITERIO_DEBE_HABER in ia.instrucciones_agente_excel
    assert ia.CRITERIO_DEBE_HABER in ia.instrucciones_contador


# =========================================================================== #
# 3. El arreglo es de LECTURA: Python sigue sin mover importes
# =========================================================================== #
def test_python_no_mueve_el_importe_de_la_39_al_haber():
    """
    Si la IA entrega la 39 en el DEBE, el sistema lo conserva y lo marca. Moverlo en
    Python seria la regla artificial "39 siempre al HABER" que este arreglo evita: el
    problema era de lectura del documento, no de validacion.
    """
    lote = [{
        "fecha": "2009-09-30",
        "glosa": "Depreciacion",
        "asiento": [
            {"cuenta": "39", "debe": 270.0, "haber": 0.0},
            {"cuenta": "68", "debe": 270.0, "haber": 0.0},
        ],
    }]
    resultado, avisos, error = ia.procesar_dinamica_contable(lote)
    assert resultado is not None, error
    asiento = list(resultado)[0]

    assert asiento["asiento"][0] == {"cuenta": "39", "debe": 270.0, "haber": 0.0}, (
        "Python no invierte la columna de la 39 por su clase ni por su elemento"
    )
    assert asiento["revisar"] is True
    assert any("no cuadra" in a for a in avisos)


def test_el_motor_sigue_declarando_que_no_decide_el_lado():
    """El contrato del nucleo contable no cambio: el lado lo decide la lectura."""
    fila = _plano(ia._leer_fila.__doc__)
    assert "NO se infiere el lado del importe" in fila
    assert "NO se invierte una columna por la clase o el elemento PCGE de la cuenta" in fila

    nucleo = _plano(ia.procesar_dinamica_contable.__doc__)
    assert "NO se mueven importes" in nucleo
    assert "NO se corrige la asignacion" in nucleo


# =========================================================================== #
# 4. La ruta de documentos sigue enviando el prompt base byte a byte
# =========================================================================== #
def test_el_prompt_base_llega_byte_a_byte_a_la_ruta_de_documentos(monkeypatch):
    """
    El PDF y el Excel siguen mandando el prompt base, sin variantes por tipo de
    documento ni recortes: si el prompt se bifurcara, estas reglas dejarian de
    aplicar justo en la ruta que se esta corrigiendo.
    """
    captured = {}

    class _Eleccion:
        finish_reason = "stop"
        message = type("M", (), {"content": "[]"})()

    class _Respuesta:
        choices = [_Eleccion()]

    class _Cliente:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **parametros):
            captured["messages"] = parametros["messages"]
            return _Respuesta()

    monkeypatch.setattr(ia, "cliente", _Cliente())
    ia.analizar_excel_completo("=== PAGINA 1 ===\n30/09/2009 39 Depreciacion -A | 135")

    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == ia.instrucciones_agente_excel
    assert ia.CRITERIO_DEBE_HABER in captured["messages"][0]["content"]