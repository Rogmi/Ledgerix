import os
import json
from dotenv import load_dotenv
from groq import Groq

# 1. Cargar credenciales
load_dotenv()
cliente = Groq(api_key=os.getenv("GROQ_API_KEY"))

# 2. Prompt Engineering de alto nivel
instrucciones_contador = """
Eres un contador público experto en el Plan Contable General Empresarial (PCGE) de Perú.
Tu tarea es leer un enunciado contable, interpretar la naturaleza de la transacción y extraer la partida doble exacta.
Responde ÚNICAMENTE con un JSON válido que contenga una lista de diccionarios con las claves 'cuenta' (código de 2 dígitos como string), 'debe' (float), y 'haber' (float).
No uses formato Markdown, no saludes, no des explicaciones. Solo devuelve el texto plano del JSON.
Ejemplo de entrada: Se compra mercadería al contado por 50000.
Ejemplo de salida: [{"cuenta": "20", "debe": 50000.0, "haber": 0.0}, {"cuenta": "10", "debe": 0.0, "haber": 50000.0}]
"""

def extraer_asiento_de_texto(enunciado):
    """
    Envía el texto crudo a la IA de Groq (Llama 3), obtiene la estructura 
    contable y la convierte en un diccionario legible para el sistema.
    """
    try:
        respuesta = cliente.chat.completions.create(
            messages=[
                {"role": "system", "content": instrucciones_contador},
                {"role": "user", "content": f"Enunciado a analizar: {enunciado}"}
            ],
            model="openai/gpt-oss-20b",
            temperature=0.1 # Temperatura baja = respuestas precisas y matemáticas
        )
        
        texto_crudo = respuesta.choices[0].message.content
        # Limpieza de seguridad
        texto_limpio = texto_crudo.replace("```json", "").replace("```", "").strip()
        
        asiento_formateado = json.loads(texto_limpio)
        return True, asiento_formateado
        
    except json.JSONDecodeError:
        return False, "La IA no devolvió un formato estructurado válido."
    except Exception as e:
        return False, f"Error de conexión con la IA de Groq: {e}"

# (Mantén tus importaciones y tu función extraer_asiento_de_texto intactas arriba)

instrucciones_agente_excel = """
Eres un Contador Público de Perú experto en el Plan Contable General Empresarial (PCGE).
Tu misión es extraer transacciones de un texto crudo de Excel y generar los asientos contables correctos.

CATÁLOGO PCGE BÁSICO (A 2 DÍGITOS - USA SOLO ESTAS CUENTAS):
- Elemento 1 (Activo): 10 Efectivo, 12 Cuentas por cobrar, 14 Cuentas al personal, 16 Cuentas por cobrar diversas.
- Elemento 2 (Activo Realizable): 20 Mercaderías, 21 Productos terminados, 24 Materias primas.
- Elemento 3 (Activo Inmovilizado): 33 Inmuebles, maquinaria y equipo, 39 Depreciación acumulada.
- Elemento 4 (Pasivo): 40 Tributos por pagar, 41 Remuneraciones, 42 Cuentas por pagar comerciales, 45 Obligaciones financieras, 46 Cuentas por pagar diversas.
- Elemento 5 (Patrimonio): 50 Capital social, 59 Resultados acumulados.
- Elemento 6 (Gastos): 60 Compras, 61 Variación de inventarios, 62 Gastos de personal, 63 Gastos de servicios, 68 Valuación y deterioro, 69 Costo de ventas.
- Elemento 7 (Ingresos): 70 Ventas.
- Elemento 9 (Cuentas de destino): 94 Gastos administrativos, 95 Gastos de ventas.

REGLAS DE ANÁLISIS:
1. Interpreta la transacción y elige la cuenta correcta del catálogo. Ej: Venta al crédito es 12 y 70. Gasto operativo/servicios es 63 y 10. Aporte de capital es 10 y 50.
2. REGLA DEL COSTO DE VENTAS: Busca información sobre "saldo final" o "inventario final". Si existe, OBLIGATORIAMENTE calcula: (Suma de mercadería comprada) - (Saldo final). Genera un asiento adicional con la cuenta 69 (Debe) y 20 (Haber) por ese importe calculado.

FORMATO DE RESPUESTA:
Devuelve ÚNICAMENTE un JSON válido. Usa la clave "razonamiento" para hacer tu cálculo matemático y explicar tu elección de cuentas ANTES de dar el asiento.
[
  {
    "fecha": "YYYY-MM-DD",
    "glosa": "Descripción",
    "razonamiento": "Se aporta efectivo para capital, usamos la cuenta 10 (Efectivo) y 50 (Capital).",
    "asiento": [
        {"cuenta": "10", "debe": 100000, "haber": 0},
        {"cuenta": "50", "debe": 0, "haber": 100000}
    ]
  }
]
"""

def analizar_excel_completo(texto_crudo_excel):
    """
    Envía la totalidad del documento a Llama 3 para que discrimine el ruido 
    de las transacciones reales de forma inteligente.
    """
    try:
        respuesta = cliente.chat.completions.create(
            messages=[
                {"role": "system", "content": instrucciones_agente_excel},
                {"role": "user", "content": f"Contenido del Excel:\n{texto_crudo_excel}"}
            ],
            model="openai/gpt-oss-20b",
            temperature=0.1
        )
        
        texto_limpio = respuesta.choices[0].message.content.replace("```json", "").replace("```", "").strip()
        lote_operaciones = json.loads(texto_limpio)
        return True, lote_operaciones
        
    except json.JSONDecodeError:
        return False, "La IA no pudo estructurar el documento. Intenta revisando el Excel."
    except Exception as e:
        return False, f"Error del Agente: {e}"