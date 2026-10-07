"""
INICIALIZACION AUTOMATICA DE LA BASE DE DATOS.

Qué protege este archivo
-----------------------
El contrato de `database.inicializar_base_de_datos_si_es_necesario()`, que es lo que
permite que una instalacion limpia funcione con solo `streamlit run app.py`:

1. Que una base inexistente se cree con las TRES tablas y las 70 cuentas del PCGE. Es
   el bug original: `sqlite3.connect()` dejaba un archivo de 0 bytes y la primera
   consulta al catalogo fallaba con `no such table: Cuentas`.
2. Que una base VACIA (el archivo de 0 bytes es el caso real) tambien se inicialice.
3. Que una base PARCIAL se repare SOLO en las tablas que faltan, sin recrear las que
   ya existian y sin tocar sus datos.
4. Que sembrar el catalogo sea idempotente de verdad. La guarda antigua era
   `COUNT(*) == 0`: con una sola cuenta presente daba el catalogo por cargado y
   dejaba el sistema con una unica cuenta. Aqui se exige que se complete a 70.
5. Que NUNCA se sobrescriba una cuenta existente, ni siquiera si su descripcion fue
   editada a mano por el usuario.
6. Que una base ya inicializada NO se toque: ni Asientos, ni Detalles, ni el SQL del
   esquema. Se comprueba con foto del contenido integro y con el `authorizer` de
   SQLite, que aborta cualquier sentencia que no sea de lectura.
7. Que una base INCONSISTENTE (movimientos sin catalogo) no se repare sola: se
   informa y no se escribe. Sembrar 70 cuentas ahi seria inventar el plan de cuentas.
8. Que la ruta se reciba por parametro y se respete. `logica.DB_PATH` y
   `database.DB_PATH` son dos nombres distintos con el mismo valor, y las pruebas
   sustituyen solo el primero: una inicializacion que leyera la constante de modulo
   escribiria en la base real durante la suite.

Base de datos de prueba
-----------------------
Ninguna prueba toca `datos/contabilidad.db`. Todas trabajan sobre una base temporal
en `tmp_path` construida por la propia funcion que se prueba o con el esquema a mano.
"""

import ast
import inspect
import os
import sqlite3
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import database as bd  # noqa: E402
import logica as lg  # noqa: E402


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
CUENTAS_ESPERADAS = 70
CUENTAS_DEL_CATALOGO = {fila[0] for fila in bd.CATALOGO_PCGE}

# Dos asientos reales, tomados de la base de referencia.
ASIENTOS = [
    (1, "2020-07-08", "Se crea una empresa con 100,000 al contado.",
     [("10", 100000.0, 0.0), ("50", 0.0, 100000.0)]),
    (2, "2020-07-20", "Se compra 50,000 de mercaderia al contado",
     [("20", 50000.0, 0.0), ("10", 0.0, 50000.0)]),
]


def _abrir(ruta):
    conexion = sqlite3.connect(str(ruta))
    try:
        conexion.execute("PRAGMA foreign_keys = ON;")
        return conexion
    except Exception:
        conexion.close()
        raise


def _archivo_vacio(ruta):
    """El archivo de 0 bytes que dejaba `sqlite3.connect()`: el estado del bug original."""
    Path(ruta).write_bytes(b"")
    return ruta


def _crear_esquema(ruta):
    """Solo el esquema, sin catalogo: el estado 'VACIA' con las 3 tablas presentes."""
    conexion = _abrir(ruta)
    try:
        for sentencia in bd.ESQUEMA_SQL:
            conexion.execute(sentencia)
        conexion.commit()
    finally:
        conexion.close()


def _crear_esquema_con_cuentas(ruta, cuentas):
    conexion = _abrir(ruta)
    try:
        for sentencia in bd.ESQUEMA_SQL:
            conexion.execute(sentencia)
        conexion.executemany(
            "INSERT INTO Cuentas (codigo, descripcion, elemento) VALUES (?, ?, ?)", cuentas
        )
        conexion.commit()
    finally:
        conexion.close()


def _registrar_asientos(ruta, asientos=ASIENTOS, claves_foraneas=True):
    """
    `claves_foraneas=False` existe solo para el caso INCONSISTENTE: `Detalles` exige
    una cuenta que existe en `Cuentas`, asi que una base con movimientos y catalogo
    vacio no se puede construir con las Foreign Keys activas. Se construye sin ellas,
    que es justo como se produce esa situacion en la vida real: una base escrita por
    una herramienta que no activo el PRAGMA.
    """
    conexion = sqlite3.connect(str(ruta))
    try:
        if claves_foraneas:
            conexion.execute("PRAGMA foreign_keys = ON;")
        for asiento_id, fecha, glosa, partidas in asientos:
            conexion.execute(
                "INSERT INTO Asientos (id, fecha, glosa) VALUES (?, ?, ?)",
                (asiento_id, fecha, glosa),
            )
            conexion.executemany(
                "INSERT INTO Detalles (asiento_id, cuenta_codigo, debe, haber)"
                " VALUES (?, ?, ?, ?)",
                [(asiento_id, c, d, h) for c, d, h in partidas],
            )
        conexion.commit()
    finally:
        conexion.close()


def _tablas(ruta):
    conexion = _abrir(ruta)
    try:
        return {
            f[0] for f in conexion.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        conexion.close()


def _cuentas(ruta):
    conexion = _abrir(ruta)
    try:
        return conexion.execute(
            "SELECT codigo, descripcion, elemento FROM Cuentas ORDER BY codigo"
        ).fetchall()
    finally:
        conexion.close()


def _contar(ruta, tabla):
    conexion = _abrir(ruta)
    try:
        return conexion.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
    finally:
        conexion.close()


def _foto(ruta):
    """Contenido integro: datos, esquema e indices. Sirve para demostrar 'no se toco'."""
    conexion = _abrir(ruta)
    try:
        foto = {
            "datos": {
                tabla: conexion.execute(f"SELECT * FROM {tabla}").fetchall()
                for tabla in sorted(_tablas(ruta))
            },
            "esquema": sorted(
                (f[0], f[1], " ".join((f[2] or "").split()))
                for f in conexion.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                )
            ),
            "fk_check": conexion.execute("PRAGMA foreign_key_check").fetchall(),
        }
        return foto
    finally:
        conexion.close()


# =========================================================================== #
# CASO B: LA BASE NO EXISTE
# =========================================================================== #
def test_una_base_inexistente_se_crea_con_las_tres_tablas_y_el_pcge(tmp_path):
    """`streamlit run app.py` sin base previa: queda lista y utilizable."""
    ruta = str(tmp_path / "contabilidad.db")
    assert not os.path.exists(ruta), "la base tiene que empezar sin existir"

    resultado = bd.inicializar_base_de_datos_si_es_necesario(ruta)

    assert resultado["estado"] == bd.NO_EXISTE, "el diagnostico debe decir que no estaba"
    assert resultado["accion"] == bd.INICIALIZADA
    assert os.path.exists(ruta)
    assert _tablas(ruta) == set(bd.TABLAS_ESTRUCTURALES)
    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS
    assert resultado["cuentas_insertadas"] == CUENTAS_ESPERADAS
    assert sorted(fila[0] for fila in _cuentas(ruta)) == sorted(CUENTAS_DEL_CATALOGO)


def test_la_base_nueva_tiene_la_misma_estructura_que_la_de_referencia():
    """
    Comparacion SEMANTICA del esquema, no identidad de bytes: la representacion
    fisica interna de SQLite puede variar y no importa. Lo que tiene que coincidir es
    lo que consulta el sistema: tablas, columnas, tipos, nulabilidad, claves primarias,
    claves foraneas, indices y la sentencia que SQLite guarda para cada objeto.
    """
    ruta = str(Path(os.environ.get("TEMP", "/tmp")) / "_esquema_nuevo.db")
    if os.path.exists(ruta):
        os.remove(ruta)
    bd.inicializar_base_de_datos_si_es_necesario(ruta)
    try:
        nueva = _estructura_semantica(ruta)
        # La base de referencia es la que la aplicacion usa hoy.
        referencia = _estructura_semantica(RAIZ / "datos" / "contabilidad.db")

        assert nueva["tablas"] == referencia["tablas"]
        assert nueva["columnas"] == referencia["columnas"]
        assert nueva["pk"] == referencia["pk"]
        assert nueva["fk"] == referencia["fk"]
        assert nueva["indices"] == referencia["indices"]
        assert nueva["sql"] == referencia["sql"]
    finally:
        if os.path.exists(ruta):
            os.remove(ruta)


def _estructura_semantica(ruta):
    conexion = _abrir(ruta)
    try:
        tablas = sorted(
            f[0] for f in conexion.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%'"
            )
        )
        info = {
            "tablas": tablas,
            "columnas": {}, "pk": {}, "fk": {}, "indices": {}, "sql": sorted(
                (f[0], f[1], " ".join((f[2] or "").split()))
                for f in conexion.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                )
            ),
        }
        for tabla in tablas:
            info["columnas"][tabla] = [
                (f[1], f[2].upper(), f[3], f[5])
                for f in conexion.execute(f"PRAGMA table_info({tabla})")
            ]
            info["pk"][tabla] = sorted(
                (f[2], f[3], f[4]) for f in conexion.execute(f"PRAGMA table_info({tabla})")
                if f[5]
            )
            info["fk"][tabla] = sorted(
                (f[2], f[3], f[4], f[5], f[6], f[7])
                for f in conexion.execute(f"PRAGMA foreign_key_list({tabla})")
            )
            info["indices"][tabla] = sorted(
                (f[1], bool(f[2]), f[3]) for f in conexion.execute(f"PRAGMA index_list({tabla})")
            )
        return info
    finally:
        conexion.close()


# =========================================================================== #
# CASO C: LA BASE EXISTE VACIA (el archivo de 0 bytes del bug original)
# =========================================================================== #
def test_un_archivo_de_cero_bytes_se_inicializa(tmp_path):
    """
    Este es el estado que dejaba el bug: el archivo existe porque `sqlite3.connect()`
    lo creo, pero esta vacio. No hay que borrarlo a mano para recuperarlo.
    """
    ruta = tmp_path / "contabilidad.db"
    _archivo_vacio(ruta)
    assert ruta.stat().st_size == 0

    resultado = bd.inicializar_base_de_datos_si_es_necesario(str(ruta))

    assert resultado["estado"] == bd.VACIA
    assert resultado["accion"] == bd.INICIALIZADA
    assert _tablas(str(ruta)) == set(bd.TABLAS_ESTRUCTURALES)
    assert _contar(str(ruta), "Cuentas") == CUENTAS_ESPERADAS


def test_esquema_sin_catalogo_tampoco_se_da_por_bueno(tmp_path):
    """Las 3 tablas existem pero `Cuentas` esta vacia y no hay movimientos."""
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema(ruta)
    assert _contar(ruta, "Cuentas") == 0

    resultado = bd.inicializar_base_de_datos_si_es_necesario(ruta)

    assert resultado["estado"] == bd.VACIA
    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS


# =========================================================================== #
# CASO D: LA BASE ESTA PARCIAL
# =========================================================================== #
def test_si_falta_una_tabla_se_crea_solo_esa(tmp_path):
    ruta = tmp_path / "contabilidad.db"
    _archivo_vacio(ruta)
    bd.inicializar_base_de_datos_si_es_necesario(str(ruta))
    assert _tablas(str(ruta)) == set(bd.TABLAS_ESTRUCTURALES)

    conexion = _abrir(str(ruta))
    try:
        conexion.execute("DROP TABLE Detalles;")
        conexion.commit()
    finally:
        conexion.close()
    assert _tablas(str(ruta)) == {"Cuentas", "Asientos"}

    resultado = bd.inicializar_base_de_datos_si_es_necesario(str(ruta))

    assert resultado["estado"] == bd.PARCIAL
    assert resultado["accion"] == bd.REPARADA
    assert resultado["tablas_creadas"] == ["Detalles"]
    assert _tablas(str(ruta)) == set(bd.TABLAS_ESTRUCTURALES)


def test_reparar_no_recrea_las_tablas_que_ya_existian(tmp_path):
    """La tabla que NO faltaba conserva su contenido, su SQL y sus movimientos."""
    ruta = tmp_path / "parcial.db"
    _archivo_vacio(ruta)
    bd.inicializar_base_de_datos_si_es_necesario(str(ruta))
    _registrar_asientos(str(ruta))  # 2 asientos, 4 detalles

    conexion = _abrir(str(ruta))
    try:
        conexion.execute("DROP TABLE Detalles;")
        conexion.commit()
    finally:
        conexion.close()
    assert _tablas(str(ruta)) == {"Cuentas", "Asientos"}

    cuentas_antes = _cuentas(str(ruta))
    sql_cuentas_antes = _sql_de(str(ruta), "Cuentas")
    sql_asientos_antes = _sql_de(str(ruta), "Asientos")
    asientos_antes = _foto(str(ruta))["datos"]["Asientos"]

    resultado = bd.inicializar_base_de_datos_si_es_necesario(str(ruta))

    assert resultado["estado"] == bd.PARCIAL
    assert resultado["tablas_creadas"] == ["Detalles"]
    assert _cuentas(str(ruta)) == cuentas_antes, "no se tocan las cuentas existentes"
    assert _sql_de(str(ruta), "Cuentas") == sql_cuentas_antes, "no se recrea Cuentas"
    assert _sql_de(str(ruta), "Asientos") == sql_asientos_antes, "no se recrea Asientos"
    # Y lo mas importante: los asientos que ya estaban registrados siguen ahi. Reparar
    # la estructura no puede ser una manera de perder contabilidad.
    assert _foto(str(ruta))["datos"]["Asientos"] == asientos_antes
    assert _contar(str(ruta), "Asientos") == 2
    assert _contar(str(ruta), "Detalles") == 0
    assert _contar(str(ruta), "Cuentas") == CUENTAS_ESPERADAS


def _sql_de(ruta, objeto):
    conexion = _abrir(ruta)
    try:
        fila = conexion.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (objeto,)
        ).fetchone()
        return " ".join((fila[0] if fila else "").split())
    finally:
        conexion.close()


# =========================================================================== #
# CASO E: EL CATALOGO ESTA INCOMPLETO
# =========================================================================== #
def test_una_sola_cuenta_se_completa_a_las_70(tmp_path):
    """
    El hueco real de la guarda antigua: `COUNT(*) == 0` daba el catalogo por cargado
    con una unica cuenta presente, y el sistema se quedaba con esa cuenta.
    """
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema_con_cuentas(ruta, [("10", "Efectivo y equivalentes de efectivo", 1)])
    assert _contar(ruta, "Cuentas") == 1

    resultado = bd.inicializar_base_de_datos_si_es_necesario(ruta)

    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS
    assert resultado["cuentas_insertadas"] == CUENTAS_ESPERADAS - 1


def test_la_cuenta_que_ya_existia_no_cambia(tmp_path):
    """Ni el codigo, ni el elemento, ni la descripcion: la fila previa se respeta."""
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema_con_cuentas(ruta, [("10", "Efectivo y equivalentes de efectivo", 1)])

    bd.inicializar_base_de_datos_si_es_necesario(ruta)

    fila = next(f for f in _cuentas(ruta) if f[0] == "10")
    assert fila == ("10", "Efectivo y equivalentes de efectivo", 1)


def test_una_descripcion_editada_por_el_usuario_no_se_sobrescribe(tmp_path):
    """`ON CONFLICT DO NOTHING` es lo que protege la edicion manual del catalogo."""
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema_con_cuentas(ruta, [("63", "GASTOS QUE SOLO EXISTEN EN ESTA EMPRESA", 6)])

    bd.inicializar_base_de_datos_si_es_necesario(ruta)

    fila = next(f for f in _cuentas(ruta) if f[0] == "63")
    assert fila[1] == "GASTOS QUE SOLO EXISTEN EN ESTA EMPRESA", "la edicion del usuario se perderia"
    assert fila[2] == 6, "tampoco se le puede cambiar el elemento"
    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS


def test_una_cuenta_ajena_al_pcge_no_se_borra(tmp_path):
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema_con_cuentas(ruta, [("9999", "Cuenta inventada de la prueba", 5)])

    bd.inicializar_base_de_datos_si_es_necesario(ruta)

    codigos = {f[0] for f in _cuentas(ruta)}
    assert "9999" in codigos, "una cuenta que no es del PCGE no es motivo para borrarla"
    assert CUENTAS_DEL_CATALOGO.issubset(codigos)


# =========================================================================== #
# CASO F: LA BASE TIENE CONTABILIDAD REGISTRADA
# =========================================================================== #
def test_una_base_con_asientos_no_se_modifica(tmp_path):
    ruta = tmp_path / "contabilidad.db"
    _archivo_vacio(ruta)
    bd.inicializar_base_de_datos_si_es_necesario(str(ruta))
    _registrar_asientos(str(ruta))

    antes = _foto(str(ruta))
    resultado = bd.inicializar_base_de_datos_si_es_necesario(str(ruta))
    despues = _foto(str(ruta))

    assert resultado["estado"] == bd.OK
    assert resultado["accion"] == bd.SIN_CAMBIOS
    assert resultado["cuentas_insertadas"] == 0
    assert despues == antes, "la inicializacion toco la base con contabilidad registrada"
    assert _contar(str(ruta), "Asientos") == 2
    assert _contar(str(ruta), "Detalles") == 4


def test_la_base_de_referencia_del_proyecto_no_se_toca(tmp_path):
    """
    La base real se abre SOLO en lectura y se compara antes y despues. Es la misma
    idea que protege `test_libros_contables`: si la inicializacion escribiera aqui, el
    resto del sistema empezaria a mentir sobre los datos del usuario.
    """
    ruta_real = RAIZ / "datos" / "contabilidad.db"
    if not ruta_real.exists():
        pytest.skip("este proyecto todavia no tiene datos/contabilidad.db")

    antes = _foto(str(ruta_real))
    resultado = bd.inicializar_base_de_datos_si_es_necesario(str(ruta_real))
    despues = _foto(str(ruta_real))

    assert resultado["estado"] == bd.OK
    assert resultado["accion"] == bd.SIN_CAMBIOS
    assert despues == antes
    assert _contar(str(ruta_real), "Cuentas") == CUENTAS_ESPERADAS
    assert _contar(str(ruta_real), "Asientos") == 5
    assert _contar(str(ruta_real), "Detalles") == 10


def test_inicializar_no_escribe_una_base_que_ya_esta_lista(tmp_path, monkeypatch):
    """
    No basta con comparar el resultado: se comprueba en tiempo de ejecucion que a la
    base ya inicializada no se le pide NI UNA escritura. Para eso se envuelve
    `sqlite3.connect` y se instala en cada conexion que el modulo abra el
    `authorizer` de SQLite, que deniega cualquier sentencia que no sea SELECT, READ
    o FUNCTION. Si la inicializacion abriera una conexion de escritura aunque no
    escribiera nada, el PRAGMA y el BEGIN tambien lo pagarian.
    """
    ruta = str(tmp_path / "contabilidad.db")
    bd.inicializar_base_de_datos_si_es_necesario(ruta)
    _registrar_asientos(ruta)

    permitidos = frozenset({
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    })
    rechazadas = []
    original = sqlite3.connect

    def connect(*argumentos, **palabras):
        conexion = original(*argumentos, **palabras)

        def authorizer(accion, arg1, arg2, dbname, origen):
            if accion not in permitidos:
                rechazadas.append(accion)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conexion.set_authorizer(authorizer)
        return conexion

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        resultado = bd.inicializar_base_de_datos_si_es_necesario(ruta)
        diagnostico = bd.diagnosticar_base_de_datos(ruta)
    finally:
        monkeypatch.undo()

    assert resultado["estado"] == bd.OK
    assert resultado["accion"] == bd.SIN_CAMBIOS
    assert diagnostico["estado"] == bd.OK
    assert rechazadas == [], "se pidio una escritura sobre una base ya inicializada"


# =========================================================================== #
# CASO G: LA BASE ESTA INCONSISTENTE
# =========================================================================== #
def test_movimientos_sin_catalogo_no_se_reparan_solos(tmp_path):
    """
    Sembrar 70 cuentas ahi no seria reparar: seria inventar el plan de cuentas de una
    contabilidad que ya tiene movimientos. La funcion informa y no escribe.
    """
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema(ruta)
    _registrar_asientos(ruta, [ASIENTOS[0]], claves_foraneas=False)  # 0 cuentas

    antes = _foto(ruta)
    resultado = bd.inicializar_base_de_datos_si_es_necesario(ruta)
    despues = _foto(ruta)

    assert resultado["estado"] == bd.INCONSISTENTE
    assert resultado["accion"] == bd.SIN_CAMBIOS
    assert resultado["cuentas_insertadas"] == 0
    assert despues == antes, "una base inconsistente no se puede tocar"
    assert _contar(ruta, "Cuentas") == 0
    assert _contar(ruta, "Asientos") == 1
    assert _contar(ruta, "Detalles") == 2
    assert "cat" in resultado["detalle"] and "vac" in resultado["detalle"], (
        "el mensaje tiene que explicar el problema al usuario"
    )


def test_el_diagnostico_tambien_lo_detecta_sin_escribir(tmp_path):
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema(ruta)
    _registrar_asientos(ruta, [ASIENTOS[0]], claves_foraneas=False)

    antes = _foto(ruta)
    diagnostico = bd.diagnosticar_base_de_datos(ruta)

    assert diagnostico["estado"] == bd.INCONSISTENTE
    assert diagnostico["cuentas"] == 0
    assert diagnostico["asientos"] == 1
    assert diagnostico["detalles"] == 2
    assert diagnostico["cuentas_faltantes"], "debe decir cuantas cuentas faltan"
    assert _foto(ruta) == antes, "el diagnostico es de solo lectura"


# =========================================================================== #
# CASO H: IDEMPOTENCIA
# =========================================================================== #
def test_inicializar_dos_veces_no_crea_nada_nuevo(tmp_path):
    ruta = str(tmp_path / "contabilidad.db")
    _archivo_vacio(ruta)

    primero = bd.inicializar_base_de_datos_si_es_necesario(ruta)
    foto_tras_primero = _foto(ruta)
    segundo = bd.inicializar_base_de_datos_si_es_necesario(ruta)
    foto_tras_segundo = _foto(ruta)

    assert primero["cuentas_insertadas"] == CUENTAS_ESPERADAS
    assert segundo["cuentas_insertadas"] == 0
    assert segundo["estado"] == bd.OK
    assert segundo["accion"] == bd.SIN_CAMBIOS
    assert foto_tras_segundo == foto_tras_primero, "la segunda pasada cambio algo"
    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS


def test_diez_pasadas_seguidas_son_una_sola(tmp_path):
    ruta = str(tmp_path / "contabilidad.db")
    _archivo_vacio(ruta)
    bd.inicializar_base_de_datos_si_es_necesario(ruta)
    foto = _foto(ruta)

    for _ in range(9):
        bd.inicializar_base_de_datos_si_es_necesario(ruta)

    assert _foto(ruta) == foto
    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS


def test_el_script_manual_tampoco_duplica(tmp_path, monkeypatch, capsys):
    """
    `inicializar_db()` sigue siendo el punto de entrada de `python database.py`, y
    delega en la misma funcion comun: por eso tampoco puede duplicar nada.
    """
    ruta = str(tmp_path / "contabilidad.db")
    monkeypatch.setattr(bd, "DB_PATH", ruta)

    bd.inicializar_db()
    primera = capsys.readouterr().out
    bd.inicializar_db()
    segunda = capsys.readouterr().out

    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS
    assert "inicializada correctamente" in primera
    assert "Catálogo PCGE cargado con éxito (70 cuentas nuevas" in primera
    # La segunda pasada informa que no hizo nada. No puede volver a decir que
    # inicializo, porque seria mentir sobre lo que ocurrio.
    assert "inicializada correctamente" not in segunda
    assert f"Estado: {bd.OK} | Accion: {bd.SIN_CAMBIOS}" in segunda
    assert "Tablas creadas" not in segunda


def test_el_script_manual_conserva_su_nombre_y_su_utilidad():
    """El contrato del script manual no se toca: se sigue pudiendo ejecutar a mano."""
    assert callable(bd.inicializar_db)
    fuente = inspect.getsource(bd.inicializar_db)
    assert "inicializar_base_de_datos_si_es_necesario" in fuente, "debe delegar"
    assert 'if __name__ == "__main__":' in inspect.getsource(bd)
    assert "inicializar_db()" in inspect.getsource(bd).split('__main__')[1]


# =========================================================================== #
# CASO I: LA RUTA
# =========================================================================== #
def test_db_path_esta_anclada_al_proyecto_y_es_un_str():
    """Que sea un `str` lo exigen las pruebas, que parchean `logica.DB_PATH` con cadenas."""
    assert isinstance(bd.DB_PATH, str)
    assert os.path.isabs(bd.DB_PATH)
    assert os.path.samefile(os.path.dirname(bd.DB_PATH), RAIZ / "datos")


def test_logica_y_database_apuntan_a_la_misma_base():
    """
    `logica.py` hace `from database import DB_PATH`, asi que recibe la misma cadena.
    Si divergieran, un script inicializaria una base y la aplicacion leeria otra.
    """
    assert lg.DB_PATH == bd.DB_PATH
    assert os.path.samefile(os.path.dirname(lg.DB_PATH), os.path.dirname(bd.DB_PATH))


def test_la_inicializacion_escribe_en_la_ruta_que_recibe(tmp_path, monkeypatch):
    """
    La trampa de la arquitectura: `logica.DB_PATH` y `database.DB_PATH` son dos
    nombres distintos con el mismo valor, y las pruebas sustituyen SOLO el primero.
    Si la inicializacion leyera la constante de modulo, escribiria en la base real.
    """
    destino = str(tmp_path / "a_where_i_was_told_to.db")
    otra = str(tmp_path / "not_this_one.db")

    resultado = bd.inicializar_base_de_datos_si_es_necesario(destino)

    assert resultado["ruta"] == destino
    assert os.path.exists(destino)
    assert not os.path.exists(otra)

    monkeypatch.setattr(lg, "DB_PATH", destino)
    lg.obtener_cuentas_con_movimiento()  # la aplicacion lee de la misma ruta
    assert not os.path.exists(otra), "la aplicacion leyo otra base"


def test_la_ruta_funciona_desde_otro_directorio_de_trabajo(tmp_path):
    """
    Con la ruta relativa de antes, `sqlite3.connect('datos/contabilidad.db')` se
    resolvia contra el CWD: desde otro directorio fallaba con 'unable to open
    database file'. Ahora la ruta no depende del CWD, y el directorio que la aloja se
    crea si falta.
    """
    anterior = os.getcwd()
    os.chdir(tmp_path)  # `import database` ya ocurrio: DB_PATH ya esta resuelto
    try:
        assert os.getcwd() != str(RAIZ)
        ruta = str(tmp_path / "desde_otro_cwd" / "contabilidad.db")
        resultado = bd.inicializar_base_de_datos_si_es_necesario(ruta)
        assert resultado["accion"] == bd.INICIALIZADA
        assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS
    finally:
        os.chdir(anterior)


def test_se_crea_el_directorio_de_la_base_si_no_existe(tmp_path):
    """`sqlite3.connect()` no crea directorios: sin esto, una ruta nueva fallaba."""
    ruta = tmp_path / "carpeta" / "que" / "no" / "existe" / "contabilidad.db"
    assert not ruta.parent.exists()

    resultado = bd.inicializar_base_de_datos_si_es_necesario(str(ruta))

    assert resultado["accion"] == bd.INICIALIZADA
    assert _contar(str(ruta), "Cuentas") == CUENTAS_ESPERADAS


# =========================================================================== #
# El diagnostico por si mismo
# =========================================================================== #
@pytest.mark.parametrize("estado_esperado, preparar", [
    (bd.NO_EXISTE, lambda ruta: None),
    (bd.VACIA, lambda ruta: _archivo_vacio(ruta)),
    (bd.VACIA, lambda ruta: _crear_esquema(ruta)),
    (bd.OK, lambda ruta: bd.inicializar_base_de_datos_si_es_necesario(ruta)),
])
def test_el_diagnostico_clasifica_cada_estado(tmp_path, estado_esperado, preparar):
    ruta = tmp_path / "contabilidad.db"
    preparar(str(ruta))

    diagnostico = bd.diagnosticar_base_de_datos(str(ruta))

    assert diagnostico["estado"] == estado_esperado
    assert diagnostico["ruta"] == str(ruta)
    assert diagnostico["detalle"], "todo estado se explica al usuario"


def test_el_diagnostico_no_crea_un_archivo_inexistente(tmp_path):
    """Comprobar el estado no puede ser lo que crea el archivo: lo crea la inicializacion."""
    ruta = tmp_path / "no_tocada.db"

    diagnostico = bd.diagnosticar_base_de_datos(str(ruta))

    assert diagnostico["estado"] == bd.NO_EXISTE
    assert diagnostico["existe"] is False
    assert not ruta.exists(), "el diagnostico creo el archivo"


def test_un_archivo_que_no_es_sqlite_se_informa_sin_tumbar_la_aplicacion(tmp_path):
    ruta = tmp_path / "esto_no_es_una_base.db"
    ruta.write_bytes(b"esto no es una base de datos, es texto plano\n" * 20)

    resultado = bd.inicializar_base_de_datos_si_es_necesario(str(ruta))

    assert resultado["estado"] == bd.ERROR_ACCESO
    assert resultado["accion"] == bd.SIN_CAMBIOS
    assert resultado["detalle"]
    assert ruta.read_bytes().startswith(b"esto no es una base")


# =========================================================================== #
# Lo que la funcion NO puede hacer
# =========================================================================== #
def _sql_ejecutado_de(modulo):
    """
    Todas las sentencias SQL que el modulo entrega a `execute`, `executemany` o
    `executescript`, tal cual, leidas del arbol sintactico.

    Se extraen del ARBOL y no del texto: los comentarios y los docstrings de
    `database.py` nombran `DROP TABLE` y `UPDATE` para explicar por que no se usan, y
    una busqueda en el fuente las contaria. Lo que se examina es lo que de verdad se
    manda a SQLite, que es lo unico que puede modificar la base.
    """
    arbol = ast.parse(inspect.getsource(modulo))
    sentencias = []

    def texto(nodo):
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            return nodo.value
        if isinstance(nodo, ast.JoinedStr):  # f"SELECT COUNT(*) FROM {tabla}"
            return "".join(
                parte.value for parte in nodo.values
                if isinstance(parte, ast.Constant) and isinstance(parte.value, str)
            )
        return None

    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        funcion = nodo.func
        if not (isinstance(funcion, ast.Attribute)
                and funcion.attr in ("execute", "executemany", "executescript")):
            continue
        for argumento in nodo.args[:1]:
            contenido = texto(argumento)
            if contenido:
                sentencias.append(contenido)
    # Las sentencias del esquema se pasan como tupla, no como argumento de execute.
    for constante in ast.walk(arbol):
        if isinstance(constante, ast.Constant) and isinstance(constante.value, str):
            if constante.value.lstrip().upper().startswith("CREATE TABLE"):
                sentencias.append(constante.value)
    return sentencias


def test_el_modulo_no_declara_sentencias_destructivas():
    """
    El invariante del que depende todo lo demas, comprobado sobre las sentencias que se
    ejecutan y no sobre el texto del archivo. Lo unico que puede escribir son tres
    `CREATE TABLE IF NOT EXISTS` y un `INSERT ... ON CONFLICT DO NOTHING`.
    """
    sentencias = _sql_ejecutado_de(bd)
    assert sentencias, "no se encontro ninguna sentencia SQL en database.py"

    prohibido = ("DROP", "DELETE", "UPDATE", "ALTER", "REPLACE", "TRUNCATE", "ATTACH")
    for sentencia in sentencias:
        planificada = sentencia.upper()
        for palabra in prohibido:
            # `ON CONFLICT DO NOTHING` no es REPLACE: se busca la palabra como tal.
            assert not any(
                parte.strip() == palabra for parte in planificada.replace(",", " ").split()
            ), f"database.py ejecuta una sentencia con {palabra}: {sentencia[:80]}"

    # Y las cuatro sentencias de escritura que si se permiten, ni una mas.
    de_escritura = [s for s in sentencias if s.lstrip().upper().startswith(
        ("CREATE", "INSERT", "UPDATE", "DELETE", "DROP", "REPLACE", "ALTER"))]
    assert len(de_escritura) == 4, [s[:40] for s in de_escritura]
    assert sum(1 for s in de_escritura if s.lstrip().upper().startswith("CREATE")) == 3
    assert sum(1 for s in de_escritura if s.lstrip().upper().startswith("INSERT")) == 1


def test_la_semilla_usa_on_conflict_do_nothing():
    """
    `ON CONFLICT DO NOTHING` es lo que da idempotencia real y lo que impide
    sobrescribir el catalogo editado por el usuario. Sin el, la guarda `COUNT(*)`
    vuelve a dejar bases con una sola cuenta.
    """
    sentencias = _sql_ejecutado_de(bd)
    semillas = [s for s in sentencias if s.lstrip().upper().startswith("INSERT")]
    assert len(semillas) == 1
    assert "ON CONFLICT(CODIGO) DO NOTHING" in semillas[0].upper()


def test_la_semilla_no_sobrescribe_una_cuenta_existente(tmp_path):
    """Prueba de ejecucion de la sentencia anterior, con una cuenta ya presente."""
    ruta = str(tmp_path / "contabilidad.db")
    _crear_esquema_con_cuentas(ruta, [("70", "Ventas", 7)])

    bd.inicializar_base_de_datos_si_es_necesario(ruta)

    fila = next(f for f in _cuentas(ruta) if f[0] == "70")
    assert fila == ("70", "Ventas", 7)
    assert _contar(ruta, "Cuentas") == CUENTAS_ESPERADAS


def test_el_catalogo_pcge_no_ha_cambiado():
    """Las 70 cuentas y sus elementos: de esto depende la naturaleza contable."""
    assert len(bd.CATALOGO_PCGE) == CUENTAS_ESPERADAS
    assert len({fila[0] for fila in bd.CATALOGO_PCGE}) == CUENTAS_ESPERADAS
    elementos = {fila[2] for fila in bd.CATALOGO_PCGE}
    assert elementos == {1, 2, 3, 4, 5, 6, 7, 8, 9}
    # Una cuenta clave de cada elemento, para que un descuido en el elemento no pase.
    por_codigo = {fila[0]: fila[2] for fila in bd.CATALOGO_PCGE}
    assert por_codigo["10"] == 1    # efectivo
    assert por_codigo["20"] == 2    # mercaderias
    assert por_codigo["39"] == 3    # depreciacion acumulada
    assert por_codigo["50"] == 5    # capital
    assert por_codigo["63"] == 6    # servicios de terceros
    assert por_codigo["70"] == 7    # ventas
    assert por_codigo["88"] == 8    # impuesto a la renta
    assert por_codigo["94"] == 9    # gastos administrativos


def test_el_esquema_declara_las_relaciones_de_integridad():
    """`CONTEXTO.md` exige Foreign Keys entre Asientos y Detalles: siguen declaradas."""
    fuente = " ".join(inspect.getsource(bd).split()).upper()
    assert "FOREIGN KEY (ASIENTO_ID) REFERENCES ASIENTOS(ID)" in fuente
    assert "FOREIGN KEY (CUENTA_CODIGO) REFERENCES CUENTAS(CODIGO)" in fuente
    assert "PRAGMA FOREIGN_KEYS = ON" in fuente