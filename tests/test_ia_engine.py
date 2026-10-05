"""
Escáner Visual (Groq Vision): el MISMO motor contable, con la imagen de entrada.

Estas pruebas fijan el invariante que hace que el escáner no sea un camino paralelo:
una foto entra por `_extraer_json` + `procesar_dinamica_contable`, exactamente igual
que un Excel o un PDF. Si alguien escribe una validacion contable solo para imagenes,
estas pruebas dejan de reflejar el sistema real.

Ninguna prueba llama a la API: la respuesta de Groq Vision se simula con un doble de
prueba (`respuesta_vision`) y se intercepta `ia.cliente.chat.completions.create`.
Asi la suite es gratuita, determinista y no depende de la red ni de la cuota.

Reglas que estas pruebas defienden:
  - La imagen entra como base64 utf-8 y con su content-type REAL (un PNG no se
    declara "image/jpeg": degrada la lectura de los importes).
  - El prompt es `instrucciones_agente_excel`, no uno nuevo: mismo contrato para
    foto y para texto.
  - El JSON del modelo SIEMPRE pasa por el motor contable (descuadre marcado,
    fila plana marcada, estructura `asiento` conservada).
  - Una respuesta vacia, sin JSON o truncada NO se acepta como resultado completo:
    se reintenta y, si persiste, se devuelve el error con diagnostico.
  - Un error de entrada (imagen vacia, texto en vez de bytes, imagen gigantic)
    se detecta ANTES de llamar a la API y no cuesta cuota.
"""

import base64
import io
import json
import os
import sys
from pathlib import Path

# El modulo construye un cliente de Groq al importarse. Ninguna prueba de este
# archivo llega a la red, pero la clave debe existir para que el import no reviente.
os.environ.setdefault("GROQ_API_KEY", "clave-de-pruebas")

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia_engine as ia  # noqa: E402

# Pillow se usa para generar imagenes reales de prueba y para inspeccionar las que
# salen del optimizador.
from PIL import Image as PILImage  # noqa: E402

import pytest  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers: dobles de prueba de la respuesta de Groq Vision
# --------------------------------------------------------------------------- #
class _Mensaje:
    def __init__(self, content):
        self.content = content


class _Eleccion:
    def __init__(self, content, finish_reason="stop"):
        self.message = _Mensaje(content)
        self.finish_reason = finish_reason


class _RespuestaGroq:
    """Imita la forma de `client.chat.completions.create` que consume `_leer_contenido`."""

    def __init__(self, content, finish_reason="stop"):
        self.choices = [_Eleccion(content, finish_reason)]


def imagen_png_falsa():
    """Bytes con la firma PNG real: no es una imagen valida, pero SI es una imagen
    para efectos de transporte (lo que se valida aqui es el contrato del payload).
    Pillow no la puede abrir, asi que sirve para probar el CAMINO DE FALLO."""
    return b"\x89PNG\r\n\x1a\n" + b"datos-falsos-de-una-factura" * 8


def imagen_real(ancho=3200, alto=2400, modo="RGB", formato="JPEG"):
    """
    Genera una imagen REAL con Pillow: un degradado con rayas, parecido a un
    comprobante impreso.

Se usa contenido comprensible y no ruido aleatorio por dos razones practicas: el
    ruido de 3200x2400 pesa mas de 20 MB (y la prueba tardaria segundos en generarlo),
    mientras que un degradado representa el caso real de una foto de documento y
    mantiene el archivo por debajo del techo de 5 MB que el motor impone.
    """
    from PIL import Image as PILImage, ImageDraw

    vertical = PILImage.new("RGB", (ancho, alto))
    dibujo = ImageDraw.Draw(vertical)
    for y in range(0, alto, 8):
        tono = 200 - int(120 * y / alto)
        dibujo.rectangle([0, y, ancho, y + 8], fill=(tono, tono + 10, 255 - tono))
    for indice in range(0, alto, 60):  # las "lineas" del comprobante
        dibujo.rectangle([40, indice, ancho - 40, indice + 3], fill=(30, 30, 30))

    if modo != "RGB":
        vertical = vertical.convert(modo)

    buffer = io.BytesIO()
    vertical.save(buffer, format=formato, quality=95)
    return buffer.getvalue()


def respuesta_vision(contenido, finish_reason="stop"):
    """Empaqueta el texto que devolveria el modelo en la forma que espera el SDK."""
    return _RespuestaGroq(contenido, finish_reason)


# Asiento valido de una compra de mercaderia pagada al contado (una foto de una
# factura): 20 al DEBE y 10 al HABER por el mismo importe. Debe = Haber.
ASIENTO_COMPRA_AL_CONTADO = [
    {
        "fecha": "2024-06-10",
        "glosa": "Compra de mercaderia al contado",
        "asiento": [
            {"cuenta": "20", "debe": 850.0, "haber": 0.0},
            {"cuenta": "10", "debe": 0.0, "haber": 850.0},
        ],
    }
]


class ClienteSimulado:
    """
    Doble del cliente de Groq. Reproduce la cadena real
    (`cliente.chat.completions.create`), devuelve respuestas en cola y registra cada
    llamada, para poder afirmar que el motor reintenta y que manda el payload esperado.
    """

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []
        self.chat = self
        self.completions = self

    def create(self, **parametros):
        self.llamadas.append(parametros)
        if not self.respuestas:
            raise AssertionError("Se llamo a la API mas veces de las esperadas.")
        return self.respuestas.pop(0)


def con_cliente(monkeypatch, *respuestas):
    """Instala un cliente simulado en el modulo y lo devuelve."""
    cliente = ClienteSimulado(respuestas)
    monkeypatch.setattr(ia, "cliente", cliente)
    monkeypatch.setattr(ia.time, "sleep", lambda _s: None)  # los reintentos no esperan
    return cliente


def mensaje_de_usuario(parametros):
    """Localiza el mensaje `user` multimodal del payload."""
    for mensaje in parametros["messages"]:
        if mensaje["role"] == "user":
            return mensaje
    raise AssertionError("El payload no traia un mensaje de usuario.")


def totales(asiento):
    return (
        round(sum(p["debe"] for p in asiento["asiento"]), 2),
        round(sum(p["haber"] for p in asiento["asiento"]), 2),
    )


# --------------------------------------------------------------------------- #
# 1. Codificacion de la imagen
# --------------------------------------------------------------------------- #
def test_codificar_imagen_devuelve_base64_utf8():
    """`_codificar_imagen_base64` devuelve TEXTO base64, no bytes."""
    crudo = imagen_png_falsa()

    codificado = ia._codificar_imagen_base64(crudo)

    assert isinstance(codificado, str), "debe ser str: un bytes se serializa como b'...' en el data URL"
    assert base64.b64decode(codificado) == crudo, "el base64 debe reconstruir la imagen exacta"
    # ASCII puro: si apareciera un byte no imprimible, el data URL viaja corrupto.
    assert codificado.encode("ascii", errors="strict").decode("ascii") == codificado


def test_codificar_imagen_rechaza_entradas_invalidas():
    """Los tres fallos que producirian un error de API confuso se cortan aqui."""
    with pytest.raises(ValueError):
        ia._codificar_imagen_base64(b"")
    with pytest.raises(ValueError):
        ia._codificar_imagen_base64(None)
    with pytest.raises(ValueError):
        ia._codificar_imagen_base64("ya-es-texto")  # el error clasico: read() en vez de getvalue()


def test_detectar_mime_usa_la_firma_real_de_la_imagen():
    """Tras el optimizador esto solo aplica al camino de fallo, pero el fallback lo
    necesita: si Pillow no abre la imagen, el data URL igual debe declarar el tipo."""
    assert ia._detectar_mime_imagen(b"\x89PNG\r\n\x1a\n" + b"x") == "image/png"
    assert ia._detectar_mime_imagen(b"\xff\xd8\xff\xe0" + b"x") == "image/jpeg"
    # Formato desconocido: se deja el default en vez de inventar un tipo.
    assert ia._detectar_mime_imagen(b"GIF89a") == "image/jpeg"


# --------------------------------------------------------------------------- #
# 1b. Optimizador de imagen (prevencion del 429 por tokens de imagen)
# --------------------------------------------------------------------------- #
def test_optimizar_reduce_los_pixeles_y_devuelve_jpeg():
    """
    El 429 de este modulo lo causa el AREA de la foto, no el texto. Una imagen de
    3200x2400 tiene que entrar al modelo por debajo del techo de pixeles.
    """
    original = imagen_real(3200, 2400)

    optimizada, mime = ia.optimizar_imagen_para_api(original)

    assert mime == "image/jpeg"
    assert len(optimizada) < len(original), "reen codificar debe reducir el peso"
    with PILImage.open(io.BytesIO(optimizada)) as imagen:
        assert imagen.format == "JPEG"
        assert max(imagen.size) <= ia.MAX_DIMENSION_IMAGEN
        # `thumbnail` conserva el aspect ratio: no deforma la factura.
        assert abs(imagen.width / imagen.height - 3200 / 2400) < 0.02


def test_optimizar_respeta_el_techo_que_se_le_pasa():
    """El limite se puede ajustar por entorno (MAX_DIM_IMAGEN) sin tocar el codigo."""
    original = imagen_real(1200, 900)

    optimizada, mime = ia.optimizar_imagen_para_api(original, max_dim=400)

    assert mime == "image/jpeg"
    with PILImage.open(io.BytesIO(optimizada)) as imagen:
        assert max(imagen.size) == 400


def test_optimizar_convierte_rgba_y_paleta_en_rgb():
    """
    JPEG no guarda canal alfa: un PNG con transparencia se guardaria con un canal
    fantasma o directamente fallaria. Ademas, una foto de documento no usa alfa.
    """
    for modo in ("RGBA", "P"):
        # Se generan en PNG porque un PNG con alfa es justamente el caso que llega de
        # una captura de pantalla, y es imposible guardarlo como JPEG: sin esta
        # conversion, `Image.save` falla antes de llegar al motor.
        original = imagen_real(900, 600, modo=modo, formato="PNG")

        optimizada, mime = ia.optimizar_imagen_para_api(original)

        assert mime == "image/jpeg"
        with PILImage.open(io.BytesIO(optimizada)) as imagen:
            assert imagen.mode == "RGB", f"la imagen en {modo} debe quedar en RGB"
            assert imagen.format == "JPEG"


def test_optimizar_no_agranda_una_imagen_pequena():
    """`thumbnail` (no `resize`) solo reduce: estirar una captura de 300x200 hasta
    1600 px costaria mas tokens para menos informacion legible."""
    original = imagen_real(300, 200)

    optimizada, _ = ia.optimizar_imagen_para_api(original, max_dim=1600)

    with PILImage.open(io.BytesIO(optimizada)) as imagen:
        assert imagen.size == (300, 200), "una imagen menor que el techo no se reescala"


def test_optimizar_cae_a_la_imagen_original_si_pillow_no_puede_abrirla():
    """
    Optimizar es una mejora de coste, nunca una condicion para escanear. Un archivo
    corrupto o un formato raro debe llegar igual a la API con su mime real.
    """
    original = imagen_png_falsa()  # firma PNG, pero no es una imagen

    optimizada, mime = ia.optimizar_imagen_para_api(original)

    assert optimizada == original, "el fallback debe devolver los bytes intactos"
    assert mime == "image/png", "y el tipo real, para que el data URL no mienta"


def test_analizar_imagen_optimiza_antes_de_gastar_la_cuota(monkeypatch):
    """
    El orden importa: si se codificara en base64 y se mandara antes de optimizar, el
    429 ya habria ocurrido. Aqui se comprueba que lo que sale hacia la API es la
    imagen reescalada, no la que entro.
    """
    original = imagen_real(4000, 3000)
    cliente = con_cliente(
        monkeypatch,
        respuesta_vision(contenido=json.dumps(ASIENTO_COMPRA_AL_CONTADO)),
    )

    exito, resultado = ia.analizar_imagen_comprobante(original)

    assert exito
    enviado = base64.b64decode(
        mensaje_de_usuario(cliente.llamadas[0])["content"][1]["image_url"]["url"].partition(",")[2]
    )
    assert enviado != original, "a la API debe viajar la imagen optimizada"
    with PILImage.open(io.BytesIO(enviado)) as imagen:
        assert max(imagen.size) <= ia.MAX_DIMENSION_IMAGEN

    # Y la traza lo declara, para que un 429 en produccion se pueda diagnosticar.
    assert any("KB originales" in aviso for aviso in resultado.avisos)


# --------------------------------------------------------------------------- #
# 2. El payload de Groq Vision
# --------------------------------------------------------------------------- #
def test_payload_es_multimodal_con_el_prompt_contable_del_sistema(monkeypatch):
    """El escáner usa `instrucciones_agente_excel` (con el prefijo visual encima): un
    prompt aparte seria un segundo contrato de salida, y por lo tanto una segunda
    regla de cuadre."""
    crudo = imagen_real(3200, 2400)
    cliente = con_cliente(
        monkeypatch,
        respuesta_vision(contenido=json.dumps(ASIENTO_COMPRA_AL_CONTADO)),
    )

    exito, _ = ia.analizar_imagen_comprobante(crudo)

    assert exito, "el mock debio producir un resultado exitoso"
    assert len(cliente.llamadas) == 1

    parametros = cliente.llamadas[0]
    assert parametros["model"] == ia.MODELO_VISION
    # No se comprueba que el nombre del modelo contenga "vision": el reemplazo que
    # exige Groq (`meta-llama/llama-4-scout-17b-16e-instruct`) no la contiene. Lo que
    # importa es que la peticion shape de vision (image_url + prompt visual) llegue
    # intacta; la capacidad de ver la tiene el modelo, no la ortografia del id.
    assert "image_url" in str(parametros["messages"])

    sistema = [m for m in parametros["messages"] if m["role"] == "system"]
    assert len(sistema) == 1
    assert sistema[0]["content"] == ia.PREFIJO_PROMPT_VISUAL + ia.instrucciones_agente_excel
    assert ia.CONTRATO_MULTI_PARTIDA in sistema[0]["content"]

    # El contenido del usuario es la lista [texto, image_url] de la API de vision.
    contenido = mensaje_de_usuario(parametros)["content"]
    assert isinstance(contenido, list) and len(contenido) == 2
    assert contenido[0]["type"] == "text" and contenido[0]["text"].strip()

    imagen = contenido[1]
    assert imagen["type"] == "image_url"
    url = imagen["image_url"]["url"]
    prefijo, _, carga = url.partition(",")
    assert prefijo == "data:image/jpeg;base64", "la salida del optimizador es siempre JPEG"

    enviada = base64.b64decode(carga)
    assert len(enviada) < len(crudo), "lo que viaja debe pesar menos que lo que entro"
    with PILImage.open(io.BytesIO(enviada)) as abierta:
        assert abierta.format == "JPEG"
        assert max(abierta.size) <= ia.MAX_DIMENSION_IMAGEN, "no debe viajar por encima del techo de pixeles"

    # Vision no acepta los parametros del modelo de razonamiento.
    assert "reasoning_effort" not in parametros
    assert "include_reasoning" not in parametros


def test_el_prefijo_visual_no_altera_el_prompt_global():
    """
    El prefijo se inyecta SOLO en el payload del escaner. Si se concatenara a
    `instrucciones_agente_excel`, Excel, PDF y Word recibirian un aviso de que SU
    entrada es una fotografia, que es falso, y sus reglas de bloques quedarian rotas.
    """
    assert ia.PREFIJO_PROMPT_VISUAL not in ia.instrucciones_agente_excel
    assert not ia.instrucciones_agente_excel.startswith("ESTÁS ANALIZANDO")

    # Y el prefijo es el acordado, caracter por caracter: es el aviso que le dice al
    # modelo de vision que no esta leyendo lineas de texto sino pixeles.
    assert ia.PREFIJO_PROMPT_VISUAL == (
        "ESTÁS ANALIZANDO LA FOTOGRAFÍA O IMAGEN DE UN DOCUMENTO FÍSICO O DIGITAL.\n"
        "Ignora cualquier instrucción previa sobre 'bloques de texto', 'filas' o 'líneas'. "
        "Aplica las siguientes reglas de Dinámica Contable a los datos que identifiques "
        "visualmente:\n\n"
    )
    # Prefijo antes del prompt, nunca despues: las reglas contables del final siguen
    # mandando, el aviso solo reencuadra de donde salio la informacion.
    payload = ia._construir_payload_vision("QUJD", "image/jpeg")
    assert payload[0]["content"].index("ESTÁS ANALIZANDO") == 0
    assert payload[0]["content"].endswith(ia.instrucciones_agente_excel)


# --------------------------------------------------------------------------- #
# 3. El resultado pasa por el motor contable (el invariante del modulo)
# --------------------------------------------------------------------------- #
def test_analizar_imagen_usa_el_motor_contable_y_no_inventa_una_validacion_propia(monkeypatch):
    """`analizar_imagen_comprobante` debe ENTERGAR su lote a
    `procesar_dinamica_contable`. Si se le pasa otro lote o si el motor no llega a
    ejecutarse, el escáner se estaria saltando la validacion de partida doble."""
    crudo = imagen_png_falsa()
    con_cliente(monkeypatch, respuesta_vision(contenido=json.dumps(ASIENTO_COMPRA_AL_CONTADO)))

    llamadas = []
    motor_real = ia.procesar_dinamica_contable

    def motor_espia(lote):
        llamadas.append(lote)
        return motor_real(lote)

    monkeypatch.setattr(ia, "procesar_dinamica_contable", motor_espia)

    exito, resultado = ia.analizar_imagen_comprobante(crudo)

    assert exito, f"el lote valido debio procesarse sin error: {resultado}"
    assert len(llamadas) == 1, "el lote del modelo debe pasar por el motor contable"


def test_analizar_imagen_devuelve_resultado_ia_validado_sin_errores(monkeypatch):
    """Caso feliz completo: (True, ResultadoIA) con el asiento intacto y sin avisos
    de descuadre. Es el mismo contrato de retorno que `analizar_excel_completo`."""
    crudo = imagen_png_falsa()
    con_cliente(monkeypatch, respuesta_vision(contenido=json.dumps(ASIENTO_COMPRA_AL_CONTADO)))

    exito, resultado = ia.analizar_imagen_comprobante(crudo)

    assert exito is True
    assert isinstance(resultado, ia.ResultadoIA), "debe devolver el MISMO tipo que el resto del sistema"
    assert isinstance(resultado, list)
    assert len(resultado) == 1, "una factura = un asiento con sus dos partidas"

    operacion = resultado[0]
    assert operacion["fecha"] == "2024-06-10"
    assert operacion["glosa"] == "Compra de mercaderia al contado"
    assert [p["cuenta"] for p in operacion["asiento"]] == ["20", "10"]
    assert totales(operacion) == (850.0, 850.0), "la partida doble debe llegar validada"
    assert not operacion.get("revisar"), "un asiento que cuadra no va a revisión"

    # `avisos` es lo que la UI lee para las advertencias: debe existir siempre.
    assert isinstance(resultado.avisos, list)


def test_analizar_imagen_marca_el_descuadre_sin_corregirlo(monkeypatch):
    """Una foto mal leida produce un descuadre: el motor lo MARCA y lo conserva, no
    lo cuadra. Es el mismo criterio que en el flujo de documentos."""
    crudo = imagen_png_falsa()
    lote_descuadrado = [{
        "fecha": "2024-06-10",
        "glosa": "Compra de mercaderia (importe mal leido)",
        "asiento": [
            {"cuenta": "20", "debe": 850.0, "haber": 0.0},
            {"cuenta": "10", "debe": 0.0, "haber": 800.0},
        ],
    }]
    con_cliente(monkeypatch, respuesta_vision(contenido=json.dumps(lote_descuadrado)))

    exito, resultado = ia.analizar_imagen_comprobante(crudo)

    assert exito, "un descuadre se entrega para revisión, no se descarta"
    assert totales(resultado[0]) == (850.0, 800.0), "los importes NO se mueven para cuadrar"
    assert resultado[0]["revisar"] is True
    assert any("no cuadra" in aviso for aviso in resultado.avisos)


def test_analizar_imagen_no_fusiona_partidas_que_llegaron_planas(monkeypatch):
    """Sin la clave `asiento` el motor no deduce la estructura: conserva cada fila y
    la marca. El escáner no puede inventar la agrupacion que el modelo no declaro."""
    crudo = imagen_png_falsa()
    filas_planas = [
        {"fecha": "2024-06-10", "glosa": "Compra al contado", "cuenta": "20", "debe": 500.0, "haber": 0.0},
        {"fecha": "2024-06-10", "glosa": "Compra al contado", "cuenta": "10", "debe": 0.0, "haber": 500.0},
    ]
    con_cliente(monkeypatch, respuesta_vision(contenido=json.dumps(filas_planas)))

    exito, resultado = ia.analizar_imagen_comprobante(crudo)

    assert exito
    assert len(resultado) == 2, "las filas planas no se agrupan en un asiento inventado"
    assert all(operacion["revisar"] is True for operacion in resultado)


def test_analizar_imagen_tolera_markdown_rodeando_el_json(monkeypatch):
    """El modelo de vision suele envolver el JSON en un bloque ```json: la extraccion
    del sistema (`_extraer_json`) debe limpiarlo igual que en el flujo de texto."""
    crudo = imagen_png_falsa()
    contenido = (
        "Aqui esta el asiento:\n```json\n"
        + json.dumps(ASIENTO_COMPRA_AL_CONTADO)
        + "\n```"
    )
    con_cliente(monkeypatch, respuesta_vision(contenido=contenido))

    exito, resultado = ia.analizar_imagen_comprobante(crudo)

    assert exito
    assert len(resultado) == 1
    assert totales(resultado[0]) == (850.0, 850.0)
    assert not resultado[0].get("revisar")


# --------------------------------------------------------------------------- #
# 4. Respuestas que NO pueden convertirse en un borrador completo
# --------------------------------------------------------------------------- #
def test_respuesta_vacia_se_reintenta_y_no_devuelve_resultado(monkeypatch):
    """Una respuesta vacia consume intentos, no cuota infinita: tras MAX_INTENTOS el
    sistema falla con diagnostico en vez de abrir un borrador vacio."""
    crudo = imagen_png_falsa()
    cliente = con_cliente(
        monkeypatch,
        *[respuesta_vision(contenido="") for _ in range(ia.MAX_INTENTOS)],
    )

    exito, mensaje = ia.analizar_imagen_comprobante(crudo)

    assert exito is False
    assert isinstance(mensaje, str)
    assert len(cliente.llamadas) == ia.MAX_INTENTOS


def test_json_truncado_se_reintenta_y_no_se_presenta_como_completo(monkeypatch):
    """
    Una respuesta cortada (`finish_reason="length"`) significa que puede FALTAR el
    final del comprobante. Aceptarla seria mostrar un resultado incompleto como si
    estuviera completo, que es justo lo que el resto del motor nunca hace.
    """
    crudo = imagen_png_falsa()
    cortado = json.dumps(ASIENTO_COMPRA_AL_CONTADO)[:-30]  # sin cerrar el array
    cliente = con_cliente(
        monkeypatch,
        *[respuesta_vision(contenido=cortado, finish_reason="length")
          for _ in range(ia.MAX_INTENTOS)],
    )

    exito, mensaje = ia.analizar_imagen_comprobante(crudo)

    assert exito is False, "una respuesta truncada no debe darse por buena"
    assert mensaje == ia.MENSAJE_IMAGEN_SIN_DATOS
    assert len(cliente.llamadas) == ia.MAX_INTENTOS


def test_reintento_recupera_una_falla_transitoria(monkeypatch):
    """El escáner es la vía más expuesta a fallos de red: un reintento que funciona
    debe bastar para no perder el trabajo del usuario."""
    crudo = imagen_png_falsa()
    cliente = con_cliente(
        monkeypatch,
        respuesta_vision(contenido=""),
        respuesta_vision(contenido=json.dumps(ASIENTO_COMPRA_AL_CONTADO)),
    )

    exito, resultado = ia.analizar_imagen_comprobante(crudo)

    assert exito, f"el segundo intento debia funcionar: {resultado}"
    assert len(cliente.llamadas) == 2
    assert len(resultado) == 1


def test_sin_json_devuelve_error_legible_sin_texto_tecnico(monkeypatch, capsys):
    """Si el modelo responde en prosa (la foto no es un comprobante), el mensaje de
    pantalla NO puede enseñar la respuesta cruda del modelo: eso no es un error del
    sistema, es la respuesta correcta ante una foto de un objeto cualquiera, y el
    usuario solo puede interpretarlo como una aplicacion rota. Debe recibir un texto
    accionable, y el diagnostico queda para la consola.

    Una respuesta sin JSON se reintenta (puede ser una foto demasiado borrosa), asi que
    se encolan MAX_INTENTOS respuestas y se agota la politica de reintentos."""
    crudo = imagen_png_falsa()
    con_cliente(
        monkeypatch,
        *[respuesta_vision(contenido="La foto dice QUISPE PERU SAC") for _ in range(ia.MAX_INTENTOS)],
    )

    exito, mensaje = ia.analizar_imagen_comprobante(crudo)

    assert exito is False
    assert mensaje == ia.MENSAJE_IMAGEN_SIN_DATOS
    assert "QUISPE" not in mensaje, "la respuesta cruda del modelo no debe llegar a la UI"
    assert "Diagnostico" not in mensaje
    # El rastro tecnico NO se pierde: sigue disponible para diagnostico en consola.
    assert "QUISPE" in capsys.readouterr().out


def test_error_de_la_api_se_reporta_sin_tirar_la_excepcion(monkeypatch):
    """La UI espera (False, mensaje): una excepcion sin capturar cortaria la pagina."""
    class ClienteQueFalla(ClienteSimulado):
        def create(self, **parametros):
            raise ConnectionError("la API de Groq no responde")

    monkeypatch.setattr(ia, "cliente", ClienteQueFalla([]))
    monkeypatch.setattr(ia.time, "sleep", lambda _s: None)

    exito, mensaje = ia.analizar_imagen_comprobante(imagen_png_falsa())

    assert exito is False
    assert "no responde" in mensaje


@pytest.mark.parametrize("error_esperado", [
    "Error code: 404 - {'error': {'message': 'The model "
    "`meta-llama/llama-4-scout-17b-16e-instruct` does not exist or you do not have "
    "access to it.', 'type': 'invalid_request_error', 'code': 'model_not_found'}}",
    "Error code: 400 - {'error': {'message': 'The model `llama-3.2-11b-vision-preview` "
    "has been decommissioned and is no longer supported.', 'type': "
    "'invalid_request_error', 'code': 'model_decommissioned'}}",
])
def test_vision_no_habilitada_da_mensaje_accionable_sin_reintentar(monkeypatch, error_esperado):
    """Si la cuenta no tiene vision, NO es un problema de la foto: reintentar gasta
    cuota para repetir el mismo error. Debe cortarse al primer intento y explicar la
    limitacion, sin volcar el error crudo de la API a la pantalla."""
    class ClienteSinVision(ClienteSimulado):
        def create(self, **parametros):
            self.llamadas.append(parametros)
            raise RuntimeError(error_esperado)

    cliente = ClienteSinVision([])
    monkeypatch.setattr(ia, "cliente", cliente)
    monkeypatch.setattr(ia.time, "sleep", lambda _s: None)

    exito, mensaje = ia.analizar_imagen_comprobante(imagen_png_falsa())

    assert exito is False
    assert mensaje == ia.MENSAJE_VISION_NO_HABILITADA
    assert "model_not_found" not in mensaje, "el error crudo no debe llegar a la UI"
    assert "model_decommissioned" not in mensaje
    assert len(cliente.llamadas) == 1, "no debe reintentar: el fallo no es transitorio"


# --------------------------------------------------------------------------- #
# 5. Errores de entrada: se detectan antes de gastar cuota
# --------------------------------------------------------------------------- #
def test_imagen_vacia_falla_sin_llamar_a_la_api(monkeypatch):
    """Cortar una foto puede dejar 0 bytes. Detectarlo aqui evita una peticion
    inútil y, sobre todo, un error de la API que el usuario no puede interpretar."""
    cliente = con_cliente(monkeypatch)  # sin respuestas: si se llama, revienta

    exito, mensaje = ia.analizar_imagen_comprobante(b"")

    assert exito is False
    assert "vacia" in mensaje or "vacío" in mensaje
    assert cliente.llamadas == []


def test_texto_en_vez_de_bytes_falla_sin_llamar_a_la_api(monkeypatch):
    """`read()` con decodificacion entrega str, no bytes: el error debe decirlo."""
    cliente = con_cliente(monkeypatch)

    exito, mensaje = ia.analizar_imagen_comprobante("contenido en texto")

    assert exito is False
    assert "getvalue" in mensaje
    assert cliente.llamadas == []


def test_imagen_demasiado_grande_falla_sin_llamar_a_la_api(monkeypatch):
    """Una foto de movil puede pesar mas que el limite de la API. Se rechaza antes
    de enviar para que el mensaje sea util y no un 413."""
    cliente = con_cliente(monkeypatch)
    gigante = b"\x89PNG\r\n\x1a\n" + b"\x00" * (ia.MAX_BYTES_IMAGEN + 1)

    exito, mensaje = ia.analizar_imagen_comprobante(gigante)

    assert exito is False
    assert "MB" in mensaje
    assert cliente.llamadas == []


# --------------------------------------------------------------------------- #
# 6. Guardarraíl de arquitectura: el escáner no abre puertas propias
# --------------------------------------------------------------------------- #
def test_el_escaner_no_altera_la_logica_de_documentos_ni_de_pdf():
    """
    Estas pruebas fijan el perimetro pedido: `analizar_excel_completo` (y su alias de
    PDF/Word) NO deben contener nada de vision. Si alguien mete un atajo visual
    dentro del flujo de texto, este test avisa antes de que rompa Excel o PDF.
    """
    fuente = (RAIZ / "ia_engine.py").read_text(encoding="utf-8")

    cuerpo_excel = fuente.split("def analizar_excel_completo", 1)[1].split("analizar_documento_completo =", 1)[0]
    for contaminante in ("base64", "image_url", "MODELO_VISION", "camara", "fotos"):
        assert contaminante not in cuerpo_excel, (
            f"analizar_excel_completo no debe conocer el flujo visual ('{contaminante}')"
        )

    # El motor contable es uno solo: no hay una validacion paralela para imagenes.
    assert fuente.count("def procesar_dinamica_contable") == 1
    assert ia.analizar_documento_completo is ia.analizar_excel_completo


def test_el_escaneo_visual_reutiliza_las_constantes_del_motor_textual():
    """Presupuestos y politica de reintentos no se duplican: si el motor textual sube
    MAX_INTENTOS o el tiempo limite, el escáner debe heredar el cambio."""
    fuente = (RAIZ / "ia_engine.py").read_text(encoding="utf-8")
    cuerpo_vision = fuente.split("def _consultar_vision", 1)[1]

    assert "MAX_COMPLETION_TOKENS," in cuerpo_vision
    assert "timeout=TIEMPO_LIMITE_SEGUNDOS" in cuerpo_vision
    assert "range(1, MAX_INTENTOS + 1)" in cuerpo_vision