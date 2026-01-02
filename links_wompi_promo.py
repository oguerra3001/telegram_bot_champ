"""
Bot de Telegram para promociones con Wompi (Versión híbrida: local o Render).
Incluye: /start, selección de promoción, validación de pago, recordatorios, baneo automático.
Añadido: sistema de códigos de referidos/alianzas (10%..50%) aplicable solo al plan mensual.
- Para desactivar un código promocional puedes comentar la línea en el diccionario CODIGOS_PROMO.
- Para desactivar la promoción "Champions" cambia CHAMPIONS_ENABLED = False.
"""

import os, csv, time, json
from datetime import datetime, timedelta, timezone
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import nest_asyncio
from fastapi import FastAPI, Request
import uvicorn
import asyncio

nest_asyncio.apply()
load_dotenv()

# -------------------- Configuración general --------------------
def must(name):
    val = os.getenv(name)
    if not val: raise RuntimeError(f"Falta variable: {name}")
    return val

BOT_TOKEN = must("BOT_TOKEN")
WOMPI_CLIENT_ID = must("WOMPI_CLIENT_ID")
WOMPI_CLIENT_SECRET = must("WOMPI_CLIENT_SECRET")
WOMPI_AUDIENCE = os.getenv("WOMPI_AUDIENCE", "wompi_api")
WOMPI_ID_URL = must("WOMPI_ID_URL")
WOMPI_API_BASE = must("WOMPI_API_BASE")
CHANNEL_ID = int(must("CHANNEL_ID"))
EMAILS_NOTIFICACION = os.getenv("EMAILS_NOTIFICACION", "notificaciones@dummy.local")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MODE = os.getenv("MODE", "local")  # "local" o "webhook"

# Toggle para habilitar/deshabilitar la promocion Champions
CHAMPIONS_ENABLED = True  # Cambia a False para deshabilitar la promoción Champions

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/El_Salvador")
except:
    LOCAL_TZ = timezone(timedelta(hours=-6))

# === Suscripciones ===
SUBS = {
    "promo": {"nombre": "Promoción Champions League (2 días)", "monto": 10.00, "dias": 2},
    "mensual": {"nombre": "Suscripción completa (30 días)", "monto": 30.00, "dias": 30}
}

# === Códigos promocionales (para plan mens) ===
# Para DESACTIVAR un código, comenta la línea (añade # al inicio).
CODIGOS_PROMO = {
    "BRYAN22": 0.10,
    # "OFERTA20": 0.20,
    # "VIP30": 0.30,
    # "ALIADO40": 0.40,
    # "SUPER50": 0.50,
    # "TEMPORAL60": 0.60,  
}

# -------------------- CSV helpers --------------------
class CSVManager:
    def __init__(self, path, headers):
        self.path = path
        self.headers = headers
        if not os.path.isfile(self.path):
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.headers)
    def append(self, row):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=self.headers).writerow(row)
    def get_today_rows(self, user_id):
        if not os.path.isfile(self.path): return []
        today = datetime.now(LOCAL_TZ).date()
        out = []
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("user_id") == str(user_id):
                    dt = datetime.fromisoformat(r["timestamp_utc"]).replace(tzinfo=timezone.utc)
                    if dt.astimezone(LOCAL_TZ).date() == today:
                        out.append(r)
        return out

csv_links = CSVManager("links.csv", ["timestamp_utc","user_id","chat_id","username","referencia","idEnlace","urlEnlace","monto_usd"])
csv_valid = CSVManager("validaciones.csv", ["timestamp_utc","user_id","referencia","idEnlace","estado"])
csv_subs = CSVManager("subs.csv", ["user_id","tipo","expiracion_utc","estado"])
csv_phones = CSVManager("telefonos.csv", ["timestamp_utc","user_id","phone"])
csv_referidos = CSVManager("referidos.csv", ["timestamp_utc","user_id","codigo","creador","descuento"])

# -------------------- Cliente Wompi --------------------
class WompiClient:
    def __init__(self):
        self.token = None
    def _get_token(self):
        if not self.token:
            data = {
                "grant_type": "client_credentials",
                "client_id": WOMPI_CLIENT_ID,
                "client_secret": WOMPI_CLIENT_SECRET,
                "audience": WOMPI_AUDIENCE,
            }
            with httpx.Client(timeout=30) as c:
                r = c.post(WOMPI_ID_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
                r.raise_for_status()
                self.token = r.json()["access_token"]
        return self.token
    def crear_enlace(self, ref, monto, nombre):
        url = f"{WOMPI_API_BASE}/EnlacePago"
        payload = {"identificadorEnlaceComercio": ref, "monto": monto, "nombreProducto": nombre, "configuracion": {"emailsNotificacion": EMAILS_NOTIFICACION}}
        with httpx.Client(timeout=30) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}, json=payload)
            r.raise_for_status()
            return r.json()
    def consultar(self, id_enlace):
        url = f"{WOMPI_API_BASE}/EnlacePago/{id_enlace}"
        with httpx.Client(timeout=30) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"})
            r.raise_for_status()
            return r.json()
    @staticmethod
    def estado(enlace):
        for k in ["transaccion","ultimaTransaccion","transacciones"]:
            v = enlace.get(k)
            if isinstance(v, dict) and (v.get("esAprobada") or v.get("estado") in ["aprobada","approved"]):
                return "aprobada"
            if isinstance(v, list):
                for t in v:
                    if isinstance(t, dict) and (t.get("esAprobada") or t.get("estado") in ["aprobada","approved"]):
                        return "aprobada"
        return "pendiente"

wompi = WompiClient()

# -------------------- Suscripción y recordatorios --------------------
scheduler = AsyncIOScheduler()
class SubManager:
    def __init__(self, app): self.app = app
    async def recordar(self, user_id): await self.app.bot.send_message(user_id, "⚠️ Tu suscripción vence en 12h. Renueva para evitar suspensión.")
    async def expirar(self, user_id):
        await self.app.bot.ban_chat_member(CHANNEL_ID, user_id)
        await self.app.bot.send_message(user_id, "❌ Tu suscripción expiró. Has sido baneado. Paga para reactivarte.")
    def programar(self, user_id, exp):
        scheduler.add_job(self.recordar, DateTrigger(run_date=exp - timedelta(hours=12)), args=[user_id])
        scheduler.add_job(self.expirar, DateTrigger(run_date=exp), args=[user_id])

# -------------------- Handlers de Telegram --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    # Mostrar o no la opción de Champions según CHAMPIONS_ENABLED
    if CHAMPIONS_ENABLED:
        kb.append([InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="tipo_mensual")])
        kb.append([InlineKeyboardButton("⚽ Champions $10 (2 días)", callback_data="tipo_promo")])
    else:
        kb.append([InlineKeyboardButton("💳 Mensual $30 (30 días)", callback_data="tipo_mensual")])

    markup = InlineKeyboardMarkup(kb)
    await update.message.reply_text("👋 ¡Bienvenido! Selecciona tu plan:", reply_markup=markup)

async def seleccionar_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tipo = q.data.split("_")[1]

    # Si el usuario seleccionó promo pero la promocion está deshabilitada, notificar
    if tipo == "promo" and not CHAMPIONS_ENABLED:
        await q.edit_message_text("La promoción Champions no está disponible actualmente. Por favor elige Mensual.")
        return

    context.user_data["tipo"] = tipo

    # Si es plan mensual, preguntar si tiene código
    if tipo == "mensual":
        context.user_data["esperando_codigo"] = True
        await q.edit_message_text(
            f"Seleccionaste {SUBS[tipo]['nombre']} (${SUBS[tipo]['monto']}).\n"
            "¿Tienes un código de descuento? Escribe el código (por ejemplo: BRYAN10), o escribe 'NO' si no tienes uno."
        )
    else:
        # Si es promo (Champions), pasa directo a pedir número
        kb = ReplyKeyboardMarkup([[KeyboardButton("📱 COMPARTIR NÚMERO", request_contact=True)]], resize_keyboard=True)
        await q.edit_message_text(f"Seleccionaste {SUBS[tipo]['nombre']} (${SUBS[tipo]['monto']}). Ahora comparte tu número.")
        await q.message.reply_text("Pulsa el botón para compartir tu número:", reply_markup=kb)

async def recibir_codigo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Este handler captura el texto cuando el bot está esperando un código
    if not context.user_data.get("esperando_codigo"):
        return
    codigo = update.message.text.strip().upper()
    context.user_data["esperando_codigo"] = False
    context.user_data["codigo_promocional"] = codigo if codigo != "NO" else ""

    kb = ReplyKeyboardMarkup([[KeyboardButton("📱 COMPARTIR NÚMERO", request_contact=True)]], resize_keyboard=True)
    await update.message.reply_text(
        "Perfecto 👍 Ahora comparte tu número para continuar.",
        reply_markup=kb
    )

async def recibir_contacto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    c = update.effective_message.contact
    if not c or c.user_id != update.effective_user.id:
        return await update.message.reply_text("Comparte tu propio número.")

    tipo = context.user_data.get("tipo")
    if not tipo:
        return await update.message.reply_text("Primero elige una suscripción con /start.")

    sub = SUBS[tipo]
    monto_final = sub["monto"]
    mensaje_descuento = ""

    # Aplicar descuento solo para plan mensual
    if tipo == "mensual":
        codigo = context.user_data.get("codigo_promocional", "").upper()
        if codigo and codigo in CODIGOS_PROMO:
            desc = CODIGOS_PROMO[codigo]
            monto_final = round(sub["monto"] * (1 - desc), 2)
            mensaje_descuento = f"✅ Código aplicado: {codigo} ({int(desc * 100)}% de descuento) — Nuevo monto: ${monto_final:.2f}\nApoyaste al aliado."

            # registrar uso del referido
            promo_creador = f"{codigo}_creador"  # Si quieres mapeo a @handles, puedes extender CODIGOS_PROMO a dicts
            csv_referidos.append({
                "timestamp_utc": datetime.utcnow().isoformat(),
                "user_id": update.effective_user.id,
                "codigo": codigo,
                "creador": promo_creador,
                "descuento": int(desc * 100)
            })
        elif codigo:
            mensaje_descuento = f"⚠️ Código {codigo} no válido. Se aplicará el precio normal (${monto_final:.2f})."

    # guardar telefono
    csv_phones.append({"timestamp_utc": datetime.utcnow().isoformat(), "user_id": update.effective_user.id, "phone": c.phone_number})

    ref = f"tg_{update.effective_user.id}_{int(time.time())}"
    data = wompi.crear_enlace(ref, monto_final, sub["nombre"])
    csv_links.append({
        "timestamp_utc": datetime.utcnow().isoformat(),
        "user_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "username": update.effective_user.username or "sin",
        "referencia": ref,
        "idEnlace": data.get("idEnlace") or data.get("id"),
        "urlEnlace": data.get("urlEnlace") or data.get("url"),
        "monto_usd": monto_final
    })

    texto = f"💳 Enlace de pago:\n{data.get('urlEnlace') or data.get('url')}\n\nReferencia: {ref}\nMonto: ${monto_final:.2f}"
    if mensaje_descuento:
        texto = f"{mensaje_descuento}\n\n" + texto

    await update.message.reply_text(texto, reply_markup=ReplyKeyboardRemove())

async def validar_pago(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = csv_links.get_today_rows(update.effective_user.id)
    if not rows: return await update.message.reply_text("No hay pagos recientes. Usa /start para crear uno.")
    reg = rows[-1]
    data = wompi.consultar(int(reg["idEnlace"]))
    estado = wompi.estado(data)
    if estado == "aprobada":
        tipo = "promo" if "promo" in reg["referencia"] else "mensual"
        dias = SUBS[tipo]["dias"]
        exp = datetime.now(timezone.utc) + timedelta(days=dias)
        csv_subs.append({"user_id": update.effective_user.id, "tipo": tipo, "expiracion_utc": exp.isoformat(), "estado": "activa"})
        try:
            await context.bot.unban_chat_member(CHANNEL_ID, update.effective_user.id)
        except Exception:
            pass
        link = await context.bot.create_chat_invite_link(CHANNEL_ID, expire_date=datetime.now(timezone.utc)+timedelta(hours=1), member_limit=1)
        await update.message.reply_text(f"✅ Pago aprobado. Acceso válido hasta {exp.astimezone(LOCAL_TZ)}.\n\nLink (1h): {link.invite_link}")
        subm.programar(update.effective_user.id, exp)
    else:
        await update.message.reply_text("⌛ Aún pendiente. Intenta más tarde.")

# -------------------- Modo local o webhook --------------------
async def setup_app():
    global subm
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("validar_pago", validar_pago))
    app.add_handler(CallbackQueryHandler(seleccionar_tipo, pattern="^tipo_"))
    app.add_handler(MessageHandler(filters.CONTACT, recibir_contacto))

    # handler para capturar códigos y otros textos (solo cuando esperamos código)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_codigo))

    subm = SubManager(app)
    scheduler.start()
    return app

if MODE == "local":
    app = asyncio.run(setup_app())
    app.run_polling()
else:
    fastapi_app = FastAPI()
    application = asyncio.run(setup_app())

    @fastapi_app.post("/webhook")
    async def webhook(req: Request):
        data = await req.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"ok": True}

    @fastapi_app.on_event("startup")
    async def on_startup(): await application.bot.set_webhook(url=WEBHOOK_URL)

    if __name__ == "__main__":
        uvicorn.run(fastapi_app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
