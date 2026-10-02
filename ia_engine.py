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