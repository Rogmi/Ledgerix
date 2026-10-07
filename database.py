"""
ESQUEMA, CATALOGO PCGE E INICIALIZACION DE LA BASE DE DATOS.

Este modulo es la fuente UNICA de la estructura del sistema. `logica.py` no crea
tablas ni siembra datos: lee y escribe sobre lo que aqui se define. `app.py` no
toca el esquema: solo pide que la base este lista al arrancar.

La instalacion limpia se sostiene con un unico requisito:

    streamlit run app.py

Ancla a la aplicacion se llama `inicializar_base_de_datos_si_es_necesario()`.
Esta funcion NO destruye datos. Su garantia, en una linea, es: completa lo que
falta y jamás reescribe lo que ya existe. Concretamente, este archivo no contiene
ni un `DROP`, ni un `DELETE`, ni un `UPDATE`, ni un `ALTER`, ni un
`REPLACE INTO`, ni un `TRUNCATE`; las tres unicas sentencias que escriben son un
`CREATE TABLE IF NOT EXISTS` y un `INSERT ... ON CONFLICT DO NOTHING`.
"""

import os
import sqlite3
from pathlib import Path

# --------------------------------------------------------------------------- #
# 1. RUTA
# --------------------------------------------------------------------------- #
# Anclada a ESTE archivo, no al directorio de trabajo. Con la ruta relativa que
# hubo antes, `sqlite3.connect('datos/contabilidad.db')` se resolvia contra el CWD
# del proceso: desde la raiz del proyecto creaba el archivo y desde cualquier otro
# directorio fallaba con 'unable to open database file'. Ademas, si el directorio
# no existia, SQLite noaba nada: por eso `datos/` tiene que existir antes de
# conectar, y eso lo hace `os.makedirs` mas abajo.
#
# Sigue siendo un `str` a proposito: las pruebas parchean `logica.DB_PATH` con
# cadenas y este valor se exporta tal cual a `logica.py`.
RAIZ = Path(__file__).resolve().parent
DB_PATH = str(RAIZ / "datos" / "contabilidad.db")

# Las tres tablas estructurales del sistema. Este es el contrato que se diagnostica:
# si las tres existen, la base esta bien formada aunque le falten catalogos.
TABLAS_ESTRUCTURALES = ("Cuentas", "Asientos", "Detalles")


# --------------------------------------------------------------------------- #
# 2. ESQUEMA
# --------------------------------------------------------------------------- #
# Tuplas, no un unico bloque de texto: `executescript()` mete un COMMIT implicito
# antes de correr el script y habria roto el control explicito de la transaccion que
# hace la inicializacion. Ademas asi se puede informar QUE tabla se creo.
#
# El DDL es identico al que se usaba antes, carcter por carcter: mismos nombres,
# mismos tipos, mismos PRIMARY KEY, mismos AUTOINCREMENT y mismas Foreign Keys.
# Los indices que aparecen al abrir una base ya inicializada (`sqlite_autoindex_*`)
# los crea SQLite por el PRIMARY KEY, no hay CREATE INDEX en este archivo.
ESQUEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS Cuentas (
        codigo TEXT PRIMARY KEY,
        descripcion TEXT NOT NULL,
        elemento INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Asientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha DATE NOT NULL,
        glosa TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Detalles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asiento_id INTEGER NOT NULL,
        cuenta_codigo TEXT NOT NULL,
        debe REAL DEFAULT 0.0,
        haber REAL DEFAULT 0.0,
        FOREIGN KEY (asiento_id) REFERENCES Asientos(id),
        FOREIGN KEY (cuenta_codigo) REFERENCES Cuentas(codigo)
    )
    """,
)


# --------------------------------------------------------------------------- #
# 3. CATALOGO PCGE
# --------------------------------------------------------------------------- #
# Las 70 cuentas de dos digitos del PCGE, con su elemento. Este dato estaba dentro
# del cuerpo de `inicializar_db()`, donde no se podia leer ni probar; aqui es una
# constante de modulo, y el contenido es exactamente el mismo: mismos codigos, misma
# descripcion (incluidos los guiones U+2013) y mismo elemento.
#
# El campo `elemento` es lo que despues leen `logica.naturaleza_de_elemento()` y las
# vistas de la aplicacion, asi que no se toca: cambiar un elemento cambiaria la
# naturaleza contable de una cuenta y los Estados Financieros.
CATALOGO_PCGE = (
    # Elemento 1: Activo Disponible y Exigible
    ("10", "Efectivo y equivalentes de efectivo", 1),
    ("11", "Inversiones financieras", 1),
    ("12", "Cuentas por cobrar comerciales – Terceros", 1),
    ("13", "Cuentas por cobrar comerciales – Relacionadas", 1),
    ("14", "Cuentas por cobrar al personal, a los accionistas y directores", 1),
    ("16", "Cuentas por cobrar diversas – Terceros", 1),
    ("17", "Cuentas por cobrar diversas – Relacionadas", 1),
    ("18", "Servicios y otros contratados por anticipado", 1),
    ("19", "Estimación de cuentas de cobranza dudosa", 1),

    # Elemento 2: Activo Realizable
    ("20", "Mercaderías", 2),
    ("21", "Productos terminados", 2),
    ("22", "Subproductos, desechos y desperdicios", 2),
    ("23", "Productos en proceso", 2),
    ("24", "Materias primas", 2),
    ("25", "Materiales auxiliares, suministros y repuestos", 2),
    ("26", "Envases y embalajes", 2),
    ("27", "Activos no corrientes mantenidos para la venta", 2),
    ("28", "Inventarios por recibir", 2),
    ("29", "Desvalorización de inventarios", 2),

    # Elemento 3: Activo Inmovilizado
    ("30", "Inversiones mobiliarias", 3),
    ("31", "Propiedades de inversión", 3),
    ("32", "Activos por derecho de uso", 3),
    ("33", "Propiedad, planta y equipo", 3),
    ("34", "Intangibles", 3),
    ("35", "Activos biológicos", 3),
    ("36", "Desvalorización de activo inmovilizado", 3),
    ("37", "Activo diferido", 3),
    ("38", "Otros activos", 3),
    ("39", "Depreciación y amortización acumulada", 3),

    # Elemento 4: Pasivo
    ("40", "Tributos, contraprestaciones y aportes al sistema público", 4),
    ("41", "Remuneraciones y participaciones por pagar", 4),
    ("42", "Cuentas por pagar comerciales – Terceros", 4),
    ("43", "Cuentas por pagar comerciales – Relacionadas", 4),
    ("44", "Cuentas por pagar a los accionistas, directores y gerentes", 4),
    ("45", "Obligaciones financieras", 4),
    ("46", "Cuentas por pagar diversas – Terceros", 4),
    ("47", "Cuentas por pagar diversas – Relacionadas", 4),
    ("48", "Provisiones", 4),
    ("49", "Pasivo diferido", 4),

    # Elemento 5: Patrimonio Neto
    ("50", "Capital", 5),
    ("51", "Acciones de inversión", 5),
    ("52", "Capital adicional", 5),
    ("56", "Resultados no realizados", 5),
    ("57", "Excedente de revaluación", 5),
    ("58", "Reservas", 5),
    ("59", "Resultados acumulados", 5),

    # Elemento 6: Gastos por Naturaleza
    ("60", "Compras", 6),
    ("61", "Variación de inventarios", 6),
    ("62", "Gastos de personal y directores", 6),
    ("63", "Gastos de servicios prestados por terceros", 6),
    ("64", "Gastos por tributos", 6),
    ("65", "Otros gastos de gestión", 6),
    ("66", "Pérdida por medición de activos no financieros", 6),
    ("67", "Gastos financieros", 6),
    ("68", "Valuación y deterioro de activos y provisiones", 6),
    ("69", "Costo de ventas", 6),

    # Elemento 7: Ingresos
    ("70", "Ventas", 7),
    ("71", "Variación de la producción almacenada", 7),
    ("72", "Producción de activo inmovilizado", 7),
    ("73", "Descuentos, rebajas y bonificaciones obtenidos", 7),
    ("74", "Descuentos, rebajas y bonificaciones concedidos", 7),
    ("75", "Otros ingresos de gestión", 7),
    ("76", "Ganancia por medición de activos no financieros", 7),
    ("77", "Ingresos financieros", 7),
    ("78", "Cargas cubiertas por provisiones", 7),
    ("79", "Cargas imputables a cuentas de costos y gastos", 7),

    # Elemento 8 y 9 (Opcionales para saldos y costos, pero útiles tenerlos)
    ("88", "Impuesto a la renta", 8),
    ("89", "Determinación del resultado del ejercicio", 8),
    ("94", "Gastos administrativos", 9),
    ("95", "Gastos de ventas", 9),
)


# Los codigos del catalogo, en un conjunto: es la pregunta que responde el
# diagnostico para distinguir "base en uso y completa" de "base a medio sembrar".
CODIGOS_DEL_CATALOGO = set(fila[0] for fila in CATALOGO_PCGE)


# --------------------------------------------------------------------------- #
# 4. ESTADOS DEL DIAGNOSTICO
# --------------------------------------------------------------------------- #
# Son cadenas y no una enumeracion porque forman parte del valor de retorno, que la
# interfaz muestra al usuario y que las pruebas comparan.
NO_EXISTE = "NO_EXISTE"                    # la ruta no esta en disco
VACIA = "VACIA"                            # existe pero no tiene estructura utilizable
PARCIAL = "PARCIAL"                        # le faltan una o dos tablas estructurales
CATALOGO_INCOMPLETO = "CATALOGO_INCOMPLETO"  # las tablas estan, falta parte del PCGE
OK = "OK"                                  # las tres tablas y el catalogo estan completos
INCONSISTENTE = "INCONSISTENTE"            # hay movimientos sin catalogo: no se repara solo
ERROR_ACCESO = "ERROR_ACCESO"              # el archivo existe pero no se puede leer

# Acciones, que no son estados: describen lo que se hizo con la base.
SIN_CAMBIOS = "SIN_CAMBIOS"
INICIALIZADA = "INICIALIZADA"
REPARADA = "REPARADA"
CATALOGO_COMPLETADO = "CATALOGO_COMPLETADO"


# --------------------------------------------------------------------------- #
# 5. DIAGNOSTICO: SOLO LECTURA
# --------------------------------------------------------------------------- #
def _tablas_de_usuario(conexion):
    """Nombres de tablas reales de la base, sin las internas de SQLite.

    `sqlite_sequence` sobrevive a un `DROP TABLE` de todo lo que tenga AUTOINCREMENT,
    asi que contarla como "estructura existente" haria que una base vacia se
    diagnosticara como PARCIAL en vez de VACIA.
    """
    filas = conexion.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {fila[0] for fila in filas}


def _contar(conexion, tabla):
    """`COUNT(*)` de una tabla de `TABLAS_ESTRUCTURALES`. Solo se llama si existe."""
    return conexion.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]


def _diagnosticar_conexion(conexion):
    """Estado de una base YA abierta. Solo ejecuta SELECT: no escribe nunca.

    Se separa de `diagnosticar_base_de_datos()` para poder repetirse DENTRO de la
    transaccion de inicializacion, que es lo que neutraliza la carrera entre dos
    sesiones de Streamlit.
    """
    existentes = _tablas_de_usuario(conexion)
    faltantes = [t for t in TABLAS_ESTRUCTURALES if t not in existentes]

    if faltantes:
        # Sin ninguna tabla del sistema es una base recien creada o un archivo de 0
        # bytes: los dos casos se inicializan igual. Con algunas tablas presentes es
        # una base parcial, que se repara y no se rehace.
        estado = VACIA if not existentes else PARCIAL
        return {
            "estado": estado,
            "tablas_existentes": sorted(existentes),
            "tablas_faltantes": faltantes,
            "cuentas": 0,
            "asientos": 0,
            "detalles": 0,
        }

    cuentas = _contar(conexion, "Cuentas")
    asientos = _contar(conexion, "Asientos")
    detalles = _contar(conexion, "Detalles")

    if cuentas == 0 and (asientos or detalles):
        # Movimientos sin catalogo. Sembrar las 70 cuentas aqui no repararia nada:
        # ademas de perder el significado de los `Detalles`, inventaria cuentas que
        # nadie declaro. Se devuelve el estado y que lo decida una persona.
        return {
            "estado": INCONSISTENTE,
            "tablas_existentes": sorted(existentes),
            "tablas_faltantes": [],
            "cuentas": 0,
            "asientos": asientos,
            "detalles": detalles,
            "cuentas_faltantes": sorted(CODIGOS_DEL_CATALOGO),
        }

    if cuentas == 0:
        # Las tres tablas existen pero no hay catalogo y no hay movimientos: es una
        # base recien creada a la que solo le falta la semilla del PCGE.
        return {
            "estado": VACIA,
            "tablas_existentes": sorted(existentes),
            "tablas_faltantes": [],
            "cuentas": 0,
            "asientos": 0,
            "detalles": 0,
            "cuentas_faltantes": sorted(CODIGOS_DEL_CATALOGO),
        }

    # Hay al menos una cuenta. Ahora la pregunta no es si la base esta en uso, que lo
    # esta, sino si el catalogo esta COMPLETO. Esta distincion es la que impide que un
    # `COUNT(*) == 0` a medias deje el sistema con una sola cuenta: con dos cuentas de
    # las 70 hay que sembrar las otras 68.
    presentes = {fila[0] for fila in conexion.execute("SELECT codigo FROM Cuentas")}
    faltantes = sorted(CODIGOS_DEL_CATALOGO - presentes)
    return {
        "estado": CATALOGO_INCOMPLETO if faltantes else OK,
        "tablas_existentes": sorted(existentes),
        "tablas_faltantes": [],
        "cuentas": cuentas,
        "asientos": asientos,
        "detalles": detalles,
        "cuentas_faltantes": faltantes,
    }


def diagnosticar_base_de_datos(ruta=DB_PATH):
    """Estado de la base en `ruta` sin modificarla. Devuelve un diccionario.

    Los estados posibles son `NO_EXISTE`, `VACIA`, `PARCIAL`, `CATALOGO_INCOMPLETO`,
    `OK`, `INCONSISTENTE` y `ERROR_ACCESO`. Esta funcion no crea el archivo: comprueba
    antes la existencia con `os.path.exists()` y, si existe, se limita a abrirlo y
    lanzar SELECT. Por eso un `sqlite3.connect()` normal es aqui suficiente y no hace
    falta abrir la base en modo `ro`, que en Windows obliga a percent-encodear la ruta
    y este proyecto tiene una ruta con espacios y con `GESTIÓN` acentuada.
    """
    ruta = os.fspath(ruta)
    comun = {
        "ruta": ruta,
        "existe": os.path.exists(ruta),
        "estado": NO_EXISTE,
        "tablas_existentes": [],
        "tablas_faltantes": list(TABLAS_ESTRUCTURALES),
        "cuentas": 0,
        "asientos": 0,
        "detalles": 0,
        "cuentas_faltantes": sorted(CODIGOS_DEL_CATALOGO),
        "detalle": "",
    }

    if not comun["existe"]:
        comun["detalle"] = "Todavia no hay archivo de base de datos en esa ruta."
        return comun

    try:
        conexion = sqlite3.connect(ruta)
    except sqlite3.Error as error:
        comun["estado"] = ERROR_ACCESO
        comun["detalle"] = f"No se pudo abrir la base de datos: {error}"
        return comun

    try:
        diagnostico = _diagnosticar_conexion(conexion)
    except sqlite3.DatabaseError as error:
        # Archivo que no es SQLite, o sqlite_master ilegible.
        comun["estado"] = ERROR_ACCESO
        comun["detalle"] = f"El archivo no se pudo leer como base de datos SQLite: {error}"
        return comun
    finally:
        conexion.close()

    comun.update(diagnostico)
    comun["detalle"] = _texto_del_estado(comun["estado"], comun)
    return comun


def _texto_del_estado(estado, diagnostico):
    """Explicacion en castellano del estado, para la interfaz y para el script manual."""
    faltantes = diagnostico.get("tablas_faltantes") or []
    if estado == NO_EXISTE:
        return "La base de datos no existe todavia: se creara con su catalogo PCGE."
    if estado == VACIA:
        if faltantes:
            return "El archivo existe pero esta vacio: faltan las tablas del sistema."
        return ("Las tablas existen pero el catalogo PCGE esta vacio y no hay "
                "movimientos: se sembrara el catalogo.")
    if estado == PARCIAL:
        return ("La base esta inicializada a medias: faltan "
                f"{', '.join(faltantes)}. Se crearan solo esas.")
    if estado == CATALOGO_INCOMPLETO:
        return (f"El catalogo de cuentas esta incompleto: hay {diagnostico['cuentas']} "
                f"cuentas y faltan {len(diagnostico['cuentas_faltantes'])} del PCGE. "
                "Se sembraran solo las que falten, sin tocar las existentes.")
    if estado == OK:
        return ("La base de datos ya esta inicializada "
                f"({diagnostico['cuentas']} cuentas, {diagnostico['asientos']} asientos). "
                "No se modifica nada.")
    if estado == INCONSISTENTE:
        return (f"La base tiene {diagnostico['asientos']} asientos y "
                f"{diagnostico['detalles']} detalles pero su catalogo de cuentas esta "
                "vacio. No se repara de forma automatica porque no se puede saber que "
                "cuentas son las correctas: revisa la base antes de seguir.")
    if estado == ERROR_ACCESO:
        return diagnostico.get("detalle", "No se pudo leer la base de datos.")
    return ""


# --------------------------------------------------------------------------- #
# 6. INICIALIZACION: SOLO CUANDO CORRESPONDE
# --------------------------------------------------------------------------- #
def inicializar_base_de_datos_si_es_necesario(ruta=DB_PATH, *, verbose=False):
    """Deja `ruta` lista para usarse, y no hace nada si ya lo estaba.

    Es la funcion que hace posible una instalacion limpia con `streamlit run app.py`.
    Su garantia es que es idempotente y no destructiva:

      * si la base no existe, la crea con el esquema y siembra el PCGE;
      * si existe vacia o con una o dos tablas de menos, crea solo lo que falta;
      * si le falta parte del catalogo, siembra solo las cuentas ausentes;
      * si ya esta inicializada, no abre ninguna conexion de escritura;
      * si tiene movimientos sin catalogo, devuelve `INCONSISTENTE` y no escribe.

    Recibe la ruta por parametro y no lee `DB_PATH` por dentro, a proposito:
    `logica.DB_PATH` es un nombre propio de `logica.py` (las pruebas lo parchean con
    `monkeypatch.setattr`) y leer una constante de modulo desde aqui escribiria en la
    base real durante la suite.

    Devuelve un diccionario con `estado`, `accion`, `ruta`, `detalle`,
    `tablas_creadas` y `cuentas_insertadas`.
    """
    ruta = os.fspath(ruta)
    diagnostico = diagnosticar_base_de_datos(ruta)
    estado = diagnostico["estado"]

    resultado = {
        "ruta": ruta,
        "estado": estado,
        "accion": SIN_CAMBIOS,
        "detalle": diagnostico["detalle"],
        "tablas_creadas": [],
        "cuentas_insertadas": 0,
        "cuentas": diagnostico["cuentas"],
        "asientos": diagnostico["asientos"],
        "detalles": diagnostico["detalles"],
    }

    if estado in (OK, INCONSISTENTE, ERROR_ACCESO):
        if verbose:
            print(f"[{estado}] {diagnostico['detalle']}")
            print(f"Base de datos: {ruta}")
        return resultado

    # A partir de aqui hay algo que hacer. `datos/` tiene que existir antes de
    # conectar: sqlite3.connect() no crea el directorio y falla con 'unable to open
    # database file' si falta.
    os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)

    conexion = sqlite3.connect(ruta, timeout=15.0, isolation_level=None)
    try:
        # Necesario para la integridad de las Foreign Keys entre Asientos y Detalles.
        conexion.execute("PRAGMA foreign_keys = ON;")

        # BEGIN IMMEDIATE toma el lock de escritura ahora, antes de decidir nada:
        # entre el diagnostico de arriba y este BEGIN otra sesion de Streamlit pudo
        # haber inicializado la base, y con el lock tomado ya no puede escribir.
        conexion.execute("BEGIN IMMEDIATE;")
        try:
            estado_dentro = _diagnosticar_conexion(conexion)
            if estado_dentro["estado"] in (OK, INCONSISTENTE, ERROR_ACCESO):
                # Otra sesion se adelanto. Se descarta el intento y se informa el
                # estado real: es exactamente lo que habria pasado si hubieramos
                # esperado un poco mas.
                conexion.execute("ROLLBACK;")
                resultado["estado"] = estado_dentro["estado"]
                resultado["detalle"] = _texto_del_estado(estado_dentro["estado"], estado_dentro)
                resultado["cuentas"] = estado_dentro["cuentas"]
                resultado["asientos"] = estado_dentro["asientos"]
                resultado["detalles"] = estado_dentro["detalles"]
                if verbose:
                    print(f"[{estado_dentro['estado']}] {resultado['detalle']}")
                return resultado

            existentes = set(estado_dentro["tablas_existentes"])

            # 1. Estructura: solo las tablas que falten. `IF NOT EXISTS` hace que las
            #    que ya estaban no se vuelvan a crear, y su contenido no se mira.
            creadas = []
            for tabla, sentencia in zip(TABLAS_ESTRUCTURALES, ESQUEMA_SQL):
                if tabla not in existentes:
                    conexion.execute(sentencia)
                    creadas.append(tabla)

            # 2. Catalogo PCGE. `ON CONFLICT(codigo) DO NOTHING` es lo que hace esto
            #    idempotente de verdad: la guarda antiga era `COUNT(*) == 0`, que con
            #    una sola cuenta presente daba el catalogo por cargado y dejaba el
            #    sistema con una unica cuenta. Aqui se insertan las 70 filas
            #    individuales y las que ya existen no se tocan.
            #
            #    La segunda garantia de esta sentencia es que nunca sobrescribe: si
            #    alguien edito la descripcion de una cuenta, el conflicto con la clave
            #    primaria descarta la fila y la descripcion editada sobrevive.
            cuentas_antes = _contar(conexion, "Cuentas")
            conexion.executemany(
                "INSERT INTO Cuentas (codigo, descripcion, elemento) VALUES (?, ?, ?) "
                "ON CONFLICT(codigo) DO NOTHING",
                CATALOGO_PCGE,
            )
            cuentas_despues = _contar(conexion, "Cuentas")
            insertadas = cuentas_despues - cuentas_antes

            conexion.execute("COMMIT;")
        except Exception:
            conexion.execute("ROLLBACK;")
            raise
    finally:
        conexion.close()

    # Que se haya inicializado una base nueva, reparado una parcial o completado un
    # catalogo a medias son cosas distintas, y el mensaje tiene que decir la verdad
    # sobre cual de las tres ocurrio.
    if estado == NO_EXISTE or len(creadas) == len(TABLAS_ESTRUCTURALES):
        resultado["accion"] = INICIALIZADA
    elif creadas:
        resultado["accion"] = REPARADA
    elif insertadas:
        resultado["accion"] = CATALOGO_COMPLETADO
    else:
        resultado["accion"] = SIN_CAMBIOS

    resultado["tablas_creadas"] = creadas
    resultado["cuentas_insertadas"] = insertadas
    resultado["cuentas"] = cuentas_despues

    if verbose:
        if creadas:
            print(f"Tablas creadas: {', '.join(creadas)}.")
        print(f"Catálogo PCGE cargado con éxito ({insertadas} cuentas nuevas, "
              f"{cuentas_despues} en total).")
        if resultado["accion"] == INICIALIZADA:
            print(f"Base de datos inicializada correctamente en: {ruta}")

    return resultado


def inicializar_db():
    """Punto de entrada manual: `python database.py`.

    Se conserva el nombre y la utilidad que tenia. Delega en la funcion comun, de
    modo que el script a mano y el arranque de la aplicacion ejecutan exactamente la
    misma logica, y no puede pasar que uno inicialice una base y el otro otra.
    """
    resultado = inicializar_base_de_datos_si_es_necesario(DB_PATH, verbose=True)
    print(f"Estado: {resultado['estado']} | Accion: {resultado['accion']}")
    return resultado


if __name__ == "__main__":
    # Ejecutar este archivo mas de una vez sobre la misma base es seguro: no borra
    # nada y no vuelve a sembrar el PCGE. Para empezar de cero hay que borrar el
    # archivo a mano, y eso sigue siendo una decision de la persona, no del script.
    inicializar_db()