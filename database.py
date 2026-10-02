import sqlite3
import os

# Ruta donde se guardará la base de datos
DB_PATH = 'datos/contabilidad.db'

def inicializar_db():
    # Conectamos a la base de datos (se creará automáticamente si no existe)
    conexion = sqlite3.connect(DB_PATH)
    cursor = conexion.cursor()

    # Activar la restricción de claves foráneas en SQLite (Vital para la integridad)
    cursor.execute("PRAGMA foreign_keys = ON;")

    # 1. Tabla Cuentas (El catálogo estandarizado del PCGE)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Cuentas (
            codigo TEXT PRIMARY KEY,
            descripcion TEXT NOT NULL,
            elemento INTEGER NOT NULL
        )
    ''')

    # 2. Tabla Asientos (La cabecera de la transacción)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Asientos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha DATE NOT NULL,
            glosa TEXT NOT NULL
        )
    ''')

    # 3. Tabla Detalles (El cuerpo que garantiza la partida doble)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS Detalles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asiento_id INTEGER NOT NULL,
            cuenta_codigo TEXT NOT NULL,
            debe REAL DEFAULT 0.0,
            haber REAL DEFAULT 0.0,
            FOREIGN KEY (asiento_id) REFERENCES Asientos(id),
            FOREIGN KEY (cuenta_codigo) REFERENCES Cuentas(codigo)
        )
    ''')

    # Inyectar las cuentas principales del PCGE peruano si la tabla está vacía
    cursor.execute("SELECT COUNT(*) FROM Cuentas")
    if cursor.fetchone()[0] == 0:
        cuentas_pcge = [
            # Elemento 1: Activo Disponible y Exigible
            ('10', 'Efectivo y equivalentes de efectivo', 1),
            ('11', 'Inversiones financieras', 1),
            ('12', 'Cuentas por cobrar comerciales – Terceros', 1),
            ('13', 'Cuentas por cobrar comerciales – Relacionadas', 1),
            ('14', 'Cuentas por cobrar al personal, a los accionistas y directores', 1),
            ('16', 'Cuentas por cobrar diversas – Terceros', 1),
            ('17', 'Cuentas por cobrar diversas – Relacionadas', 1),
            ('18', 'Servicios y otros contratados por anticipado', 1),
            ('19', 'Estimación de cuentas de cobranza dudosa', 1),
            
            # Elemento 2: Activo Realizable
            ('20', 'Mercaderías', 2),
            ('21', 'Productos terminados', 2),
            ('22', 'Subproductos, desechos y desperdicios', 2),
            ('23', 'Productos en proceso', 2),
            ('24', 'Materias primas', 2),
            ('25', 'Materiales auxiliares, suministros y repuestos', 2),
            ('26', 'Envases y embalajes', 2),
            ('27', 'Activos no corrientes mantenidos para la venta', 2),
            ('28', 'Inventarios por recibir', 2),
            ('29', 'Desvalorización de inventarios', 2),

            # Elemento 3: Activo Inmovilizado
            ('30', 'Inversiones mobiliarias', 3),
            ('31', 'Propiedades de inversión', 3),
            ('32', 'Activos por derecho de uso', 3),
            ('33', 'Propiedad, planta y equipo', 3),
            ('34', 'Intangibles', 3),
            ('35', 'Activos biológicos', 3),
            ('36', 'Desvalorización de activo inmovilizado', 3),
            ('37', 'Activo diferido', 3),
            ('38', 'Otros activos', 3),
            ('39', 'Depreciación y amortización acumulada', 3),

            # Elemento 4: Pasivo
            ('40', 'Tributos, contraprestaciones y aportes al sistema público', 4),
            ('41', 'Remuneraciones y participaciones por pagar', 4),
            ('42', 'Cuentas por pagar comerciales – Terceros', 4),
            ('43', 'Cuentas por pagar comerciales – Relacionadas', 4),
            ('44', 'Cuentas por pagar a los accionistas, directores y gerentes', 4),
            ('45', 'Obligaciones financieras', 4),
            ('46', 'Cuentas por pagar diversas – Terceros', 4),
            ('47', 'Cuentas por pagar diversas – Relacionadas', 4),
            ('48', 'Provisiones', 4),
            ('49', 'Pasivo diferido', 4),

            # Elemento 5: Patrimonio Neto
            ('50', 'Capital', 5),
            ('51', 'Acciones de inversión', 5),
            ('52', 'Capital adicional', 5),
            ('56', 'Resultados no realizados', 5),
            ('57', 'Excedente de revaluación', 5),
            ('58', 'Reservas', 5),
            ('59', 'Resultados acumulados', 5),

            # Elemento 6: Gastos por Naturaleza
            ('60', 'Compras', 6),
            ('61', 'Variación de inventarios', 6),
            ('62', 'Gastos de personal y directores', 6),
            ('63', 'Gastos de servicios prestados por terceros', 6),
            ('64', 'Gastos por tributos', 6),
            ('65', 'Otros gastos de gestión', 6),
            ('66', 'Pérdida por medición de activos no financieros', 6),
            ('67', 'Gastos financieros', 6),
            ('68', 'Valuación y deterioro de activos y provisiones', 6),
            ('69', 'Costo de ventas', 6),

            # Elemento 7: Ingresos
            ('70', 'Ventas', 7),
            ('71', 'Variación de la producción almacenada', 7),
            ('72', 'Producción de activo inmovilizado', 7),
            ('73', 'Descuentos, rebajas y bonificaciones obtenidos', 7),
            ('74', 'Descuentos, rebajas y bonificaciones concedidos', 7),
            ('75', 'Otros ingresos de gestión', 7),
            ('76', 'Ganancia por medición de activos no financieros', 7),
            ('77', 'Ingresos financieros', 7),
            ('78', 'Cargas cubiertas por provisiones', 7),
            ('79', 'Cargas imputables a cuentas de costos y gastos', 7),
            
            # Elemento 8 y 9 (Opcionales para saldos y costos, pero útiles tenerlos)
            ('88', 'Impuesto a la renta', 8),
            ('89', 'Determinación del resultado del ejercicio', 8),
            ('94', 'Gastos administrativos', 9),
            ('95', 'Gastos de ventas', 9)
        ]
        cursor.executemany('INSERT INTO Cuentas (codigo, descripcion, elemento) VALUES (?, ?, ?)', cuentas_pcge)
        print("Catálogo completo de cuentas PCGE (a 2 dígitos) cargado con éxito.")

    conexion.commit()
    conexion.close()
    print(f"Base de datos inicializada correctamente en: {DB_PATH}")

if __name__ == "__main__":
    # Si la base de datos ya existe, puedes eliminar el archivo contabilidad.db manualmente 
    # antes de ejecutar esto para que se cree desde cero con todas las cuentas.
    inicializar_db()