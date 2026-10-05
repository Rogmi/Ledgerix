"""
Contrato de salida de la IA: un ASIENTO = una operacion contable con N partidas.

Estas pruebas fijan el contrato que la IA debe cumplir y que Python debe conservar
tal cual. Reproducen el fallo de la prueba visual real: la IA devolvia las partidas
planas de una operacion (una por objeto) y Ledgerix las contaba como N asientos
independientes en lugar de 1 asiento de N partidas.

Reglas que estas pruebas defienden:
  - Un objeto con `asiento: [...]` es UN asiento con esas partidas, en ese orden.
  - Dos operaciones con la misma fecha y la misma glosa NO se fusionan.
  - Una fila plana sin `asiento` NO se agrupa con nadie: se conserva y se marca.
  - No se mueven importes ni se corrigen descuadres para que el asiento cuadre.
"""

import os
import sys
from pathlib import Path

# El modulo construye un cliente de Groq al importarse. La clave no se usa aqui
# (ninguna prueba llama a la API), pero debe existir para que el import no reviente.
os.environ.setdefault("GROQ_API_KEY", "clave-de-pruebas")

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia_engine as ia  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ejecutar(lote):
    """Corre el nucleo contable y exige que devuelva un resultado."""
    resultado, avisos, error = ia.procesar_dinamica_contable(lote)
    assert resultado is not None, f"no se pudo procesar el lote: {error} / {avisos}"
    return list(resultado), avisos


def totales(asiento):
    return (
        round(sum(p["debe"] for p in asiento["asiento"]), 2),
        round(sum(p["haber"] for p in asiento["asiento"]), 2),
    )


# --------------------------------------------------------------------------- #
# 1. Caso real de la prueba visual: aporte de capital
# --------------------------------------------------------------------------- #
def test_aporte_de_capital_en_efectivo_es_un_asiento_de_dos_partidas():
    """10 Debe 10000 / 50 Haber 10000 = 1 asiento, 2 partidas, Debe = Haber."""
    lote = [{
        "fecha": "2024-03-15",
        "glosa": "Aporte de capital en Efectivo",
        "asiento": [
            {"cuenta": "10", "debe": 10000.0, "haber": 0.0},
            {"cuenta": "50", "debe": 0.0, "haber": 10000.0},
        ],
    }]

    operaciones, avisos = ejecutar(lote)

    assert len(operaciones) == 1, "la operacion debia quedar en UN solo asiento"
    asiento = operaciones[0]
    assert len(asiento["asiento"]) == 2, "el asiento debia traer sus 2 partidas"
    assert [p["cuenta"] for p in asiento["asiento"]] == ["10", "50"]
    assert totales(asiento) == (10000.0, 10000.0)
    assert asiento["fecha"] == "2024-03-15"
    assert asiento["glosa"] == "Aporte de capital en Efectivo"

    # Fecha y glosa pertenecen al asiento, no a cada partida.
    for partida in asiento["asiento"]:
        assert set(partida) == {"cuenta", "debe", "haber"}

    # Sin incertidumbre: no hay nada que revisar.
    assert not asiento.get("revisar")
    assert not [a for a in avisos if "no cuadra" in a or "revisión" in a]


# --------------------------------------------------------------------------- #
# 2. Dos operaciones independientes con la MISMA fecha y la MISMA glosa
# --------------------------------------------------------------------------- #
def test_dos_operaciones_con_misma_fecha_y_misma_glosa_no_se_fusionan():
    """Misma fecha y misma glosa NO son motivo para fusionar dos operaciones."""
    lote = [
        {
            "fecha": "2024-05-02",
            "glosa": "Aporte de capital en Efectivo",
            "asiento": [
                {"cuenta": "10", "debe": 10000.0, "haber": 0.0},
                {"cuenta": "50", "debe": 0.0, "haber": 10000.0},
            ],
        },
        {
            "fecha": "2024-05-02",
            "glosa": "Aporte de capital en Efectivo",
            "asiento": [
                {"cuenta": "10", "debe": 2500.0, "haber": 0.0},
                {"cuenta": "50", "debe": 0.0, "haber": 2500.0},
            ],
        },
    ]

    operaciones, avisos = ejecutar(lote)

    assert len(operaciones) == 2, "dos operaciones distintas NO pueden convertirse en una"
    for asiento, importe in zip(operaciones, (10000.0, 2500.0)):
        assert len(asiento["asiento"]) == 2
        assert totales(asiento) == (importe, importe)
        assert asiento["fecha"] == "2024-05-02"
        assert asiento["glosa"] == "Aporte de capital en Efectivo"
        assert not asiento.get("revisar")

    # Ningun asiento se comio las partidas del otro.
    assert not [a for a in avisos if "no cuadra" in a]


def test_dos_operaciones_que_comparten_cuenta_e_importe_no_se_fusionan():
    """Mismo par de cuentas y mismo importe tampoco habilita la fusion."""
    lote = [
        {"fecha": "2024-05-02", "glosa": "Cobro a cliente",
         "asiento": [{"cuenta": "10", "debe": 500.0, "haber": 0.0},
                     {"cuenta": "12", "debe": 0.0, "haber": 500.0}]},
        {"fecha": "2024-05-02", "glosa": "Cobro a cliente",
         "asiento": [{"cuenta": "10", "debe": 500.0, "haber": 0.0},
                     {"cuenta": "12", "debe": 0.0, "haber": 500.0}]},
    ]

    operaciones, _ = ejecutar(lote)

    assert len(operaciones) == 2
    assert all(len(o["asiento"]) == 2 for o in operaciones)
    assert all(totales(o) == (500.0, 500.0) for o in operaciones)


# --------------------------------------------------------------------------- #
# 3. Regresion del fallo real: partidas planas devueltas fuera de `asiento`
# --------------------------------------------------------------------------- #
def test_filas_planas_se_conservan_y_se_marcan_sin_agruparse():
    """
    Lo que devolvio la IA en la prueba real. NO debe fusionarse en un asiento de
    dos partidas (eso seria inventar la estructura), pero tampoco puede perderse:
    cada fila se conserva y queda marcada para revision humana.
    """
    lote = [
        {"fecha": "2024-03-15", "glosa": "Aporte de capital en Efectivo",
         "cuenta": "10", "debe": 10000.0, "haber": 0.0},
        {"fecha": "2024-03-15", "glosa": "Aporte de capital en Efectivo",
         "cuenta": "50", "debe": 0.0, "haber": 10000.0},
    ]

    operaciones, avisos = ejecutar(lote)

    # No se perdio ninguna fila...
    assert len(operaciones) == 2
    assert [o["asiento"][0]["cuenta"] for o in operaciones] == ["10", "50"]
    assert totales(operaciones[0]) == (10000.0, 0.0)
    assert totales(operaciones[1]) == (0.0, 10000.0)

    # ...pero ninguna se presento como un asiento completo.
    for operacion in operaciones:
        assert operacion.get("revisar") is True
        assert "asiento" in operacion.get("incertidumbres", "")

    assert [a for a in avisos if "sin la clave" in a], "debe avisar del incumplimiento del contrato"


def test_asiento_con_partida_suelta_fuera_de_asiento_no_se_duplica():
    """Una partida repetida al nivel del asiento no se agrega: duplicaria el importe."""
    lote = [{
        "fecha": "2024-03-15", "glosa": "Aporte de capital en Efectivo",
        "cuenta": "10", "debe": 10000.0, "haber": 0.0,
        "asiento": [
            {"cuenta": "10", "debe": 10000.0, "haber": 0.0},
            {"cuenta": "50", "debe": 0.0, "haber": 10000.0},
        ],
    }]

    operaciones, avisos = ejecutar(lote)

    assert len(operaciones) == 1
    assert len(operaciones[0]["asiento"]) == 2, "la partida suelta no debe entrar al asiento"
    assert totales(operaciones[0]) == (10000.0, 10000.0)
    assert operaciones[0].get("revisar") is True
    assert [a for a in avisos if "fuera de la clave 'asiento'" in a]


# --------------------------------------------------------------------------- #
# 4. No se fuerza el cuadre ni se mueven importes
# --------------------------------------------------------------------------- #
def test_descuadre_se_conserva_y_se_marca_sin_corregirse():
    lote = [{
        "fecha": "2024-03-15", "glosa": "Aporte de capital en Efectivo",
        "asiento": [
            {"cuenta": "10", "debe": 10000.0, "haber": 0.0},
            {"cuenta": "50", "debe": 0.0, "haber": 9000.0},
        ],
    }]

    operaciones, avisos = ejecutar(lote)

    assert totales(operaciones[0]) == (10000.0, 9000.0), "los importes no se mueven"
    assert operaciones[0].get("revisar") is True
    assert [a for a in avisos if "no cuadra" in a and "1,000.00" in a]


def test_partida_doble_de_tres_cu_partidas_se_conserva_entera():
    lote = [{
        "fecha": "2024-04-01", "glosa": "Compra de mercadería al crédito",
        "asiento": [
            {"cuenta": "20", "debe": 3000.0, "haber": 0.0},
            {"cuenta": "40", "debe": 0.0, "haber": 1000.0},
            {"cuenta": "42", "debe": 0.0, "haber": 2000.0},
        ],
    }]

    operaciones, avisos = ejecutar(lote)

    assert len(operaciones) == 1
    assert len(operaciones[0]["asiento"]) == 3
    assert totales(operaciones[0]) == (3000.0, 3000.0)
    assert not [a for a in avisos if "no cuadra" in a]


# --------------------------------------------------------------------------- #
# 5. La incertidumbre declarada por la IA se conserva
# --------------------------------------------------------------------------- #
def test_incertidumbre_declarada_por_la_ia_se_conserva():
    lote = [{
        "fecha": "", "glosa": "Compra sin importe legible",
        "revisar": True,
        "incertidumbres": "el importe de la linea 3 esta cortado en el PDF",
        "asiento": [
            {"cuenta": "20", "debe": 0.0, "haber": 0.0},
            {"cuenta": "42", "debe": 0.0, "haber": 0.0},
        ],
    }]

    operaciones, avisos = ejecutar(lote)

    assert len(operaciones) == 1
    assert operaciones[0]["revisar"] is True
    assert "importe de la linea 3" in operaciones[0]["incertidumbres"]
    assert [a for a in avisos if "revisión" in a]


# --------------------------------------------------------------------------- #
# 6. Guardarraíl del prompt: el contrato se le dice a la IA, no solo al codigo
# --------------------------------------------------------------------------- #
def test_el_prompt_exige_un_objeto_por_operacion_con_lista_de_partidas():
    prompt = ia.instrucciones_agente_excel

    assert ia.CONTRATO_MULTI_PARTIDA in prompt, "el contrato debe estar en el prompt, no ser codigo muerto"
    assert '`asiento`' in prompt
    assert "UN objeto por OPERACION, no un objeto por fila" in prompt

    # La instruccion que provocaba el fallo (una fila = un asiento) no debe volver.
    assert "UNA FILA contable individual" not in prompt
    assert "NO agrupes por fecha" not in prompt

    # Y el contrato debe prohibir explicitamente la fusion por fecha/glosa.
    assert "NUNCA se fusionan" in ia.CONTRATO_MULTI_PARTIDA
    assert "NO se agrupa por fecha, glosa, cercania, proximidad, importe ni cuenta" in (
        ia.procesar_dinamica_contable.__doc__
    )