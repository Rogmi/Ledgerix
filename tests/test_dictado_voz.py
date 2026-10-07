"""
Dictado por Voz: el MISMO pipeline que los documentos, con dos decisiones propias.

Antes el dictado usaba `extraer_asiento_de_texto`, cuyo contrato exigia SOLO la
lista plana de partidas (cuenta/debe/haber). Ese contrato no trae fecha ni glosa
ni estructura multi-operacion, y de ahi salian los tres fallos: borrador sin
fecha aunque el enunciado la traia, glosa armada con los primeros caracteres de la
transcripcion cruda, y varias transacciones fusionadas en un solo asiento porque
el prompt declara "UN solo asiento".

Estas pruebas fijan el comportamiento nuevo sin tocar la red:
  - FECHA: solo viaja al borrador una fecha ESCRITA en la transcripcion. Una que
    la IA invento se vacia para revision humana; si el texto trae una sola fecha
    y la IA no la uso, se usa esa; si el texto no trae ninguna, el borrador queda
    sin fecha (nunca con una inventada ni con la de hoy).
  - GLOSA: se deriva de forma DETERMINISTA de la oracion transcrita (fecha,
    importes, moneda, muletillas y nexos colgantes fuera), sin depender de que la
    IA redacte.
  - MULTI-OPERACION: una transcripcion con varias transacciones genera varios
    asientos, cada uno con su fecha y su glosa.

La oracion del pipeline (`analizar_documento_completo`) se sustituye por un doble
de prueba: la suite es gratuita, determinista y no depende de la cuota.
"""

import os
import sys
from pathlib import Path

# El modulo construye un cliente de Groq al importarse. Ninguna prueba de este
# archivo llega a la red, pero la clave debe existir para que el import no reviente.
os.environ.setdefault("GROQ_API_KEY", "clave-de-pruebas")

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia_engine as ia  # noqa: E402
import pytest  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers: dobles de prueba del pipeline de documentos
# --------------------------------------------------------------------------- #
def _resultado(operaciones, avisos=None):
    return ia.ResultadoIA(operaciones, avisos or [])


def _instalar_pipeline(monkeypatch, operaciones, avisos=None):
    """Sustituye `analizar_documento_completo` por un doble que ya viene procesado."""
    respuestas = [operaciones]
    avisos_respuesta = list(avisos or [])

    def doble(texto, origen=None):
        operaciones_doble = [dict(op, asiento=list(op.get("asiento") or [])) for op in respuestas[0]]
        return True, _resultado(operaciones_doble, list(avisos_respuesta))

    monkeypatch.setattr(ia, "analizar_documento_completo", doble)


# --------------------------------------------------------------------------- #
# Fechas: solo cuenta lo que esta escrito en la transcripcion
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "enunciado",
    [
        "8.07.2020 se crea una empresa al contado.",
        "08/07/2020 se crea una empresa al contado.",
        "08-07-2020 se crea una empresa al contado.",
        "2020-07-08 se crea una empresa al contado.",
        "8 de julio de 2020 se crea una empresa al contado.",
        "8 de julio del 2020 se crea una empresa al contado.",
    ],
)
def test_acepta_las_formas_de_fecha_que_se_dicen_y_se_escriben(enunciado):
    assert ia.fechas_en_texto(enunciado) == {"2020-07-08"}


def test_un_importe_no_es_una_fecha():
    texto = "Se compra mercaderia por 50,000 con capital de 100.000 y 1,200 de gasto."
    assert ia.fechas_en_texto(texto) == set()


def test_sin_fecha_en_el_texto_no_se_detecta_nada():
    assert ia.fechas_en_texto("Se compra mercaderia al contado por 50,000.") == set()
    assert ia.fechas_en_texto("") == set()


def test_a_fecha_acepta_mes_en_letras():
    assert ia._a_fecha("8 de julio de 2020") == "2020-07-08"
    assert ia._a_fecha("8 de julio del 2020") == "2020-07-08"
    assert ia._a_fecha("1 de enero de 2021") == "2021-01-01"


def test_a_fecha_rechaza_un_dia_inexistente():
    assert ia._a_fecha("31 de febrero de 2020") == ""
    assert ia._a_fecha("10 de brumario de 2020") == ""


# --------------------------------------------------------------------------- #
# Glosa: determinista desde la oracion, con la limpieza correcta
# --------------------------------------------------------------------------- #
def test_glosa_es_la_operacion_y_no_el_enunciado_completo():
    assert (
        ia.glosa_desde_transcripcion("8.07.2020 se crea una empresa con 100.000 al contado.")
        == "Se crea una empresa al contado"
    )
    assert (
        ia.glosa_desde_transcripcion("8 de julio de 2020 se crea una empresa con 100.000 al contado.")
        == "Se crea una empresa al contado"
    )


def test_glosa_no_deja_importes_ni_preposiciones_colgantes():
    assert (
        ia.glosa_desde_transcripcion("El 01/07/2020 se compra mercaderia al contado por 5,000 soles.")
        == "Se compra mercaderia al contado"
    )
    assert (
        ia.glosa_desde_transcripcion("Se paga alquiler de la oficina por 1,200 soles")
        == "Se paga alquiler de la oficina"
    )


def test_glosa_conserva_los_nexos_que_enlazan_palabras_reales():
    assert ia.glosa_desde_transcripcion("Se paga alquiler de la oficina") == "Se paga alquiler de la oficina"
    assert ia.glosa_desde_transcripcion("Al contado se paga la luz") == "Al contado se paga la luz"
    assert ia.glosa_desde_transcripcion("Se compra mercaderia al contado") == "Se compra mercaderia al contado"


def test_glosa_quita_las_muletillas_de_enumeracion():
    assert ia.glosa_desde_transcripcion("Luego se paga la luz por 1,200 soles") == "Se paga la luz"


def test_glosa_no_inventa_cuando_no_queda_nada():
    assert ia.glosa_desde_transcripcion("5,000 soles") == ""
    assert ia.glosa_desde_transcripcion("   ") == ""
    assert ia.glosa_desde_transcripcion("") == ""


def test_las_oraciones_se_cortan_por_puntuacion_y_por_conectores():
    texto = "Se compra por 5,000 soles. Luego se paga la luz por 1,200 y finalmente se cobra al cliente."
    partes = ia.oraciones_de_dictado(texto)
    assert len(partes) == 3


def test_una_fecha_con_punto_no_se_parte_en_dos_oraciones():
    partes = ia.oraciones_de_dictado("8.07.2020 se crea una empresa al contado. El 02/07/2020 se compra.")
    assert len(partes) == 2


# --------------------------------------------------------------------------- #
# analizar_dictado: correcciones propias sobre el pipeline (con doble)
# --------------------------------------------------------------------------- #
def test_la_fecha_que_la_ia_invento_se_vacia_para_revision(monkeypatch):
    # El enunciado NO trae fecha; la IA escribe "2023-01-01" de donde no la saco.
    _instalar_pipeline(
        monkeypatch,
        [{"fecha": "2023-01-01", "glosa": "Compra de mercaderia", "asiento": [{"cuenta": "20", "debe": 50000.0, "haber": 0.0}]}],
    )
    ok, res = ia.analizar_dictado("Se compra mercaderia al contado por 50,000.")
    assert ok
    assert res[0]["fecha"] == ""
    assert any("no aparece en la transcripcion" in aviso for aviso in res.avisos)


def test_se_usa_la_unica_fecha_escrita_cuando_la_ia_no_la_toma(monkeypatch):
    _instalar_pipeline(
        monkeypatch,
        [{"fecha": "", "glosa": "Creacion de empresa", "asiento": [{"cuenta": "10", "debe": 100000.0, "haber": 0.0}]}],
    )
    ok, res = ia.analizar_dictado("8 de julio de 2020 se crea una empresa con 100.000 al contado.")
    assert ok
    assert res[0]["fecha"] == "2020-07-08"


def test_se_conserva_la_fecha_de_la_ia_cuando_si_esta_en_el_texto(monkeypatch):
    _instalar_pipeline(
        monkeypatch,
        [{"fecha": "2020-07-08", "glosa": "Creacion de empresa", "asiento": [{"cuenta": "10", "debe": 100000.0, "haber": 0.0}]}],
    )
    ok, res = ia.analizar_dictado("8.07.2020 se crea una empresa al contado.")
    assert ok
    assert res[0]["fecha"] == "2020-07-08"
    assert not any("no aparece en la transcripcion" in aviso for aviso in res.avisos)


def test_varias_transacciones_no_se_fusionan_en_un_asiento(monkeypatch):
    # Doble que devuelve 3 operaciones (asi lo hace el pipeline multi-operacion).
    _instalar_pipeline(
        monkeypatch,
        [
            {"fecha": "2020-07-01", "glosa": "a", "asiento": [{"cuenta": "20", "debe": 5000.0, "haber": 0.0}]},
            {"fecha": "2020-07-02", "glosa": "b", "asiento": [{"cuenta": "10", "debe": 7000.0, "haber": 0.0}]},
            {"fecha": "2020-07-05", "glosa": "c", "asiento": [{"cuenta": "68", "debe": 1200.0, "haber": 0.0}]},
        ],
    )
    texto = (
        "El 01/07/2020 se compra mercaderia al contado por 5,000 soles. "
        "El 02/07/2020 se vende mercaderia por 7,000 soles al contado. "
        "El 05/07/2020 se paga alquiler de la oficina por 1,200 soles."
    )
    ok, res = ia.analizar_dictado(texto)
    assert ok
    assert len(res) == 3
    assert [op["fecha"] for op in res] == ["2020-07-01", "2020-07-02", "2020-07-05"]
    assert [op["glosa"] for op in res] == [
        "Se compra mercaderia al contado",
        "Se vende mercaderia al contado",
        "Se paga alquiler de la oficina",
    ]
    # Ningun asiento quedo vacio ni colapsado.
    assert all(op["asiento"] for op in res)


def test_cada_asiento_conserva_su_propia_fecha_cuando_el_texto_trae_varias(monkeypatch):
    _instalar_pipeline(
        monkeypatch,
        [
            {"fecha": "2020-07-01", "glosa": "a", "asiento": [{"cuenta": "20", "debe": 5000.0, "haber": 0.0}]},
            {"fecha": "2020-07-02", "glosa": "b", "asiento": [{"cuenta": "10", "debe": 7000.0, "haber": 0.0}]},
        ],
    )
    ok, res = ia.analizar_dictado("El 01/07/2020 se compra por 5,000. El 02/07/2020 se vende por 7,000.")
    assert ok
    assert [op["fecha"] for op in res] == ["2020-07-01", "2020-07-02"]


def test_si_el_texto_no_trae_fecha_el_borrador_queda_sin_fecha(monkeypatch):
    _instalar_pipeline(
        monkeypatch,
        [{"fecha": "", "glosa": "Compra de mercaderia", "asiento": [{"cuenta": "20", "debe": 50000.0, "haber": 0.0}]}],
    )
    ok, res = ia.analizar_dictado("Se compra mercaderia al contado por 50,000.")
    assert ok
    assert res[0]["fecha"] == ""


def test_las_glosas_se_derivan_de_cada_oracion(monkeypatch):
    # El pipeline devolvio glosas genericas; la segmentacion coincide 1 a 1.
    _instalar_pipeline(
        monkeypatch,
        [
            {"fecha": "2020-07-01", "glosa": "Asiento", "asiento": [{"cuenta": "20", "debe": 5000.0, "haber": 0.0}]},
            {"fecha": "2020-07-05", "glosa": "Asiento", "asiento": [{"cuenta": "68", "debe": 1200.0, "haber": 0.0}]},
        ],
    )
    texto = "El 01/07/2020 se compra mercaderia al contado por 5,000 soles. El 05/07/2020 se paga la luz por 1,200."
    ok, res = ia.analizar_dictado(texto)
    assert ok
    assert [op["glosa"] for op in res] == ["Se compra mercaderia al contado", "Se paga la luz"]


def test_si_la_segmentacion_no_coincide_se_limpia_la_glosa_de_la_ia(monkeypatch):
    # La IA agrupo dos frases en UNA operacion: no se puede mapear oracion a
    # oracion, pero la glosa que trae la IA pasa por el MISMO limpiador.
    _instalar_pipeline(
        monkeypatch,
        [
            {
                "fecha": "2020-07-01",
                "glosa": "01/07/2020 se compra mercaderia al contado por 5,000 soles",
                "asiento": [{"cuenta": "20", "debe": 5000.0, "haber": 0.0}],
            }
        ],
    )
    ok, res = ia.analizar_dictado("El 01/07/2020 se compra mercaderia al contado por 5,000 soles")
    assert ok
    assert res[0]["glosa"] == "Se compra mercaderia al contado"


def test_los_avisos_del_pipeline_se_conservan(monkeypatch):
    _instalar_pipeline(
        monkeypatch,
        [{"fecha": "2020-07-01", "glosa": "Compra", "asiento": [{"cuenta": "20", "debe": 5000.0, "haber": 0.0}]}],
        avisos=["aviso del motor contable"],
    )
    ok, res = ia.analizar_dictado("El 01/07/2020 se compra mercaderia al contado por 5,000 soles.")
    assert ok
    assert "aviso del motor contable" in list(res.avisos)


def test_un_error_del_pipeline_se_devuelve_como_error(monkeypatch):
    def doble(texto, origen=None):
        return False, "La IA devolvio una respuesta vacia o ilegible tras varios intentos."

    monkeypatch.setattr(ia, "analizar_documento_completo", doble)
    ok, res = ia.analizar_dictado("Se compra mercaderia al contado por 50,000.")
    assert not ok
    assert "vacia o ilegible" in res


# --------------------------------------------------------------------------- #
# Contrato: el dictado reutiliza el pipeline de documentos, no un prompt propio
# --------------------------------------------------------------------------- #
def test_el_dictado_pasa_por_el_mismo_pipeline_que_los_documentos():
    fuente = (RAIZ / "ia_engine.py").read_text(encoding="utf-8")
    cuerpo_dictado = fuente.split("def analizar_dictado", 1)[1].split("\ndef ", 1)[0]
    # Usa el pipeline de documentos y no escribe su propio prompt ni su propia
    # validacion contable: multi-operacion, partida doble y fecha normalizada
    # llegan del mismo camino que PDF/Word/Excel.
    assert "analizar_documento_completo" in cuerpo_dictado
    assert "instrucciones" not in cuerpo_dictado
    assert "def " not in cuerpo_dictado
