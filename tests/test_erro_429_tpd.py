"""
EL 429 DE GROQ: LA CUOTA DIARIA (TPD) NO SE REINTENTA, EL RITMO (TPM) SÍ.

Por que existe este archivo
---------------------------
La API de Groq responde 429 por dos motivos distintos:

  - TPD (tokens por DÍA): la cuenta agotó su cuota diaria. Groq lo avisa con
    `x-should-retry: false` y un `retry-after` de minutos. Reintentar dos veces más no
    puede funcionar: la cuota se repone por tiempo, no por insistence.
  - TPM (tokens por MINUTO): la petición llegó en ráfaga y sí se resuelve esperando.

El código los trataba igual: 3 intentos con esperas fijas y un diagnóstico que decía
"(413/429)", que no permite saber si el problema era el día o el minuto. Con la cuota
del día agotada, esas esperas son tiempo perdido y el mensaje esconde la causa real.

Lo que se fija aquí:
  - Un 429 TPD hace UNA llamada, sin esperas, y explica el límite, lo usado y cuándo
    se repone.
  - Un 429 transitorio conserva los reintentos y ahora usa `retry-after` cuando la API
    lo envía, acotado para no bloquear la interfaz.
  - El camino exitoso no cambia: una llamada, resultado completo, ninguna espera.
  - La salvaguarda de no devolver resultados parciales sigue en pie en ambos fallos.

Ningún test llama a la API: el cliente de Groq se sustituye por un doble y el error se
construye con la misma forma que devuelve el SDK (status_code, body, response.headers).
"""

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("GROQ_API_KEY", "clave-de-pruebas")

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia_engine as ia  # noqa: E402


# --------------------------------------------------------------------------- #
# Dobles de test
# --------------------------------------------------------------------------- #
class _RespuestaFalsa:
    """Éxito: el modelo devuelve un lote válido."""

    def __init__(self, contenido, finish_reason="stop"):
        mensaje = type("M", (), {"content": contenido, "reasoning": ""})()
        self.choices = [type("C", (), {"message": mensaje, "finish_reason": finish_reason})()]


class ErrorFalso(Exception):
    """Imita la forma de un error del SDK de Groq: status_code, body y cabeceras."""

    def __init__(self, status_code, mensaje, cuerpo=None, cabeceras=None):
        super().__init__(mensaje)
        self.status_code = status_code
        self.body = cuerpo if cuerpo is not None else {"error": {"message": mensaje}}
        self.response = type("R", (), {"headers": cabeceras or {}})()


MENSAJE_TPD = (
    "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01` "
    "service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 198207, "
    "Requested 4601. Please try again in 20m13.056s. Need more tokens? "
    "Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
)
CABECERAS_TPD = {
    "retry-after": "1214",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "8000",
    "x-should-retry": "false",
}

MENSAJE_TPM = (
    "Rate limit reached for model `openai/gpt-oss-20b`. Please try again in 2.5s."
)
CABECERAS_TPM = {
    "retry-after": "2.5",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "0",
    "x-should-retry": "true",
}


def error_tpd():
    return ErrorFalso(
        429,
        f"Error code: 429 - {json.dumps({'error': {'message': MENSAJE_TPD}})}",
        cuerpo={"error": {"message": MENSAJE_TPD, "type": "tokens", "code": "rate_limit_exceeded"}},
        cabeceras=CABECERAS_TPD,
    )


def error_tpm():
    return ErrorFalso(
        429,
        f"Error code: 429 - {json.dumps({'error': {'message': MENSAJE_TPM}})}",
        cuerpo={"error": {"message": MENSAJE_TPM, "code": "rate_limit_exceeded"}},
        cabeceras=CABECERAS_TPM,
    )


class ClienteQueFalla:
    """Sustituye a `cliente` de Groq y cuenta cuántas llamadas se hicieron."""

    def __init__(self, errores):
        self.errores = list(errores)
        self.llamadas = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **parametros):
        self.llamadas.append(parametros)
        if self.errores:
            raise self.errores.pop(0)
        return _RespuestaFalsa(_LOTE_JSON)


class ClienteQueAcierta:
    def __init__(self):
        self.llamadas = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **parametros):
        self.llamadas.append(parametros)
        return _RespuestaFalsa(_LOTE_JSON)


_LOTE_JSON = json.dumps(
    [
        {
            "fecha": "2009-09-30",
            "glosa": "Depreciacion",
            "asiento": [
                {"cuenta": "39", "debe": 0.0, "haber": 135.0},
                {"cuenta": "68", "debe": 135.0, "haber": 0.0},
            ],
        }
    ],
    ensure_ascii=False,
)

DOCUMENTO = (
    "=== PAGINA 1 ===\n"
    "30/09/2009 39 Depreciacion ACUMULADA -A |  | 135 | \n"
    "68 Gastos de Depreciacion G+ | 135 |  | \n"
)


@pytest.fixture
def sin_esperas(monkeypatch):
    """Las esperas se cronometran, no se cumplen: la suite no debe tardar 16 segundos."""
    registradas = []
    monkeypatch.setattr(ia.time, "sleep", lambda segundos: registradas.append(segundos))
    return registradas


def _correr(monkeypatch, cliente):
    monkeypatch.setattr(ia, "cliente", cliente)
    return ia.analizar_excel_completo(DOCUMENTO)


# --------------------------------------------------------------------------- #
# 1. El 429 de cuota DIARIA hace una sola llamada y explica la causa
# --------------------------------------------------------------------------- #
def test_429_de_cuota_diaria_hace_una_sola_llamada(monkeypatch, sin_esperas):
    cliente = ClienteQueFalla([error_tpd(), error_tpd(), error_tpd()])
    ok, mensaje = _correr(monkeypatch, cliente)

    assert len(cliente.llamadas) == 1, (
        f"la cuota del día agotada no se reintenta, y se hicieron {len(cliente.llamadas)} llamadas"
    )
    assert ok is False
    assert "no se pudo leer completo" in mensaje
    assert sin_esperas == [], f"no debe esperarse nada antes de rendirse: {sin_esperas}"


def test_el_diagnostico_del_429_diario_dice_la_causa(monkeypatch, sin_esperas):
    ok, mensaje = _correr(monkeypatch, ClienteQueFalla([error_tpd()]))

    assert ok is False
    assert "CUOTA DIARIA" in mensaje
    assert "TPD" in mensaje
    assert "200,000" in mensaje, "el límite diario que dice la API debe aparecer"
    assert "198,207" in mensaje, "los tokens ya usados deben aparecer"
    assert "4,601" in mensaje, "los tokens solicitados por esta llamada deben aparecer"
    assert "20m 14s" in mensaje, (
        "el tiempo hasta reponer la cuota sale de la cabecera retry-after (1214 s), "
        "la fuente que el servidor marca como autoritativa"
    )
    assert "No se reintenta" in mensaje


def test_el_429_diario_no_muestra_el_mensaje_ambiguo(monkeypatch, sin_esperas):
    ok, mensaje = _correr(monkeypatch, ClienteQueFalla([error_tpd()]))
    assert "(413/429)" not in mensaje, "ese texto no dice si el problema es el día o el minuto"


def test_un_429_sin_marca_de_dia_no_se_trata_como_cuota_diaria(monkeypatch, sin_esperas):
    """
    Un 429 que NO habla de tokens por día es un límite de ritmo y debe conservar los
    reintentos. Sin esta prueba, cualquier 429 se declararía "cuota del día" y dejaría de
    reintentarse lo que sí se puede reintentar.
    """
    cliente = ClienteQueFalla([error_tpm(), error_tpm(), error_tpm()])
    ok, mensaje = _correr(monkeypatch, cliente)

    assert len(cliente.llamadas) == 3, "un 429 de ritmo sí debe reintentarse"
    assert ok is False
    assert "CUOTA DIARIA" not in mensaje


# --------------------------------------------------------------------------- #
# 2. El 429 transitorio conserva los reintentos y usa `retry-after`
# --------------------------------------------------------------------------- #
def test_429_transitorio_reintenta_y_avisa_cuando_se_recupera(monkeypatch, sin_esperas):
    cliente = ClienteQueFalla([error_tpm(), error_tpm()])
    ok, resultado = _correr(monkeypatch, cliente)

    assert len(cliente.llamadas) == 3, "debe reintentar hasta MAX_INTENTOS"
    assert ok is True
    assert len(resultado) == 1
    assert sin_esperas, "entre reintentos tiene que haber una espera"


def test_el_reintento_usa_el_retry_after_de_la_api(monkeypatch, sin_esperas):
    """La API pidió 2.5 s: se espera eso, no los 4 s fijos ni los 8 s del segundo intento."""
    cliente = ClienteQueFalla([error_tpm(), error_tpm(), error_tpm()])
    _correr(monkeypatch, cliente)

    assert 2.5 in sin_esperas, f"se esperaba el retry-after de la API; esperas: {sin_esperas}"


def test_el_retry_after_se_acota_para_no_bloquear_la_interfaz(monkeypatch, sin_esperas):
    """
    Un `retry-after` de 1214 s dejaría la petición de Streamlit colgada 20 minutos. El
    tope es lo que hace seguro usar el valor del servidor.
    """
    error = ErrorFalso(429, "Rate limit reached. Please try again in 1214s.", cabeceras={"retry-after": "1214"})
    espera = ia._espera_antes_de_reintentar(error, 1)
    assert espera == ia.ESPERA_MAXIMA_POR_RETRY_AFTER
    assert espera <= 60.0


def test_sin_retry_after_se_conserva_la_espera_por_intento(monkeypatch):
    """Sin cabecera y sin mensaje, el comportamiento anterior (4 s x intento) no cambia."""
    error = ErrorFalso(429, "Error code: 429")
    assert ia._espera_antes_de_reintentar(error, 1) == ia.ESPERA_ENTRE_INTENTOS * 1
    assert ia._espera_antes_de_reintentar(error, 3) == ia.ESPERA_ENTRE_INTENTOS * 3


# --------------------------------------------------------------------------- #
# 3. El camino exitoso no cambia
# --------------------------------------------------------------------------- #
def test_el_exito_sigue_haciendo_una_sola_llamada_sin_esperas(monkeypatch, sin_esperas):
    cliente = ClienteQueAcierta()
    ok, resultado = _correr(monkeypatch, cliente)

    assert ok is True
    assert len(cliente.llamadas) == 1
    assert sin_esperas == []
    assert list(resultado)[0]["asiento"] == [
        {"cuenta": "39", "debe": 0.0, "haber": 135.0},
        {"cuenta": "68", "debe": 135.0, "haber": 0.0},
    ]


def test_el_exito_conserva_los_parametros_de_la_llamada(monkeypatch, sin_esperas):
    cliente = ClienteQueAcierta()
    _correr(monkeypatch, cliente)
    llamada = cliente.llamadas[0]

    assert llamada["model"] == ia.MODELO_IA
    assert llamada["temperature"] == 0.1
    assert llamada["max_completion_tokens"] == ia.MAX_COMPLETION_TOKENS
    assert llamada["messages"][0]["content"] == ia.instrucciones_agente_excel
    assert "39 Depreciacion ACUMULADA" in llamada["messages"][1]["content"]


# --------------------------------------------------------------------------- #
# 4. La salvaguarda de resultados parciales sigue en pie
# --------------------------------------------------------------------------- #
def test_un_bloque_fallido_no_devuelve_resultados_parciales(monkeypatch, sin_esperas):
    ok, mensaje = _correr(monkeypatch, ClienteQueFalla([error_tpd()]))
    assert ok is False
    assert "No se devuelve un resultado parcial" in mensaje
    assert isinstance(mensaje, str), "el fallo devuelve un mensaje, nunca una lista de operaciones"


# --------------------------------------------------------------------------- #
# 5. La clasificación de los errores, aislada
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "error, es_tpd",
    [
        (error_tpd(), True),
        (error_tpm(), False),
        (ErrorFalso(413, "Request too large"), False),
        (ErrorFalso(404, "model_not_found: does not exist"), False),
        (ErrorFalso(500, "Internal Server Error"), False),
    ],
)
def test_la_clasificacion_separa_cuota_diaria_del_resto(error, es_tpd):
    assert ia._es_error_de_cuota_diaria(error) is es_tpd


def test_un_500_se_reporta_como_error_de_api_y_no_se_reintenta(monkeypatch, sin_esperas):
    """Comportamiento previo: un error que no es 413/429 corta en el primer intento."""
    cliente = ClienteQueFalla([ErrorFalso(500, "Internal Server Error")])
    ok, mensaje = _correr(monkeypatch, cliente)

    assert len(cliente.llamadas) == 1
    assert ok is False
    assert "error de la API" in mensaje


def test_el_413_sigue_siendo_reintentable():
    """El 413 (payload demasiado grande) mantiene su tratamiento anterior: se reintenta."""
    error = ErrorFalso(413, "Request too large", cabeceras={})
    assert ia._es_error_de_cuota_diaria(error) is False
    assert ia._es_error_de_tamano_o_ritmo(error) is True


# --------------------------------------------------------------------------- #
# 6. Lectura de los números que envía la API
# --------------------------------------------------------------------------- #
def test_se_leen_las_cifras_de_la_cuota_diaria():
    informe = ia._informe_de_cuota_diaria(error_tpd())
    assert "límite 200,000 tokens/día" in informe
    assert "usados 198,207" in informe
    assert "solicitados 4,601" in informe
    assert "20m 14s" in informe, "la cabecera retry-after manda sobre el texto del mensaje"


def test_el_informe_no_inventa_datos_si_la_api_no_los_manda():
    """Sin cifras ni esperas, el informe dice la causa y nada más."""
    informe = ia._informe_de_cuota_diaria(ErrorFalso(429, "tokens per day exhausted"))
    assert "CUOTA DIARIA" in informe
    assert "límite" not in informe
    assert "usados" not in informe
    assert "repone" not in informe


def test_la_duracion_de_la_api_se_lee_en_varios_formatos():
    assert ia._duracion_a_segundos("Please try again in 20m13.056s.") == pytest.approx(1213.056)
    assert ia._duracion_a_segundos("Please try again in 45s.") == pytest.approx(45.0)
    assert ia._duracion_a_segundos("Please try again in 1h2m3s.") == pytest.approx(3723.0)
    assert ia._duracion_a_segundos("sin fecha de reintento") is None