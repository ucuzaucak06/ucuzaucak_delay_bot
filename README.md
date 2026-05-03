# Flight Delay Telegram Bot

Belirttiğin havalimanı kalkış uçuşlarını izler, **60 dakika veya daha fazla** gecikme olunca Telegram'dan bildirim gönderir.

## Komutlar

- `/izle LUX 2025-05-03` — havalimanını izlemeye başla (sadece bugün veya yarın)
- `/durdur LUX 2025-05-03` — izlemeyi durdur
- `/liste` — aktif izlemelerin
- `/yardim` — yardım

## Kurulum

### 1. Telegram bot token al

1. Telegram'da [@BotFather](https://t.me/BotFather) ile sohbet aç
2. `/newbot` yaz, isim ve kullanıcı adı belirle
3. Verdiği **token**'ı kaydet

### 2. AviationStack API key al

1. https://aviationstack.com/ adresine kaydol (ücretsiz tier: 100 istek/ay)
2. Dashboard'da **API Access Key**'i kopyala

> ⚠️ Ücretsiz tier düşük (100/ay). 1 havalimanını 24 saat boyunca 15dk aralıkla izlemek = 96 istek. Yani ayda fiilen 1 günlük izleme. Ciddi kullanım için $9.99/ay Basic plan gerekir (10,000 istek).

### 3. Yerel test

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=xxx
export AVIATIONSTACK_KEY=yyy
python bot.py
```

### 4. Railway'e deploy (önerilen)

1. https://railway.app → New Project → Deploy from GitHub repo
2. Bu klasörü repo olarak push et
3. Variables sekmesinden ekle:
   - `TELEGRAM_TOKEN`
   - `AVIATIONSTACK_KEY`
   - `DELAY_THRESHOLD_MIN=60` (opsiyonel)
   - `POLL_INTERVAL_SEC=900` (opsiyonel, 15 dk)
4. Deploy → Logs'tan "Bot başlıyor..." görmelisin

### 5. Render'a deploy (alternatif)

1. https://render.com → New → Background Worker
2. Repo'yu bağla
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. Env vars'ları ekle (yukarıdaki gibi)

> **Önemli:** Render/Railway'de free tier ephemeral disk kullanır, restart'ta state kaybolur. Bot yeniden başlayınca aktif izlemeler `STATE_FILE`'dan geri yüklenir ama sadece dosya kalıcıysa. Railway'de "Volume" ekle ve `STATE_FILE=/data/state.json` yap.

## Nasıl çalışır

- `/izle` komutu bir izleme görevi (job) oluşturur
- Her **15 dakikada** bir AviationStack'ten o havalimanının kalkış listesi çekilir
- Her uçuş için `departure.delay` alanına bakılır (yoksa scheduled vs estimated farkı hesaplanır)
- Eşik aşılırsa Telegram'a tek mesaj gönderilir
- Aynı uçuş için tekrar bildirilmez; ancak gecikme **+30 dk daha artarsa** güncelleme atılır
- Tarih geçtikten sonra görev otomatik kapanır

## Sınırlar

- Sadece IATA 3 harfli havalimanı kodu (LUX, IST, SAW, FRA...)
- Sadece bugün veya yarın
- AviationStack ücretsiz tier'da gecikme verisi gecikmeli/eksik gelebilir
- AviationStack `flight_status` filtresi ücretli, biz manuel filtreliyoruz
