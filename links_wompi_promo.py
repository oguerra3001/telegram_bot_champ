# bot_final_wompi.py
"""
Flujo:
  /start              -> bienvenida
  /promo_champions_league  -> pide teléfono (botón) y espera al contacto (NO crea link todavía)
  [CONTACTO]          -> al recibir teléfono: crea enlace Wompi, guarda y ENVÍA el link
  /validar_pago       -> valida SOLO enlaces generados HOY (zona horaria El Salvador)
  /mi_link            -> reenvía tu enlace de HOY si lo perdiste

Requisitos:
  pip install python-telegram-bot httpx python-dotenv

Variables de entorno (.env):
  BOT_TOKEN=...
  WOMPI_CLIENT_ID=...
  WOMPI_CLIENT_SECRET=...
  WOMPI_AUDIENCE=wompi_api
  WOMPI_ID_URL=https://id.wompi.sv/connect/token
  WOMPI_API_BASE=https://api.wompi.sv
  EMAILS_NOTIFICACION=tu-correo@dominio.com
  CHANNEL_ID=-100XXXXXXXXXX
"""

import os, csv, time, json
from datetime import datetime, timedelta, timezone
import httpx
from dotenv import load_dotenv
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ===== Zona horaria local (El Salvador) =====
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/El_Salvador")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=-6))  # fallback UTC-6

load_dotenv()

# ===== Helpers de entorno =====
def _must(var_name: str) -> str:
    val = os.getenv(var_name)
    if not val:
        raise RuntimeError(f"Falta variable de entorno: {var_name}")
    return val

# ===== Config obligatoria =====
BOT_TOKEN           = _must("BOT_TOKEN")
WOMPI_CLIENT_ID     = _must("WOMPI_CLIENT_ID")
WOMPI_CLIENT_SECRET = _must("WOMPI_CLIENT_SECRET")
WOMPI_AUDIENCE      = _must("WOMPI_AUDIENCE")     # típicamente "wompi_api"
WOMPI_ID_URL        = _must("WOMPI_ID_URL")       # https://id.wompi.sv/connect/token
WOMPI_API_BASE      = _must("WOMPI_API_BASE")     # https://api.wompi.sv
CHANNEL_ID          = int(_must("CHANNEL_ID"))

# Opcional (con fallback)
EMAILS_NOTIFICACION = os.getenv("EMAILS_NOTIFICACION") or "notificaciones@dummy.local"

# Producto
SUSCRIPCION_NOMBRE    = "Promocion Champions League (2 dias)!"
SUSCRIPCION_MONTO_USD = 10.00

# Archivos CSV
CSV_LINKS   = "links_wompi.csv"
CSV_VALID   = "validaciones_wompi.csv"
CSV_PHONES  = "telefonos.csv"

# ===== Util CSV =====
def _ensure_headers(path: str, headers: list[str]) -> None:
    if not os.path.isfile(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(headers)

def append_link_row(row: dict) -> None:
    _ensure_headers(
        CSV_LINKS,
        ["timestamp_utc","user_id","chat_id","username","referencia","idEnlace","urlEnlace","monto_usd","estado_inicial"],
    )
    with open(CSV_LINKS, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp_utc","user_id","chat_id","username","referencia","idEnlace","urlEnlace","monto_usd","estado_inicial"]
        )
        writer.writerow(row)

def append_validation(row: dict) -> None:
    _ensure_headers(CSV_VALID, ["timestamp_utc","user_id","referencia","idEnlace","estado","detalle_snippet"])
    with open(CSV_VALID, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["timestamp_utc","user_id","referencia","idEnlace","estado","detalle_snippet"]
        )
        writer.writerow(row)

def upsert_phone(user_id: int, chat_id: int, username: str, phone: str) -> None:
    _ensure_headers(CSV_PHONES, ["timestamp_utc","user_id","chat_id","username","phone_number"])
    with open(CSV_PHONES, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp_utc","user_id","chat_id","username","phone_number"]
        )
        writer.writerow({
            "timestamp_utc": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "chat_id": chat_id,
            "username": username,
            "phone_number": phone
        })

def _parse_utc_iso(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        dt = datetime.fromisoformat(ts.replace("Z", ""))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt

def get_last_link_for_user_today(user_id: int):
    if not os.path.isfile(CSV_LINKS):
        return None

    today_local = datetime.now(LOCAL_TZ).date()
    candidates = []

    with open(CSV_LINKS, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("user_id")) != str(user_id):
                continue
            ts = row.get("timestamp_utc")
            if not ts:
                continue
            try:
                dt_utc = _parse_utc_iso(ts)
                dt_local = dt_utc.astimezone(LOCAL_TZ)
                if dt_local.date() == today_local:
                    candidates.append((dt_local, row))
            except Exception:
                continue

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]

def get_today_last_link_text_for_user(user_id: int):
    reg = get_last_link_for_user_today(user_id)
    if not reg:
        return None
    return (
        f"💳 Enlace de pago:\n{reg.get('urlEnlace')}\n\n"
        f"Referencia: {reg.get('referencia')}\n"
        f"Monto: ${reg.get('monto_usd')} USD\n\n"
        f"Cuando termines tu pago HOY, usa /validar_pago."
    )

# ===== OAuth2 Token =====
def get_wompi_access_token() -> str:
    data = {
        "grant_type": "client_credentials",
        "client_id": WOMPI_CLIENT_ID,
        "client_secret": WOMPI_CLIENT_SECRET,
        "audience": WOMPI_AUDIENCE,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    with httpx.Client(timeout=30) as client:
        r = client.post(WOMPI_ID_URL, data=data, headers=headers)
        r.raise_for_status()
        j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"Token Wompi sin access_token: {j}")
    return j["access_token"]

def wompi_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# ===== Wompi API =====
def crear_enlace_pago(referencia: str, monto_usd: float, nombre_producto: str) -> dict:
    token = get_wompi_access_token()
    url = f"{WOMPI_API_BASE}/EnlacePago"
    payload = {
        "identificadorEnlaceComercio": referencia,
        "monto": round(monto_usd, 2),
        "nombreProducto": nombre_producto,
        "configuracion": {
            "emailsNotificacion": EMAILS_NOTIFICACION
        },
    }
    with httpx.Client(timeout=30) as client:
        r = client.post(url, headers=wompi_headers(token), json=payload)
        r.raise_for_status()
        j = r.json()
    if not j.get("idEnlace") or not j.get("urlEnlace"):
        raise RuntimeError(f"Respuesta de Enlace inesperada: {j}")
    return j

def consultar_enlace(id_enlace: int) -> dict:
    token = get_wompi_access_token()
    url = f"{WOMPI_API_BASE}/EnlacePago/{id_enlace}"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=wompi_headers(token))
        r.raise_for_status()
        return r.json()

def inferir_estado_transaccion(json_enlace: dict):
    data = json_enlace or {}
    candidatos = []
    for k in ("transaccion", "transaccionCompra", "ultimaTransaccion", "transacciones"):
        if k in data:
            candidatos.append(data[k])

    flat = []
    for c in candidatos:
        if isinstance(c, list):
            flat.extend(c)
        else:
            flat.append(c)

    for t in flat:
        if not isinstance(t, dict):
            continue
        if str(t.get("esAprobada", "")).lower() == "true" or t.get("esAprobada") is True:
            return "aprobada", t
        estado = str(t.get("estado", "")).lower()
        if estado in {"aprobada", "approved", "success"}:
            return "aprobada", t
        if estado in {"pendiente", "pending"}:
            return "pendiente", t
        if estado in {"fallida", "declinada", "failed", "rejected"}:
            return "fallida", t

    return "desconocido", None

# ===== Invite link helper =====
async def crear_invite_link(context: ContextTypes.DEFAULT_TYPE, chat_id: int, horas_validez: int = 1, usos: int = 1) -> str:
    expire_dt = datetime.now(timezone.utc) + timedelta(hours=horas_validez)
    link_obj = await context.bot.create_chat_invite_link(
        chat_id=chat_id,
        expire_date=expire_dt,
        member_limit=usos
    )
    return link_obj.invite_link

# ===== Bot Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Bienvenido! Soy el bot de promociones STATS.\n\n"
        "Para unirte al canal haz clic en:\n"
        "/promo_champions_league\n"
    )

async def promo_champions_league(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra botón para compartir teléfono y marca 'pendiente' en user_data.
    NO crea ni envía link hasta recibir el contacto.
    """
    user = update.effective_user
    # flag para este usuario
    context.user_data["awaiting_phone"] = True
    context.user_data["awaiting_phone_since"] = int(time.time())

    share_phone_btn = KeyboardButton(text="📱 COMPARTIR NUMERO", request_contact=True)
    kb = ReplyKeyboardMarkup([[share_phone_btn]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "Espera unos segundos, abajo aparecera un boton para que des clic y compartas tu numero de celular. Por favor espera a que el boton aparezca y no intentes escribir tu numero, el bot lo leera cuando oprimas el boton",
        reply_markup=kb
    )

async def recibir_contacto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Cuando llega el CONTACT, guardamos el teléfono y, si había un /promo_champions_league pendiente,
    creamos y enviamos el enlace de pago.
    """
    contact = update.effective_message.contact
    user = update.effective_user
    chat = update.effective_chat

    if not contact:
        return

    if contact.user_id and contact.user_id != user.id:
        await update.message.reply_text("⚠️ Comparte tu propio número usando el botón, por favor.")
        return

    # Guardar teléfono
    upsert_phone(user.id, chat.id, user.username or "sin_username", contact.phone_number)
    await update.message.reply_text(f"✅ Número recibido: {contact.phone_number}", reply_markup=ReplyKeyboardRemove())

    # ¿Estábamos esperando teléfono para generar link?
    if not context.user_data.get("awaiting_phone"):
        # Si no había flujo pendiente, no generamos enlace automáticamente.
        return

    # Generar y enviar enlace ahora
    referencia = f"tg_{user.id}_{int(time.time())}"
    try:
        data = crear_enlace_pago(referencia, SUSCRIPCION_MONTO_USD, SUSCRIPCION_NOMBRE)
    except Exception as e:
        await context.bot.send_message(chat_id=chat.id, text=f"❌ No pude crear tu enlace de pago:\n{e}")
        context.user_data["awaiting_phone"] = False
        return

    id_enlace = data["idEnlace"]
    url_enlace = data["urlEnlace"]

    append_link_row({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "user_id": user.id,
        "chat_id": chat.id,
        "username": user.username or "sin_username",
        "referencia": referencia,
        "idEnlace": id_enlace,
        "urlEnlace": url_enlace,
        "monto_usd": f"{SUSCRIPCION_MONTO_USD:.2f}",
        "estado_inicial": "pendiente",
    })

    await context.bot.send_message(
        chat_id=chat.id,
        text=(
            f"💳 Enlace de pago:\n{url_enlace}\n\n"
            f"Referencia: {referencia}\nMonto: ${SUSCRIPCION_MONTO_USD:.2f} USD\n\n"
            f"Cuando termines tu pago HOY, usa \n /validar_pago."
        ),
        reply_markup=ReplyKeyboardRemove()
    )

    # Limpia el flag
    context.user_data["awaiting_phone"] = False

async def mi_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    txt = get_today_last_link_text_for_user(user.id)
    if txt is None:
        await update.message.reply_text(
            "No encuentro enlaces generados HOY para ti. Usa /promo_champions_league para crear uno nuevo."
        )
        return
    await update.message.reply_text(txt)

def get_last_link_for_user_today_or_msg(user_id: int):
    reg = get_last_link_for_user_today(user_id)
    if not reg:
        return None, "No encuentro enlaces generados HOY para ti. Usa /promo_champions_league para generar uno nuevo."
    return reg, None

def get_wompi_estado_y_guardar(user_id: int, id_enlace: int, referencia: str):
    detalle = consultar_enlace(int(id_enlace))
    estado, nodo = inferir_estado_transaccion(detalle)
    snippet = json.dumps(nodo if nodo else detalle)[:600]
    append_validation({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "user_id": user_id,
        "referencia": referencia or "",
        "idEnlace": id_enlace,
        "estado": estado,
        "detalle_snippet": snippet,
    })
    return estado

async def validar_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    registro, msg = get_last_link_for_user_today_or_msg(user.id)
    if msg:
        await update.message.reply_text(msg)
        return

    id_enlace = registro.get("idEnlace")
    referencia = registro.get("referencia")
    if not id_enlace:
        await update.message.reply_text("No tengo el idEnlace de HOY. Genera uno nuevo con /promo_champions_league.")
        return

    try:
        estado = get_wompi_estado_y_guardar(user.id, int(id_enlace), referencia or "")
    except httpx.HTTPError as e:
        await update.message.reply_text(f"❌ Error consultando el enlace #{id_enlace}: {e}")
        return
    except ValueError:
        await update.message.reply_text(f"❌ idEnlace inválido: {id_enlace}")
        return

    if estado == "aprobada":
        try:
            invite_link = await crear_invite_link(context, CHANNEL_ID, horas_validez=1, usos=1)
            await update.message.reply_text(
                "✅ Pago aprobado. Aquí tienes tu enlace de acceso (1 uso, válido 1 hora):\n" + invite_link
            )
        except Exception as e:
            await update.message.reply_text(
                "✅ Pago aprobado, pero hubo un problema creando tu enlace. "
                "Por favor avísame y lo solucionamos.\n"
                f"Detalle técnico: {e}"
            )
    elif estado == "pendiente":
        await update.message.reply_text("⌛ Tu pago aún aparece como pendiente. Intenta de nuevo en un momento.")
    elif estado == "fallida":
        await update.message.reply_text("❌ La transacción figura fallida/declinada. Vuelve a intentarlo cuando gustes.")
    else:
        await update.message.reply_text("🤔 No pude determinar el estado aún. Probemos más tarde.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("promo_champions_league", promo_champions_league))
    app.add_handler(CommandHandler("validar_pago", validar_pago))
    app.add_handler(CommandHandler("mi_link", mi_link))
    app.add_handler(MessageHandler(filters.CONTACT, recibir_contacto))
    print("Bot corriendo. Comandos: /start /promo_champions_league /validar_pago /mi_link")
    app.run_polling()

if __name__ == "__main__":
    main()
