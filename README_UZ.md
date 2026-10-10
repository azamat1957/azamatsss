# Kuynavo/Shazam uslubidagi Telegram bot

Bu loyiha quyidagilarni bajaradi:

- Telegram audio/voice faylidan qo'shiqni taniydi
- Telegram video/video-note ichidagi musiqani taniydi
- YouTube havolasidan videoni yuklaydi
- Instagram Reel/Post havolasidan mediani yuklaydi
- Yuklangan videodan MP3 ajratadi
- Videodagi musiqani Shazam orqali aniqlaydi
- Guruhlarda ham ishlaydi

## 1. Talablar

Tavsiya:

- Windows 10/11 yoki Linux
- Python 3.11+
- FFmpeg
- Internet

YouTube'ning to'liq yt-dlp qo'llovi uchun JavaScript runtime ham foydali.
Deno yoki Node.js o'rnatishingiz mumkin.

## 2. Python o'rnatish

Windows'da Python 3.11 yoki 3.12 ni o'rnating.
O'rnatishda **Add Python to PATH** ni belgilang.

Tekshirish:

```bat
python --version
```

## 3. FFmpeg

FFmpeg binary o'rnating va `ffmpeg.exe` joylashgan `bin` papkani PATH'ga qo'shing.

Tekshirish:

```bat
ffmpeg -version
```

## 4. Loyiha

Arxivni oching va papkada terminal oching.

Virtual environment:

```bat
python -m venv .venv
.venv\Scripts\activate
```

Kutubxonalar:

```bat
python -m pip install -U pip
pip install -r requirements.txt
```

## 5. Token

`.env.example` faylidan nusxa oling va nomini `.env` qiling:

```bat
copy .env.example .env
```

`.env` ni Notepad bilan ochib:

```env
BOT_TOKEN=PASTE_YOUR_BOT_TOKEN_HERE
```

o'rniga BotFather tokeningizni yozing.

Tokenni `bot.py` ichiga yozmang.

## 6. Ishga tushirish

```bat
python bot.py
```

Yoki `run_windows.bat` faylini oching.

## 7. Foydalanish

Botga quyidagilardan birini yuboring:

- voice
- audio
- video
- video note
- YouTube link
- Instagram Reel/Post link

URL yuborilganda bot:
1. videoni oladi;
2. MP3 ajratadi;
3. Shazam orqali qo'shiqni topadi;
4. video + MP3 + natijani Telegramga yuboradi.

## Muhim cheklovlar

- Faqat public YouTube/Instagram URL lar ko'zda tutilgan.
- Private/DRM bilan himoyalangan kontentni aylanib o'tish qo'shilmagan.
- Instagram yoki YouTube sayt tomondan o'zgarsa, `yt-dlp` ni yangilash kerak bo'lishi mumkin:

```bat
pip install -U yt-dlp yt-dlp-ejs
```

- Katta videolar Telegram Bot API limitiga tushmasligi mumkin. `.env` dagi
  `MAX_SEND_MB` bilan limit boshqariladi.
- Faqat yuklab olish huquqingiz/ruxsatingiz bo'lgan kontentdan foydalaning.

## Keyingi qo'shilishi mumkin bo'lgan funksiyalar

- Inline tugmalar: Video / MP3 / Shazam
- TikTok, Facebook, SoundCloud
- Admin panel
- PostgreSQL foydalanuvchi statistikasi
- Majburiy kanal obunasi
- Reklama tizimi
- Redis queue
- Docker/VPS deploy


## Yangi: matn bo'yicha musiqa qidirish va tugmalar

Botga oddiy matn yuboring, masalan `very sad`, `Adele lovesong`, ijrochi nomi yoki qo'shiq matnidan parcha. Bot YouTube natijalarini 1-6 tugmalar bilan ko'rsatadi. Natijani tanlaganda `Video`, `Audio` va `Musiqani aniqlash` tugmalari chiqadi. YouTube/Instagram link yuborilganda ham shu tugmalar chiqadi.

## Audio effektlar

Media kartasidagi `🎛 Effektlar` tugmasi orqali quyidagilar ishlaydi:
Slowed + Reverb, Sped Up, Bass Boost, 8D, Nightcore, Deep/Low Pitch, Echo, Vocal Boost.
Effekt faqat foydalanuvchi tugmani bosganda ishlanadi, shuning uchun oddiy qidiruvni sekinlashtirmaydi.
