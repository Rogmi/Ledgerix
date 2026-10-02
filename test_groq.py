import os
from dotenv import load_dotenv
from groq import Groq

load_dotenv()
clave = os.getenv("GROQ_API_KEY")

if not clave:
    print("❌ Error: No se encontró la clave GROQ_API_KEY en el archivo .env")
else:
    print("✅ Clave de Groq cargada correctamente.")
    cliente = Groq(api_key=clave)
    
    print("\nModelos habilitados y activos para tu cuenta:")
    try:
        modelos = cliente.models.list()
        for m in modelos.data:
            print(f" - {m.id}")
    except Exception as e:
        print(f"Error al consultar la API: {e}")