"""
Interpretacion contable del Debe/Haber: la columna la decide el EFECTO de la operacion,
no la clase de la cuenta.

Estos casos reproducen los tres errores de la prueba real (14 operaciones -> 14 asientos
con la estructura ya correcta, pero con el lado de los importes mal interpretado):

  1. Cobro de factura antes de su vencimiento con descuento -> 70 al HABER por el descuento.
  2. Pago de alquiler y servicios del mes -> la caja (10) al DEBE.
  3. Depreciacion -> la depreciacion acumulada (39) al DEBE.

Los tres tienen la misma causa: la IA decidio la columna por la clase de la cuenta
("los ingresos van al HABER", "los activos van al DEBE", "39 es un activo") en lugar de
por lo que la operacion le hace a esa cuenta. La correccion vive en el prompt
(CRITERIO_DEBE_HABER) y estas pruebas la fijan.

Reglas que estas pruebas defienden:
  - Cada operacion canonica conserva su estructura y su lado (Debe/Haber) esperado.
  - La salida erronea real NO se corrige en Python: se conserva integra y se marca para
    revision humana.
  - Python nunca invierte una columna por la clase de la cuenta, ni agrupa, ni completa,
    ni mueve importes: no hay ninguna regla "si cuenta X entonces Debe/Haber Y".
  - El prompt dice explicitamente que la columna depende del efecto de la operacion.
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
# Helpers: leen lo que quedo, sin interpretar nada
# --------------------------------------------------------------------------- #
def ejecutar(lote):
    """Corre el nucleo contable y exige que devuelva un resultado."""
    resultado, avisos, error = ia.procesar_dinamica_contable(lote)
    assert resultado is not None, f"no se pudo procesar el lote: {error} / {avisos}"
    return list(resultado), avisos


def un_asiento(lote):
    """Ejecuta el lote y devuelve el unico asiento que resulta, exigiendo que sea uno."""
    operaciones, avisos = ejecutar(lote)
    assert len(operaciones) == 1, f"la operacion debia quedar en UN asiento, no {len(operaciones)}"
    return operaciones[0], avisos


def lados(asiento):
    """[(cuenta, lado, importe)] en el orden declarado, tal como quedo tras el proceso."""
    filas = []
    for partida in asiento["asiento"]:
        if partida["debe"] and not partida["haber"]:
            filas.append((partida["cuenta"], "debe", partida["debe"]))
        elif partida["haber"] and not partida["debe"]:
            filas.append((partida["cuenta"], "haber", partida["haber"]))
        else:
            filas.append((partida["cuenta"], "indefinido", partida["debe"] or partida["haber"]))
    return filas


def totales(asiento):
    return (
        round(sum(p["debe"] for p in asiento["asiento"]), 2),
        round(sum(p["haber"] for p in asiento["asiento"]), 2),
    )


def afirmarsin_revision(asiento, avisos):
    """Un asiento bien interpretado no queda pendiente de revision humana."""
    assert not asiento.get("revisar"), f"no debía marcarse para revisión: {asiento.get('incertidumbres')}"
    assert not [a for a in avisos if "no cuadra" in a or "revisión" in a]


def afirmarsin_cuadre(asiento, debe, haber):
    """Comprueba el descuadre tal cual quedo, sin que Python haya movido un centavo."""
    assert totales(asiento) == (debe, haber), "Python no debe alterar los importes para cuadrar"
    assert asiento.get("revisar") is True, "un descuadre se marca, no se corrige"


def partida(cuenta, debe=0.0, haber=0.0):
    return {"cuenta": cuenta, "debe": debe, "haber": haber}


# =========================================================================== #
# 1. CASO REAL: cobro de factura antes de su vencimiento con descuento
# =========================================================================== #
def test_cobro_de_factura_con_descuento_concedido():
    """
    Factura de 3000 cobrada antes de vencer con 300 de descuento.

    Efecto de la operacion:
      - la cuenta por cobrar (12) DISMINUYE por el total de la factura -> HABER 3000;
      - entra efectivo por el NETO -> DEBE 2700 en la caja (10);
      - el descuento REDUCE el ingreso -> DEBE 300 en descuentos concedidos (74).

    El error real fue poner el descuento en 70 al HABER (un ingreso mas) y la cuenta por
    cobrar (12) al DEBE: las dos columnas decididas por la clase de cuenta, no por el
    efecto del cobro.
    """
    lote = [{
        "fecha": "2024-05-10",
        "glosa": "COBRO DE FACTURA ANTES DE SU VENCIMIENTO CON DESCUENTO",
        "asiento": [
            partida("10", debe=2700.0),
            partida("74", debe=300.0),
            partida("12", haber=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert len(asiento["asiento"]) == 3, "el cobro conserva sus 3 partidas"
    assert lados(asiento) == [
        ("10", "debe", 2700.0),
        ("74", "debe", 300.0),   # el descuento va al DEBE: reduce el ingreso
        ("12", "haber", 3000.0),  # la por cobrar disminuye: total de la factura
    ]
    assert totales(asiento) == (3000.0, 3000.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 2. CASO REAL: pago de alquiler y servicios del mes
# =========================================================================== #
def test_pago_de_alquiler_y_servicios_del_mes():
    """
    Pago de 1300 (alquiler 800 + servicios 500).

    Efecto de la operacion:
      - los dos gastos se RECONOCEN -> DEBE;
      - sale efectivo del banco/caja -> la caja (10) va al HABER.

    El error real fue poner la caja en el DEBE porque "10 es un activo y los activos
    aumentan en DEBE": en un PAGO la caja disminuye, y eso no lo dice la clase de la
    cuenta sino el verbo de la operacion.
    """
    lote = [{
        "fecha": "2024-05-31",
        "glosa": "PAGO DE ALQUILER Y SERVICIOS DEL MES",
        "asiento": [
            partida("63", debe=800.0),
            partida("63", debe=500.0),
            partida("10", haber=1300.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    # Las dos lineas de 63 siguen siendo dos: no se fusionan ni se agrupan.
    assert len(asiento["asiento"]) == 3
    assert lados(asiento) == [
        ("63", "debe", 800.0),
        ("63", "debe", 500.0),
        ("10", "haber", 1300.0),  # salida de efectivo: la caja disminuye
    ]
    assert totales(asiento) == (1300.0, 1300.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 3. CASO REAL: depreciacion
# =========================================================================== #
def test_depreciacion_del_periodo():
    """
    Depreciacion de 135 del periodo.

    Efecto de la operacion:
      - se reconoce el gasto del periodo (68) -> DEBE;
      - la depreciacion acumulada (39) es un CONTRA-activo: acumula contra el activo
        -> HABER. No va al DEBE aunque su codigo empiece por 3.
    """
    lote = [{
        "fecha": "2024-05-31",
        "glosa": "DEPRECIACION DEL MES",
        "asiento": [
            partida("68", debe=135.0),
            partida("39", haber=135.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [
        ("68", "debe", 135.0),    # gasto del periodo
        ("39", "haber", 135.0),   # contra-activo: acumula
    ]
    assert totales(asiento) == (135.0, 135.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 4. Venta normal
# =========================================================================== #
def test_venta_normal_al_credito():
    """
    Venta al credito de 3000: la por cobrar AUMENTA -> DEBE, el ingreso se reconoce -> HABER.

    Contraste con el caso 1: la misma cuenta 12 va al DEBE aqui y al HABER alli. Lo unico
    que cambia es la operacion (vender vs cobrar), no la cuenta.
    """
    lote = [{
        "fecha": "2024-05-02",
        "glosa": "VENTA DE MERCADERIA AL CREDITO",
        "asiento": [
            partida("12", debe=3000.0),
            partida("70", haber=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("12", "debe", 3000.0), ("70", "haber", 3000.0)]
    assert totales(asiento) == (3000.0, 3000.0)
    afirmarsin_revision(asiento, avisos)


def test_venta_normal_al_contado():
    """Venta al contado: entra efectivo -> DEBE en la caja, ingreso -> HABER."""
    lote = [{
        "fecha": "2024-05-03",
        "glosa": "VENTA DE MERCADERIA AL CONTADO",
        "asiento": [
            partida("10", debe=3000.0),
            partida("70", haber=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("10", "debe", 3000.0), ("70", "haber", 3000.0)]
    assert totales(asiento) == (3000.0, 3000.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 5. Venta con descuento
# =========================================================================== #
def test_venta_con_descuento_concedido():
    """
    Venta de 3000 con 300 de descuento y cobro en efectivo de 2700.

    El ingreso NETO no se transcribe: el ingreso bruto de la factura va al HABER (3000) y
    el descuento concedido se reconoce en su propia cuenta (74) al DEBE (300). El efectivo
    que entra es el neto (2700). Los tres importes juntos cuadran sin mover nada.
    """
    lote = [{
        "fecha": "2024-05-04",
        "glosa": "VENTA DE MERCADERIA CON DESCUENTO CONCEDIDO",
        "asiento": [
            partida("10", debe=2700.0),
            partida("74", debe=300.0),
            partida("70", haber=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [
        ("10", "debe", 2700.0),   # efectivo neto recibido
        ("74", "debe", 300.0),    # el descuento reduce el ingreso
        ("70", "haber", 3000.0),  # ingreso bruto de la factura
    ]
    assert totales(asiento) == (3000.0, 3000.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 6. Cobro de cuenta por cobrar
# =========================================================================== #
def test_cobro_de_cuenta_por_cobrar_sin_descuento():
    """
    Cobro de una factura de 3000 sin descuento: efectivo al DEBE, por cobrar al HABER.

    La por cobrar se debita por el total cobrado, no por una parte: el cobro la cancela.
    """
    lote = [{
        "fecha": "2024-06-05",
        "glosa": "COBRO DE CUENTA POR COBRAR",
        "asiento": [
            partida("10", debe=3000.0),
            partida("12", haber=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("10", "debe", 3000.0), ("12", "haber", 3000.0)]
    assert totales(asiento) == (3000.0, 3000.0)
    afirmarsin_revision(asiento, avisos)


def test_cobro_parcial_de_una_factura():
    """Cobro parcial: solo se cancela la por cobrar por lo cobrado, el resto sigue abierta."""
    lote = [{
        "fecha": "2024-06-06",
        "glosa": "COBRO PARCIAL DE FACTURA",
        "asiento": [
            partida("10", debe=1000.0),
            partida("12", haber=1000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("10", "debe", 1000.0), ("12", "haber", 1000.0)]
    assert totales(asiento) == (1000.0, 1000.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 7. Pago de gasto
# =========================================================================== #
def test_pago_de_gasto_del_mes():
    """Pago de servicios basicos de 350: gasto reconocido al DEBE, salida de caja al HABER."""
    lote = [{
        "fecha": "2024-05-20",
        "glosa": "PAGO DE SERVICIOS BASICOS DEL MES",
        "asiento": [
            partida("63", debe=350.0),
            partida("10", haber=350.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("63", "debe", 350.0), ("10", "haber", 350.0)]
    assert totales(asiento) == (350.0, 350.0)
    afirmarsin_revision(asiento, avisos)


def test_pago_de_factura_ya_registrada():
    """
    Pago del pasivo de una compra ya registrada: lo que se cancela (42) va al DEBE y la
    caja al HABER. La 42 es un pasivo y aun asi va al DEBE: por el efecto del pago.
    """
    lote = [{
        "fecha": "2024-06-10",
        "glosa": "PAGO A PROVEEDOR POR FACTURA PENDIENTE",
        "asiento": [
            partida("42", debe=2000.0),
            partida("10", haber=2000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("42", "debe", 2000.0), ("10", "haber", 2000.0)]
    assert totales(asiento) == (2000.0, 2000.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 8. Depreciacion (segunda variante: otra clase de activo, mismo efecto)
# =========================================================================== #
def test_depreciacion_de_otro_activo_misma_regla_de_efecto():
    """
    Amortizacion de un intangible: mismo efecto que el caso 3 (gasto al DEBE, acumulada al
    HABER). La regla no depende del codigo de la cuenta afectada sino de la operacion.
    """
    lote = [{
        "fecha": "2024-06-30",
        "glosa": "AMORTIZACION DE LICENCIAS DE SOFTWARE",
        "asiento": [
            partida("68", debe=90.0),
            partida("39", haber=90.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("68", "debe", 90.0), ("39", "haber", 90.0)]
    assert totales(asiento) == (90.0, 90.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 9. Aporte de capital
# =========================================================================== #
def test_aporte_de_capital_en_efectivo():
    """Aporte en efectivo: entra caja al DEBE, el patrimonio aumenta al HABER."""
    lote = [{
        "fecha": "2024-03-15",
        "glosa": "APORTE DE CAPITAL EN EFECTIVO",
        "asiento": [
            partida("10", debe=10000.0),
            partida("50", haber=10000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("10", "debe", 10000.0), ("50", "haber", 10000.0)]
    assert totales(asiento) == (10000.0, 10000.0)
    afirmarsin_revision(asiento, avisos)


def test_aporte_de_capital_en_mercaderias():
    """
    Aporte no monetario: lo que aporta el socio (20) va al DEBE y el capital (50) al HABER.

    La caja no aparece: no es que "10 siempre va al DEBE", es que aqui no hay efectivo.
    """
    lote = [{
        "fecha": "2024-03-20",
        "glosa": "APORTE DE SOCIO EN MERCADERIAS",
        "asiento": [
            partida("20", debe=5000.0),
            partida("50", haber=5000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("20", "debe", 5000.0), ("50", "haber", 5000.0)]
    assert totales(asiento) == (5000.0, 5000.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 10. Regresion de la salida REAL de la IA: se conserva y se marca, no se corrige
# =========================================================================== #
def test_salida_real_del_caso_descuento_se_conserva_y_se_marca():
    """
    Lo que devolvio la IA en la prueba real: 70 al HABER por el descuento, 12 al DEBE y
    10 al DEBE por el neto.

    Python no corrige ninguna columna ni mueve importes: conserva las tres partidas con el
    lado que llegaron, detecta el descuadre y lo manda a revision humana.
    """
    lote = [{
        "fecha": "2024-05-10",
        "glosa": "COBRO DE FACTURA ANTES DE SU VENCIMIENTO CON DESCUENTO",
        "asiento": [
            partida("70", haber=300.0),
            partida("10", debe=2700.0),
            partida("12", debe=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [
        ("70", "haber", 300.0),
        ("10", "debe", 2700.0),
        ("12", "debe", 3000.0),
    ], "ninguna columna debe invertir: solo la IA interpreta el lado"
    afirmarsin_cuadre(asiento, 5700.0, 300.0)
    assert [a for a in avisos if "no cuadra" in a]


def test_salida_real_del_caso_pago_no_invierte_la_caja():
    """
    Lo que devolvio la IA en la prueba real: la caja (10) al DEBE y los dos gastos al DEBE.

    El sistema no "arregla" la caja porque 10 sea un activo: la deja en el Debe y reporta
    el descuadre.
    """
    lote = [{
        "fecha": "2024-05-31",
        "glosa": "PAGO DE ALQUILER Y SERVICIOS DEL MES",
        "asiento": [
            partida("10", debe=1300.0),
            partida("63", debe=800.0),
            partida("63", debe=500.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [
        ("10", "debe", 1300.0),
        ("63", "debe", 800.0),
        ("63", "debe", 500.0),
    ]
    afirmarsin_cuadre(asiento, 2600.0, 0.0)
    assert [a for a in avisos if "no cuadra" in a]


def test_salida_real_del_caso_depreciacion_no_invierte_la_acumulada():
    """
    Lo que devolvio la IA en la prueba real: 39 y 68 las dos al DEBE.

    La 39 no se pasa al Haber "porque es contra-activo": se conserva al Debe y se marca.
    """
    lote = [{
        "fecha": "2024-05-31",
        "glosa": "DEPRECIACION DEL MES",
        "asiento": [
            partida("39", debe=135.0),
            partida("68", debe=135.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("39", "debe", 135.0), ("68", "debe", 135.0)]
    afirmarsin_cuadre(asiento, 270.0, 0.0)
    assert [a for a in avisos if "no cuadra" in a]


def test_la_incertidumbre_declarada_sobre_el_lado_se_conserva():
    """
    Si la IA reconoce que duda del lado, su marca se respeta tal cual: el borrador la
    muestra y el sistema no la descarta ni la reescribe.
    """
    lote = [{
        "fecha": "2024-05-10",
        "glosa": "COBRO DE FACTURA CON DESCUENTO",
        "revisar": True,
        "incertidumbres": "no se sabe si el descuento de 300 fue concedido u obtenido",
        "asiento": [
            partida("10", debe=2700.0),
            partida("70", haber=300.0),
            partida("12", haber=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert asiento["revisar"] is True
    assert "concedido u obtenido" in asiento["incertidumbres"]
    assert [a for a in avisos if "revisión" in a]


# =========================================================================== #
# 11. Python NO decide el lado (ninguna regla "cuenta X -> columna Y")
# =========================================================================== #
def test_python_no_invierte_una_columna_por_la_clase_de_la_cuenta():
    """
    La 74 pertenece a una clase acreedora (elemento 7 del PCGE) y aun asi puede llegar al
    DEBE: Python no la manda al Haber. Y si llega al Haber en un contexto de descuento,
    tampoco la pasa al Debe. Lo unico que hace es comparar los totales.
    """
    en_debe, avisos_debe = un_asiento([{
        "fecha": "2024-05-10", "glosa": "COBRO CON DESCUENTO",
        "asiento": [partida("10", debe=2700.0), partida("74", debe=300.0), partida("12", haber=3000.0)],
    }])
    assert lados(en_debe) == [("10", "debe", 2700.0), ("74", "debe", 300.0), ("12", "haber", 3000.0)]
    assert not en_debe.get("revisar")

    en_haber, avisos_haber = un_asiento([{
        "fecha": "2024-05-10", "glosa": "COBRO CON DESCUENTO",
        "asiento": [partida("10", debe=2700.0), partida("74", haber=300.0), partida("12", haber=3000.0)],
    }])
    assert lados(en_haber) == [("10", "debe", 2700.0), ("74", "haber", 300.0), ("12", "haber", 3000.0)]
    afirmarsin_cuadre(en_haber, 2700.0, 3300.0)
    assert [a for a in avisos_haber if "no cuadra" in a]
    assert not avisos_debe, "el primer lote cuadra y no genera avisos"


def test_python_no_juzga_la_interpretacion_de_un_asiento_que_cuadra():
    """
    Limite documentado del sistema: si el asiento CUADRA, Python no opina sobre si la
    interpretacion es la correcta (aquí se deprecia el activo en 33 en vez de su acumulada
    39). No hay ninguna regla contable en Python que pueda detectarlo: esa lectura es de
    la revision humana del borrador.
    """
    lote = [{
        "fecha": "2024-05-31",
        "glosa": "DEPRECIACION DEL MES",
        "asiento": [partida("68", debe=135.0), partida("33", haber=135.0)],
    }]

    asiento, avisos = un_asiento(lote)

    assert lados(asiento) == [("68", "debe", 135.0), ("33", "haber", 135.0)]
    assert totales(asiento) == (135.0, 135.0)
    assert not asiento.get("revisar"), "Python no reinterpreta un asiento que cuadra"
    assert not [a for a in avisos if "no cuadra" in a]


def test_el_descuento_registrado_en_la_misma_cuenta_70_se_conserva_sin_normalizar():
    """
    Criterio alternativo valido en algunos cursos: reducir el propio ingreso en 70 al DEBE
    en vez de usar la cuenta 74. Python no sabe cual de los dos criterios aplica el profesor:
    conserva el que llego y solo verifica el cuadre.
    """
    lote = [{
        "fecha": "2024-05-04",
        "glosa": "VENTA CON DESCUENTO (REDUCCION EN LA MISMA CUENTA DE INGRESO)",
        "asiento": [
            partida("10", debe=2700.0),
            partida("70", debe=300.0),
            partida("70", haber=3000.0),
        ],
    }]

    asiento, avisos = un_asiento(lote)

    assert len(asiento["asiento"]) == 3, "no se fusionan las dos lineas de 70"
    assert lados(asiento) == [
        ("10", "debe", 2700.0),
        ("70", "debe", 300.0),
        ("70", "haber", 3000.0),
    ], "Python no cambia de cuenta ni de columna"
    assert totales(asiento) == (3000.0, 3000.0)
    afirmarsin_revision(asiento, avisos)


# =========================================================================== #
# 12. Guardarraíl del prompt: el criterio se le dice a la IA, no solo al código
# =========================================================================== #
def test_el_criterio_por_efecto_esta_en_los_dos_prompts():
    """Los dos caminos de la IA (documento y enunciado) leen el mismo criterio."""
    assert ia.CRITERIO_DEBE_HABER in ia.instrucciones_agente_excel
    assert ia.CRITERIO_DEBE_HABER in ia.instrucciones_contador


def test_el_prompt_no_fija_columnas_por_clase_de_cuenta():
    """
    Las reglas que produjeron los tres errores no pueden volver: eran absolutos por clase
    ("SIEMPRE van al DEBE/HABER") y una orden de decidir solo con la clase y la glosa.
    """
    for prompt in (ia.instrucciones_agente_excel, ia.instrucciones_contador):
        assert "SIEMPRE van al DEBE" not in prompt
        assert "SIEMPRE van al HABER" not in prompt
        assert "Decide el lado del importe únicamente con esta dinámica" not in prompt
        assert "DINÁMICA DE LAS CUENTAS" not in prompt


def test_el_prompt_exige_decidir_por_el_efecto_de_la_operacion():
    for prompt in (ia.instrucciones_agente_excel, ia.instrucciones_contador):
        assert "POR EL EFECTO DE LA OPERACIÓN, NO POR LA CLASE DE LA CUENTA" in prompt
        assert "Una cuenta NO tiene una columna fija" in prompt
        assert "si AUMENTA, si DISMINUYE, si SE RECONOCE" in prompt
        # La parte que se conserva del criterio antiguo: el sentido natural de cada clase.
        assert "Activo que la operación AUMENTA: DEBE" in prompt
        # La incertidumbre se declara, no se resuelve por clase.
        assert 'NO lo resuelvas suponiendo' in prompt
        assert '"revisar": true' in prompt


def test_el_prompt_cubre_las_operaciones_del_libro_real():
    """Cada verbo del libro real tiene su regla de efecto, no una regla por cuenta."""
    criterio = ia.CRITERIO_DEBE_HABER
    for operacion in ("COMPRA", "VENTA", "COBRO", "PAGO", "DESCUENTO CONCEDIDO",
                      "DEPRECIACIÓN", "APORTE DE CAPITAL", "DEVOLUCIÓN"):
        assert operacion in criterio, f"falta la regla de efecto para {operacion}"
    # Los tres errores concretos quedan nombrados en el criterio.
    assert "una caja que SALE va al HABER" in criterio
    assert "contra-activo (19, 29, 36, 39)" in criterio
    assert "NO es un ingreso, reduce el ingreso" in criterio


def test_el_prompt_mantiene_la_partida_doble_sin_mover_importes():
    for prompt in (ia.instrucciones_agente_excel, ia.instrucciones_contador):
        assert "NO muevas" in prompt
        assert "importes de una partida a otra para cuadrar" in prompt
        assert "NO inventes una partida que el documento no muestre" in prompt
    # La validacion de cuadre sigue siendo trabajo de Python, sin correccion.
    assert "NO se mueven importes" in ia.procesar_dinamica_contable.__doc__
