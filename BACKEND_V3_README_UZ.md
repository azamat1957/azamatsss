# UZ NAVO BOT — Backend V3

Bu versiya eski monolit media yuklash qismini barqarorroq arxitekturaga o'tkazadi.

## Nima o'zgardi

- **PyTubeFix asosiy YouTube provider**.
- **yt-dlp fallback provider**. Birinchi provider ishlamasa keyingisi avtomatik sinanadi.
- **Circuit breaker**: bir provider ketma-ket xato bersa vaqtincha o'chadi va trafik boshqasiga o'tadi.
- **Redis media queue**: audio/video/effekt/Shazam ishlarini bot jarayonidan ajratadi.
- **Worker pool**: og'ir media ishlari alohida workerlar tomonidan bajariladi.
- **Telegram file_id cache**: oldin yuborilgan audio/video qayta yuklanmasdan Telegram cache orqali tez yuboriladi.
- **Safe errors**: foydalanuvchiga `Sign in to confirm...`, cookie yoki traceback matnlari yuborilmaydi; texnik xato server logida qoladi.
- **Inline fallback**: Redis ishlamasa bot media ishini o'z jarayonida davom ettirishga urinadi.

## Muhim cheklov

Bu tizim YouTube autentifikatsiyasi yoki anti-bot himoyasini chetlab o'tmaydi. Agar platforma server IP yoki providerga media bermasa, provider manager boshqa ruxsatli usulni sinaydi. Barcha providerlar rad etsa foydalanuvchiga qisqa xabar chiqadi.

## Railway — eng oson deploy

1. Repo/ZIP ichidagi fayllarni Railway loyihangizga joylang.
2. Variables ichida kamida quyidagilar bo'lsin:
   - `BOT_TOKEN`
   - `REQUIRED_CHANNEL`
   - `REQUIRED_CHANNEL_URL`
   - `REQUIRED_CHANNEL_NAME`
3. Railway yoki Upstash orqali Redis yarating va uning URL manzilini `REDIS_URL` ga yozing.
4. Oddiy bitta servis uchun `SERVICE_MODE=all` qoldiring.
5. Deploy qiling.

`run_backend.py` Redis bor bo'lsa bot va worker jarayonlarini birga ishga tushiradi. Redis bo'lmasa faqat bot ishga tushadi va inline fallback ishlaydi.

## Kuchliroq variant: bot va workerlarni ajratish

Bir xil repodan ikki Railway service yarating:

### Service 1 — BOT
- `SERVICE_MODE=bot`
- `BOT_TOKEN` va `REDIS_URL` bir xil

### Service 2 — WORKER
- `SERVICE_MODE=worker`
- `BOT_TOKEN` va `REDIS_URL` bir xil
- kerak bo'lsa `WORKER_CONCURRENCY=2` yoki `3`

Keyin Worker service'ni 2-3 replica qilish mumkin. Redis queue joblarni workerlar orasida navbat bilan taqsimlaydi.

## Asosiy fayllar

- `bot.py` — Telegram UI, qidiruv, tugmalar, obuna
- `media_providers.py` — provider fallback + circuit breaker
- `job_queue.py` — Redis queue va media cache
- `worker.py` — audio/video/effekt/Shazam worker
- `run_backend.py` — service mode launcher
- `Dockerfile` — Railway deploy

## Tavsiya etilgan Variables

```env
SERVICE_MODE=all
REDIS_URL=redis://...
YOUTUBE_PROVIDERS=pytubefix,yt-dlp
WORKER_CONCURRENCY=2
JOB_RETRIES=1
PROVIDER_FAILURE_THRESHOLD=3
PROVIDER_COOLDOWN_SECONDS=120
MEDIA_CACHE_TTL_SECONDS=604800
```

## Server/IP haqida

Workerlarni turli serverlarda ko'paytirish mumkin. Bu **load balancing va failover** uchun. YouTube'ning bot tekshiruvini yashirin bypass qilish uchun IP aylantirish bu loyihaning maqsadi emas.
