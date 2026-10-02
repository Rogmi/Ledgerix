import sqlite3
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
        elemento = row['elemento']
        debe = row['total_debe']
        haber = row['total_haber']
        
        # Elementos 1, 2, 3, 6, 9: Naturaleza Deudora (Saldo = Debe - Haber)
        if elemento in [1, 2, 3, 6, 9]:
            saldo = debe - haber
        # Elementos 4, 5, 7: Naturaleza Acreedora (Saldo = Haber - Debe)
        elif elemento in [4, 5, 7]:
            saldo = haber - debe
        # Elemento 8: Puede variar, pero lo tratamos como acreedor por defecto para resultados
        else:
            saldo = haber - debe 
            
        saldos.append(saldo)

    # Agregamos la columna de saldo calculado al DataFrame
    df['saldo'] = saldos
    return df