import os
import json
import re
import io
import time
import datetime
import unicodedata
import base64

from dotenv import load_dotenv
from groq import Groq
from PIL import Image

from database import CATALOGO_PCGE
from logica import naturaleza_de_elemento

# 1. Cargar credenciales
load_dotenv()
cliente = Groq(api_key=os.getenv("GROQ_API_KEY"))

# 2. CONFIGURACIÓN DE LA API
MODELO_IA = "openai/gpt-oss-20b"

# `openai/gpt-oss-20b` es un modelo de RAZONAMIENTO: antes de emitir el JSON final
# quema cientos o miles de tokens en su canal de razonamiento. Con el límite por
# defecto de 2048 tokens ese razonamiento se come TODO el presupuesto de salida y
# la API responde `finish_reason="length"` con `message.content == ""`.
MAX_COMPLETION_TOKENS = 4096
MAX_COMPLETION_TOKENS_REFUERZO = 8192
ESFUERZO_RAZONAMIENTO = "low"  # "low" baja el razonamiento de ~8190 a ~80 tokens

TIEMPO_LIMITE_SEGUNDOS = 90
MAX_INTENTOS = 3
ESPERA_ENTRE_INTENTOS = 4.0

# La cuenta Groq de este proyecto está en el tier gratuito, que tiene un límite de
# 8000 tokens por minuto (TPM).
#
# MAX_CHARS_ENTRADA es el presupuesto de ENTRADA de UNA sola petición, no un techo
# para el documento. Si el documento no cabe, se divide en varios bloques (ver
# `dividir_en_bloques`) y se manda bloque por bloque: recortar el documento a este
# número descartaba operaciones del medio del libro y partía filas por la mitad.
MAX_CHARS_ENTRADA = 9000

# Al procesarse por bloques, varias peticiones seguidas pueden rebasar el TPM. Se
# mide el bloque enviado y se espera lo necesario para no sumar más tokens por
# minuto de los que la cuenta admite. Configurable por entorno para cuentas de pago.
LIMITE_TPM_CUENTA = int(os.getenv("LIMITE_TPM", "8000"))
CARACTERES_POR_TOKEN = 3.5          # estimación conservadora para texto contable en español
ESPERA_MINIMA_ENTRE_BLOQUES = 5.0   # segundos
ESPERA_MAXIMA_ENTRE_BLOQUES = 60.0  # segundos

# CONTRATO DE SALIDA DE LA IA (multi-partida). Es un invariante del sistema, NO una
# heuristica: Python NUNCA deduce que filas forman un mismo asiento. Este texto se
# inyecta en el prompt (ver `instrucciones_agente_excel`), de modo que el contrato
# que el sistema respeta y el contrato que la IA lee son el mismo.
CONTRATO_MULTI_PARTIDA = """
FORMATO DE SALIDA. OBLIGATORIO, sin excepciones. La respuesta es un array JSON y
cada elemento del array es UNA operacion contable independiente, es decir UN ASIENTO,
con TODAS sus partidas dentro de la clave `asiento`:

[
  {
    "fecha": "AAAA-MM-DD",
    "glosa": "Aporte de capital en Efectivo",
    "asiento": [
      {"cuenta": "10", "debe": 10000.0, "haber": 0.0},
      {"cuenta": "50", "debe": 0.0, "haber": 10000.0}
    ]
  }
]

1. Un ASIENTO es una operacion independiente: no se fusiona con otro, no se reordena
   y no se deduplica.
2. Un ASIENTO tiene SIEMPRE la clave `asiento` con la LISTA de sus PARTIDAS (una por
   linea contable de esa operacion), en el orden exacto en que aparecen.
3. Cada PARTIDA tiene SOLO `cuenta`, `debe` y `haber`. La fecha y la glosa NO se
   repiten dentro de las partidas: pertenecen al asiento.
4. Devuelve UN objeto por OPERACION, no un objeto por fila. Las N lineas contables
   que el documento muestra como una sola operacion van dentro del MISMO array
   `asiento` de un unico objeto.
5. Dos operaciones distintas NUNCA se fusionan, aunque compartan fecha, glosa,
   importe o cuentas. Si el documento trae dos operaciones con la misma fecha y la
   misma glosa, son DOS elementos del array, cada uno con su propio `asiento`.
6. Si no puedes determinar con certeza que lineas forman una misma operacion, NO
   inventes la estructura: transcribe solo las lineas que SI leiste, agrupalas
   unicamente si el documento las muestra juntas de forma inequivoca y marca el
   elemento con "revisar": true e "incertidumbres": "<que es dudoso>".
7. NO completes, NO rellenes, NO calcules sumas, NO muevas importes de una partida a
   otra y NO ajustes un descuadre para que la operacion cuadre. Si las partidas no
   cuadran, se devuelven descuadradas y se marcan con "revisar": true.

Un objeto plano con `cuenta`/`debe`/`haber` fuera de `asiento` incumple este
contrato: el sistema conserva la fila tal cual, pero NO deduce a que operacion
pertenece y la marca para revision humana.
""".strip()

# SEGUNDO INVARIANTE DEL SISTEMA: el LADO del importe. Python no lo decide (ver
# `_leer_fila` y `procesar_dinamica_contable`), asi que las reglas que lo deciden tienen
# que estar escritas aqui para que la IA las pueda leer.
#
# Este criterio REEMPLAZA la tabla "clase de cuenta -> columna" que estaba antes. Esa
# tabla produjo tres errores repetidos en la prueba real:
#   1) Cobro de factura con descuento: 70 al HABER por 300, porque "ingresos SIEMPRE al
#      HABER" vencia sobre el efecto del descuento (que REDUCE el ingreso).
#   2) Pago de alquiler y servicios: 10 (caja) al DEBE, porque "activo aumenta en DEBE" se
#      aplico sin preguntarse de que el pago hace DISMINUIR la caja.
#   3) Depreciacion: 39 al DEBE, porque 39 es un activo y el modelo no conocia que es un
#      CONTRA-activo que acumula.
#
# Y despues la sustituyo una segunda forma del mismo error, mas sutil: tratar la NOTACION
# del documento (A+, V-, G+, P+, -A...) como si fuera la columna. En el libro real eso
# produjo el asiento de "Venta al contado y al credito" con las TRES lineas al DEBE
# (12,000 contra 0) y el de "Venta al contado" con 6,000 contra 0, porque las tres lineas
# de la venta llevan "+" y el modelo lo leyo como un cargo. La prueba esta en el propio
# documento: 70 V+, 10 A+ y 12 A+ llevan el mismo "+" y van a columnas distintas.
#
# Por eso la seccion A es la que manda: el LADO sale de una cadena de cuatro pasos,
#   NOTACION -> EFECTO ECONOMICO -> NATURALEZA DE LA CUENTA -> DEBE / HABER
# El signo declara un EFECTO (el concepto que nombra aumenta o disminuye), nunca una
# columna. El signo se cruza con la naturaleza de la cuenta donde aparece, y por eso no
# sirve una equivalencia universal de simbolo a columna: la 39 (contra-activo, acreedora)
# con "-A" sigue yendo al HABER, y la 70 (ingreso, acreedora) con "V-" va al DEBE. Las
# secciones siguientes dan el sentido natural de cada cuenta (util, insuficiente por si
# solo) y el efecto de cada operacion, sin absolutos por clase y sin inventar partidas.
CRITERIO_DEBE_HABER = """
CÓMO DECIDIR EL DEBE Y EL HABER: POR EL EFECTO DE LA OPERACIÓN, NO POR LA CLASE DE LA CUENTA
Una cuenta NO tiene una columna fija: la misma cuenta va al DEBE en unas operaciones y al
HABER en otras. Antes de colocar un importe, identifica en silencio qué operación es (el verbo
de la glosa: comprar, vender, cobrar, pagar, conceder u obtener descuento, devolver, anular,
depreciar, aportar) y qué ocurre con esa cuenta: si AUMENTA, si DISMINUYE, si SE RECONOCE, si
SE CANCELA o si se RECLASIFICA. Si la línea trae notación (A+, V-, G+, -A...), léela
primero (sección A).
Estas reglas solo sirven para colocar en la columna correcta
los importes que el documento ya muestra: NO agregan, NO completan y NO recalculan partidas, y
NO inventes una partida que el documento no muestre.

A. LA NOTACIÓN: EL SIGNO ES UN EFECTO, NO UNA COLUMNA
   El libro no escribe "Debe"/"Haber": escribe una inicial que NOMBRA el concepto (A activo,
   V venta, G gasto, P pasivo) y un signo que dice si AUMENTA o DISMINUYE, delante o detrás
   ("+A", "-A") o con espacio ("A -", "I +"). El signo NO es la columna: en "70 V+ 6,000 / 10
   Caja A+ 3,000 / 12 CxCobrar A+ 3,000" las TRES líneas llevan "+" y van a columnas distintas
   (70 al HABER, 10 y 12 al DEBE): "+" NO significa DEBE ni HABER, significa que aumenta.
   El lado sale de esta cadena, en este orden:
      NOTACIÓN → EFECTO ECONÓMICO → NATURALEZA DE LA CUENTA → DEBE / HABER
   1) cuenta y movimiento 2) notación: signo y concepto 3) efecto: ¿aumenta o disminuye?
   4) cruce con la naturaleza de esa cuenta: si AUMENTA y es deudora (activo, gasto, costo) →
   DEBE; si AUMENTA y es acreedora (ingreso, pasivo, patrimonio, contra-activo) → HABER; si
   DISMINUYE, al revés. Si no basta: "revisar": true e "incertidumbres": "<qué es dudoso>".
   5) Nunca muevas importes, no completes ni inventes partidas ni rebalancees para cuadrar.
   EJEMPLOS del cruce, NO una tabla de equivalencias: A+ activo → DEBE; A- activo → HABER;
   V+ ingreso → HABER; V- ingreso → DEBE; G+ gasto → DEBE; P+ pasivo → HABER; P- pasivo →
   DEBE. El mismo signo cae distinto según la cuenta.
   PROHIBIDO: "V+ = Haber", "V- = Debe", "A+ = Debe" o "A- = Haber" como regla universal,
   "+ = Debe", "- = Haber", y decidir solo por la naturaleza ignorando la notación.
   CASO QUE EXIGE LA NATURALEZA, NO EL SIGNO: "39 Depreciación ACUMULADA -A" lleva "-A" y NO
   se resuelve con "A- → HABER": la 39 es correctora del activo y ACREEDORA, así que 68 queda
   al DEBE y 39 al HABER. La notación declara el efecto y se cruza con la naturaleza.

B. SENTIDO NATURAL DE CADA CUENTA (base, no suficiente por sí sola)
   - Activo que la operación AUMENTA: DEBE. El mismo activo cuando la operación lo DISMINUYE:
     HABER. Por eso una caja (10) que entra va al DEBE y una caja que SALE va al HABER.
   - Pasivo o patrimonio que AUMENTA: HABER. Cuando la operación lo disminuye (lo paga, lo
     cancela o lo devuelve): DEBE.
   - Cuentas de contra-activo (19, 29, 36, 39): son acreedoras, ACUMULAN contra el activo y
     van al HABER. Que su código empiece por 3 no las vuelve deudoras.
   - Gasto o costo: DEBE cuando la operación lo RECONOCE; HABER cuando la operación lo CANCELA
     (devolución, anulación, corrección, reversión).
   - Ingreso: HABER cuando la operación lo RECONOCE; DEBE cuando la operación lo REDUCE por su
     propia cuenta (descuento concedido, devolución, anulación).
   Estas reglas indican el sentido natural de cada cuenta, pero NO bastan por sí solas: si la
   operación cancela un gasto o reduce un ingreso, esas cuentas van al lado contrario.

C. EFECTO DE LA OPERACIÓN (esto es lo que decide la columna)
   - COMPRA: lo que se compra (20, 24, 30, 33, un gasto) va al DEBE; lo que se paga o se
     financia (10, 42, 45) va al HABER.
   - VENTA: lo que se entrega (10, 12, 20) va al DEBE y el ingreso bruto de la factura al HABER.
   - COBRO de cuenta por cobrar (12): entra efectivo al DEBE y la cuenta por cobrar DISMINUYE al
     HABER por el total de la factura, no solo por lo que se recibe en efectivo.
   - PAGO de un gasto, un servicio, un alquiler o un tributo: el gasto que la operación reconoce
     va al DEBE y la salida de efectivo va al HABER de la caja (10). Si es el pago de una
     factura ya reconocida, lo que se cancela (el pasivo o el gasto) va al DEBE y la caja al
     HABER. Nunca pongas la caja en el DEBE porque "es un activo": en un pago la caja disminuye.
   - DESCUENTO CONCEDIDO al comprador: NO es un ingreso, reduce el ingreso, así que la línea del
     descuento va al DEBE. Si el documento dice en qué cuenta va, transcribe esa; si no la dice,
     usa descuentos concedidos (74). Si el descuento lo otorga el PROVEEDOR es descuentos
     obtenidos (73) y entonces AUMENTA el ingreso: va al HABER. Si no sabes si fue concedido u
     obtenido, no lo deduzcas por la clase de la cuenta: deja la línea como está y marca el
     elemento con "revisar": true.
   - DEPRECIACIÓN: el gasto del periodo (68) va al DEBE y la depreciación acumulada (39) va al
     HABER, porque acumula contra el activo.
   - APORTE DE CAPITAL: lo que aporta el socio (10, 20, 30, 33) va al DEBE y el capital o las
     acciones (50, 51) van al HABER.
   - DEVOLUCIÓN o ANULACIÓN: se invierte el efecto original, aunque las cuentas sean de ingresos
     o de gastos.

D. PARTIDA DOBLE
   Comprueba con los importes que escribiste que la suma del DEBE sea igual a la suma del HABER. En
   un cobro con descuento los totales igualan aunque no cuadren partida por partida. NO muevas, NO
   ajustes y NO completes importes de una partida a otra para cuadrar: si no cuadra sin moverlos,
   devuelve el asiento descuadrado y márcalo con "revisar": true.

E. DÓNDE ESTÁ ESCRITO EL DEBE Y EL HABER: SEPARA EL SIGNO DE LA COLUMNA DEL IMPORTE
   Son dos datos distintos del documento y no se les puede aplicar lo mismo.
   - EL SIGNO nunca es una columna: un "CARGO / ABONO" arriba del libro NO convierte un
     "+" en cargo ni un "-" en abono, y la sección A manda igual sobre las líneas que
     traen notación. No saques el lado de un "+" o de un "-".
   - LA COLUMNA DEL IMPORTE sí es una columna. Las tablas llegan serializadas como
     "descripción | celda | celda | celda": una celda VACÍA intercalada no es un importe,
     pero la CUENTA, y el importe se lee en la columna en la que está escrito. Esa columna
     es la del rótulo que la encabeza, y los rótulos de un libro se leen de izquierda a
     derecha, en el MISMO orden en que están sus columnas.
   - CALIBRA LAS COLUMNAS antes de fijar el lado, y calibra con el documento, no con la
     clase de la cuenta. Los rótulos pueden estar en otra página del mismo libro: si los
     tienes a la vista, sigue su orden de izquierda a derecha hasta las columnas de
     importe. Si NO hay rótulo a la vista, deduce la correspondencia con las filas cuya
     notación NO admite duda, y solo con esas: un ACTIVO que AUMENTA (10 Caja A+, 20
     Inventarios A+) lleva su importe en la columna del DEBE, y un ACTIVO que DISMINUYE o
     un INGRESO que AUMENTA (20 Inventarios A-, 70 Ingresos V+) lo llevan en la del HABER.
     NO uses "todo lo que aumenta va al DEBE": un ingreso que aumenta va al HABER, igual
     que una contra-activo que acumula. Con esa correspondencia fijada, aplícala a TODAS
     las filas de la página aunque su notación parezca contradecirla: una línea
     "39 ... -A" cuyo importe está en la columna que ya calibraste como HABER va al HABER.
     El signo explica el efecto; la columna explica el lado.
   - Si no puedes ver el rótulo NI calibrar las columnas, decide por el efecto de la
     operación cruzado con la naturaleza de la cuenta (secciones A, B y C), nunca por la
     clase de la cuenta ni por la sangría ni por el código.

F. INCERTIDUMBRE
   Si dudas de la composición del asiento o del lado de alguna partida, NO lo resuelvas suponiendo
   que una clase va siempre al DEBE o siempre al HABER: devuelve lo que leíste y marca el elemento
   con "revisar": true e "incertidumbres": "<qué es dudoso>".
""".strip()


def _soporta_reasoning_effort():
    """El SDK no siempre expone `reasoning_effort`; si falta, se degrada sin romper."""
    try:
        import inspect
        return "reasoning_effort" in inspect.signature(cliente.chat.completions.create).parameters
    except Exception:
        return False


_SOPORTA_RAZONAMIENTO = _soporta_reasoning_effort()


# 3. Prompt Engineering de alto nivel
instrucciones_contador = """
Eres un contador público experto en el Plan Contable General Empresarial (PCGE) de Perú.
Tu tarea es leer un enunciado contable, interpretar la naturaleza de la transacción y extraer la partida doble exacta.
Un enunciado es UN solo asiento, así que tu respuesta es la LISTA de sus PARTIDAS: un array JSON de objetos con las claves 'cuenta' (código de 2 dígitos como string), 'debe' (float) y 'haber' (float). Es la MISMA lista que va dentro de la clave 'asiento' de un documento.
No uses formato Markdown, no saludes, no des explicaciones. Solo devuelve el texto plano del JSON.
Analiza en silencio: identifica la operación y decide el lado de cada importe con el criterio de abajo, sin escribir razonamientos.
Si el enunciado no permite deducir la partida doble, responde con una lista vacía: [].

""" + CRITERIO_DEBE_HABER + """

En este flujo la incertidumbre NO se marca con "revisar": true (el contrato de salida es
solo la lista de partidas): si dudas del lado, de la composición de una partida o de si el
asiento cuadra, responde con una lista vacía [] en vez de un asiento dudoso.
Ejemplo de entrada: Se compra mercadería al contado por 50000.
Ejemplo de salida: [{"cuenta": "20", "debe": 50000.0, "haber": 0.0}, {"cuenta": "10", "debe": 0.0, "haber": 50000.0}]
"""

# La IA transcribe la ESTRUCTURA; el LADO de cada importe lo interpreta. Son dos decisiones
# distintas y por eso viven en dos bloques: CONTRATO_MULTI_PARTIDA (una operacion = un
# asiento con sus N partidas) y CRITERIO_DEBE_HABER (que efecto tiene la operacion sobre
# cada cuenta). El prompt se mantiene corto y sin catálogo PCGE, porque cada instrucción
# extra (catálogo, cálculos) consumía el presupuesto de razonamiento de gpt-oss-20b y
# provocaba que se omitieran filas para "hacer cuadrar" los asientos.
#
# OJO: el prompt anterior pedia "un objeto por FILA contable" con cuenta/debe/haber
# sueltos y prohibia agrupar. Eso convertia cada partida de una operacion en un
# asiento independiente: 31 filas planeas llegaban como 31 asientos descuadrados. El
# texto de abajo invierte la instruccion: un objeto por OPERACION y sus partidas
# dentro de `asiento`.
instrucciones_agente_excel = """
Eres un experto en dinámica contable del Plan Contable General Empresarial (PCGE) de Perú.

Tu salida es un array JSON. Cada elemento es UNA operación contable del documento,
es decir UN ASIENTO, con todas sus partidas dentro de la clave `asiento`. Transcribes
la estructura y los importes que están escritos; el lado (Debe/Haber) de cada importe
lo decides interpretando la operación, con las reglas de más abajo.

""" + CONTRATO_MULTI_PARTIDA + """

""" + CRITERIO_DEBE_HABER + """

Si una línea del documento no aporta cuenta o importe, transcríbela con 0.0 y marca el
elemento con "revisar": true. Si una operación aparece cortada o ilegible, transcribe
solo lo legible y márcala con "revisar": true. Es mejor un asiento marcado para revisión
humana que una estructura inventada.


Responde ÚNICAMENTE con el array JSON, sin markdown, sin explicaciones, sin comentarios
y sin texto antes o después.
"""

# --------------------------------------------------------------------------- #
# EXCEPCION SOLO PARA HOJAS DE CALCULO
# --------------------------------------------------------------------------- #
# Este bloque NO forma parte de `instrucciones_agente_excel`, que sigue congelada y se
# entrega byte a byte al PDF, al Word, al dictado y al escaner visual. Se concatena al
# final unicamente cuando el documento es un Excel (`_prompt_para_origen`).
#
# Que corrige
# -----------
# Una hoja de calculo academica trae, junto a las operaciones, una frase que OBSERVA un
# saldo en vez de describir un movimiento economico: "en el inventario se observa un
# saldo final de 10,000 soles al cierre de mes". Esa frase no es una operacion, pero si
# es informacion necesaria para determinar una operacion que la hoja NO escribe. Antes,
# al transcribirla como si fuera un asiento, el libro llevaba esa economia DOS veces.
#
# Que NO cambia
# -------------
# El prompt base sigue mandando sobre todo lo demas: las operaciones escritas se
# transcriben tal cual, con su fecha, su glosa y sus partidas, sin mover un importe.
BLOQUE_EXCEL_DERIVACION = """

EXCEPCION UNICA A LA REGLA 7, Y SOLO EN HOJAS DE CALCULO
========================================================

Este bloque sustituye a la regla 7 ("no deduzcas operaciones") unicamente para el
documento que acabas de leer y unicamente para el caso de abajo. En un PDF, un Word o
un dictado la regla 7 se mantiene intacta.

1. UN SALDO QUE SE OBSERVA NO ES UNA OPERACION.
   Si una frase del documento OBSERVA un saldo en lugar de describir un movimiento
   economico ("en el inventario se observa un saldo final de 10,000 soles al cierre de
   mes", "el saldo final de mercaderias es 10,000"), esa frase NO es una operacion
   contable: no la devuelvas dentro del array de operaciones y no le inventes partidas,
   aunque mencione cuentas o importes. Un saldo es el resultado de lo que ya ocurrio;
   transcribirlo como asiento repetiria en el libro una economia que ya esta escrita en
   las operaciones de arriba.

2. ESE CONTEXTO SE DECLARA EN UNA CLAVE HERMANA.
   Lo que si es CONTEXTO del caso se devuelve FUERA del array, en un objeto con dos
   claves:

   {"contexto": [{"tipo": "inventario_final", "importe": 10000}],
    "operaciones": [ ... el array de operaciones, exactamente igual que siempre ... ]}

   - "tipo" solo puede ser "inventario_inicial" o "inventario_final".
   - "importe" es el numero tal como esta escrito en el documento.
   - Si la frase que describe el saldo trae una fecha inequivoca ("cierre del
     31/07/2020"), copiala tal cual en "fecha". Si no la trae, NO escribas "fecha": no
     inventes una.
   - Si el documento no trae ningun saldo que sirva para esto, responde solo el array de
     operaciones, como hasta ahora.

3. EL SISTEMA DERIVA LO QUE FALTA, NO TU.
   Cuando declaras un "inventario_final", el sistema calcula por su cuenta la salida de
   mercaderias del periodo (inventario inicial + compras - inventario final) y arma el
   asiento de costo que la hoja no escribe. Tu no escribes ese asiento, ni sus cuentas,
   ni su fecha: no los deduzcas ni los copies de ninguna parte.

4. LO QUE SIGUE VALIENDO IGUAL.
   - Las operaciones escritas se transcriben completas, con su fecha, su glosa y sus
     partidas, sin cambiar ni un importe.
   - "revisar": true e "incertidumbres" siguen siendo la forma de declarar que algo no
     esta claro.
   - No inventes operaciones, fechas ni cuentas que el documento no escriba.
"""

# Solo las hojas de calculo reciben el bloque de arriba. El nombre del archivo ya llega
# en minusculas desde app.py, pero se normaliza aqui para que el selector no dependa de
# quien lo llame.
ORIGENES_EXCEL = ("xlsx", "xls")


def _prompt_para_origen(origen):
    """
    Elige el prompt del extractor segun de donde venga el documento.

    `instrucciones_agente_excel` es el prompt congelado y se entrega tal cual a PDF,
    Word, dictado y escaner visual. Solo una hoja de calculo recibe, anadido al final,
    `BLOQUE_EXCEL_DERIVACION`: el bloque se apoya en las reglas del prompt base (por eso
    define una excepcion a la regla 7 y no la reemplaza), de modo que tiene que ir
    DESPUES, nunca antes.

    Un origen desconocido o ausente recibe el prompt base: ante la duda, no se añade
    ningun bloque.
    """
    if str(origen or "").strip().lower() in ORIGENES_EXCEL:
        return instrucciones_agente_excel + BLOQUE_EXCEL_DERIVACION
    return instrucciones_agente_excel

# 4. SANITIZACIÓN DE TEXTO (capa defensiva: la principal vive en app.py)
_ESPACIOS_EXCESIVOS = re.compile(r"[ \t]{6,}")
_SALTOS_EN_BLOQUE = re.compile(r"\n{3,}")
_CARACTERES_OCULTOS = re.compile("[\u00ad\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060\ufeff]")


def limpiar_texto_documento(texto):
    """
    Deja el texto en un formato que la API tolera: fuera el byte NUL y todo el
    bloque de caracteres de control.
    """
    if texto is None:
        return ""
    if not isinstance(texto, str):
        texto = str(texto)

    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = unicodedata.normalize("NFKC", texto)
    texto = _CARACTERES_OCULTOS.sub("", texto)
    texto = "".join(c for c in texto if c in "\n\t" or unicodedata.category(c) != "Cc")
    texto = texto.replace("\ufffd", "")  # glifos rotos por la codificación del PDF

    lineas = [_ESPACIOS_EXCESIVOS.sub("      ", ln).rstrip() for ln in texto.split("\n")]
    return _SALTOS_EN_BLOQUE.sub("\n\n", "\n".join(lineas)).strip()


def dividir_en_bloques(texto, max_chars=MAX_CHARS_ENTRADA):
    """
    Divide el documento en bloques de como mucho `max_chars` caracteres SIN cortar
    ninguna linea por la mitad y sin descartar nada.

    Por que no se recorta cabeza/cola: en un libro contable las operaciones viven a
    lo largo de todo el documento, no en los extremos. Un recorte de cabeza/cola
    (el metodo anterior) eliminaba de golpe todo el tramo central y ademas partia
    filas al borde del corte, produciendo operaciones con la glosa o el importe
    cortados a media palabra. Un recorte NUNCA es una operacion incompleta.

    Por que se corta por lineas: una linea es la unidad fisica que ya entrega la
    extraccion (una fila de tabla = una linea "celda | celda | celda"). Partir solo
    entre lineas garantiza que cada operacion llega completa al modelo y que el
    orden del documento se conserva al concatenar los resultados.

    Caso limite: si una sola linea supera el presupuesto se envia igual, entera,
    en su propio bloque. Antes se partia por la mitad, lo que si fabricaba una
    operacion incompleta.
    """
    if not texto:
        return []

    bloques, actual, largo_actual = [], [], 0
    for linea in texto.split("\n"):
        costo = len(linea) + 1
        if actual and largo_actual + costo > max_chars:
            bloques.append("\n".join(actual))
            actual, largo_actual = [], 0
        actual.append(linea)
        largo_actual += costo
    if actual:
        bloques.append("\n".join(actual))
    return bloques


def _espera_entre_bloques(contenido_bloque):
    """Pausa necesaria para no exceder el TPM de la cuenta entre dos peticiones."""
    tokens = int(len(contenido_bloque) / CARACTERES_POR_TOKEN)
    tokens += int(len(instrucciones_agente_excel) / CARACTERES_POR_TOKEN) + 200
    segundos = 60.0 * tokens / max(1, LIMITE_TPM_CUENTA)
    return max(ESPERA_MINIMA_ENTRE_BLOQUES, min(ESPERA_MAXIMA_ENTRE_BLOQUES, segundos))


# Etiqueta de pagina que app.py coloca antes del contenido de cada pagina del PDF.
_ENCABEZADO_PAGINA = re.compile(r"^=== PAGINA \d+ ===$")


def _etiquetas_de_pagina(texto, bloques):
    """
    Devuelve, para cada bloque, la etiqueta de pagina vigente en su primera linea.

    Los bloques son trozos contiguos de lineas del documento, asi que la etiqueta se
    localiza contando las lineas de los bloques anteriores. NO agrupa ni reordena
    operaciones: solo restituye el nombre de la pagina a la que pertenece cada
    bloque cuando el corte cayo en mitad de una pagina.
    """
    pagina_por_linea, vigente = {}, None
    for indice, linea in enumerate(texto.split("\n")):
        if _ENCABEZADO_PAGINA.match(linea.strip()):
            vigente = linea.strip()
        pagina_por_linea[indice] = vigente

    etiquetas, cursor = [], 0
    for bloque in bloques:
        etiquetas.append(pagina_por_linea.get(cursor))
        cursor += len(bloque.split("\n"))
    return etiquetas


def _preparar_bloque(bloque, indice, total, etiqueta_pagina=None):
    """
    Anade al bloque su contexto de pagina y su posicion en el documento.

    La etiqueta de pagina NO agrupa ni reordena nada: solo restituye el nombre de
    la pagina a la que pertenecen esas lineas. El numero de bloque le dice al
    modelo que solo puede transcribir las filas escritas en ese bloque.
    """
    lineas = bloque.split("\n")
    if etiqueta_pagina and not _ENCABEZADO_PAGINA.match(lineas[0].strip()):
        lineas.insert(0, etiqueta_pagina)
    cuerpo = "\n".join(lineas)

    if total > 1:
        return (
            f"BLOQUE {indice} DE {total} DEL DOCUMENTO\n"
            "Transcribe UNICAMENTE las operaciones contables escritas en este bloque, "
            "respetando el formato del contrato: un objeto por operacion, con todas sus "
            "partidas dentro de la clave `asiento`. No copies, completes ni deduzcas "
            "operaciones que no esten escritas aqui. Si el corte del bloque deja una "
            "operacion a medias, transcribe solo las partidas visibles y marca ese "
            "elemento con \"revisar\": true.\n\n"
            f"Contenido del documento:\n{cuerpo}"
        )
    return f"Contenido del documento:\n{cuerpo}"


# 5. LECTURA Y REPARACIÓN DE LA RESPUESTA DE LA IA
_MARCAS_HARMONY = re.compile(r"<\|[^|>]*\|>")  # gpt-oss emite <|channel|>final<|message|>
_BLOQUE_CODIGO = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


class ResultadoIA(list):
    """Lista de operaciones con avisos de validación adjuntos (sigue siendo una list)."""

    def __init__(self, operaciones, avisos, contexto=None):
        super().__init__(operaciones)
        self.avisos = avisos
        # Informacion que el documento OBSERVA pero no escribe como operacion (un saldo
        # final de inventario). Solo la declara la hoja de calculo; en el resto de
        # origenes queda vacia y no se usa.
        self.contexto = list(contexto or [])


def _quitar_marcas_ia(texto):
    """Elimina los metadatos de canal de gpt-oss y las vallas de Markdown."""
    texto = _MARCAS_HARMONY.sub("", texto or "")
    bloques = _BLOQUE_CODIGO.findall(texto)
    if bloques:
        texto = max(bloques, key=len)  # el modelo a veces razonó en prosa dentro de un bloque
    return texto.strip()


def _cargar_json(texto):
    if not texto:
        return None
    try:
        return json.loads(texto)
    except (json.JSONDecodeError, ValueError):
        return None


def _cerrar_json_incompleto(texto):
    """
    Si la respuesta se cortó a mitad (finish_reason="length"), cierra los corchetes
    y llaves que quedaron abiertos para poder recuperar los asientos ya emitidos.
    """
    pila, en_cadena, escape = [], False, False
    for caracter in texto:
        if escape:
            escape = False
            continue
        if caracter == "\\" and en_cadena:
            escape = True
            continue
        if caracter == '"':
            en_cadena = not en_cadena
        elif not en_cadena and caracter in "{[":
            pila.append(caracter)
        elif not en_cadena and caracter in "}]":
            if pila:
                pila.pop()

    if not pila:
        return None
    reparado = texto.rstrip()
    while reparado.endswith(","):  # una coma colgante no es JSON válido
        reparado = reparado[:-1]
    return reparado + "".join("]" if c == "[" else "}" for c in reversed(pila))


def _extraer_json(respuesta):
    """
    Caza el JSON válido dentro de la respuesta. Devuelve (objeto, error, truncado).
    `truncado` es True cuando el JSON solo pudo recuperarse cerrando una estructura
    incompleta: puede faltarle el final de las operaciones y no debe presentarse
    como respuesta completa.
    """
    texto = _quitar_marcas_ia(respuesta)
    if not texto:
        return None, "La IA devolvió una respuesta completamente vacía.", False

    candidatos = []
    bloque = re.search(r"\[[\s\S]*\]", texto)
    if bloque:
        candidatos.append((bloque.group(0), False))
    reparado = _cerrar_json_incompleto(texto)
    if reparado:
        candidatos.append((reparado, True))
    candidatos.append((texto, False))

    for candidato, truncado in candidatos:
        objeto = _cargar_json(candidato)
        if objeto is not None:
            return objeto, None, truncado
    return None, texto[:600], False


def _leer_contenido(respuesta):
    """Devuelve (contenido, finish_reason, rastro_de_razonamiento). Nunca lanza si content es None."""
    eleccion = respuesta.choices[0]
    mensaje = eleccion.message

    contenido = getattr(mensaje, "content", None)
    if contenido is None:
        contenido = ""
    elif not isinstance(contenido, str):
        try:
            contenido = json.dumps(contenido, ensure_ascii=False)
        except (TypeError, ValueError):
            contenido = str(contenido)
    contenido = contenido.strip()

    if contenido:
        return contenido, eleccion.finish_reason, ""
    
    razonamiento = getattr(mensaje, "reasoning", "") or ""
    if not isinstance(razonamiento, str):
        razonamiento = str(razonamiento)
    razonamiento = razonamiento.strip()
    if razonamiento:
        return razonamiento, eleccion.finish_reason, razonamiento[-400:]
    return "", eleccion.finish_reason, ""


def _es_error_de_tamano_o_ritmo(error):
    """HTTP 413 / 429: el documento no cabe en la ventana de 8000 TPM o llega demasiado rápido."""
    texto = str(error).lower()
    codigo = getattr(error, "status_code", None)
    if codigo in (413, 429):
        return True
    return "413" in texto or "429" in texto or "rate_limit" in texto or "too large" in texto


# --------------------------------------------------------------------------- #
# EL 429 DE GROQ SON DOS COSAS DISTINTAS
# --------------------------------------------------------------------------- #
# El mismo código HTTP 429 significa dos cosas que no se pueden tratar igual, y este
# archivo las trataba como una sola:
#
#   - TPD (tokens por DÍA): la cuenta agotó su cuota diaria. No se recupera reintentando
#     ni esperando 4 u 8 segundos. Groq lo dice con `x-should-retry: false` y un
#     `retry-after` de minutos, y el mensaje trae el límite, lo usado y lo solicitado.
#   - TPM (tokens por MINUTO): la petición llegó en ráfaga. Sí se resuelve esperando y
#     reintentando, que es lo que el mecanismo de reintentos siempre quiso cubrir.
#
# Confundirlas costaba 16 segundos de esperas inútiles y un diagnóstico que decía
# "(413/429)", dejando a quien lee el error sin saber que el problema era la cuota del día.
# La API sí dice cuál es; este código es el que no lo escuchaba.
_MARCA_CUOTA_DIARIA = re.compile(r"tokens per day|\bTPD\b", re.IGNORECASE)
_CIFRAS_CUOTA = re.compile(r"(Limit|Used|Requested)\s*(\d[\d,]*)", re.IGNORECASE)
_ESPERA_SUGERIDA = re.compile(
    r"try again in\s+((?:\d+h)?(?:\d+m)?(?:\d+(?:[.,]\d+)?s)?)", re.IGNORECASE
)

# Techo de la espera que se puede tomar de un `retry-after`. El reintento vive dentro de
# una petición de Streamlit: sin este tope, un `retry-after` de 20 minutos dejaría la
# interfaz colgada y sin poder cancelarse.
ESPERA_MAXIMA_POR_RETRY_AFTER = 60.0


def _texto_del_error(error):
    """Mensaje del error como lo devuelve el SDK, incluyendo el cuerpo de la respuesta."""
    partes = [str(error)]
    cuerpo = getattr(error, "body", None)
    if cuerpo:
        partes.append(cuerpo if isinstance(cuerpo, str) else json.dumps(cuerpo, ensure_ascii=False))
    return "\n".join(partes)


def _es_error_de_cuota_diaria(error):
    """True solo ante un 429 de tokens por DÍA (TPD): agotar la cuota no se reintenta."""
    if getattr(error, "status_code", None) != 429:
        return False
    return bool(_MARCA_CUOTA_DIARIA.search(_texto_del_error(error)))


def _duracion_a_segundos(texto):
    """Convierte la duración que escribe Groq ("20m13.056s") en segundos."""
    encontrado = _ESPERA_SUGERIDA.search(texto or "")
    if not encontrado:
        return None
    horas = re.search(r"(\d+)h", encontrado.group(1))
    minutos = re.search(r"(\d+)m", encontrado.group(1))
    segundos = re.search(r"(\d+(?:[.,]\d+)?)s", encontrado.group(1))
    if not (horas or minutos or segundos):
        return None
    total = 0.0
    if horas:
        total += int(horas.group(1)) * 3600
    if minutos:
        total += int(minutos.group(1)) * 60
    if segundos:
        total += float(segundos.group(1).replace(",", "."))
    return total


def _segundos_de_retry_after(error):
    """Cuánto sugiere la API esperar: primero su cabecera `retry-after`, luego su mensaje."""
    cabeceras = getattr(getattr(error, "response", None), "headers", None) or {}
    for clave in ("retry-after", "Retry-After"):
        valor = cabeceras.get(clave)
        if valor in (None, ""):
            continue
        try:
            return float(str(valor).strip())
        except (TypeError, ValueError):
            continue
    return _duracion_a_segundos(_texto_del_error(error))


def _duracion_legible(segundos):
    """20m13s, para que el mensaje se lea sin tener que mentalizar 1213.056."""
    segundos = int(round(segundos))
    if segundos < 60:
        return f"{segundos}s"
    if segundos < 3600:
        return f"{segundos // 60}m {segundos % 60}s" if segundos % 60 else f"{segundos // 60}m"
    return f"{segundos // 3600}h {(segundos % 3600) // 60}m"


def _informe_de_cuota_diaria(error):
    """Explica el 429 de TPD con los números que la propia API envía."""
    texto = _texto_del_error(error)
    cifras = {}
    for clave, valor in _CIFRAS_CUOTA.findall(texto):
        cifras.setdefault(clave.lower(), int(valor.replace(",", "")))

    espera = _segundos_de_retry_after(error)
    detalles = []
    if "limit" in cifras:
        detalles.append(f"límite {cifras['limit']:,} tokens/día")
    if "used" in cifras:
        detalles.append(f"usados {cifras['used']:,}")
    if "requested" in cifras:
        detalles.append(f"solicitados {cifras['requested']:,}")
    if espera is not None:
        detalles.append(f"la cuota se repone en {_duracion_legible(espera)}")

    return (
        "se alcanzó la CUOTA DIARIA de tokens de Groq (TPD"
        + (": " + ", ".join(detalles) if detalles else "")
        + "). No se reintenta porque con la cuota del día agotada los otros intentos "
        "serían rechazados igual; vuelve a intentarlo cuando se reponga."
    )


def _espera_antes_de_reintentar(error, intento):
    """
    Espera previa a reintentar un 413/429 transitorio.

    Conserva la espera por intento que ya existía y solo la sustituye cuando la API
    indica cuánto falta (`retry-after`), que es más fiable que un valor fijo. Se acota
    con `ESPERA_MAXIMA_POR_RETRY_AFTER` para no bloquear la interfaz.
    """
    predeterminada = ESPERA_ENTRE_INTENTOS * intento
    sugerida = _segundos_de_retry_after(error)
    if sugerida is None:
        return predeterminada
    return max(0.0, min(sugerida, ESPERA_MAXIMA_POR_RETRY_AFTER))


def _es_error_de_modelo_no_disponible(error):
    """HTTP 404 model_not_found / HTTP 400 model_decommissioned.

    El 404 de Groq es ambiguo a proposito: "does not exist **or you do not have access
    to it**". Las dos causas son el mismo problema desde el punto de vista del
    usuario (no hay vision habilitada en la cuenta) y, sobre todo, NO son un fallo
    de la foto: reintentar es gastar cuota para obtener el mismo error. Por eso se
    corta el reintento y se responde con un mensaje que dice que hacer.
    """
    texto = str(error).lower()
    if "model_not_found" in texto or "model_decommissioned" in texto:
        return True
    if getattr(error, "status_code", None) == 404 and "model" in texto:
        return True
    return "decommissioned" in texto or "does not exist" in texto


# 6. NORMALIZACIÓN DEL JSON CONTABLE
def _a_numero(valor):
    """Tolera '50,000.00', '1.234,56', 'S/ 50000' y paréntesis de negativo."""
    if valor is None or isinstance(valor, bool):
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)

    texto = str(valor).strip()
    if not texto:
        return 0.0
    negativo = texto.startswith("(") and texto.endswith(")")
    texto = re.sub(r"[^\d,.\-]", "", texto)
    if not texto:
        return 0.0

    if "," in texto and "." in texto:
        texto = (texto.replace(".", "").replace(",", ".")
                 if texto.rfind(",") > texto.rfind(".") else texto.replace(",", ""))
    elif "," in texto:
        texto = texto.replace(",", ".")

    try:
        numero = float(texto)
    except ValueError:
        return 0.0
    return -numero if negativo else numero


# Meses en letras para fechas dichas o escritas como "8 de julio de 2020" /
# "8 de julio del 2020". El dictado por voz transcribe la fecha tal como se
# habla, y un PDF puede traerla igual: la normalizacion es UNA sola para todos
# los origenes, y este mapa solo ACEPTA formatos que antes se descartaban.
_MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_FECHA_EN_LETRAS = re.compile(
    r"^(\d{1,2})\s+de\s+([a-záéíóú]+)(?:\s+(?:de|del)\s+|\s+)(\d{4})$",
    re.IGNORECASE,
)


def _a_fecha(valor):
    """
    Devuelve la fecha en ISO o "" si falta o no se puede interpretar. Nunca
    sustituye por la fecha de hoy: una fecha ausente queda ausente y se avisa
    para revision humana.

    Ademas de los formatos numericos (AAAA-MM-DD, dd/mm/AAAA, dd-mm-AAAA,
    yyyy/mm/dd, dd.mm.AAAA) acepta la fecha en letras del dictado:
    "8 de julio de 2020" y "8 de julio del 2020".
    """
    if not valor:
        return ""
    texto = str(valor).strip()
    for patron in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(texto, patron).date().isoformat()
        except ValueError:
            continue
    en_letras = _FECHA_EN_LETRAS.match(texto)
    if en_letras:
        dia, mes_txt, anio = en_letras.groups()
        mes = _MESES_ES.get(_sin_acentos(mes_txt))
        if mes:
            try:
                return datetime.date(int(anio), mes, int(dia)).isoformat()
            except ValueError:
                return ""
    return ""


def _a_cuenta(valor):
    codigo = re.sub(r"[^\d]", "", str(valor if valor is not None else ""))
    return codigo.zfill(2) if codigo else ""


# Tolerancia de céntimos para comparar Debes y Haberes (los importes se truncan a 2).
TOLERANCIA_CENTIMOS = 0.01

# Ruido de OCR en glosas y codigos: comillas, pipes de la serializacion de tablas
# y encabezados de columna pegados al texto.
_CARACTERES_NO_PALABRA = re.compile(r"[|_/\\*#=~^<>]+")
_INDICE_DE_FILA = re.compile(r"^\s*(?:\(?\d{1,3}[\)\.\-])\s+")


def normalizar_descripcion(valor, largo=120):
    """
    Limpia glosas y nombres de cuenta: unifica espacios, quita el separador de
    tabla que inyecta la extraccion de PDF y descarta el indice de fila que
    el OCR suele pegarle al principio del texto.
    """
    if valor is None or isinstance(valor, bool):
        return ""
    texto = unicodedata.normalize("NFKC", str(valor))
    texto = _CARACTERES_OCULTOS.sub("", texto)
    texto = texto.replace("\ufffd", "")
    texto = _CARACTERES_NO_PALABRA.sub(" ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = _INDICE_DE_FILA.sub("", texto).strip()
    return texto[:largo]


def _primer_valor(fila, claves):
    for clave in claves:
        if clave in fila and fila[clave] not in (None, ""):
            return fila[clave]
    return None


def _leer_fila(fila):
    """
    Convierte una fila cruda de la IA en un movimiento contable.

    Contrato actual: la IA decide Debe/Haber por el EFECTO de la operacion sobre cada
    cuenta (ver CRITERIO_DEBE_HABER), no por la clase de la cuenta. Aqui solo se tipan los
    campos cuenta/debe/haber: NO se interpreta posicion espacial, NO se usa
    monto_izquierdo/monto_derecho, NO se infiere el lado del importe y NO se invierte una
    columna por la clase o el elemento PCGE de la cuenta.
    """
    return {
        "cuenta": _a_cuenta(fila.get("cuenta")),
        "debe": _a_numero(fila.get("debe")),
        "haber": _a_numero(fila.get("haber")),
    }


def _sin_acentos(texto):
    """Minúsculas sin tildes: para comparar descripciones del catálogo sin depender de cómo se escribió."""
    return "".join(
        caracter for caracter in unicodedata.normalize("NFKD", str(texto or ""))
        if not unicodedata.combining(caracter)
    ).lower()


def _es_envoltorio(objeto):
    """
    Un dict es envoltorio cuando declara, como listas, las operaciones que OBSERVA el
    documento (su transcribe) o el contexto que declara aparte. `context` aparece como
    variante de `contexto` en respuestas reales de la API y se lee igual.
    """
    return (
        isinstance(objeto, dict)
        and (
            isinstance(objeto.get("operaciones"), list)
            or isinstance(objeto.get("contexto"), list)
            or isinstance(objeto.get("context"), list)
        )
    )


def _separar_contexto(lote):
    """
    Separa el CONTEXTO que declara la IA del ARRAY DE OPERACIONES. Devuelve
    (operaciones, contexto).

    El caso normal es que la IA responda con el array plano, como siempre, y entonces el
    contexto es vacío: el resultado es idéntico al de antes. Una hoja de cálculo puede
    responder con un objeto {"contexto": [...], "operaciones": [...]}, y ahí se separan
    las dos mitades para que el contexto nunca llegue a `procesar_dinamica_contable` como
    si fuera una operación.

    El envoltorio aparece en tres formas que son el mismo contrato: desnudo, como único
    elemento de un array, o anidado dentro de un array junto a las operaciones planas. En
    los tres casos el contenido autoritativo es el del envoltorio: la API real duplica las
    operaciones dentro de él, así que si un array trae un envoltorio, sus `operaciones` y
    su `contexto` valen por el array completo y los elementos planos se descartan para no
    escribir dos veces la misma economía.

    Con la clave "contexto" ausente, un dict desnudo es una operación y un array plano se
    devuelve tal cual. No reordena, no agrupa y no completa nada.
    """
    if isinstance(lote, dict):
        if _es_envoltorio(lote):
            return _partes_del_envoltorio(lote)
        return [lote], []
    if isinstance(lote, list):
        envoltorios = [e for e in lote if _es_envoltorio(e)]
        if envoltorios:
            operaciones = []
            contexto = []
            for envoltorio in envoltorios:
                ops, ctx = _partes_del_envoltorio(envoltorio)
                operaciones.extend(ops)
                contexto.extend(ctx)
            return operaciones, contexto
        return [e for e in lote if isinstance(e, dict)], []
    return [], []


def _partes_del_envoltorio(objeto):
    """
    Separa el envoltorio {"contexto": [...], "operaciones": [...]} en sus dos mitades.
    Cada mitad se devuelve tal como la IA la escribió (solo diccionarios).
    """
    contexto = objeto.get("contexto")
    if contexto is None:
        contexto = objeto.get("context")
    return (
        [e for e in (objeto.get("operaciones") or []) if isinstance(e, dict)],
        [e for e in (contexto or []) if isinstance(e, dict)],
    )


def procesar_dinamica_contable(lote):
    """
    Nucleo contable del sistema. Recibe el JSON que devuelve la IA y devuelve
    (ResultadoIA|None, avisos, error).

    Aplica el CONTRATO_MULTI_PARTIDA sin excepciones:
      - Si un elemento trae la lista `asiento`, se conserva tal cual: es un asiento
        con esas partidas, en ese orden. NO se reagrupa, NO se reordena, NO se deduplica.
      - Si un elemento llega como fila plana (cuenta/debe/haber sin `asiento`) incumple
        el contrato: la fila se conserva integra, pero NO se deduce a que operacion
        pertenece y el asiento queda marcado `revisar` para revision humana. Perder la
        fila o fusionarla con otra seria peor que mostrarlo pendiente de revisar.
      - NO se agrupa por fecha, glosa, cercania, proximidad, importe ni cuenta: no se
        infiere estructura que la IA no haya entregado.
      - Si un elemento no aporta una cuenta y un importe utilizables, se OMITE y se
        avisa: no se inventa la fila que falta. Excepcion: si el propio elemento venia
        marcado con `revisar`, la linea se conserva con 0.00 para que el hueco quede
        visible en la revision humana.

    En codigo solo se tipan los valores, se normalizan fecha/cuenta/glosa y se
    valida la partida doble. NO se mueven importes, NO se corrige la asignacion
    Debe/Haber y NO se invierte una columna por la clase de la cuenta: un asiento mal
    interpretado se conserva tal cual y se reporta con `revisar`, para que la revision
    humana decida.
    """
    # La IA puede envolver el array en una clave o devolver un dict suelto.
    if isinstance(lote, dict):
        for clave in ("registros", "filas", "asientos", "operaciones", "resultado", "datos", "items"):
            if isinstance(lote.get(clave), list):
                lote = lote[clave]
                break
        else:
            lote = [lote]
    if not isinstance(lote, list) or not lote:
        return None, [], "El extractor no devolvio ninguna fila contable del documento."

    avisos = []
    asientos = []
    for indice, elemento in enumerate(lote, start=1):
        if not isinstance(elemento, dict):
            avisos.append(f"El elemento #{indice} vino con un formato inesperado y fue omitido.")
            continue

        # La fecha no se sustituye por hoy: si falta o no se entiende, queda vacia.
        fecha = _a_fecha(elemento.get("fecha"))
        if not fecha:
            avisos.append(
                f"El elemento #{indice} no tiene una fecha interpretable: se dejo vacia para revision humana."
            )

        glosa = normalizar_descripcion(_primer_valor(elemento, ("glosa", "concepto", "descripcion")))

        # La IA puede declarar que su lectura es insegura. Se respeta tal cual.
        revisar = bool(elemento.get("revisar"))
        incertidumbre = elemento.get("incertidumbres") or elemento.get("incertidumbre")
        if incertidumbre and not isinstance(incertidumbre, str):
            incertidumbre = str(incertidumbre)
        if isinstance(incertidumbre, str):
            incertidumbre = normalizar_descripcion(incertidumbre)
        else:
            incertidumbre = ""
        if incertidumbre:
            avisos.append(
                f"Asiento #{indice} ('{glosa or 'sin glosa'}') quedó marcado para revisión: "
                f"{incertidumbre}"
            )

        # Estructura anidada explicita: se conserva sin reagrupar ni inferir.
        if "asiento" in elemento and not isinstance(elemento.get("asiento"), list):
            avisos.append(
                f"El elemento #{indice} trae la clave 'asiento' que no es una lista de partidas. "
                "No se deduce su contenido y solo se lee lo que la fila declare por separado."
            )
        if isinstance(elemento.get("asiento"), list):
            movimientos = []
            for subindice, partida in enumerate(elemento["asiento"], start=1):
                if not isinstance(partida, dict):
                    avisos.append(
                        f"La partida #{subindice} del asiento #{indice} vino con un formato inesperado y fue omitida."
                    )
                    continue
                movimiento = _leer_fila(partida)
                if not movimiento["cuenta"]:
                    avisos.append(
                        f"La partida #{subindice} del asiento #{indice} no tiene cuenta reconocible y fue omitida."
                    )
                    continue
                if not movimiento["debe"] and not movimiento["haber"]:
                    if not revisar:
                        avisos.append(
                            f"La partida #{subindice} del asiento #{indice} (cuenta {movimiento['cuenta']}) "
                            "no tiene importe y fue omitida."
                        )
                        continue
                    # La IA declaro que su lectura es insegura: la linea se conserva
                    # con 0.00 para que la revision humana vea el hueco, no se tira.
                    avisos.append(
                        f"La partida #{subindice} del asiento #{indice} (cuenta {movimiento['cuenta']}) "
                        "llegó sin importe y la IA la marcó para revisión: se conserva con 0.00."
                    )
                movimientos.append(movimiento)
            if not movimientos:
                avisos.append(f"El asiento #{indice} no trajo ninguna partida utilizable y fue omitido.")
                continue
            # Una partida repetida fuera de `asiento` NO se agrega: podría duplicar el
            # importe del asiento. Se conserva la estructura declarada y se avisa.
            suelta = _leer_fila(elemento)
            if suelta["cuenta"] and (suelta["debe"] or suelta["haber"]):
                revisar = True
                if not incertidumbre:
                    incertidumbre = (
                        "llego con una partida suelta (cuenta/debe/haber) ademas de la lista "
                        "'asiento'; no se agrego para no duplicar importes"
                    )
                avisos.append(
                    f"El asiento #{indice} trae una partida suelta (cuenta/debe/haber) fuera de la "
                    f"clave 'asiento' y no se agrego: el sistema no inventa ni duplica partidas. "
                    "Revisa si esa fila es una partida real del asiento."
                )
        else:
            # Fila plana: llega SIN la lista `asiento`, o sea sin estructura declarada.
            # Se conserva integra (no se descarta ningun dato) pero NO se infiere a que
            # operacion pertenece: se marca para revision humana en lugar de presentarla
            # como un asiento completo.
            movimiento = _leer_fila(elemento)
            if not movimiento["cuenta"]:
                avisos.append(f"El elemento #{indice} no tiene cuenta reconocible y fue omitido.")
                continue
            if not movimiento["debe"] and not movimiento["haber"] and not revisar:
                avisos.append(f"El elemento #{indice} (cuenta {movimiento['cuenta']}) no tiene importe y fue omitido.")
                continue
            movimientos = [movimiento]
            revisar = True
            if not incertidumbre:
                incertidumbre = (
                    "llego como fila plana (cuenta/debe/haber) sin la lista 'asiento' de partidas: "
                    "su composicion real es desconocida y el sistema no la deduce"
                )
            avisos.append(
                f"El elemento #{indice} (cuenta {movimiento['cuenta']}) llegó sin la clave "
                "'asiento': se recibe como una sola partida, no como una operacion con N "
                "partidas. Se conserva tal cual y queda para revision humana. No se agrupa "
                "con ninguna otra fila."
            )

        asientos.append({
            "fecha": fecha,
            "glosa": glosa,
            "revisar": revisar,
            "incertidumbres": incertidumbre,
            "movimientos": movimientos,
        })

    if not asientos:
        return None, avisos, "Ninguna fila trajo una cuenta y un importe utilizables."

    # Validacion de partida doble. No se corrige nada: solo se reporta.
    operaciones = []
    for indice, asiento in enumerate(asientos, start=1):
        movimientos = asiento["movimientos"]

        debe = round(sum(m["debe"] for m in movimientos), 2)
        haber = round(sum(m["haber"] for m in movimientos), 2)
        if abs(debe - haber) > TOLERANCIA_CENTIMOS:
            etiqueta = f"Asiento #{indice} ('{asiento['glosa'] or 'sin glosa'}')"
            avisos.append(
                f"{etiqueta} no cuadra: Debe S/ {debe:,.2f} vs Haber S/ {haber:,.2f} "
                f"(diferencia S/ {debe - haber:,.2f}). Revisa las columnas en el borrador."
            )
            # El descuadre tambien es una incertidumbre: se marca, no se corrige.
            asiento["revisar"] = True
            if not asiento["incertidumbres"]:
                asiento["incertidumbres"] = (
                    f"descuadre de S/ {debe - haber:,.2f} en la partida doble"
                )

        operacion = {
            "fecha": asiento["fecha"],
            "glosa": asiento["glosa"] or "Asiento sin glosa",
            "razonamiento": "",
            "asiento": [{"cuenta": m["cuenta"], "debe": m["debe"], "haber": m["haber"]}
                        for m in movimientos],
        }
        # Solo los asientos con incertidumbre llevan las claves de marca: el resto
        # mantiene exactamente la forma que consume el borrador.
        if asiento["revisar"]:
            operacion["revisar"] = True
            operacion["incertidumbres"] = asiento["incertidumbres"]
        operaciones.append(operacion)

    return ResultadoIA(operaciones, avisos), avisos, None


# --------------------------------------------------------------------------- #
# 6.b CONTEXTO QUE PERMITE DERIVAR UNA OPERACION (solo hojas de calculo)
# --------------------------------------------------------------------------- #
# Que es CONTEXTO y que es OPERACION
# ---------------------------------
# Una operacion contable describe un movimiento economico: algo que pasa y que por eso
# tiene doble partida. Un saldo que el documento OBSERVA ("se observa un saldo final de
# 10,000 al cierre de mes") no describe ningun movimiento: es el residuo de los
# movimientos ya escritos. Meterlo en el libro lo repetiria.
#
# Pero un saldo no es inutil: junto con las compras del periodo determina el costo de las
# mercaderias que salieron del almacen. Ese costo SI es una operacion (la mercaderia
# existe y se consume), y por eso se deriva como asiento, no como frase.
#
# Lo que NO se hace aqui
# ----------------------
# No hay ningun codigo de cuenta escrito a mano. Las cuentas se leen del catalogo PCGE
# por lo que representan: la cuenta de mercaderias (un ACTIVO, elemento 2, que se
# descarga) y la cuenta de costo de ventas (un RESULTADO, elemento 6, que lo recibe).
# "69 al DEBE y 20 al HABER" no es una regla de este sistema, es el resultado de leer en
# el plan de cuentas que papel tiene cada una.
_ELEMENTO_INVENTARIO = 2
_ELEMENTO_COSTOS = 6


def _cuenta_por_naturaleza(elemento, patron_descripcion):
    """
    Localiza en el catálogo PCGE la cuenta que representa `patron_descripcion` dentro
    del elemento `elemento` del plan de cuentas. Devuelve el código, o None si el
    catálogo no describe ninguna cuenta con ese papel.

    Se busca por lo que la cuenta REPRESENTA, no por un código escrito a mano: el mismo
    criterio funciona si el ejercicio usa otra cuenta de mercaderías u otra de costo de
    ventas dentro del mismo elemento.
    """
    for codigo, descripcion, elemento_de_la_cuenta in CATALOGO_PCGE:
        if elemento_de_la_cuenta != elemento:
            continue
        if patron_descripcion in _sin_acentos(descripcion):
            return codigo
    return None


# ============================================================================
# NORMALIZACION DETERMINISTA DE LAS CUENTAS INFERIDAS EN UNA HOJA DE CALCULO
# ============================================================================
# Una hoja de calculo del caso solo trae fecha y glosa: la cuenta de cada economia la
# INFIERE la IA y, en la practica, la acierta hasta la compra y la falla en los gastos
# (escribe una cuenta de ingresos, activo o patrimonio donde va un gasto) y a veces
# invierte una venta. Esta capa, SOLO activa para Excel, valida las cuentas inferidas
# contra el plan de cuentas del ejercicio y corrige únicamente lo que tiene seguro: que
# la glosa dice un gasto de un subtipo y la IA le puso al DEBE una cuenta de otra clase.
# Lo que no se puede determinar (cuenta en el lado equivocado, asiento sin la cuenta que
# la economia pide) no se inventa: queda marcado para revision humana.
#
# La regla Debe/Haber es la MISMA de todo el sistema, `logica.naturaleza_de_elemento`:
# un gasto (elemento 6) es deudora y su aumento va al DEBE; una venta (elemento 7) es
# acreedora y va al HABER. No se corrige, completa ni invierte nada sobre PDF, voz o
# scanner: esos documentos traen sus cuentas escritas y solo la hoja las infiere.
_CONCEPTOS_EXCEL = (
    # (etiqueta, elemento, patron del catalogo, remapear, palabras que lo delatan)
    ("gastos financieros", 6, "gastos financieros", True,
     (("gastos", "financieros"), ("gasto", "financiero"), ("interes",), ("intereses",))),
    ("gastos de personal", 6, "gastos de personal y directores", True,
     (("gastos", "personal"), ("gasto", "personal"), ("sueldo",), ("sueldos",),
      ("remuneracion",), ("remuneraciones",))),
    ("gastos por tributos", 6, "gastos por tributos", True,
     (("gastos", "tributos"), ("gasto", "tributo"), ("impuesto",), ("impuestos",), ("igv",))),
    # "gastos" sin apellido: los operativos del caso. A falta de mas precision se leen
    # como "gastos de servicios prestados por terceros", la cuenta de gasto generico.
    ("gastos", 6, "servicios prestados por terceros", True,
     (("gastos",), ("pago", "gastos"), ("gasto",))),
    # Mercaderia comprada y venta: el modelo las acierta, la capa solo VALIDA su forma.
    ("mercaderias", 2, "mercaderia", False,
     (("mercaderia",), ("mercaderias",))),
    ("venta", 7, "ventas", False,
     (("venta",), ("ventas",), ("vendida",), ("vendidas",))),
)


def _concepto_de_glosa(glosa):
    """
    Identifica la economia que describe la glosa por PALABRAS COMPLETAS (sin acentos, en
    minusculas), sin reglas de substring: "gastos" no se confunde con "gastaron" ni "pago"
    con "pagar". Devuelve la entrada de `_CONCEPTOS_EXCEL` que cuadre, o None.
    """
    palabras = set(re.findall(r"[a-z0-9]+", _sin_acentos(glosa)))
    if not palabras:
        return None
    for concepto in _CONCEPTOS_EXCEL:
        for requerida in concepto[4]:
            if all(palabra in palabras for palabra in requerida):
                return concepto
    return None


def _normalizar_cuentas_excel(operaciones):
    """
    Valida y, solo en el caso seguro, corrije las cuentas que la IA infirio en una hoja
    de calculo. Devuelve los avisos. Modifica en su lugar los dicts de `operaciones`:
    la cuenta inferida se reemplaza por la cuenta del plan de cuentas solo cuando la
    glosa declara el concepto y la partida que lo sustituye esta en el lado correcto
    segun `naturaleza_de_elemento`; todo asiento tocado o no determinable se marca
    `revisar`. Avisos y glosas son legibles por el humano que aprueba el borrador.
    """
    avisos = []
    for indice, operacion in enumerate(operaciones, start=1):
        glosa = operacion.get("glosa")
        concepto = _concepto_de_glosa(glosa)
        if concepto is None:
            continue
        asiento = operacion.get("asiento")
        if not isinstance(asiento, list) or not asiento:
            continue

        etiqueta, elemento, patron, remapear, _ = concepto
        cuenta_esperada = _cuenta_por_naturaleza(elemento, patron)
        if cuenta_esperada is None:
            avisos.append(
                f"Asiento #{indice} ('{glosa}'): el plan de cuentas no describe la cuenta de "
                f"{etiqueta}: se conserva lo que infirio la IA y queda para revision."
            )
            operacion["revisar"] = True
            continue

        lado = "debe" if naturaleza_de_elemento(elemento) == "deudora" else "haber"
        perfil = [
            (_a_cuenta(partida.get("cuenta")), _a_numero(partida.get(lado)) or 0.0)
            for partida in asiento
        ]

        # La cuenta que la economia pide ya esta donde toca: la IA la acerto.
        if any(cuenta == cuenta_esperada and importe > 0 for cuenta, importe in perfil):
            continue

        # Quien podria ocupar el lugar de la cuenta correcta: una partida con importe en
        # el lado que la economia exige y un codigo distinto al esperado.
        candidatas = [p for p in perfil if p[1] > 0 and p[0] != cuenta_esperada]

        if remapear and len(candidatas) == 1:
            cuenta_vieja = candidatas[0][0]
            for partida in asiento:
                if (
                    _a_cuenta(partida.get("cuenta")) == cuenta_vieja
                    and (_a_numero(partida.get(lado)) or 0.0) > 0
                ):
                    partida["cuenta"] = cuenta_esperada
                    break
            operacion["revisar"] = True
            avisos.append(
                f"Asiento #{indice} ('{glosa}'): la IA infirio la cuenta {cuenta_vieja} en el "
                f"{lado.upper()} de '{etiqueta}' y el sistema la reemplazo por la {cuenta_esperada} "
                "del plan de cuentas. Queda marcado para revision."
            )
        else:
            operacion["revisar"] = True
            avisos.append(
                f"Asiento #{indice} ('{glosa}'): no se puede confirmar que la cuenta infirida "
                f"corresponda a '{etiqueta}': queda marcado para revision humana."
            )
    return avisos


def derivar_asientos_de_contexto(contexto, operaciones):
    """
    Arma la operación contable que el documento OBSERVA pero no escribe, a partir del
    CONTEXTO que declaró la hoja de cálculo. Devuelve (asientos_derivados, avisos).

    El único caso que cubre es el del ciclo de mercaderías:

        COSTO DE VENTAS = inventario inicial + compras - inventario final

    Las compras no se leen del contexto: son las que el documento ya escribió, y se toman
    del DEBE de la cuenta de mercaderías en las operaciones transcritas. El inventario
    final viene del contexto. El inventario inicial, si el documento no lo declara, se
    asume 0.00 y el supuesto queda escrito en `incertidumbres`: es un dato que no está en
    el documento y quien revise el borrador tiene que poder verlo.

    Si falta un dato necesario NO se inventa: se avisa y no se deriva nada. El asiento que
    sí sale llega siempre con "revisar": true, porque no está escrito en el documento y
    lo decidió el sistema, no el documento.
    """
    avisos = []
    declarados = {"inventario_inicial": [], "inventario_final": []}
    fecha_contexto = ""

    for entrada in contexto or []:
        tipo = _sin_acentos(entrada.get("tipo"))
        if tipo not in declarados:
            avisos.append(
                f"El contexto declara el tipo {entrada.get('tipo')!r}, que no se usa para derivar "
                "asientos: se descarta como información, no como operación."
            )
            continue
        valor = _a_numero(entrada.get("importe"))
        if valor is None:
            avisos.append(
                f"El contexto de tipo '{entrada.get('tipo')}' no trae un importe utilizable: se ignora."
            )
            continue
        declarados[tipo].append(valor)
        fecha_contexto = fecha_contexto or _a_fecha(entrada.get("fecha"))

    if not declarados["inventario_final"]:
        return [], avisos

    for tipo in ("inventario_inicial", "inventario_final"):
        if len(declarados[tipo]) > 1:
            avisos.append(f"El documento declara varios saldos de {tipo.replace('_', ' ')}: se suman.")

    inventario_final = round(sum(declarados["inventario_final"]), 2)

    cuenta_mercaderias = (
        _cuenta_por_naturaleza(_ELEMENTO_INVENTARIO, "mercaderia")
        or _cuenta_por_naturaleza(_ELEMENTO_INVENTARIO, "existencias")
    )
    if cuenta_mercaderias is None:
        avisos.append("El catálogo PCGE no describe una cuenta de mercaderías: no se deriva el costo.")
        return [], avisos

    cuenta_costo = _cuenta_por_naturaleza(_ELEMENTO_COSTOS, "costo de ventas")
    if cuenta_costo is None:
        avisos.append("El catálogo PCGE no describe una cuenta de costo de ventas: no se deriva el asiento.")
        return [], avisos

    compras = 0.0
    for operacion in operaciones or []:
        for movimiento in operacion.get("asiento") or []:
            if _a_cuenta(movimiento.get("cuenta")) == cuenta_mercaderias:
                compras += _a_numero(movimiento.get("debe")) or 0.0
    compras = round(compras, 2)

    if compras <= 0:
        avisos.append(
            f"Las operaciones transcritas no cargan nada a la cuenta {cuenta_mercaderias}: sin compras "
            "del periodo no hay costo de ventas que derivar."
        )
        return [], avisos

    if declarados["inventario_inicial"]:
        inventario_inicial = round(sum(declarados["inventario_inicial"]), 2)
    else:
        inventario_inicial = 0.0

    costo = round(inventario_inicial + compras - inventario_final, 2)
    if costo <= 0:
        avisos.append(
            f"El inventario del periodo (inicial {inventario_inicial:,.2f} + compras {compras:,.2f}) no supera "
            f"el inventario final ({inventario_final:,.2f}): no sale mercadería y no se deriva asiento."
        )
        return [], avisos

    incertidumbres = [
        "el documento no escribe este asiento: el sistema lo derivó del inventario que el documento observa"
    ]
    if not declarados["inventario_inicial"]:
        incertidumbres.append(
            "el documento no declara el inventario inicial del periodo; se asumió 0.00"
        )
    if not fecha_contexto:
        incertidumbres.append(
            "el documento no da una fecha de cierre inequívoca: complete la fecha antes de aprobar"
        )

    derived = {
        "fecha": fecha_contexto,
        "glosa": (
            "DERIVADO / REVISAR: costo de ventas por salida de mercadería "
            f"(inv. inicial {inventario_inicial:,.2f} + compras {compras:,.2f} "
            f"- inv. final {inventario_final:,.2f})"
        ),
        "razonamiento": "",
        "revisar": True,
        "incertidumbres": " | ".join(incertidumbres),
        "asiento": [
            {"cuenta": cuenta_costo, "debe": costo, "haber": 0.0},
            {"cuenta": cuenta_mercaderias, "debe": 0.0, "haber": costo},
        ],
    }

    avisos.append(
        f"Asiento DERIVADO del saldo final de inventario: {cuenta_costo} al DEBE por {costo:,.2f} y "
        f"{cuenta_mercaderias} al HABER por {costo:,.2f}. Queda marcado para revisión porque no está "
        "escrito en el documento."
    )
    return [derived], avisos


# 7. LLAMADA A LA API CON REINTENTOS
def _consultar_agente(contenido_documento, max_completion_tokens, prompt=None):
    parametros = {
        "model": MODELO_IA,
        "temperature": 0.1,
        "max_completion_tokens": max_completion_tokens,
        "timeout": TIEMPO_LIMITE_SEGUNDOS,
        "include_reasoning": False,
    }
    if _SOPORTA_RAZONAMIENTO:
        parametros["reasoning_effort"] = ESFUERZO_RAZONAMIENTO

    return cliente.chat.completions.create(
        messages=[
            {"role": "system", "content": prompt if prompt else instrucciones_agente_excel},
            {"role": "user", "content": f"Contenido del documento:\n{contenido_documento}"},
        ],
        **parametros,
    )


def _procesar_bloque(contenido_bloque, indice, total, traza, prompt=None):
    """
    Procesa UN bloque y devuelve su ResultadoIA, o None si el bloque no se pudo
    leer completo.

    Una respuesta truncada (finish_reason="length" o JSON cerrado a la fuerza) se
    reintenta con mas presupuesto de salida y, si vuelve a cortarse, el bloque se
    declara fallido. No se acepta un bloque incompleto: presentarlo como si
    estuviera completo ocultaria operaciones ausentes.
    """
    for intento in range(1, MAX_INTENTOS + 1):
        if intento > 1:
            time.sleep(ESPERA_ENTRE_INTENTOS)

        presupuesto = MAX_COMPLETION_TOKENS if intento == 1 else MAX_COMPLETION_TOKENS_REFUERZO

        try:
            respuesta = _consultar_agente(contenido_bloque, presupuesto, prompt)
        except Exception as error:
            if _es_error_de_cuota_diaria(error):
                # La cuota de tokens del día está agotada: los intentos 2 y 3 serían
                # rechazados con el mismo 429. Se corta aquí, sin esperas, y se dice por qué.
                traza.append(f"Bloque {indice}/{total}, intento {intento}: {_informe_de_cuota_diaria(error)}")
                return None
            if _es_error_de_tamano_o_ritmo(error):
                espera = _espera_antes_de_reintentar(error, intento)
                traza.append(
                    f"Bloque {indice}/{total}, intento {intento}: la API rechazó el tamaño o el ritmo "
                    f"(HTTP {getattr(error, 'status_code', '413/429')}); se reintenta en {espera:.0f} s."
                )
                time.sleep(espera)
                continue
            traza.append(f"Bloque {indice}/{total}, intento {intento}: error de la API: {error}")
            return None

        contenido, finish_reason, razonamiento = _leer_contenido(respuesta)
        if not contenido:
            traza.append(
                f"Bloque {indice}/{total}, intento {intento}: respuesta vacia (finish_reason={finish_reason})."
                + (f" Rastro de razonamiento: {razonamiento[-160:]}" if razonamiento else "")
            )
            continue

        lote, error_json, json_truncado = _extraer_json(contenido)
        if lote is None:
            traza.append(
                f"Bloque {indice}/{total}, intento {intento}: sin JSON utilizable. Respuesta: {error_json[:200]}"
            )
            continue

        if finish_reason == "length" or json_truncado:
            traza.append(
                f"Bloque {indice}/{total}, intento {intento}: la respuesta se corto antes de escribir "
                f"todas las filas (finish_reason={finish_reason}). Se reintenta con mas presupuesto."
            )
            continue

        operaciones, contexto = _separar_contexto(lote)

        if not operaciones:
            # Un bloque que solo aporta contexto no tiene nada que validar. Se conserva
            # el contexto y no se inventa un asiento para que el bloque "pase".
            if contexto:
                return ResultadoIA([], [], contexto)
            traza.append(f"Bloque {indice}/{total}: sin operaciones utilizables.")
            return None

        resultado, avisos, error_normalizacion = procesar_dinamica_contable(operaciones)
        if resultado is None:
            detalle = " | ".join(avisos[:4]) if avisos else ""
            traza.append(
                f"Bloque {indice}/{total}, intento {intento}: {error_normalizacion}"
                + (f" Detalle: {detalle}" if detalle else "")
            )
            # Una respuesta que no trae ninguna fila utilizable es, casi siempre, una
            # lectura fortuita: el mismo bloque devuelve filas legibles en el intento
            # siguiente. No se declara fallido el bloque hasta agotar los reintentos,
            # igual que con una respuesta truncada o sin JSON.
            continue
        if contexto:
            resultado.contexto.extend(contexto)
        return resultado

    return None


def analizar_excel_completo(texto_crudo_excel, origen=None):
    """
    Envía el documento (Excel, PDF o Word) al extractor y devuelve (True, ResultadoIA)
    con los asientos ya normalizados y validados en Python, o (False, mensaje de error).
    La IA transcribe la estructura y decide el Debe/Haber por el efecto de cada operación
    (CRITERIO_DEBE_HABER); Python solo tipa y valida que Debe = Haber.

    El documento entra COMPLETO. Si excede MAX_CHARS_ENTRADA se divide en bloques
    por lineas (`dividir_en_bloques`) y se procesa bloque por bloque: no se
    descarta ningun contenido, cada operacion llega entera y el orden del
    documento se conserva al concatenar los bloques en el orden en que se leyeron.

    `origen` es la extension del archivo ("xlsx", "pdf", "docx") y decide el prompt:
    solo una hoja de calculo recibe `BLOQUE_EXCEL_DERIVACION`. Con origen ausente o
    desconocido se usa el prompt base.

    Cuando la hoja de calculo declara CONTEXTO (un saldo que el documento observa pero no
    escribe), el asiento que ese contexto permite determinar lo arma `derivar_asientos_de_contexto`
    aqui, en Python y no la IA, y llega marcado para revision. Esa frase nunca se
    transcribe como operacion.
    """
    documento = limpiar_texto_documento(texto_crudo_excel)
    if not documento:
        return False, (
            "No se pudo extraer texto legible del documento. Si el PDF es una "
            "fotografía o escaneo, necesita OCR previo: usa la pestaña de lectura "
            "del enunciado o pega el texto."
        )

    es_excel = str(origen or "").strip().lower() in ORIGENES_EXCEL
    prompt = _prompt_para_origen(origen)

    bloques = dividir_en_bloques(documento)
    total_bloques = len(bloques)
    etiquetas = _etiquetas_de_pagina(documento, bloques)
    traza = [
        f"Documento de {len(documento):,} caracteres en {total_bloques} bloque(s) de hasta "
        f"{MAX_CHARS_ENTRADA:,} caracteres. No se omite ninguna linea."
    ]

    operaciones, avisos, contexto = [], [], []
    for indice, bloque in enumerate(bloques, start=1):
        contenido_bloque = _preparar_bloque(bloque, indice, total_bloques, etiquetas[indice - 1])
        resultado = _procesar_bloque(contenido_bloque, indice, total_bloques, traza, prompt)
        if resultado is None:
            return False, (
                f"El bloque {indice} de {total_bloques} no se pudo leer completo. No se devuelve un "
                "resultado parcial a propósito: mostrar solo una parte del documento haría pasar por "
                "completo un libro al que le faltan operaciones.\n\nDiagnóstico: " + " | ".join(traza)
            )

        operaciones.extend(resultado)
        contexto.extend(getattr(resultado, "contexto", []) or [])
        avisos.extend(
            f"[Bloque {indice}/{total_bloques}] {aviso}"
            for aviso in getattr(resultado, "avisos", [])
        )

        if indice < total_bloques:
            time.sleep(_espera_entre_bloques(contenido_bloque))

    # Solo una hoja de calculo infiere cuentas (PDF, voz y scanner las traen escritas):
    # se validan contra el plan de cuentas antes de derivar, para que el asiento derivado
    # parta de las cuentas ya corregidas.
    if es_excel:
        avisos.extend(_normalizar_cuentas_excel(operaciones))

    if es_excel and contexto:
        derivados, avisos_derivacion = derivar_asientos_de_contexto(contexto, operaciones)
        operaciones.extend(derivados)
        avisos.extend(avisos_derivacion)

    if not operaciones:
        return False, (
            "El extractor no devolvió operaciones utilizables tras "
            f"{MAX_INTENTOS} intentos.\n\nDiagnóstico: " + " | ".join(traza)
        )

    if total_bloques > 1:
        avisos.append(
            f"El documento se procesó en {total_bloques} bloques por límite de caracteres de la API. "
            "No se descartó ninguna línea, pero un asiento que quede partido entre dos bloques puede "
            "aparecer momentáneamente como descuadrado: confírmalo contra el documento original."
        )

    return True, ResultadoIA(operaciones, avisos, contexto)


# Alias con nombre acorde al nuevo flujo multipropósito (Excel, PDF y Word).
analizar_documento_completo = analizar_excel_completo


def extraer_asiento_de_texto(enunciado):
    """
    Envía el enunciado crudo a la IA, y devuelve (True, asiento) con la partida doble.
    Aplica la misma defensa contra respuestas vacías y contenido corrupto.
    """
    enunciado = limpiar_texto_documento(enunciado)
    if not enunciado:
        return False, "El enunciado está vacío."

    finish_reason = "sin respuesta"
    for intento in range(1, MAX_INTENTOS + 1):
        if intento > 1:
            time.sleep(ESPERA_ENTRE_INTENTOS)

        parametros = {
            "model": MODELO_IA,
            "temperature": 0.1,
            "max_completion_tokens": MAX_COMPLETION_TOKENS if intento == 1 else MAX_COMPLETION_TOKENS_REFUERZO,
            "timeout": TIEMPO_LIMITE_SEGUNDOS,
            "include_reasoning": False,
        }
        if _SOPORTA_RAZONAMIENTO:
            parametros["reasoning_effort"] = ESFUERZO_RAZONAMIENTO

        try:
            respuesta = cliente.chat.completions.create(
                messages=[
                    {"role": "system", "content": instrucciones_contador},
                    {"role": "user", "content": f"Enunciado a analizar: {enunciado}"},
                ],
                **parametros,
            )
        except Exception as error:
            if _es_error_de_cuota_diaria(error):
                return False, f"Error de conexión con la IA de Groq: {_informe_de_cuota_diaria(error)}"
            if _es_error_de_tamano_o_ritmo(error):
                time.sleep(_espera_antes_de_reintentar(error, intento))
                continue
            return False, f"Error de conexión con la IA de Groq: {error}"

        contenido, finish_reason, _ = _leer_contenido(respuesta)
        if not contenido:
            continue

        asiento, _, _ = _extraer_json(contenido)
        if isinstance(asiento, dict):
            asiento = [asiento]
        if isinstance(asiento, list) and asiento:
            return True, asiento
        if isinstance(asiento, list):  # lista vacía: el modelo no dedujo nada
            return False, "La IA no pudo deducir la partida doble de ese enunciado."

    return False, (
        "La IA devolvió una respuesta vacía o ilegible tras varios intentos "
        f"(finish_reason={finish_reason}). Prueba a reformular el enunciado."
    )


# --------------------------------------------------------------------------- #
# 7.b DICTADO POR VOZ (misma tuberia que un documento)
#
# Antes el dictado usaba `extraer_asiento_de_texto`, cuyo contrato pedia SOLO la
# lista plana de partidas (cuenta/debe/haber). Ese contrato no trae ni `fecha` ni
# `glosa` ni la estructura multi-operacion, y de ahi salian los tres fallos
# observados:
#   1) la fecha nunca llegaba: app.py leia `resultado[0]["fecha"]`, clave que el
#      contrato de partidas no contempla, y el borrador quedaba sin fecha aunque
#      el enunciado la traia escrita;
#   2) la glosa se armaba con los primeros 45 caracteres de la transcripcion cruda
#      (fecha e importe incluidos), no con la descripcion de la operacion;
#   3) una grabacion con varias transacciones se analizaba con un prompt que
#      declara "UN solo asiento", asi que el modelo devolia todas las partidas
#      fusionadas en un unico array (o un [] que cortaba el flujo).
#
# La correccion NO inventa un camino nuevo: el dictado sube por el MISMO pipeline
# de documentos (`analizar_documento_completo` -> CONTRATO_MULTI_PARTIDA ->
# `_extraer_json` -> `procesar_dinamica_contable`), de modo que una transcripcion
# es un documento de texto y hereda multi-operacion, division por bloques,
# reintentos, normalizacion de fecha y validacion de partida doble.
#
# Sobre lo que Python decide AQUI y no la IA:
#   - FECHA: solo se conserva una fecha que este ESCRITA en la transcripcion
#     (`fechas_en_texto`). Si la IA escribe una fecha que no aparece en el
#     dictado, es una invencion y se vacia para revision humana. No se rellena
#     con hoy ni con ningun valor supuesto.
#   - GLOSA: se deriva de forma DETERMINISTA de la propia oracion transcrita
#     (`glosa_desde_transcripcion`), quitando fecha, importes, moneda, muletillas
#     y preposiciones colgantes, y conservando solo la descripcion de la
#     operacion. No depende de que la IA redacte ni invente una glosa.
# --------------------------------------------------------------------------- #

# Numeros que la transcripcion escribe como fecha: 8.07.2020, 08/07/2020,
# 08-07-2020 y 2020-07-08. El patron exige DOS separadores, asi que un importe
# ("100.000", "5,000.00") nunca se confunde con una fecha.
_FECHA_NUMERICA_TX = r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
_FECHA_ISO_TX = r"\d{4}-\d{1,2}-\d{1,2}"
_FECHA_LETRA_TX = r"\d{1,2}\s+de\s+[a-záéíóú]+(?:\s+(?:de|del)\s+|\s+)\d{4}"
_PATRON_FECHA_TX = rf"(?:{_FECHA_LETRA_TX}|{_FECHA_ISO_TX}|{_FECHA_NUMERICA_TX})"

# Candidatas a fecha tal como estan escritas en el texto. Cada candidata pasa por
# `_a_fecha`: si no es una fecha real (mes inexistente, dia 32), se descarta.
_CANDIDATAS_FECHA = re.compile(rf"\b(?:{_PATRON_FECHA_TX})\b", re.IGNORECASE)

# Mismo patron con el articulo o la palabra "dia" que el hablante le pone por
# delante ("El 01/07/2020 se compra..."): se quita junto con la fecha para que la
# glosa no empiece con un articulo huérfano.
_FECHA_CON_ARTICULO = re.compile(
    rf"\b(?:(?:el|la|los|las|día)\s+)?(?:{_PATRON_FECHA_TX})\b", re.IGNORECASE
)

# Cortes de oracion: puntuacion y conectores de enumeracion que el hablante usa
# para separar operaciones ("... soles. Luego se paga..." / "... contado, luego
# se paga..."). El conector se descarta: es relleno, no descripcion.
_CORTE_ORACION = re.compile(
    r"(?<=[.!?;])\s+"
    r"|(?<=\w)\s+(?:y\s+también|también|tambien|luego|después|despues|"
    r"seguidamente|finalmente|posteriormente|a\s+continuación|a\s+continuacion)(?=\s+)",
    re.IGNORECASE,
)

# Preposiciones y nexos que pueden quedar colgando tras quitar fecha e importe
# ("empresa con 100.000 al contado" -> "empresa con al contado" -> "empresa al
# contado"; "por 5,000 soles" -> "por").
_NEXOS_GLOSA = ("con", "de", "del", "por", "para", "a", "al", "en", "desde",
                "hasta", "sobre", "entre", "hacia", "sin", "e", "y")
_PATRON_NEXOS = "|".join(_NEXOS_GLOSA)

# Relleno hablado que no describe la operacion.
_RUIDO_GLOSA = re.compile(
    r"\b(?:luego|después|despues|seguidamente|también|tambien|finalmente|"
    r"posteriormente|a continuación|a continuacion|ahora|primero|primera|"
    r"acto seguido)\b",
    re.IGNORECASE,
)
_RUIDO_IMPORTE = re.compile(r"\b\d[\d.,]*\b")
_RUIDO_MONEDA = re.compile(
    r"\b(?:soles|nuevos soles|dólares|dolares|dólar|dolar|dolares americanos)\b",
    re.IGNORECASE,
)


def fechas_en_texto(texto):
    """
    Fechas REALES escritas en la transcripcion, en ISO y sin repetir.

    Es la fuente de verdad de la fecha del dictado: lo que no esta escrito en el
    texto no puede viajar al borrador, venga de donde venga. Formatos que acepta:
    8.07.2020, 08/07/2020, 08-07-2020, 2020-07-08, "8 de julio de 2020" y
    "8 de julio del 2020". Si no hay ninguna, devuelve un conjunto vacio: el
    borrador queda SIN fecha para revision humana, nunca con una inventada.
    """
    if not texto:
        return set()
    encontradas = set()
    for candidata in _CANDIDATAS_FECHA.finditer(str(texto)):
        iso = _a_fecha(candidata.group(0))
        if iso:
            encontradas.add(iso)
    return encontradas


def oraciones_de_dictado(texto):
    """
    Divide la transcripcion en oraciones para emparejarlas, una a una, con las
    operaciones que devolvio la IA. Corta por puntuacion y por conectores de
    enumeracion; nunca corta dentro de una fecha ("8.07.2020" no se parte porque
    el corte exige espacio justo despues del punto).
    """
    if not texto:
        return []
    return [parte.strip() for parte in _CORTE_ORACION.split(str(texto)) if parte and parte.strip()]


def glosa_desde_transcripcion(texto):
    """
    Glosa DETERMINISTA de una oracion transcrita: conserva la descripcion de la
    operacion y quita fecha, importes, moneda, muletillas y nexos colgantes.

        "8.07.2020 se crea una empresa con 100.000 al contado."
         -> "Se crea una empresa al contado"

    No usa la IA ni el catalogo: es la misma limpieza para todo el dictado. Si
    tras limpiar no queda nada, devuelve "" y el llamador decide (nunca inventa
    una glosa a partir de nada).
    """
    if not texto:
        return ""
    limpio = unicodedata.normalize("NFKC", str(texto))
    limpio = _CARACTERES_OCULTOS.sub("", limpio)
    limpio = _FECHA_CON_ARTICULO.sub(" ", limpio)
    limpio = _CANDIDATAS_FECHA.sub(" ", limpio)
    limpio = _RUIDO_IMPORTE.sub(" ", limpio)
    limpio = _RUIDO_MONEDA.sub(" ", limpio)
    limpio = limpio.replace("S/", " ")
    limpio = _RUIDO_GLOSA.sub(" ", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()

    # Nexos colgantes: un nexo solo se quita si va seguido de OTRO nexo ("con
    # al") o si queda al final de la oracion ("... por"). "al contado" o
    # "de la oficina" no se tocan: ahi el nexo si enlaza palabras reales.
    for _ in range(5):
        antes = limpio
        limpio = re.sub(
            rf"\b(?:{_PATRON_NEXOS})\b(?=\s+(?:{_PATRON_NEXOS})\b)", " ", limpio, flags=re.IGNORECASE
        )
        limpio = re.sub(rf"(?:^|\s+)(?:{_PATRON_NEXOS})\s*\.?$", " ", limpio, flags=re.IGNORECASE)
        limpio = re.sub(r"\s+", " ", limpio).strip(" ,;:")
        if limpio == antes:
            break

    limpio = re.sub(r"^[yY]\s+", "", limpio).strip(" .,;:!?")
    if not limpio:
        return ""
    return limpio[0].upper() + limpio[1:]


def analizar_dictado(texto):
    """
    Analiza una transcripcion de voz y devuelve (True, ResultadoIA) con las
    operaciones ya normalizadas y validadas, o (False, mensaje de error).

    Flujo: transcripcion -> `analizar_documento_completo` (mismo pipeline, prompt
    y validacion que PDF/Word) -> correcciones propias del dictado:
      - FECHA: solo se conserva una fecha escrita en la transcripcion. Una fecha
        que la IA escribio pero que no esta en el texto se VACIA (es una
        invencion); si el texto trae una sola fecha y la IA no la uso, se toma
        esa. Sin fecha en el texto -> sin fecha en el borrador, con aviso.
      - GLOSA: se deriva de la oracion que describe cada operacion cuando la
        segmentacion coincide; si no, de la glosa que trajo la IA limpiada con el
        MISMO limpiador determinista. Nunca queda vacia si el texto dice algo.
    """
    exito, resultado = analizar_documento_completo(texto, origen=None)
    if not exito:
        return False, resultado

    detectadas = fechas_en_texto(texto)
    avisos = list(getattr(resultado, "avisos", []) or [])

    for operacion in resultado:
        if not isinstance(operacion, dict):
            continue
        fecha = operacion.get("fecha") or ""
        if fecha in detectadas:
            continue
        if len(detectadas) == 1:
            operacion["fecha"] = next(iter(detectadas))
        elif fecha:
            operacion["fecha"] = ""
            avisos.append(
                f"Asiento '{operacion.get('glosa') or 'sin glosa'}': la fecha que devolvio la IA "
                "no aparece en la transcripcion; se dejo vacia para revision humana."
            )
        # Sin fechas detectadas y sin fecha de la IA no se añade nada: el
        # normalizador ya aviso "no tiene una fecha interpretable".

    oraciones = oraciones_de_dictado(texto)
    if len(oraciones) == len(resultado):
        for operacion, oracion in zip(resultado, oraciones):
            if not isinstance(operacion, dict):
                continue
            glosa = glosa_desde_transcripcion(oracion) or glosa_desde_transcripcion(operacion.get("glosa") or "")
            if glosa:
                operacion["glosa"] = glosa
    else:
        # La segmentacion no coincide con las operaciones (el hablante no uso
        # puntuacion, o la IA agrupo dos frases en un asiento): se limpia la glosa
        # que trajo la IA con el MISMO limpiador, y si tampoco dice nada se usa el
        # texto completo ya limpio.
        for operacion in resultado:
            if not isinstance(operacion, dict):
                continue
            glosa = (
                glosa_desde_transcripcion(operacion.get("glosa") or "")
                or glosa_desde_transcripcion(texto)
            )
            if glosa:
                operacion["glosa"] = glosa

    if isinstance(resultado, ResultadoIA):
        resultado.avisos = avisos
    return True, resultado


# --------------------------------------------------------------------------- #
# 8. ESCÁNER VISUAL (Groq Vision)
#
# Este módulo NO es un camino paralelo al de documentos: es el MISMO motor con otra
# forma de entrada. La imagen se limitó a ser el documento de entrada, la respuesta del
# modelo pasa por el MISMO `_extraer_json` y el MISMO `procesar_dinamica_contable`
# (CONTRATO_MULTI_PARTIDA + validación de partida doble) que usan Excel, PDF y Word.
#
# Consecuencia de diseño: un asiento transcrito de una foto tiene exactamente los
# mismos derechos y las mismas obligaciones que uno transcrito de un Excel. Si el
# escáner devuelve un descuadre, el motor lo marca y lo entrega para revisión humana,
# igual que en cualquier otro flujo: la foto nunca abre una puerta que el texto no
# tiene.
# --------------------------------------------------------------------------- #

# Configurable por entorno para no dejar el modelo clavado en el codigo: Groq retiro
# `llama-3.2-90b-vision-preview` Y `llama-3.2-11b-vision-preview` en la misma fecha
# (04/14/25). Para los dos, Groq declara como unico reemplazo valido este
# `meta-llama/llama-4-scout-17b-16e-instruct`. Si Groq degrada este a su vez, basta
# con `MODELO_VISION=...` en `.env`, sin tocar codigo.
MODELO_VISION = os.getenv("MODELO_VISION", "meta-llama/llama-4-scout-17b-16e-instruct")

# Techo de la imagen ANTES de optimizar. Una foto de movil de 12 MP pesa varios MB y
# su version en base64 es ~33% mas grande: mandarla sin limite produce un 413 de la
# API o un 429 de TPM que el usuario no puede interpretar. Se rechaza antes de gastar
# la cuota y con un mensaje que dice que hacer.
MAX_BYTES_IMAGEN = int(os.getenv("MAX_BYTES_IMAGEN", str(5 * 1024 * 1024)))

# Lado maximo (en pixeles) y calidad JPEG de la imagen ya optimizada. El coste del
# escaner no lo marca el tamano del archivo sino los TOKENS DE IMAGEN que consume el
# modelo, que crecen con el area en pixeles. Una foto de 4000x3000 pasa a 1600x1200 y
# divide por ~6 el coste de vision: de ahi que 1600 px sea el techo por defecto. Es
# ademas resolucion de sobra para leer cifras de un comprobante.
MAX_DIMENSION_IMAGEN = int(os.getenv("MAX_DIM_IMAGEN", "1600"))
CALIDAD_JPEG = int(os.getenv("CALIDAD_JPEG", "85"))

# Mensaje de salida del escaner visual cuando la IA no devuelve nada contable. Este
# texto SIEMPRE llega al usuario final a traves de `st.error`, asi que no puede
# contener rastro tecnico: el caso tipico es una foto de un objeto que no es un
# comprobante, y un volcado del JSON crudo aterriza en pantalla como si fuera un
# fallo del sistema. El diagnostico real se conserva en la traza interna.
MENSAJE_IMAGEN_SIN_DATOS = (
    "No se detectaron transacciones contables legibles en la imagen. Asegúrate de "
    "enfocar un comprobante, factura o documento claro, y evita tomar fotos a objetos "
    "aleatorios."
)

# Mensaje de salida del escaner visual cuando la cuenta no tiene vision habilitada.
# Es una limitacion de la cuenta (404 model_not_found / 400 model_decommissioned), no
# un fallo de la foto ni de la conexion: sin este mensaje el usuario ve el volcado del
# error de la API y concluye que el sistema esta roto. Se dice que hacer y donde.
MENSAJE_VISION_NO_HABILITADA = (
    "El escáner visual no está disponible: la IA con visión no está habilitada en "
    "esta cuenta de Groq. El resto del sistema funciona con normalidad. Para activarlo, "
    "hay que habilitar el acceso a un modelo de visión en la cuenta; mientras tanto, usa "
    "la carga de documentos (Excel, PDF o Word) o el ingreso manual."
)

# Firmas binarias de los formatos que acepta la UI (png, jpg, jpeg). Solo se usan en
# el camino de fallo del optimizador, para que el data URL declare el tipo REAL: el
# endpoint de vision no puede deducirlo de los bytes codificados.
_FIRMAS_IMAGEN = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)

MIME_POR_DEFECTO = "image/jpeg"


def _detectar_mime_imagen(imagen_bytes):
    """
    Devuelve el content-type real de la imagen a partir de su firma binaria.

    Reconoce PNG y JPEG (los unicos que acepta la UI) y cae en el default en vez de
    inventar un tipo: la API es quien decide si un formato desconocido es valido.

    Tras el optimizador esto solo se usa en el CAMINO DE FALLO (imagen que Pillow no
    pudo abrir): si la optimizacion funciono, la salida es JPEG y el tipo se sabe.
    """
    for firma, mime in _FIRMAS_IMAGEN:
        if imagen_bytes.startswith(firma):
            return mime
    return MIME_POR_DEFECTO


def optimizar_imagen_para_api(imagen_bytes, max_dim=MAX_DIMENSION_IMAGEN):
    """
    Prepara la imagen para la API de vision y devuelve (bytes, content-type).

    Que problema resuelve: el error 429 (limite de tokens por minuto) es la causa
    numero uno de fallo del escaner, y no la tiene que ver con el texto sino con el
    AREA EN PIXELES de la foto. Reencodar aqui, antes de codificar en base64 y antes
    de gastar la cuota, baja el coste de la peticion y evita el rechazo por tamano.

    Reglas:
      - RGBA y P (y cualquier modo con alfa o paleta) pasan a RGB: JPEG no guarda
        canal alfa y una foto de documento no lo necesita. Sin esta conversion,
        `Image.save` falla y la foto se pierde.
      - `thumbnail` (no `resize`) reduce hasta el techo MANTENIENDO el aspect ratio, y
        NUNCA agranda: una imagen mas chica que el techo se devuelve tal cual en vez
        de estirarla a 1600 px y pagar mas tokens por menos informacion.
      - La salida es siempre JPEG quality=85, con el mime correspondiente.

    Devuelve los bytes originales con su mime detectado si Pillow no puede abrir la
    imagen: optimizar es una mejora de coste, nunca una condicion para poder escanear.
    """
    datos = bytes(imagen_bytes)
    try:
        with Image.open(io.BytesIO(datos)) as imagen:
            # JPEG solo sabe guardar RGB (y grises). RGBA y P -una captura con canal
            # alfa o con paleta- no se pueden guardar: se aplanan a RGB.
            if imagen.mode != "RGB":
                imagen = imagen.convert("RGB")

            imagen.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            imagen.save(buffer, format="JPEG", quality=CALIDAD_JPEG)
            return buffer.getvalue(), "image/jpeg"
    except Exception:
        # Formato no soportado, archivo corrupto o memoria insuficiente: se devuelve
        # lo que llego. Que la API decida si lo acepta.
        return datos, _detectar_mime_imagen(datos)


def _codificar_imagen_base64(imagen_bytes):
    """
    Convierte los bytes de la imagen a un string Base64 en utf-8, que es el formato
    que espera `image_url` de la API de Groq Vision.

    Acepta bytes o bytearray y NUNCA devuelve un objeto bytes: si se escapara un
    `b64encode` sin `.decode()`, el f-string del data URL produciria "b'...'" y la
    peticion fallaria con un error de la API en lugar de un error local claro.
    """
    if imagen_bytes is None:
        raise ValueError("No se recibio ninguna imagen.")
    if isinstance(imagen_bytes, str):
        raise ValueError(
            "Se esperaba la imagen en bytes, no en texto. Usar getvalue() sobre el "
            "UploadedFile de Streamlit, no read() con decodificacion."
        )
    if not isinstance(imagen_bytes, (bytes, bytearray, memoryview)):
        raise ValueError(
            f"Tipo de imagen no soportado: {type(imagen_bytes).__name__}."
        )

    datos = bytes(imagen_bytes)
    if not datos:
        raise ValueError("La imagen llego vacia (0 bytes).")

    return base64.b64encode(datos).decode("utf-8")


# Prefijo que SOLO se antepone al system prompt del escaner visual.
#
# El texto base (`instrucciones_agente_excel`) esta escrito para texto: habla de
# "lineas", "filas", "bloques del documento" y del corte de un documento largo. Un
# modelo de vision recibe una fotografia, no esos cortes, y sin este aviso puede
# buscar lineas que no existen o pôr "no hay bloques" en un comprobante de una pagina.
#
# Se antepone en el payload, NO en la constante: Excel, PDF y Word deben seguir
# leyendo el mismo prompt de siempre, porque sus reglas SI hablan de bloques.
PREFIJO_PROMPT_VISUAL = (
    "ESTÁS ANALIZANDO LA FOTOGRAFÍA O IMAGEN DE UN DOCUMENTO FÍSICO O DIGITAL.\n"
    "Ignora cualquier instrucción previa sobre 'bloques de texto', 'filas' o 'líneas'. "
    "Aplica las siguientes reglas de Dinámica Contable a los datos que identifiques "
    "visualmente:\n\n"
)

# Instruccion de la peticion multimodal. El system prompt es el de los demas flujos
# (instrucciones_agente_excel) con el prefijo de arriba, asi que el modelo lee el
# mismo contrato multipartida; aqui solo se le aclara DE DONDE sale la informacion.
_INSTRUCCION_VISUAL = (
    "La imagen adjunta es la UNICA fuente de informacion. Transcribe a ella las "
    "operaciones contables que esten escritas: importes, cuentas, fechas y glosas. "
    "No completes, no calcules y no deduzcas operaciones que no aparezcan en la "
    "imagen. Si una parte esta borrosa, cortada o ilegible, transcribe solo lo "
    "legible y marca ese elemento con \"revisar\": true."
).strip()


def _construir_payload_vision(imagen_b64, mime):
    """
    Arma el `messages` multimodal: system prompt contable (con el prefijo visual) +
    texto + image_url.

    El prefijo se concatena AQUI y no sobre la constante global: es una adaptation al
    canal de entrada, no un cambio del contrato contable.
    """
    return [
        {"role": "system", "content": PREFIJO_PROMPT_VISUAL + instrucciones_agente_excel},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _INSTRUCCION_VISUAL},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{imagen_b64}"},
                },
            ],
        },
    ]


def _consultar_vision(imagen_b64, mime):
    """
    Una peticion a Groq Vision. No lleva `reasoning_effort` ni `include_reasoning`:
    son parametros del modelo de razonamiento y la API los rechaza en el de vision.
    """
    return cliente.chat.completions.create(
        model=MODELO_VISION,
        messages=_construir_payload_vision(imagen_b64, mime),
        temperature=0.1,
        max_tokens=MAX_COMPLETION_TOKENS,
        timeout=TIEMPO_LIMITE_SEGUNDOS,
    )


def analizar_imagen_comprobante(imagen_bytes, mime=None):
    """
    Envia una imagen (foto de comprobante, factura escaneada o captura de un caso)
    al modelo Vision de Groq y devuelve (True, ResultadoIA) con los asientos ya
    normalizados y validados en Python, o (False, mensaje de error).

    Es el MISMO pipeline de los demas metodos de entrada, con la imagen en lugar del
    texto: el prompt es `instrucciones_agente_excel` (con el prefijo visual encima),
    la respuesta pasa por `_extraer_json` y el lote pasa OBLIGATORIAMENTE por
    `procesar_dinamica_contable`. No hay una segunda validacion contable para imagenes,
    y no debe haberla: dos reglas de cuadre distintas divergen en cuanto el modelo
    falla una vez.

    Antes de salir a la API la foto pasa por `optimizar_imagen_para_api` (RGB, techo
    de pixeles, JPEG quality=85): el limite de TPM se mide en tokens de imagen y es
    la causa dominante de 429 en este modulo. Optimizar es una mejora de coste, no una
    condicion: si Pillow no puede abrir la imagen, se envia la original.

    Garantias que hereda del motor y que aqui se respetan:
      - Una respuesta vacia, sin JSON o TRUNCADA no se acepta como completa: se
        reintenta y, si persiste, se devuelve el error. Presentar la mitad de un
        comprobante como si fuera el asiento completo seria mostrar un resultado
        falso (mismo criterio que `_procesar_bloque`).
      - Un descuadre no se corrige: se marca `revisar` y viaja al borrador humano.
      - `avisos` queda poblado para que la UI muestre los avisos de revision.
    """
    # Validaciones locales ANTES de gastar cuota: un error de entrada no debe
    # costar una peticion ni devolver un error de red disfrazado de problema del usuario.
    # Primero se tipa y se valida el contenido (tipo y bytes no vacios), despues el
    # tamano: al reves, `len(bytes("texto"))` revienta con un TypeError opaco que
    # oculta el mensaje util de "se paso texto en vez de la imagen".
    try:
        imagen_original = base64.b64decode(_codificar_imagen_base64(imagen_bytes))
    except (ValueError, TypeError) as error_dato:
        return False, f"No se pudo preparar la imagen: {error_dato}"

    if len(imagen_original) > MAX_BYTES_IMAGEN:
        return False, (
            f"La imagen pesa {len(imagen_original) / 1024 / 1024:.1f} MB y el limite "
            f"es {MAX_BYTES_IMAGEN / 1024 / 1024:.0f} MB. Recorta la foto o bajale la "
            "resolucion: mandarla entera la rechaza la API sin extraer nada."
        )

    # Optimizacion de coste ANTES de codificar y de llamar: el 429 viene del area en
    # pixeles de la foto, no del texto. Reencodar aqui es lo que evita el rechazo.
    datos_optimizados, mime_optimizado = optimizar_imagen_para_api(imagen_original)
    mime = mime or mime_optimizado
    imagen_b64 = _codificar_imagen_base64(datos_optimizados)

    traza = [
        f"Analisis visual con {MODELO_VISION}.",
        f"Imagen optimizada: {len(imagen_original) / 1024:.0f} KB originales -> "
        f"{len(datos_optimizados) / 1024:.0f} KB en {mime}, "
        f"{len(imagen_b64):,} caracteres en base64.",
    ]

    for intento in range(1, MAX_INTENTOS + 1):
        if intento > 1:
            time.sleep(ESPERA_ENTRE_INTENTOS)

        try:
            respuesta = _consultar_vision(imagen_b64, mime)
        except Exception as error:
            if _es_error_de_modelo_no_disponible(error):
                # No es un problema de la foto ni de la red: la cuenta no tiene
                # vision habilitada. Reintentar devolveria el mismo error tres
                # veces (y gastaria cuota), asi que se corta aqui y se explica.
                traza.append(f"Vision no habilitada en la cuenta: {error}")
                print("[escaner visual] " + " | ".join(traza))
                return False, MENSAJE_VISION_NO_HABILITADA
            if _es_error_de_cuota_diaria(error):
                # 429 por tokens por DÍA: la cuota del día se agotó y no se repone
                # reintentando. Cortar aquí evita 16 s de esperas para nada.
                informe = f"Intento {intento}: {_informe_de_cuota_diaria(error)}"
                traza.append(informe)
                print("[escaner visual] " + " | ".join(traza))
                return False, f"Error de conexion con la IA Vision de Groq: {_informe_de_cuota_diaria(error)}"
            if _es_error_de_tamano_o_ritmo(error):
                traza.append(
                    f"Intento {intento}: la API rechazo el tamano de la imagen o el ritmo "
                    f"(HTTP {getattr(error, 'status_code', '413/429')})."
                )
                time.sleep(_espera_antes_de_reintentar(error, intento))
                continue
            return False, f"Error de conexion con la IA Vision de Groq: {error}"

        # `_leer_contenido` y no `response.choices[0].message.content`: es la capa que
        # evita que un content vacio o None se cuele como error de NoneType.
        contenido, finish_reason, _ = _leer_contenido(respuesta)
        if not contenido:
            traza.append(f"Intento {intento}: respuesta vacia (finish_reason={finish_reason}).")
            continue

        lote, error_json, json_truncado = _extraer_json(contenido)
        if lote is None:
            # `error_json` puede ser el volcado crudo de lo que devolvio el modelo. Se
            # conserva solo en la traza (superficie tecnica), nunca en el mensaje de
            # error que ve el usuario: si la foto no es un comprobante, ese texto
            # tecnico es lo que hace que la aplicacion parezca rota.
            traza.append(
                f"Intento {intento}: sin JSON utilizable. Respuesta: {error_json[:200]}"
            )
            continue

        if finish_reason == "length" or json_truncado:
            traza.append(
                f"Intento {intento}: la respuesta se corto antes de escribir todas las "
                f"operaciones (finish_reason={finish_reason}). Se reintenta con mas presupuesto."
            )
            continue

        # Unico punto de verdad contable: el MISMO motor que usan Excel, PDF y Word.
        resultado, avisos, error_normalizacion = procesar_dinamica_contable(lote)
        if resultado is None:
            detalle = " | ".join(avisos[:4]) if avisos else ""
            return False, f"{error_normalizacion} Detalle: {detalle}".strip()

        resultado.avisos = avisos + traza
        return True, resultado

    # Traduccion de diagnostico a lenguaje de usuario. Antes este mensaje concatenaba la
    # traza completa, y la traza del camino "sin JSON utilizable" incluye hasta 200
    # caracteres de la respuesta cruda del modelo. Con una foto de un objeto que no es un
    # comprobante, esa respuesta no es un error del sistema sino la respuesta correcta, y
    # mostrarla hace pensar que la aplicacion fallo. El texto tecnico queda disponible
    # para el diagnostico: se imprime en la salida de ejecucion de Streamlit, no en el
    # mensaje que `st.error` pinta en pantalla.
    print("[escaner visual] " + " | ".join(traza))
    return False, MENSAJE_IMAGEN_SIN_DATOS