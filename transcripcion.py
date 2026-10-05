"""Módulo de transcripción de audio usando Groq.

Este módulo está aislado del motor contable (ia_engine.py).
No modifica prompts, contrato ni validaciones contables.
"""
import io
import os
from typing import Tuple, Optional

try:
    from groq import Groq
except Exception as e:  # pragma: no cover - import error path
    Groq = None  # type: ignore


def _obtener_cliente(api_key: Optional[str] = None):
    if Groq is None:
        raise RuntimeError("La librería groq no está disponible. Instala las dependencias.")
    key = api_key or os.getenv("GROQ_API_KEY") or os.getenv("API_GROQ")
    if not key:
        raise ValueError("No se encontró GROQ_API_KEY en el entorno.")
    return Groq(api_key=key)


def transcribir_audio_bytes(
    audio_bytes: bytes,
    filename: str = "grabacion.wav",
    model: str = "whisper-large-v3",
    language: Optional[str] = None,
) -> Tuple[bool, str]:
    if not audio_bytes:
        return False, "El audio está vacío."

    try:
        cliente = _obtener_cliente()
        archivo = io.BytesIO(audio_bytes)
        archivo.name = filename

        params = {
            "model": model,
            "file": archivo,
        }
        if language:
            params["language"] = language

        respuesta = cliente.audio.transcriptions.create(**params)
    except Exception as e:
        msg = str(e)
        return False, f"Error al transcribir el audio: {msg}"

    try:
        texto = getattr(respuesta, "text", None)
        if texto is None and isinstance(respuesta, dict):
            texto = respuesta.get("text")
        texto = (texto or "").strip()
        if not texto:
            return False, "La transcripción devolvió texto vacío."
        return True, texto
    except Exception as e:
        return False, f"Error al procesar la respuesta de transcripción: {e}"
