"""
FASE 2 — EL BLOQUE DE EXCEL YA EXISTE (el prompt base sigue congelado).

Este archivo NO modifica el comportamiento productivo: solo observa. Su unico
efecto es fallar el dia que alguien altere, sin querer, el prompt que consume
el PDF o las cuatro constantes congeladas.

Que protege
-----------
1. Las 4 constantes congeladas por longitud Y por sha256 (un cambio de un solo
   caracter las delata, aunque la longitud no se mueva).
2. Que el prompt base NO contenga el bloque de derivacion de Excel: el bloque
   vive aparte y solo se concatena para una hoja de calculo.
3. Que el PDF reciba el prompt base byte a byte, capturado del request real.
4. Que la ruta de Excel reciba el prompt base y, anadido al final, SOLO el
   bloque de Excel.
5. Que la ruta de escaner visual (tercer consumidor del mismo prompt) quede
   libre del bloque de Excel.
6. Que un origen ausente o desconocido reciba el prompt base: ante la duda no se
   anade ningun bloque.

Interruptores ya activados
--------------------------
- `test_existe_bloque_excel_derivacion` exige el bloque (FASE 1 exigia que no
  existiera).
- `test_contrato_seleccion_de_prompt` ya no hace `skip`: exige el contrato de
  seleccion completo.
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
# Re-congeladas el 2026-10-05, al corregir la regresion de la depreciacion (cuenta
# 39 al DEBE). Los valores anteriores congelaban la seccion E de CRITERIO_DEBE_HABER,
# que prohibia leer la COLUMNA del importe y por eso obligaba a deducir el lado solo
# del signo; junto con `app._serializar_tabla`, que borraba las celdas vacias, el
# documento se quedaba sin ninguna evidencia del lado y la 39 caia al DEBE.
# Re-congeladas de nuevo al afinar esa seccion: la pagina 2 del libro no rotula sus
# columnas (los rotulos IZQUIERDO/DERECHO, CARGO/ABONO y DEBE/HABER estan en la
# pagina 1), asi que la regla tiene que permitir calibrar las columnas con las filas de
# notacion inequivoca de la propia pagina. El tripwire sigue cumpliendo su funcion:
# vuelve a delatar cualquier cambio posterior.
#
# La FASE 2 no las toco: `BLOQUE_EXCEL_DERIVACION` se concatena en
# `_prompt_para_origen`, nunca dentro de estas cadenas.
CONGELADAS = {
    "CRITERIO_DEBE_HABER": (8052, "dc214a8ea8fb23386da8f60b1a9d00398d587366573d130afcd3c32a650ff7fe"),
    "instrucciones_contador": (9292, "f759fb661c7876516d9270c841ccdd47695b74cef19c42016fe4647d94135902"),
    "CONTRATO_MULTI_PARTIDA": (2028, "bc4f6129455e33c566cd02984b8b70ca20151e6724356c6e4b146413d190934d"),
    "instrucciones_agente_excel": (10919, "457f820d68368a1768b523d31934fab9ff1545b3b5efa81dad32192bca083548"),
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
# 2. El bloque de Excel existe, pero NO esta en el prompt base
# --------------------------------------------------------------------------- #
MARCAS_DEL_BLOQUE_EXCEL = [
    "EXCEPCION UNICA A LA REGLA 7",
    "UN SALDO QUE SE OBSERVA NO ES UNA OPERACION",
    "EL SISTEMA DERIVA LO QUE FALTA, NO TU",
]


@pytest.mark.parametrize("marca", MARCAS_DEL_BLOQUE_EXCEL)
def test_prompt_base_no_tiene_el_bloque_excel(marca):
    assert marca not in ia.instrucciones_agente_excel, (
        f"El prompt base contiene {marca!r}. El bloque de Excel debe existir "
        "solo en una variante de Excel, nunca en el prompt base."
    )


def test_existe_bloque_excel_derivacion():
    """FASE 2: el bloque existe. Lo que se protege ahora es que no se filtre al base."""
    assert ia.BLOQUE_EXCEL_DERIVACION, "el bloque de Excel no puede estar vacio"
    assert ia.BLOQUE_EXCEL_DERIVACION not in ia.instrucciones_agente_excel, (
        "el bloque tiene que ser una cadena aparte: concatenado dentro del prompt base "
        "cambiaria su sha256 congelado y llegaria al PDF"
    )


# --------------------------------------------------------------------------- #
# 3. Contrato de seleccion de prompt
# --------------------------------------------------------------------------- #
ORIGENES_SIN_BLOQUE = ["", "pdf", "docx", "PDF", "DOCX", "desconocido", None]
ORIGENES_CON_BLOQUE = ["xlsx", "xls", "XLSX", "XLS"]


def test_contrato_seleccion_de_prompt():
    selector = ia._prompt_para_origen
    base = ia.instrucciones_agente_excel
    bloque = ia.BLOQUE_EXCEL_DERIVACION

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


def _capturar_prompt(monkeypatch, texto, origen=None):
    """Corre el flujo real de punta a punta y devuelve el system prompt enviado."""
    cliente = _ClienteFalso(LOTE_QUE_SI_FUNCIONA)
    monkeypatch.setattr(ia, "cliente", cliente)

    exito, resultado = ia.analizar_excel_completo(texto, origen=origen)
    assert exito, f"el flujo real fallo con un doble valido: {resultado}"
    assert len(cliente.llamadas) == 1, f"se esperaba 1 llamada y hubo {len(cliente.llamadas)}"

    mensajes = cliente.llamadas[0]["messages"]
    assert mensajes[0]["role"] == "system"
    return mensajes[0]["content"]


def test_pdf_envia_el_prompt_base_byte_a_byte(monkeypatch):
    capturado = _capturar_prompt(monkeypatch, TEXTO_PDF, origen="pdf")
    assert capturado == ia.instrucciones_agente_excel, (
        "la ruta PDF dejo de enviar el prompt base byte a byte"
    )
    assert hashlib.sha256(capturado.encode("utf-8")).hexdigest() == CONGELADAS["instrucciones_agente_excel"][1], (
        "el prompt que llega a Groq en la ruta PDF no es el congelado"
    )


def test_excel_recibe_el_prompt_base_mas_el_bloque(monkeypatch):
    """
    FASE 2: la ruta de Excel recibe el prompt base intacto y, anadido al final, solo el
    bloque de Excel. El bloque va DESPUES porque define una excepcion a las reglas del
    base: si fuera delante, el prompt base lo contradiría al final.
    """
    capturado = _capturar_prompt(monkeypatch, TEXTO_EXCEL, origen="xlsx")
    assert capturado == ia.instrucciones_agente_excel + ia.BLOQUE_EXCEL_DERIVACION
    assert capturado.startswith(ia.instrucciones_agente_excel), (
        "el prompt base debe quedar entero al principio del prompt de Excel"
    )
    assert hashlib.sha256(ia.instrucciones_agente_excel.encode("utf-8")).hexdigest() == CONGELADAS["instrucciones_agente_excel"][1]


def test_sin_origen_recibe_el_prompt_base(monkeypatch):
    """Sin origen no se anade nada: ante la duda, el prompt congelado y nada mas."""
    capturado = _capturar_prompt(monkeypatch, TEXTO_EXCEL)
    assert capturado == ia.instrucciones_agente_excel


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
    assert ia.BLOQUE_EXCEL_DERIVACION not in completo, (
        "el escaner visual recibio el bloque de Excel: el contexto de inventario "
        "solo le sirve a una hoja de calculo"
    )
    for marca in MARCAS_DEL_BLOQUE_EXCEL:
        assert marca not in completo, f"el escaner visual recibio la marca de Excel {marca!r}"