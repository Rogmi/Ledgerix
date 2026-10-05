import sqlite3
from datetime import date

import pandas as pd
from database import DB_PATH  # Importamos la ruta de la base de datos de nuestro Fase 1

def obtener_conexion():
    """Crea y retorna una conexión a la base de datos SQLite."""
    conn = sqlite3.connect(DB_PATH)
    # Habilitamos las Foreign Keys en cada conexión
    conn.execute("PRAGMA foreign_keys = ON;") 
    return conn

def registrar_asiento_completo(fecha, glosa, detalles_asiento):
    """
    Registra un asiento y sus detalles de forma transaccional.
    detalles_asiento debe ser una lista de diccionarios:
    [{'cuenta': '10', 'debe': 1000.0, 'haber': 0.0}, ...]
    """
    # 1. EL GUARDIÁN DE LA PARTIDA DOBLE
    # Sumamos todo el debe y todo el haber
    total_debe = round(sum(detalle['debe'] for detalle in detalles_asiento), 2)
    total_haber = round(sum(detalle['haber'] for detalle in detalles_asiento), 2)

    # Si no cuadran exactamente, abortamos antes de tocar la base de datos
    if total_debe != total_haber:
        raise ValueError(f"Error de Partida Doble: El Debe ({total_debe}) no cuadra con el Haber ({total_haber}).")

    # 2. EL REGISTRADOR (Transacción Atómica)
    conn = obtener_conexion()
    cursor = conn.cursor()

    try:
        # Iniciamos la transacción. Si algo falla, el 'rollback' deshará todo.
        # Insertamos la cabecera en la tabla Asientos
        cursor.execute(
            "INSERT INTO Asientos (fecha, glosa) VALUES (?, ?)", 
            (fecha, glosa)
        )
        asiento_id = cursor.lastrowid # Obtenemos el ID del asiento recién creado

        # Preparamos los datos para la tabla Detalles
        detalles_para_insertar = []
        for det in detalles_asiento:
            detalles_para_insertar.append((
                asiento_id, 
                det['cuenta'], 
                det['debe'], 
                det['haber']
            ))

        # Insertamos todos los detalles de golpe
        cursor.executemany(
            "INSERT INTO Detalles (asiento_id, cuenta_codigo, debe, haber) VALUES (?, ?, ?, ?)",
            detalles_para_insertar
        )

        # Si llegamos aquí sin errores, guardamos los cambios definitivamente
        conn.commit()
        return True, "Asiento registrado correctamente."

    except sqlite3.IntegrityError as e:
        conn.rollback()
        # Esto saltará si intentas usar una cuenta que no existe en la tabla Cuentas
        raise ValueError(f"Error de Integridad (¿Cuenta incorrecta?): {e}")
    except Exception as e:
        conn.rollback()
        raise Exception(f"Error inesperado al registrar: {e}")
    finally:
        conn.close()

def obtener_saldos_cuentas():
    """
    Calcula los saldos reales de todas las cuentas con movimientos,
    respetando la naturaleza contable según el elemento del PCGE.
    """
    conn = obtener_conexion()
    
    # Esta consulta suma los débitos y créditos por cada cuenta
    query = """
    SELECT 
        c.codigo, 
        c.descripcion, 
        c.elemento,
        SUM(d.debe) as total_debe,
        SUM(d.haber) as total_haber
    FROM 
        Cuentas c
    JOIN 
        Detalles d ON c.codigo = d.cuenta_codigo
    GROUP BY 
        c.codigo, c.descripcion, c.elemento
    """
    
    # Usamos pandas para procesar los datos más fácilmente
    df = pd.read_sql_query(query, conn)
    conn.close()

    # Si no hay movimientos, devolvemos un DataFrame vacío
    if df.empty:
        return df

    # 3. EL CALCULADOR DE SALDOS
    # Aquí aplicamos la lógica matemática del PCGE usando el 'elemento'
    saldos = []
    for index, row in df.iterrows():
        debe = row['total_debe']
        haber = row['total_haber']

        # La naturaleza la decide UNA sola regla compartida (`naturaleza_de_elemento`),
        # la misma que usa el Libro Mayor. Antes esta decisión vivía solo aquí y el
        # Libro Mayor la reimplementaba por su cuenta: dos reglas, dos motores.
        if naturaleza_de_elemento(row['elemento']) == "deudora":
            saldo = debe - haber
        else:
            saldo = haber - debe

        saldos.append(saldo)

    # Agregamos la columna de saldo calculado al DataFrame
    df['saldo'] = saldos
    return df


# ============================================================================
# NATURALEZ CONTABLE: UNA SOLA REGLA PARA TODO EL SISTEMA
# ============================================================================
# El Libro Mayor y los Estados Financieros no pueden discrepar sobre si una cuenta
# es deudora o acreedora. Por eso la regla vive aqui, en un solo lugar, y las dos
# consultas la consumen: los Estados Financieros en Python, el Libro Mayor a traves
# de la funcion SQL `pcge_signo` que se registra en la conexion.


def naturaleza_de_elemento(elemento):
    """
    Regla ÚNICA de naturaleza contable del sistema.

    Deudora para los elementos 1, 2, 3, 6 y 9. Acreedora para todos los demás,
    y el "todos los demás" es deliberado: incluye el 4, el 5, el 7 y también el 8,
    que desde el inicio se trató como acreedor. Un elemento desconocido no se
    inventa: cae en acreedora, que es lo que hacía el cálculo anterior.
    """
    return "deudora" if elemento in (1, 2, 3, 6, 9) else "acreedora"


def signo_de_elemento(elemento):
    """
    La misma regla de `naturaleza_de_elemento`, en forma numérica y sin repetir la
    lista de elementos: +1 si la cuenta es deudora, -1 si es acreedora.

    Multiplicar (Debe - Haber) por este signo da el saldo con signo contable.
    """
    return 1 if naturaleza_de_elemento(elemento) == "deudora" else -1


# ============================================================================
# LIBROS CONTABLES (SOLO LECTURA)
# ============================================================================
# El Libro Diario y el Libro Mayor no son un motor contable: son VISTAS de lo que
# ya está registrado. Por lo tanto, en toda esta sección:
#
#   - solo hay SELECT; ninguna función escribe, actualiza ni borra;
#   - ninguna recalcula, corrige, completa ni deduplica un asiento;
#   - ninguna inventa fechas: si el asiento tiene la fecha vacía, el libro la
#     muestra vacía, igual que la grabó el usuario;
#   - la única puerta de escritura del sistema sigue siendo
#     `registrar_asiento_completo`, y sigue exigiendo la aprobación humana del
#     borrador (ver `render_borrador` en app.py).
#
# Si un asiento está descuadrado, el libro lo muestra descuadrado: ocultarlo sería
# mentir sobre la contabilidad ya registrada.

# `lower()` de SQLite solo pliega ASCII; el de Python también pliega acentos y eñes.
# La búsqueda en glosa usa este criterio: sin distinción de mayúsculas.
def _minusculas(texto):
    return "" if texto is None else str(texto).lower()


# Carácter de escape del patrón LIKE del filtro de cuenta.
_LIKE_ESCAPE = "\\"


def _prefijo_like(texto):
    """
    Convierte el texto del filtro de cuenta en un patrón LIKE seguro.

    Sin esto, escribir `10%` en el filtro capturaría todas las cuentas que empiezan
    por 10 y además cualquier otra: los comodines de LIKE deben ser texto, no
    comodines, porque el usuario está eligiendo un prefijo de cuenta, no un patrón.
    """
    escapado = (str(texto)
                .replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
                .replace("%", _LIKE_ESCAPE + "%")
                .replace("_", _LIKE_ESCAPE + "_"))
    return escapado + "%"


def _preparar_lectura(conn):
    """
    Registra en la conexión las dos funciones que las consultas de libros necesitan.

    Se hace aquí y no en `obtener_conexion()` a propósito: la ruta de escritura no
    tiene por qué cargar funciones de consulta, y `registrar_asiento_completo()`
    queda intacta.
    """
    conn.create_function("lower_es", 1, _minusculas, deterministic=True)
    conn.create_function("pcge_signo", 1, signo_de_elemento, deterministic=True)


def _texto_fecha(valor):
    """Normaliza un `date` a ISO, que es como SQLite almacena la columna `fecha`."""
    return valor.isoformat() if hasattr(valor, "isoformat") else str(valor)


def obtener_rango_fechas_asientos():
    """
    Primera y última fecha realmente registrada. Devuelve (None, None) si todavía
    no hay asientos: la interfaz usa ese dato para poner el rango por defecto y para
    no mostrar filtros sobre una base vacía. No recurre a la fecha de hoy.
    """
    conn = obtener_conexion()
    try:
        fila = conn.execute("SELECT MIN(date(fecha)), MAX(date(fecha)) FROM Asientos").fetchone()
    finally:
        conn.close()
    if fila is None or fila[0] is None:
        return None, None
    return date.fromisoformat(fila[0]), date.fromisoformat(fila[1])


def obtener_cuentas_con_movimiento():
    """
    Cuentas del catálogo PCGE que tienen al menos un movimiento registrado, con su
    naturaleza tomada de `naturaleza_de_elemento`. Es la lista que ofrece el Libro
    Mayor para elegir una cuenta: no se ofrece una cuenta sin movimiento porque no
    tendría saldo que mostrar, ni se inventa ninguna que no esté en el catálogo.
    """
    conn = obtener_conexion()
    try:
        _preparar_lectura(conn)
        df = pd.read_sql_query("""
            SELECT c.codigo, c.descripcion, c.elemento
            FROM Cuentas c
            WHERE EXISTS (SELECT 1 FROM Detalles d WHERE d.cuenta_codigo = c.codigo)
            ORDER BY c.codigo
        """, conn)
    finally:
        conn.close()
    if df.empty:
        return df
    df["naturaleza"] = [naturaleza_de_elemento(elemento) for elemento in df["elemento"]]
    return df


def obtener_libro_diario(fecha_desde=None, fecha_hasta=None, cuenta=None, glosa=None):
    """
    Libro Diario de SOLO LECTURA: una fila por partida contable ya registrada, en
    orden cronológico.

    El número de asiento es `Asientos.id`, el correlativo real de la base de datos:
    no se inventa una numeración de muestra. La denominación de la cuenta sale de
    `Cuentas.descripcion`, nunca de un texto fijo en la interfaz.

    Devuelve un DataFrame con las columnas:
        numero_asiento, fecha, glosa, cuenta, denominacion, debe, haber

    Filtros (los mismos que la interfaz ya ofrecía, ahora sobre datos reales):
        fecha_desde / fecha_hasta : extremos inclusivos del periodo
        cuenta  : prefijo del código; '10' incluye '10' y cualquier código que empiece por 10
        glosa   : subcadena, sin distinción de mayúsculas
    """
    condiciones, parametros = ["1 = 1"], []

    if fecha_desde is not None:
        condiciones.append("date(a.fecha) >= ?")
        parametros.append(_texto_fecha(fecha_desde))
    if fecha_hasta is not None:
        condiciones.append("date(a.fecha) <= ?")
        parametros.append(_texto_fecha(fecha_hasta))
    if cuenta:
        condiciones.append(f"d.cuenta_codigo LIKE ? ESCAPE '{_LIKE_ESCAPE}'")
        parametros.append(_prefijo_like(cuenta))
    if glosa:
        condiciones.append("instr(lower_es(a.glosa), lower_es(?)) > 0")
        parametros.append(glosa)

    conn = obtener_conexion()
    try:
        _preparar_lectura(conn)
        df = pd.read_sql_query(f"""
            SELECT
                a.id            AS numero_asiento,
                a.fecha         AS fecha,
                a.glosa         AS glosa,
                d.cuenta_codigo AS cuenta,
                c.descripcion   AS denominacion,
                d.debe          AS debe,
                d.haber         AS haber
            FROM Asientos a
            JOIN Detalles d ON d.asiento_id = a.id
            JOIN Cuentas  c ON c.codigo = d.cuenta_codigo
            WHERE {' AND '.join(condiciones)}
            ORDER BY date(a.fecha), a.id, d.id
        """, conn, params=parametros)
    finally:
        conn.close()
    return df


def obtener_libro_mayor(fecha_desde=None, fecha_hasta=None, cuenta=None):
    """
    Libro Mayor de SOLO LECTURA: los movimientos reales, agrupados por cuenta y con
    el saldo acumulado de esa misma cuenta.

    El saldo se calcula en SQL con una función de ventana particionada por cuenta,
    sobre la fila anterior de ESA cuenta, y con el signo de `pcge_signo`, que es
    `signo_de_elemento`, que es `naturaleza_de_elemento`: la misma regla que usa
    `obtener_saldos_cuentas()`. No hay una segunda regla de naturaleza en el sistema.

    Con `cuenta=None` el resultado trae todas las cuentas, cada una con SU saldo
    acumulado running. No existe un saldo único para la mezcla: eso lo decide la
    interfaz, que solo muestra un saldo final cuando hay una cuenta elegida.

    Devuelve un DataFrame con las columnas:
        numero_asiento, fecha, cuenta, denominacion, glosa, debe, haber, elemento, saldo
    """
    condiciones, parametros = ["1 = 1"], []

    if fecha_desde is not None:
        condiciones.append("date(a.fecha) >= ?")
        parametros.append(_texto_fecha(fecha_desde))
    if fecha_hasta is not None:
        condiciones.append("date(a.fecha) <= ?")
        parametros.append(_texto_fecha(fecha_hasta))
    if cuenta:
        condiciones.append("d.cuenta_codigo = ?")
        parametros.append(cuenta)

    conn = obtener_conexion()
    try:
        _preparar_lectura(conn)
        df = pd.read_sql_query(f"""
            SELECT
                a.id            AS numero_asiento,
                a.fecha         AS fecha,
                d.cuenta_codigo AS cuenta,
                c.descripcion   AS denominacion,
                a.glosa         AS glosa,
                d.debe          AS debe,
                d.haber         AS haber,
                c.elemento      AS elemento,
                ROUND(SUM((d.debe - d.haber) * pcge_signo(c.elemento)) OVER (
                    PARTITION BY d.cuenta_codigo
                    ORDER BY date(a.fecha), a.id, d.id
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ), 2) AS saldo
            FROM Asientos a
            JOIN Detalles d ON d.asiento_id = a.id
            JOIN Cuentas  c ON c.codigo = d.cuenta_codigo
            WHERE {' AND '.join(condiciones)}
            ORDER BY d.cuenta_codigo, date(a.fecha), a.id, d.id
        """, conn, params=parametros)
    finally:
        conn.close()
    return df