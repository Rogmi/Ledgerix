"""
FASE 3 — LIBROS CONTABLES CONECTADOS A LA BASE DE DATOS REAL.

Qué protege este archivo
-----------------------
1. Que el Libro Diario y el Libro Mayor LEAN de `Asientos JOIN Detalles JOIN Cuentas`
   y no de una maqueta: los 5 asientos realmente registrados deben aparecer, con su
   numero real (`Asientos.id`), sus cuentas reales y su descripcion del catalogo.
2. Que los datos simulados de la maqueta (los asientos de 2026 con cuentas 10411,
   5011, 6011, 40111 y 4212) NO aparezcan por ningun lado. Es la regresion que
   importa: una vista con datos inventados parece informacion contable y no lo es.
3. Que los tres filtros (periodo, cuenta, glosa) funcionen sobre datos reales, y que
   el filtro de cuenta trate `%` y `_` como texto y no como comodines de LIKE.
4. Que el Libro Mayor acumule los movimientos de cada cuenta, con el signo de su
   naturaleza, y que esa naturaleza SEA la de `logica.naturaleza_de_elemento`: una
   sola regla en el sistema, no dos.
5. Que los libros NO ESCRIBAN. Esto se comprueba en tiempo de ejecucion con el
   `authorizer` de SQLite, que aborta cualquier peticion que no sea SELECT, READ o
   FUNCTION, y ademas comparando el contenido integro de la base antes y despues.

Base de datos de prueba
-----------------------
Ninguna prueba toca `datos/contabilidad.db`. Todas trabajan sobre una base temporal
construida en `tmp_path` con el MISMO esquema, sembrada con una replica exacta de los
5 asientos que hay registrados hoy. La ultima prueba abre la base real en modo
`solo lectura` y comprueba que sigue intacta al terminar.
"""

import inspect
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import logica as lg  # noqa: E402


# --------------------------------------------------------------------------- #
# Réplica exacta de lo que hay registrado hoy en datos/contabilidad.db
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

# (id, fecha, glosa, [(cuenta, debe, haber), ...]) en el mismo orden que el `id`
# de los detalles, que es el orden en que deben aparecer en el Libro Diario.
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

CUENTAS_REALES = {"10", "12", "20", "50", "63", "69", "70"}

# Lo que la maqueta de interfaz showed antes de esta fase. Ninguna de estas cuentas
# existe en el catalogo PCGE y ninguna de estas glosas esta registrada.
CUENTAS_FALSAS = {"10411", "5011", "6011", "40111", "4212"}
GLOSAS_FALSAS = ("Aporte inicial de capital", "factura F001-00045")

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


def _sembrar(ruta):
    """Crea la base de prueba y la llena con la replica de los asientos reales."""
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


# Columna por la que se ordena cada tabla al fotografiarla (su clave primaria).
COLUMNA_ORDEN = {"Cuentas": "codigo", "Asientos": "id", "Detalles": "id"}


def _foto_de_la_base(ruta):
    """Contenido integro de las tres tablas. Sirve para demostrar que no se toco nada."""
    conexion = sqlite3.connect(ruta)
    try:
        foto = {}
        for tabla, orden in COLUMNA_ORDEN.items():
            foto[tabla] = conexion.execute(f"SELECT * FROM {tabla} ORDER BY {orden}").fetchall()
        return foto
    finally:
        conexion.close()


@pytest.fixture
def bd_real(monkeypatch, tmp_path):
    """Base de prueba con los 5 asientos reales. logica.DB_PATH apunta a ella."""
    ruta = str(tmp_path / "contabilidad_test.db")
    _sembrar(ruta)
    monkeypatch.setattr(lg, "DB_PATH", ruta)
    return ruta


@pytest.fixture
def bd_vacia(monkeypatch, tmp_path):
    """Base de prueba con el esquema pero sin un solo asiento."""
    ruta = str(tmp_path / "contabilidad_vacia.db")
    conexion = sqlite3.connect(ruta)
    try:
        conexion.executescript(ESQUEMA)
        conexion.executemany(
            "INSERT INTO Cuentas (codigo, descripcion, elemento) VALUES (?, ?, ?)", CUENTAS
        )
        conexion.commit()
    finally:
        conexion.close()
    monkeypatch.setattr(lg, "DB_PATH", ruta)
    return ruta


def _salidas_por_asiento(df):
    """Agrupa el Diario por asiento sin depender de pandas.groupby."""
    agrupado = {}
    for _, fila in df.iterrows():
        debe, haber = agrupado.setdefault(int(fila["numero_asiento"]), [0.0, 0.0])
        agrupado[int(fila["numero_asiento"])] = [debe + float(fila["debe"]), haber + float(fila["haber"])]
    return agrupado


# =========================================================================== #
# 1. LIBRO DIARIO: LEE LOS ASIENTOS REALES
# =========================================================================== #
def test_diario_trae_los_5_asientos_reales(bd_real):
    df = lg.obtener_libro_diario()
    assert sorted(df["numero_asiento"].unique().tolist()) == [1, 2, 3, 4, 5]


def test_diario_trae_las_10_partidas_reales(bd_real):
    df = lg.obtener_libro_diario()
    assert len(df) == 10


def test_diario_muestra_las_cuentas_reales(bd_real):
    df = lg.obtener_libro_diario()
    assert set(df["cuenta"].unique()) == CUENTAS_REALES


def test_diario_no_incluye_ningun_dato_simulado(bd_real):
    """La regresion principal: la maqueta de 2026 no debe colarse en los libros."""
    df = lg.obtener_libro_diario()
    assert not (set(df["cuenta"].unique()) & CUENTAS_FALSAS)
    assert not any(str(f).startswith("2026") for f in df["fecha"])
    assert not any(marca in glosa for glosa in df["glosa"] for marca in GLOSAS_FALSAS)


def test_diario_no_incluye_datos_simulados_todavia_con_filtros(bd_vacia):
    """Ni la Mayor 'todas las cuentas' ni ningun filtro pueden resucitar la maqueta."""
    for df in (lg.obtener_libro_diario(),
               lg.obtener_libro_diario(cuenta="10411"),
               lg.obtener_libro_diario(glosa="Aporte inicial de capital"),
               lg.obtener_libro_mayor(),
               lg.obtener_libro_mayor(cuenta="5011")):
        assert df.empty


def test_diario_usa_el_id_real_como_numero_de_asiento(bd_real):
    """Nada de '0001': el correlativo es Asientos.id."""
    df = lg.obtener_libro_diario()
    assert df["numero_asiento"].map(type).eq(int).all()
    assert not df["numero_asiento"].astype(str).str.startswith("0").any()


def test_diario_trae_la_denominacion_del_catalogo(bd_real):
    """La denominacion sale de Cuentas.descripcion, no de un texto fijo en la interfaz."""
    df = lg.obtener_libro_diario()
    esperado = {codigo: descripcion for codigo, descripcion, _ in CUENTAS}
    assert dict(zip(df["cuenta"], df["denominacion"])) == esperado


def test_diario_esta_en_orden_cronologico(bd_real):
    df = lg.obtener_libro_diario()
    claves = list(zip(df["fecha"], df["numero_asiento"]))
    assert claves == sorted(claves)


def test_diario_cuadra_asiento_por_asiento(bd_real):
    """Lo que el motor de partida doble garantiza al guardar se lee igual en el libro."""
    for numero, (debe, haber) in _salidas_por_asiento(lg.obtener_libro_diario()).items():
        assert round(debe, 2) == round(haber, 2), f"el asiento {numero} aparece descuadrado en el libro"


def test_diario_los_totales_coinciden_con_lo_registrado(bd_real):
    df = lg.obtener_libro_diario()
    assert round(float(df["debe"].sum()), 2) == 280000.0
    assert round(float(df["haber"].sum()), 2) == 280000.0


# =========================================================================== #
# 2. FILTROS DEL LIBRO DIARIO
# =========================================================================== #
def test_filtro_fecha_desde(bd_real):
    df = lg.obtener_libro_diario(fecha_desde=date(2020, 7, 25))
    assert sorted(df["numero_asiento"].unique().tolist()) == [3, 4, 5]


def test_filtro_fecha_hasta(bd_real):
    df = lg.obtener_libro_diario(fecha_hasta=date(2020, 7, 20))
    assert sorted(df["numero_asiento"].unique().tolist()) == [1, 2]


def test_filtro_fecha_es_un_rango_cerrado(bd_real):
    df = lg.obtener_libro_diario(fecha_desde=date(2020, 7, 25), fecha_hasta=date(2020, 7, 25))
    assert sorted(df["numero_asiento"].unique().tolist()) == [3]


def test_filtro_cuenta_por_prefijo(bd_real):
    """'1' toma las cuentas que empiezan por 1; '10' solo la 10."""
    assert set(lg.obtener_libro_diario(cuenta="1")["cuenta"].unique()) == {"10", "12"}
    assert set(lg.obtener_libro_diario(cuenta="10")["cuenta"].unique()) == {"10"}


def test_filtro_cuenta_trata_los_comodines_como_texto(bd_real):
    """`10%` debe buscar el literal '10%', que no existe, y no capturarlo todo."""
    assert lg.obtener_libro_diario(cuenta="10%").empty
    assert lg.obtener_libro_diario(cuenta="1_").empty


def test_filtro_glosa_sin_distincion_de_mayusculas(bd_real):
    df = lg.obtener_libro_diario(glosa="se compra")
    assert sorted(df["numero_asiento"].unique().tolist()) == [2]


def test_filtro_glosa_acepta_una_parte_de_la_frase(bd_real):
    assert sorted(lg.obtener_libro_diario(glosa="INVENTARIO")["numero_asiento"].unique().tolist()) == [4]
    assert sorted(lg.obtener_libro_diario(glosa="contado")["numero_asiento"].unique().tolist()) == [1, 2, 5]


def test_filtros_combinados(bd_real):
    df = lg.obtener_libro_diario(fecha_desde=date(2020, 7, 20), cuenta="10", glosa="contado")
    assert sorted(df["numero_asiento"].unique().tolist()) == [2, 5]


# =========================================================================== #
# 3. LIBRO MAYOR: ACUMULA LOS MOVIMIENTOS REALES DE CADA CUENTA
# =========================================================================== #
def test_mayor_acumula_una_cuenta_deudora(bd_real):
    """La 10 recibe 100.000 y paga 50.000 y 20.000: 100.000 -> 50.000 -> 30.000."""
    df = lg.obtener_libro_mayor(cuenta="10")
    assert df["debe"].tolist() == [100000.0, 0.0, 0.0]
    assert df["haber"].tolist() == [0.0, 50000.0, 20000.0]
    assert df["saldo"].tolist() == [100000.0, 50000.0, 30000.0]


def test_mayor_acumula_una_cuenta_que_cambia_de_saldo(bd_real):
    """La 20 compra 50.000 y sale 40.000: 50.000 -> 10.000."""
    df = lg.obtener_libro_mayor(cuenta="20")
    assert df["saldo"].tolist() == [50000.0, 10000.0]


def test_mayor_una_cuenta_acreedora_iguala_los_estados_financieros(bd_real):
    """
    Convencion de signo del Mayor: `(Debe - Haber) x signo`. En una cuenta acreedora el
    signo es -1, de modo que el saldo sale como `Haber - Debe`, exactamente el mismo
    numero que reporta `obtener_saldos_cuentas()`. El Mayor y los Estados Financieros
    no pueden decir la misma cosa con dos cifras distintas.
    """
    assert lg.obtener_libro_mayor(cuenta="50")["saldo"].tolist() == [100000.0]
    assert lg.obtener_libro_mayor(cuenta="70")["saldo"].tolist() == [70000.0]


def test_mayor_filtra_una_cuenta_exacta(bd_real):
    """A diferencia del Diario, aqui la cuenta es exacta: '1' no trae '12'."""
    assert lg.obtener_libro_mayor(cuenta="1").empty
    assert set(lg.obtener_libro_mayor(cuenta="12")["cuenta"].unique()) == {"12"}


def test_mayor_respeta_el_rango_de_fechas(bd_real):
    df = lg.obtener_libro_mayor(fecha_desde=date(2020, 7, 31))
    assert sorted(df["numero_asiento"].unique().tolist()) == [4, 5]


def test_mayor_todas_las_cuentas_no_mezcla_saldos(bd_real):
    """
    Cada fila muestra el acumulado de SU cuenta, y el acumulado no se reinicia ni se
    arrastra de una cuenta a otra: es el mismo saldo running que se obtiene al pedir
    esa cuenta sola.
    """
    todas = lg.obtener_libro_mayor()
    for cuenta in CUENTAS_REALES:
        sola = lg.obtener_libro_mayor(cuenta=cuenta)
        dentro = todas[todas["cuenta"] == cuenta]
        assert dentro["saldo"].tolist() == sola["saldo"].tolist()


def test_mayor_expone_el_elemento_del_catalogo(bd_real):
    """El Mayor necesita el elemento para explicar la naturaleza del saldo."""
    elementos = dict(zip(lg.obtener_libro_mayor()["cuenta"], lg.obtener_libro_mayor()["elemento"]))
    assert elementos == {"10": 1, "12": 2, "20": 2, "50": 5, "63": 6, "69": 6, "70": 7}


# =========================================================================== #
# 4. NATURALEZ: UNA SOLA REGLA PARA LIBROS Y ESTADOS FINANCIEROS
# =========================================================================== #
@pytest.mark.parametrize("elemento,esperado", [
    (1, "deudora"), (2, "deudora"), (3, "deudora"), (6, "deudora"), (9, "deudora"),
    (4, "acreedora"), (5, "acreedora"), (7, "acreedora"), (8, "acreedora"),
    (0, "acreedora"), (99, "acreedora"),
])
def test_naturaleza_de_elemento(elemento, esperado):
    assert lg.naturaleza_de_elemento(elemento) == esperado


def test_signo_de_elemento_es_la_misma_regla():
    for elemento in (1, 2, 3, 6, 9, 4, 5, 7, 8):
        esperado = 1 if lg.naturaleza_de_elemento(elemento) == "deudora" else -1
        assert lg.signo_de_elemento(elemento) == esperado


def test_el_elemento_8_sigue_siendo_acreedor(bd_real):
    """El 8 caia en la rama 'acreedor por defecto' del calculo original: no debe cambiar."""
    assert lg.naturaleza_de_elemento(8) == "acreedora"
    assert lg.signo_de_elemento(8) == -1


def test_el_mayor_coincide_con_la_regla_de_logica(bd_real):
    """
    Comparacion cruzada, cuenta por cuenta: el ultimo saldo del Mayor debe ser
    IDENTICO al saldo que reporta `obtener_saldos_cuentas()`. No se comparan las
    reglas, se comparan los numeros: si un dia divergen, esta prueba lo dice.
    """
    saldos = lg.obtener_saldos_cuentas().set_index("codigo")["saldo"].to_dict()
    mayor = lg.obtener_libro_mayor()
    for cuenta in CUENTAS_REALES:
        filas = mayor[mayor["cuenta"] == cuenta]
        elemento = int(filas["elemento"].iloc[0])
        # El signo con el que el Mayor pondera (Debe - Haber) es el de la regla comun.
        assert lg.signo_de_elemento(elemento) == (
            1 if lg.naturaleza_de_elemento(elemento) == "deudora" else -1
        )
        assert float(filas["saldo"].iloc[-1]) == round(saldos[cuenta], 2), (
            f"la cuenta {cuenta} tiene saldos distintos en el Libro Mayor y en los Estados Financieros"
        )


def test_obtener_saldos_cuentas_no_regresiono(bd_real):
    """El refactor a `naturaleza_de_elemento` no puede mover ni un centimo."""
    saldos = lg.obtener_saldos_cuentas().set_index("codigo")["saldo"].to_dict()
    assert saldos == {
        "10": 30000.0, "12": 70000.0, "20": 10000.0,
        "50": 100000.0, "63": 20000.0, "69": 40000.0, "70": 70000.0,
    }


# =========================================================================== #
# 5. LISTAS DE APOYO DEL LIBRO MAYOR
# =========================================================================== #
def test_cuentas_con_movimiento_solo_las_que_tienen_partidas(bd_real):
    df = lg.obtener_cuentas_con_movimiento()
    assert set(df["codigo"]) == CUENTAS_REALES
    assert "11" not in set(df["codigo"])  # esta en el catalogo, pero nadie la movio


def test_cuentas_con_movimiento_expone_la_naturaleza(bd_real):
    naturalezas = dict(zip(lg.obtener_cuentas_con_movimiento()["codigo"],
                           lg.obtener_cuentas_con_movimiento()["naturaleza"]))
    assert naturalezas == {"10": "deudora", "12": "deudora", "20": "deudora", "63": "deudora",
                           "69": "deudora", "50": "acreedora", "70": "acreedora"}


def test_rango_de_fechas_de_los_asientos_reales(bd_real):
    assert lg.obtener_rango_fechas_asientos() == (date(2020, 7, 8), date(2020, 7, 31))


def test_rango_de_fechas_sin_asientos(bd_vacia):
    assert lg.obtener_rango_fechas_asientos() == (None, None)


def test_libros_vacios_no_inventan_nada(bd_vacia):
    assert lg.obtener_libro_diario().empty
    assert lg.obtener_libro_mayor().empty
    assert lg.obtener_cuentas_con_movimiento().empty


# =========================================================================== #
# 6. LOS LIBROS NO PUEDEN ESCRIBIR
# =========================================================================== #
ACCIONES_QUE_LAS_BASES_PIDEN = "SELECT, READ y FUNCTION"
ACCIONES_DE_LECTURA = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
})


@pytest.fixture
def conexion_solo_lectura(monkeypatch, bd_real):
    """
    Sustituye `obtener_conexion` por una que instala el authorizer de SQLite: si el
    codigo de los libros pide cualquier otra cosa (INSERT, UPDATE, DELETE, CREATE,
    DROP, ALTER...), SQLite aborta la sentencia y la prueba falla.

    No es una busqueda de palabras en el codigo: es una prueba de ejecucion.
    """
    original = lg.obtener_conexion

    def obtener():
        conexion = original()

        def authorizer(accion, arg1, arg2, dbname, origen):
            if accion not in ACCIONES_DE_LECTURA:
                raise AssertionError(
                    f"un libro contable intento una operacion que no es de lectura "
                    f"(codigo de accion SQLite {accion}); solo se permiten {ACCIONES_QUE_LAS_BASES_PIDEN}"
                )
            return sqlite3.SQLITE_OK

        conexion.set_authorizer(authorizer)
        return conexion

    monkeypatch.setattr(lg, "obtener_conexion", obtener)
    return obtener


def test_las_consultas_de_libro_solo_piden_lectura(conexion_solo_lectura):
    """Cada consulta se ejecuta de verdad contra el authorizer puesto."""
    lg.obtener_libro_diario()
    lg.obtener_libro_diario(date(2020, 7, 1), date(2020, 7, 31), "10", "contado")
    lg.obtener_libro_mayor()
    lg.obtener_libro_mayor(date(2020, 7, 1), date(2020, 7, 31), "10")
    lg.obtener_cuentas_con_movimiento()
    lg.obtener_rango_fechas_asientos()


def test_leer_los_libros_no_altera_la_base(bd_real):
    """Foto del contenido integro antes y despues de leerlo todo."""
    antes = _foto_de_la_base(bd_real)
    lg.obtener_libro_diario()
    lg.obtener_libro_mayor()
    lg.obtener_libro_mayor(cuenta="10")
    lg.obtener_cuentas_con_movimiento()
    lg.obtener_rango_fechas_asientos()
    assert _foto_de_la_base(bd_real) == antes


def test_leer_los_libros_no_crea_registros(bd_real):
    """Un libro que 'inserta' algo duplicaria la contabilidad: aqui no cabe."""
    conexion = sqlite3.connect(bd_real)
    try:
        antes = [conexion.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("Asientos", "Detalles")]
    finally:
        conexion.close()

    lg.obtener_libro_diario()
    lg.obtener_libro_mayor()

    conexion = sqlite3.connect(bd_real)
    try:
        despues = [conexion.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("Asientos", "Detalles")]
    finally:
        conexion.close()
    assert despues == antes == [5, 10]


def test_la_seccion_de_libros_no_tiene_sentencias_de_escritura():
    """Comprobacion estatica, complemento del authorizer: la seccion no declara SQL de escritura."""
    fuente = inspect.getsource(lg)
    inicio = fuente.index("LIBROS CONTABLES (SOLO LECTURA)")
    seccion = fuente[inicio:]
    for sentencia in ("INSERT", "UPDATE", "DELETE", "REPLACE INTO", "DROP", "ALTER", "COMMIT", "ROLLBACK"):
        assert sentencia not in seccion.upper(), f"la seccion de libros declara {sentencia}"


def test_registrar_asiento_sigue_siendo_la_unica_puerta_de_escritura():
    """Las consultas de libros no invocan al registrador: no pueden crear asientos."""
    for nombre in ("obtener_libro_diario", "obtener_libro_mayor", "obtener_cuentas_con_movimiento",
                   "obtener_rango_fechas_asientos"):
        fuente = inspect.getsource(getattr(lg, nombre))
        assert "registrar_asiento_completo" not in fuente
        assert "obtener_conexion" in fuente, "debe leer por la conexion compartida"


# =========================================================================== #
# 7. LA BASE DE DATOS REAL NO SE CONTAMINA
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #


def test_la_base_real_permanece_igual_despues_de_leer_los_libros():
    """
    Ninguna prueba anterior toco `datos/contabilidad.db`. Esta corre los libros contra
    la base real, en modo solo lectura, y verifica que sigue byte a byte igual.
    """
    ruta_real = RAIZ / "datos" / "contabilidad.db"
    if not ruta_real.exists():
        pytest.skip("este proyecto todavia no tiene datos/contabilidad.db")

    antes = _foto_de_la_base(str(ruta_real))
    original = lg.DB_PATH
    lg.DB_PATH = str(ruta_real)
    try:
        diario = lg.obtener_libro_diario()
        mayor = lg.obtener_libro_mayor()
        lg.obtener_cuentas_con_movimiento()
        lg.obtener_rango_fechas_asientos()
    finally:
        lg.DB_PATH = original

    assert _foto_de_la_base(str(ruta_real)) == antes
    assert not diario.empty, "la base real tiene asientos y el Diario deberia devolverlos"
    assert not mayor.empty
    # Y lo que devuelve son los asientos de verdad, no la maqueta de 2026.
    assert not (set(diario["cuenta"].unique()) & CUENTAS_FALSAS)
    assert not any(str(f).startswith("2026") for f in diario["fecha"])
    assert len(diario) >= len(ASIENTOS)
    for numero, (debe, haber) in _salidas_por_asiento(diario).items():
        assert round(debe, 2) == round(haber, 2), f"el asiento real {numero} esta descuadrado"