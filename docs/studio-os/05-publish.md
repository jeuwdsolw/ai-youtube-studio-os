# Studio OS — Етап 5: публікація YouTube (2026-11-24)

Статус: **працює** — перше відео (еп. 019, слот 2026-11-25 18:00) завантажено агентом як private + `publishAt`,
обкладинка й SRT встановлені, статус перевірено через `videos.list`.

## Як працює
- Етап `publish` стартує лише коли `can_publish`: QC без критичних, фінал погоджено **людиною** (auto — лише з
  `auto_publish`); ручний `start … publish` цю перевірку не обходить.
- `studio_os/agents/publish.py`: метадані з `youtube.md` (назва — варіант 1, опис, теги, аудиторія, категорія, мова).
  «Змінений/синтетичний вміст» має бути явно «Так»/«Ні» — інакше етап зупиняється (рішення власниці).
- Завантаження (`adapters/youtube_upload.py`, лише stdlib): resumable upload, `privacyStatus=private` + `publishAt` = час
  слоту → YouTube сам публікує. Стан у таблиці `uploads` (URI сесії, ID відео): повтор задачі продовжує, а не дублює.
- Перевірка каналу: вантажимо лише на канал, обраний під час `youtube-auth` (інакше етап зупиняється).
- Обкладинка (`thumbnail/cover.jpg` < 2 МБ) і SRT — помилка тут не зупиняє публікацію (попередження).
- Квота: `budgets.youtube_quota_units`; вичерпано → задача відкладається до нового дня (як і бюджет сесій Claude).

## Підключення каналу (одноразово, зроблено 2026-11-24 — канал Rules of Survival)
1. Google Cloud (проєкт ключа API) → Google Auth Platform: Audience External; Clients → Desktop app → JSON у
   `data/youtube/client_secret.json` (якщо секрету в JSON немає — `YOUTUBE_CLIENT_SECRET=` у `.env`).
2. Застосунок у статусі **Testing** → пошта власника каналу в **Test users**; refresh-токен живе **7 днів** →
   раз на тиждень `python -m studio_os youtube-auth`. Щоб прибрати це: Branding → домашня сторінка + політика
   конфіденційності → Audience → Publish app (In production).
3. `python -m studio_os youtube-auth` → вхід у браузері → токен і канал у `data/youtube/token.json` (не в git).
   Перевірка: `python -m studio_os youtube-channel`.

## Інше
- `python -m studio_os swap <слот> <слот>` — поміняти відео двох слотів одного формату з усією історією.
- Тести: `tests/studio_os/test_publish.py` (підставний HTTP; справжні запити до Google у тестах заборонені).
