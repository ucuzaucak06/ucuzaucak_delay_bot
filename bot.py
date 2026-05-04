"""
Flight Delay Telegram Bot - FlightLabs API
- /izle <IATA> <YYYY-MM-DD>  -> havalimanını izlemeye başlar (sadece bugün/yarın)
- /durdur <IATA> <YYYY-MM-DD> -> izlemeyi durdurur
- /liste                       -> aktif izlemeleri gösterir
- /debug <IATA> [FLIGHT_NO]    -> ham veriyi gösterir (teşhis)
- 60dk+ gecikme olunca bildirim gönderir, her uçuşu sadece 1 kez bildirir
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------- Ayarlar ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
FLIGHTLABS_KEY = os.environ["FLIGHTLABS_KEY"]
DELAY_THRESHOLD_MIN = int(os.environ.get("DELAY_THRESHOLD_MIN", "60"))
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "1800"))  # 30 dk
PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "200"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "/tmp/flight_bot_state.json"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("flightbot")


# ---------- State ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"watches": {}, "notified": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ---------- FlightLabs API ----------
def fetch_departures(iata: str) -> list[dict]:
    """
    FlightLabs advanced-flights-schedules endpoint.
    Departure programını çeker; has_more varsa skip ile sayfalama yapar.
    Response yapısı: data dict olup, sayısal anahtarlarda uçuşlar var.
    """
    url = "https://www.goflightlabs.com/advanced-flights-schedules"
    all_flights = []
    skip = 0
    max_pages = 5  # güvenlik

    for _ in range(max_pages):
        params = {
            "access_key": FLIGHTLABS_KEY,
            "iataCode": iata.upper(),
            "type": "departure",
            "limit": PAGE_LIMIT,
            "skip": skip,
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        if not payload.get("success"):
            raise RuntimeError(f"FlightLabs hata: {payload}")

        data = payload.get("data", {})
        # data formatı: {"0": {...}, "1": {...}, "limit": N, "skip": N, "total_items": N, "has_more": bool}
        flights = []
        for k, v in data.items():
            if k.isdigit() and isinstance(v, dict):
                flights.append(v)

        all_flights.extend(flights)

        if not data.get("has_more"):
            break
        skip += PAGE_LIMIT

    return all_flights


def compute_delay_minutes(flight: dict) -> int:
    """
    Öncelik: dep_delayed -> delayed -> timestamp farkları.
    """
    # 1) dep_delayed (dakika cinsinden direkt)
    if flight.get("dep_delayed") is not None:
        try:
            return int(flight["dep_delayed"])
        except (TypeError, ValueError):
            pass

    # 2) delayed (genel gecikme)
    if flight.get("delayed") is not None:
        try:
            return int(flight["delayed"])
        except (TypeError, ValueError):
            pass

    # 3) dep_time_ts vs dep_actual_ts
    sched_ts = flight.get("dep_time_ts")
    actual_ts = flight.get("dep_actual_ts")
    if sched_ts and actual_ts:
        try:
            return int((int(actual_ts) - int(sched_ts)) // 60)
        except Exception:
            pass

    # 4) dep_time_ts vs dep_estimated_ts
    est_ts = flight.get("dep_estimated_ts")
    if sched_ts and est_ts:
        try:
            return int((int(est_ts) - int(sched_ts)) // 60)
        except Exception:
            pass

    return 0


def format_alert(flight: dict, delay: int) -> str:
    flight_no = flight.get("flight_iata") or flight.get("flight_number") or "?"
    airline = flight.get("airline_iata", "?")
    arr = flight.get("arr_iata", "?")
    sched = flight.get("dep_time", "?")
    actual = flight.get("dep_actual") or flight.get("dep_estimated") or "—"
    status = flight.get("status", "?")
    return (
        f"🛫 *Gecikme uyarısı*\n"
        f"Uçuş: `{flight_no}` ({airline})\n"
        f"Hedef: {arr}\n"
        f"Planlanan: {sched}\n"
        f"Gerçek/Tahmini: {actual}\n"
        f"Gecikme: *{delay} dk*\n"
        f"Durum: {status}"
    )


# ---------- İzleme görevi ----------
async def check_airport_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    iata = job.data["iata"]
    flight_date = job.data["date"]
    chat_id = job.data["chat_id"]
    key = f"{chat_id}:{iata}:{flight_date}"

    state = load_state()
    notified: dict = state.setdefault("notified", {}).setdefault(key, {})

    try:
        flights = fetch_departures(iata)
    except Exception as e:
        log.error("Fetch hatası %s: %s", iata, e)
        return

    # Sadece izlenen tarihteki uçuşlar
    today_flights = [
        f for f in flights
        if (f.get("dep_time") or "")[:10] == flight_date
    ]

    log.info("Çekildi %s: toplam %d, %s tarihinde %d",
             iata, len(flights), flight_date, len(today_flights))

    new_alerts = 0
    for fl in today_flights:
        delay = compute_delay_minutes(fl)
        if delay < DELAY_THRESHOLD_MIN:
            continue

        status = (fl.get("status") or "").lower()
        if status in ("cancelled", "canceled"):
            continue

        flight_id = fl.get("flight_iata") or fl.get("flight_number")
        if not flight_id:
            continue

        prev = notified.get(flight_id, 0)
        # İlk geçişte VEYA gecikme +30dk daha arttıysa tekrar bildir
        if prev == 0 or delay >= prev + 30:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=format_alert(fl, delay),
                    parse_mode="Markdown",
                )
                notified[flight_id] = delay
                new_alerts += 1
            except Exception as e:
                log.error("Mesaj gönderilemedi: %s", e)

    save_state(state)
    log.info("%s: %d yeni uyarı", key, new_alerts)

    # Tarih geçtiyse görevi kapat
    try:
        d = datetime.strptime(flight_date, "%Y-%m-%d").date()
        if datetime.now(timezone.utc).date() > d:
            log.info("Tarih geçti, %s görevi kapatılıyor", key)
            job.schedule_removal()
            state["watches"].pop(key, None)
            save_state(state)
    except Exception:
        pass


# ---------- Komutlar ----------
HELP = (
    "✈️ *Uçuş Gecikme Botu*\n\n"
    "`/izle SAW 2026-05-04` — havalimanını izlemeye başla\n"
    "`/durdur SAW 2026-05-04` — izlemeyi durdur\n"
    "`/liste` — aktif izlemelerin\n"
    "`/debug SAW` — tüm uçuşların özeti\n"
    "`/debug SAW PC316` — belirli uçuşun ham verisi\n"
    "`/yardim` — bu mesaj\n\n"
    f"Eşik: {DELAY_THRESHOLD_MIN} dk · Yoklama: {POLL_INTERVAL_SEC // 60} dk"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def cmd_yardim(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


def _validate_args(args: list[str]) -> tuple[str, str] | None:
    if len(args) != 2:
        return None
    iata, date_str = args[0].upper(), args[1]
    if len(iata) != 3 or not iata.isalpha():
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now(timezone.utc).date()
    if d < today or d > today + timedelta(days=1):
        return None
    return iata, date_str


async def cmd_izle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parsed = _validate_args(ctx.args)
    if not parsed:
        await update.message.reply_text(
            "Kullanım: `/izle SAW 2026-05-04`\n"
            "• IATA 3 harf olmalı\n"
            "• Tarih bugün veya yarın olmalı",
            parse_mode="Markdown",
        )
        return
    iata, date_str = parsed
    chat_id = update.effective_chat.id
    key = f"{chat_id}:{iata}:{date_str}"

    state = load_state()
    if key in state["watches"]:
        await update.message.reply_text(f"Zaten izleniyor: {iata} {date_str}")
        return

    ctx.job_queue.run_repeating(
        check_airport_job,
        interval=POLL_INTERVAL_SEC,
        first=5,
        name=key,
        data={"iata": iata, "date": date_str, "chat_id": chat_id},
    )
    state["watches"][key] = {"iata": iata, "date": date_str, "chat_id": chat_id}
    save_state(state)

    await update.message.reply_text(
        f"✅ İzleniyor: *{iata}* kalkışları, tarih *{date_str}*.\n"
        f"{DELAY_THRESHOLD_MIN}dk+ gecikme olursa haber veririm.\n"
        f"İlk tarama 5 saniye içinde başlar.",
        parse_mode="Markdown",
    )


async def cmd_durdur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parsed = _validate_args(ctx.args)
    if not parsed:
        await update.message.reply_text("Kullanım: `/durdur SAW 2026-05-04`", parse_mode="Markdown")
        return
    iata, date_str = parsed
    chat_id = update.effective_chat.id
    key = f"{chat_id}:{iata}:{date_str}"

    jobs = ctx.job_queue.get_jobs_by_name(key)
    for j in jobs:
        j.schedule_removal()

    state = load_state()
    state["watches"].pop(key, None)
    state.get("notified", {}).pop(key, None)
    save_state(state)

    await update.message.reply_text(
        "🛑 Durduruldu" if jobs else "Aktif izleme bulunamadı."
    )


async def cmd_liste(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = load_state()
    mine = [w for k, w in state["watches"].items() if w["chat_id"] == chat_id]
    if not mine:
        await update.message.reply_text("Aktif izleme yok.")
        return
    lines = [f"• {w['iata']} — {w['date']}" for w in mine]
    await update.message.reply_text("Aktif izlemeler:\n" + "\n".join(lines))


async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /debug SAW            -> en gecikmeli 5 + status dağılımı
    /debug SAW PC316      -> belirli uçuşun ham verisi
    """
    args = ctx.args
    if len(args) < 1:
        await update.message.reply_text(
            "Kullanım: `/debug SAW` veya `/debug SAW PC316`",
            parse_mode="Markdown",
        )
        return

    iata = args[0].upper()
    flight_filter = args[1].upper() if len(args) >= 2 else None

    await update.message.reply_text(f"⏳ {iata} kalkışları çekiliyor...")

    try:
        flights = fetch_departures(iata)
    except Exception as e:
        await update.message.reply_text(f"❌ API hatası: {e}")
        return

    await update.message.reply_text(f"✅ Toplam {len(flights)} uçuş döndü.")

    if flight_filter:
        matched = [
            f for f in flights
            if (f.get("flight_iata") or "").upper() == flight_filter
            or (f.get("flight_icao") or "").upper() == flight_filter
        ]
        if not matched:
            sample = ", ".join(
                (f.get("flight_iata") or "?") for f in flights[:8]
            )
            await update.message.reply_text(
                f"❌ {flight_filter} listede yok.\nİlk 8 uçuş: {sample}"
            )
            return

        for fl in matched[:3]:
            delay = compute_delay_minutes(fl)
            msg = (
                f"🔍 *{fl.get('flight_iata', '?')}*\n"
                f"Hedef: {fl.get('arr_iata', '?')}\n"
                f"Status: `{fl.get('status', '?')}`\n"
                f"dep_time: `{fl.get('dep_time', '-')}`\n"
                f"dep_estimated: `{fl.get('dep_estimated', '-')}`\n"
                f"dep_actual: `{fl.get('dep_actual', '-')}`\n"
                f"dep_delayed: `{fl.get('dep_delayed', '-')}`\n"
                f"delayed: `{fl.get('delayed', '-')}`\n"
                f"*Hesaplanan gecikme: {delay} dk*"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        with_delay = [(compute_delay_minutes(f), f) for f in flights]
        with_delay.sort(key=lambda x: -x[0])

        lines = ["*En gecikmeli 5 uçuş:*"]
        for d, fl in with_delay[:5]:
            lines.append(
                f"• `{fl.get('flight_iata', '?')}` → "
                f"{fl.get('arr_iata', '?')} | {d} dk | "
                f"{fl.get('status', '?')}"
            )

        status_count = {}
        for f in flights:
            s = f.get("status", "?")
            status_count[s] = status_count.get(s, 0) + 1
        lines.append("\n*Durum dağılımı:*")
        for s, c in sorted(status_count.items(), key=lambda x: -x[1]):
            lines.append(f"• {s}: {c}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- Yeniden başlatmada izlemeleri geri yükle ----------
async def post_init(app: Application):
    state = load_state()
    today = datetime.now(timezone.utc).date()
    restored = 0
    for key, w in list(state["watches"].items()):
        try:
            d = datetime.strptime(w["date"], "%Y-%m-%d").date()
            if d < today:
                state["watches"].pop(key, None)
                continue
        except Exception:
            continue
        app.job_queue.run_repeating(
            check_airport_job,
            interval=POLL_INTERVAL_SEC,
            first=10,
            name=key,
            data=w,
        )
        restored += 1
    save_state(state)
    log.info("Geri yüklenen izleme: %d", restored)


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("yardim", cmd_yardim))
    app.add_handler(CommandHandler("help", cmd_yardim))
    app.add_handler(CommandHandler("izle", cmd_izle))
    app.add_handler(CommandHandler("durdur", cmd_durdur))
    app.add_handler(CommandHandler("liste", cmd_liste))
    app.add_handler(CommandHandler("debug", cmd_debug))
    log.info("Bot başlıyor...")
    app.run_polling()


if __name__ == "__main__":
    main()
