"""
Script de diagnostico: lista los modelos activos de la cuenta de Groq.

NO es una prueba unitaria. Vive fuera de la raiz y fuera de `tests/` a proposito:
pytest recolecta por convencion de nombre cualquier `test_*.py` de la raiz y este
archivo hacia una llamada REAL a la API (y cobraba cuota) en cada corrida de la suite.
Ejecutalo a mano cuando cambie el modelo o si sospechas de un 404 de modelo:

    .\\venv\\Scripts\\python.exe scripts\\listar_modelos.py
"""

import os
import sys

from dotenv import load_dotenv
from groq import Groq


def main():
    # La consola de Windows usa cp1252 y revienta con los emojis de este archivo.
    # `errors="replace"` imprime un caracter en vez de tumbar el script.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    load_dotenv()
    clave = os.getenv("GROQ_API_KEY")

    if not clave:
        print("[X] Error: no se encontro la clave GROQ_API_KEY en el archivo .env")
        return 1

    print("[OK] Clave de Groq cargada correctamente.")
    cliente = Groq(api_key=clave)

    print("\nModelos habilitados y activos para tu cuenta:")
    try:
        for modelo in cliente.models.list().data:
            print(f" - {modelo.id}")
    except Exception as error:
        print(f"Error al consultar la API: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())