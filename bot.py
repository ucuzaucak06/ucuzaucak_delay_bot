"""
Flight Delay Telegram Bot
- /izle <IATA> <YYYY-MM-DD>  -> o havalimanını izlemeye başlar
- /durdur <IATA> <YYYY-MM-DD> -> izlemeyi durdurur
- /liste                       -> aktif izlemeleri gösterir
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
AVIATIONSTACK_KEY = os.environ["AVIATIONSTACK_KEY"]
DELAY_THRESHOLD_MIN = int(os.environ.get("DELAY_THRESHOLD_MIN", "60"))
POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "900"))  # 15 dk

STATE_FILE = Path(os.environ.get("STATE_FILE", "/tmp/flight_bot_state.json"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("flightbot")


# ---------- State (kalıcı izleme listesi + bildirilmiş uçuşlar) ----------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"watches": {}, "notified": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ---------- AviationStack çağrısı ----------
def fetch_departures(iata: str, flight_date: str) -> list[dict]:
    """
    AviationStack /flights endpoint - dep_iata + flight_date filtresi.
    Ücretsiz tier'da http (https değil) ve flight_date çalışır.
    """
    url = "http://api.aviationstack.com/v1/flights"
    params = {
        "access_key": AVIATIONSTACK_KEY,
        "dep_iata": iata.upper(),
        "flight_date": flight_date,
        "limit": 100,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"AviationStack hata: {data['error']}")
    return data.get("data", [])


def compute_delay_minutes(flight: dict) -> int:
    """
    Önce actual (kalkmış uçuş) → estimated (tahmini) → delay alanı sırasıyla bakar.
    AviationStack ücretsiz tier'da delay alanı sıklıkla null gelir.
    """
    dep = flight.get("departure") or {}
    sched = dep.get("scheduled")

    # En güvenilir: scheduled vs actual (uçuş kalkmışsa)
    actual = dep.get("actual")
    if sched and actual:
        try:
            s = datetime.fromisoformat(sched.replace("Z", "+00:00"))
            a = datetime.fromisoformat(actual.replace("Z", "+00:00"))
            return int((a - s).total_seconds() // 60)
        except Exception:
            pass

    # Sonra: scheduled vs estimated (kalkmadıysa tahmini)
    est = dep.get("estimated")
    if sched and est:
        try:
            s = datetime.fromisoformat(sched.replace("Z", "+00:00"))
            e = datetime.fromisoformat(est.replace("Z", "+00:00"))
            return int((e - s).total_seconds() // 60)
        except Exception:
            pass

    # Son çare: delay alanı (string veya int olabilir)
    if dep.get("delay") is not None:
        try:
            return int(dep["delay"])
        except (TypeError, ValueError):
            pass

    return 0


def format_alert(flight: dict, delay: int) -> str:
    f = flight.get("flight") or {}
    airline = (flight.get("airline") or {}).get("name", "?")
    flight_no = f.get("iata") or f.get("number") or "?"
    arr = (flight.get("arrival") or {}).get("iata", "?")
    arr_name = (flight.get("arrival") or {}).get("airport", "")
    sched = (flight.get("departure") or {}).get("scheduled", "?")
    status = flight.get("flight_status", "?")
    return (
        f"🛫 *Gecikme uyarısı*\n"
        f"Uçuş: `{flight_no}` ({airline})\n"
        f"Hedef: {arr} {arr_name}\n"
        f"Planlanan kalkış: {sched}\n"
        f"Gecikme: *{delay} dk*\n"
        f"Durum: {status}"
    )


# ---------- İzleme görevi (her havalimanı/tarih için) ----------
async def check_airport_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    iata, flight_date, chat_id = job.data["iata"], job.data["date"], job.data["chat_id"]
    key = f"{chat_id}:{iata}:{flight_date}"

    state = load_state()
    notified: dict = state.setdefault("notified", {}).setdefault(key, {})

    try:
        flights = fetch_departures(iata, flight_date)
    except Exception as e:
        log.error("Fetch hatası %s %s: %s", iata, flight_date, e)
        return

    log.info("Çekildi: %s %s -> %d uçuş", iata, flight_date, len(flights))

    new_alerts = 0
    for fl in flights:
        delay = compute_delay_minutes(fl)
        if delay < DELAY_THRESHOLD_MIN:
            continue
        f = fl.get("flight") or {}
        flight_id = f.get("iata") or f.get("number")
        if not flight_id:
            continue
        prev = notified.get(flight_id, 0)
        # Sadece ilk geçişte VEYA gecikme +30dk daha arttıysa tekrar bildir
        if prev == 0 or delay >= prev + 30:
            await context.bot.send_message(
                chat_id=chat_id,
                text=format_alert(fl, delay),
                parse_mode="Markdown",
            )
            notified[flight_id] = delay
            new_alerts += 1

    save_state(state)

    # Tarih geçtiyse görevi kendiliğinden durdur
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
    "`/izle LUX 2025-05-03` — havalimanını izlemeye başla\n"
    "`/durdur LUX 2025-05-03` — izlemeyi durdur\n"
    "`/liste` — aktif izlemelerin\n"
    "`/yardim` — bu mesaj\n\n"
    f"Eşik: {DELAY_THRESHOLD_MIN} dk · Yoklama: {POLL_INTERVAL_SEC // 60} dk · "
    "Tarih sadece önümüzdeki 24 saat içinde olmalı."
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
            "Kullanım: `/izle LUX 2025-05-03`\n"
            "• IATA 3 harf olmalı\n"
            "• Tarih bugün veya yarın olmalı (sadece 24 saat penceresi)",
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
        f"{DELAY_THRESHOLD_MIN}dk+ gecikme olursa haber veririm.",
        parse_mode="Markdown",
    )


async def cmd_durdur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parsed = _validate_args(ctx.args)
    if not parsed:
        await update.message.reply_text("Kullanım: `/durdur LUX 2025-05-03`", parse_mode="Markdown")
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
    /debug SAW 2026-05-04 [PC316]
    Belirli havalimanı/tarih için API'nin döndürdüğü uçuşları gösterir.
    Opsiyonel 3. argüman: belirli bir uçuş numarası (örn. PC316)
    """
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "Kullanım: `/debug SAW 2026-05-04` veya `/debug SAW 2026-05-04 PC316`",
            parse_mode="Markdown",
        )
        return
    iata, date_str = args[0].upper(), args[1]
    flight_filter = args[2].upper() if len(args) >= 3 else None

    await update.message.reply_text(f"⏳ {iata} {date_str} için API çağrılıyor...")

    try:
        flights = fetch_departures(iata, date_str)
    except Exception as e:
        await update.message.reply_text(f"❌ API hatası: {e}")
        return

    await update.message.reply_text(
        f"✅ API'den {len(flights)} uçuş döndü."
    )

    # Filtre varsa o uçuşları bul
    if flight_filter:
        matched = [
            f for f in flights
            if (f.get("flight") or {}).get("iata", "").upper() == flight_filter
            or (f.get("flight") or {}).get("number", "").upper() == flight_filter.replace("PC", "")
        ]
        if not matched:
            # Geniş arama: numarayla başlayanlar
            matched = [
                f for f in flights
                if flight_filter in (f.get("flight") or {}).get("iata", "").upper()
            ]
        if not matched:
            await update.message.reply_text(
                f"❌ {flight_filter} listede yok. İlk 5 uçuş numarası: " +
                ", ".join((f.get("flight") or {}).get("iata", "?") for f in flights[:5])
            )
            return

        for fl in matched[:3]:
            dep = fl.get("departure") or {}
            arr = fl.get("arrival") or {}
            f_info = fl.get("flight") or {}
            delay = compute_delay_minutes(fl)
            msg = (
                f"🔍 *{f_info.get('iata', '?')}*\n"
                f"Hedef: {arr.get('iata', '?')}\n"
                f"Status: `{fl.get('flight_status', '?')}`\n"
                f"scheduled: `{dep.get('scheduled', '-')}`\n"
                f"estimated: `{dep.get('estimated', '-')}`\n"
                f"actual: `{dep.get('actual', '-')}`\n"
                f"delay alanı: `{dep.get('delay', '-')}`\n"
                f"*Hesaplanan gecikme: {delay} dk*"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        # Filtre yok: en gecikmeli 5 uçuşu göster
        with_delay = []
        for fl in flights:
            d = compute_delay_minutes(fl)
            with_delay.append((d, fl))
        with_delay.sort(key=lambda x: -x[0])

        lines = ["*En gecikmeli 5 uçuş:*"]
        for d, fl in with_delay[:5]:
            f_info = fl.get("flight") or {}
            arr = (fl.get("arrival") or {}).get("iata", "?")
            status = fl.get("flight_status", "?")
            lines.append(
                f"• `{f_info.get('iata', '?')}` → {arr} | {d} dk | {status}"
            )
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
