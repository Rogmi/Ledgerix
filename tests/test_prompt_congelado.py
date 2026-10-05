"""
FASE 1 — CONGELAMIENTO DEL PROMPT (blindaje antes de tocar nada).

Este archivo NO modifica el comportamiento productivo: solo observa. Su unico
efecto es fallar el dia que alguien altere, sin querer, el prompt que consume
el PDF o las cuatro constantes congeladas.

Que protege
-----------
1. Las 4 constantes congeladas por longitud Y por sha256 (un cambio de un solo
   caracter las delata, aunque la longitud no se mueva).
2. Que el prompt base NO contenga todavia el bloque de derivacion de Excel.
3. Que el PDF reciba el prompt base byte a byte, capturado del request real.
4. Que la ruta de Excel reciba HOY tambien el prompt base byte a byte.
5. Que la ruta de escaner visual (tercer consumidor del mismo prompt) quede
   libre del bloque de Excel.

Interruptores de FASE 2
----------------------
- `test_excel_por_ahora_recibe_el_prompt_base` falla a proposito en cuanto exista
  `BLOQUE_EXCEL_DERIVACION`: obliga a actualizar la expectativa de Excel.
- `test_contrato_seleccion_de_prompt` se activa solo cuando exista
  `ia._prompt_para_origen` y exige el contrato completo.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

# Ninguna prueba de este archivo llama a la API: el cliente se sustituye por un
# doble, pero la clave debe existir para que el import no reviente.
os.environ.setdefault("GROQ_API_KEY", "clave-de-pruebas")

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia_engine as ia  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Constantes congeladas: longitud y contenido
# --------------------------------------------------------------------------- #
CONGELADAS = {
    "CRITERIO_DEBE_HABER": (6495, "d5b3a1883ddb1f99573d722a95da21678d7cd8b52bb59b32d78690e94576ee99"),
    "instrucciones_contador": (7735, "af36bd25a2fd4e14710e758b65086bafabad9e78fc909e103f2b0b334decbb20"),
    "CONTRATO_MULTI_PARTIDA": (2028, "bc4f6129455e33c566cd02984b8b70ca20151e6724356c6e4b146413d190934d"),
    "instrucciones_agente_excel": (9362, "c61e342480d55461e498ae71f87bc1d3328a61566835cd1fee15924c98eb057e"),
}


@pytest.mark.parametrize("nombre", sorted(CONGELADAS))
def test_constante_congelada_no_cambio_de_longitud(nombre):
    esperado, _ = CONGELADAS[nombre]
    real = len(getattr(ia, nombre))
    assert real == esperado, (
        f"{nombre} mide {real} caracteres y deberia medir {esperado}. "
        "Esta constante esta congelada: no se modifica en esta fase."
    )


@pytest.mark.parametrize("nombre", sorted(CONGELADAS))
def test_constante_congelada_no_cambio_de_contenido(nombre):
    _, esperado = CONGELADAS[nombre]
    real = hashlib.sha256(getattr(ia, nombre).encode("utf-8")).hexdigest()
    assert real == esperado, (
        f"{nombre} cambio de contenido con la misma longitud.\n"
        f"  esperado sha256: {esperado}\n"
        f"  real    sha256: {real}"
    )


# --------------------------------------------------------------------------- #
# 2. El prompt base no debe contener todavia el bloque de Excel
# --------------------------------------------------------------------------- #
MARCAS_DEL_BLOQUE_EXCEL = [
    "EXCEPCION UNICA A LA REGLA 7",
    "COSTO DE VENTAS = mercaderia comprada",
    "asiento DERIVADO del saldo final de inventario",
    "es CONTEXTO del caso (un saldo final",
    "cuenta 69 al DEBE y cuenta 20 al HABER",
]


@pytest.mark.parametrize("marca", MARCAS_DEL_BLOQUE_EXCEL)
def test_prompt_base_no_tiene_el_bloque_excel(marca):
    assert marca not in ia.instrucciones_agente_excel, (
        f"El prompt base contiene {marca!r}. El bloque de Excel debe existir "
        "solo en una variante de Excel, nunca en el prompt base."
    )


def test_no_existe_bloque_excel_derivacion_en_fase_1():
    assert not hasattr(ia, "BLOQUE_EXCEL_DERIVACION"), (
        "BLOQUE_EXCEL_DERIVACION ya existe: la FASE 2 fue implementada. "
        "Actualiza las expectativas de Excel de este archivo."
    )


# --------------------------------------------------------------------------- #
# 3. Contrato de seleccion de prompt (armado para FASE 2)
# --------------------------------------------------------------------------- #
ORIGENES_SIN_BLOQUE = ["", "pdf", "docx", "PDF", "DOCX", "desconocido", None]
ORIGENES_CON_BLOQUE = ["xlsx", "xls", "XLSX", "XLS"]


def test_contrato_seleccion_de_prompt():
    selector = getattr(ia, "_prompt_para_origen", None)
    if selector is None:
        pytest.skip("FASE 2 pendiente: ia._prompt_para_origen aun no existe")

    base = ia.instrucciones_agente_excel
    bloque = getattr(ia, "BLOQUE_EXCEL_DERIVACION", "")

    for origen in ORIGENES_SIN_BLOQUE:
        assert selector(origen) == base, f"el origen {origen!r} recibio un prompt distinto del base"

    for origen in ORIGENES_CON_BLOQUE:
        elegido = selector(origen)
        assert elegido.startswith(base), f"el origen {origen!r} no conserva el prompt base al inicio"
        assert elegido == base + bloque, f"el origen {origen!r} recibio un bloque distinto al esperado"


# --------------------------------------------------------------------------- #
# 4. Captura del request real (doble de cliente, sin red)
# --------------------------------------------------------------------------- #
LOTE_QUE_SI_FUNCIONA = json.dumps(
    [
        {
            "fecha": "2020-07-01",
            "glosa": "Compra de mercaderia",
            "asiento": [
                {"cuenta": "60", "debe": 100000, "haber": 0},
                {"cuenta": "42", "debe": 0, "haber": 100000},
            ],
        }
    ],
    ensure_ascii=False,
)


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


class _ClienteFalso:
    """Sustituye a `cliente` de Groq y guarda cada `messages` enviado."""

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


TEXTO_PDF = """=== PAGINA 1 ===
Compras del mes de julio de 2020
Se compran mercaderias por 100,000.00 soles
"""

TEXTO_EXCEL = """Fecha,Glosa,Cuenta,Debe,Haber
2020-07-01,Compra de mercaderia,60,100000,
2020-07-01,Compra de mercaderia,42,,100000
"""


def _capturar_prompt(monkeypatch, texto):
    """Corre el flujo real de punta a punta y devuelve el system prompt enviado."""
    cliente = _ClienteFalso(LOTE_QUE_SI_FUNCIONA)
    monkeypatch.setattr(ia, "cliente", cliente)

    exito, resultado = ia.analizar_excel_completo(texto)
    assert exito, f"el flujo real fallo con un doble valido: {resultado}"
    assert len(cliente.llamadas) == 1, f"se esperaba 1 llamada y hubo {len(cliente.llamadas)}"

    mensajes = cliente.llamadas[0]["messages"]
    assert mensajes[0]["role"] == "system"
    return mensajes[0]["content"]


def test_pdf_envia_el_prompt_base_byte_a_byte(monkeypatch):
    capturado = _capturar_prompt(monkeypatch, TEXTO_PDF)
    assert capturado == ia.instrucciones_agente_excel, (
        "la ruta PDF dejo de enviar el prompt base byte a byte"
    )
    assert hashlib.sha256(capturado.encode("utf-8")).hexdigest() == CONGELADAS["instrucciones_agente_excel"][1], (
        "el prompt que llega a Groq en la ruta PDF no es el congelado"
    )


def test_excel_por_ahora_recibe_el_prompt_base(monkeypatch):
    """
    FASE 1: sin BLOQUE_EXCEL_DERIVACION, la ruta de Excel recibe el prompt base
    exacto. En FASE 2 este test debe cambiar a exigir base + bloque.
    """
    capturado = _capturar_prompt(monkeypatch, TEXTO_EXCEL)
    assert capturado == ia.instrucciones_agente_excel, (
        "la ruta Excel recibio algo distinto del prompt base en FASE 1"
    )
    assert hashlib.sha256(capturado.encode("utf-8")).hexdigest() == CONGELADAS["instrucciones_agente_excel"][1]


def test_ruta_escaneo_visual_no_recibe_el_bloque_excel():
    """
    El escaner visual es un TERCER consumidor del prompt base. Debe seguir
    recibiendo el prompt completo sin el bloque de Excel.
    """
    if not hasattr(ia, "PREFIJO_PROMPT_VISUAL"):
        pytest.skip("esta version no tiene ruta de escaner visual")

    completo = ia.PREFIJO_PROMPT_VISUAL + ia.instrucciones_agente_excel
    assert ia.instrucciones_agente_excel in completo, (
        "el escaner visual debe seguir embebiendo el prompt base completo"
    )
    for marca in MARCAS_DEL_BLOQUE_EXCEL:
        assert marca not in completo, f"el escaner visual recibio la marca de Excel {marca!r}"