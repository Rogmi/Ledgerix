import os
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
clave = os.getenv("GEMINI_API_KEY")

if not clave:
    print("❌ Error: No se encontró la clave en el archivo .env")
else:
    print(f"✅ Clave cargada correctamente (termina en ...{clave[-4:]})")
    genai.configure(api_key=clave)
    
    print("\nModelos habilitados para tu cuenta:")
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(f" - {m.name}")
    except Exception as e:
        print(f"Error al consultar la API: {e}")