# Studio OS — Етап 3: виробничі агенти (2026-11-23)

Статус: **реалізовано й протестовано** (50 тестів `tests/studio_os/`, усі зелені; справжній Claude Code, ElevenLabs
і YouTube у тестах не викликаються — підставний CLI `tests/studio_os/fake_claude.py`, мережа не потрібна).
На реальному відео агенти ще **не запускались** (це перший кроковий прогін у режимі `manual`, див. «Як запустити»).

## Агенти й етапи
| Етап | Агент (`studio_os/agents/`) | Що робить | Ресурс |
|---|---|---|---|
| research | `research.py` | раз на тиждень (звіт ≤ 6 днів використовується повторно) збирає відео конкурентів через YouTube Data API v3 (запити UA + EN, 30 днів, перегляди/день, «outlier» = перегляди / підписники, топ-коментарі) → Claude оцінює теми за 7 критеріями (охоплення 20, актуальність 15, емоція 15, утримання 15, оригінальність 15, анімованість 10, канал 10). Докази — лише справжні `yt_id` зі зібраних даних; без ключа актуальність = `null`, звіт позначено «немає даних» | claude |
| planning | `strategist.py` | детермінований вибір теми: найвищий бал, штраф 15 за жанр, що вже був серед останніх 5 відео формату; `reject` з коментарем → нова тема; тему, задану вручну (`topic`), не чіпає | — |
| script | `scriptwriter.py` | сценарій + блоки озвучки за схемою; пише `script.md` (стара версія → `script.vN.md`), `voice/{elevenlabs.md, requests.json, voices.json, voice-plan.json}`; лінтер: штампи (повтор із попередніх відео = помилка), реальні бренди, довгі речення, повтори, хронометраж Shorts 45–75 с при 140 сл/хв; одна автоматична переробка, далі — людина | claude |
| voice | `voice.py` | кастинг і закріплений диктор → квота ElevenLabs (`/v1/user/subscription`) і місячний бюджет → наявний `scripts/audio/elevenlabs_episode.py` (уже згенероване не повторює) → `scripts/voice_build.py` (темп 140 сл/хв) | elevenlabs |
| storyboard | `storyboard.py` | план розкадровки за схемою (ракурси/крупності/техніка 2d/parallax/blender) → `scripts/storyboard_build.py` → `tests/check_storyboard.py`; до 3 спроб із зауваженнями перевірки | claude |
| animation | `production.py` | агентна сесія Claude Code в теці студії (Remotion, наявні риги/локації); перевірка: композиції є у справжньому списку `npx remotion compositions --quiet` (у Root.tsx їх часто створює цикл, тож пошук тексту — лише запасний варіант без Remotion), `video/scripts/motion_check.mjs` (не «слайд-шоу») | claude, remotion |
| edit | `production.py` | агентна сесія: монтаж, мікс, мастер; перевірка: `renders/final_master.mp4` існує, `tests/check_video.py` проходить, роздільність формату | claude, remotion |
| thumbnail | `production.py` | **Shorts:** одна сесія як у студії (еп. 009–018): `EpNNNThumb` за еталоном v4 з текстом → `cover.png/.jpg` + `small.png`, без ChatGPT. **Long:** крок 1: чорнова `thumbnail/cover-notext.png` + `gpt-prompt.md` → **чекає файл власника** `thumbnail/cover-gpt.png`; крок 2: текст як на еталоні → `cover.jpg` (< 2 МБ) + `cover_360.png` | claude, remotion |

Етапи `qc`, `approval`, `publish` — Етапи 4–5 (оркестратор показує «агент ще не реалізовано», не пропускає).

## Голос (рішення власника, 2026-11-23)
- ElevenLabs — **лише ключ власника** `ELEVENLABS_API_KEY` у `.env` (його читає `scripts/audio/elevenlabs.py`; агенти ключ не логують).
- Диктор закріплений у `config/studio.toml [production]`: `NR` = **True - Crime & Horror Narrator** (`tZssYepgGaQmegsMEXjK`), **140 сл/хв**.
  Каталог голосів для сценариста перекриває старий NR ранніх епізодів (там був інший диктор); агент голосу блокує
  `voices.json` з іншим voice_id.
- `narrator_only = false` (власник, 2026-11-23): персонажі можуть мати власні голоси — лише ті, що студія вже використовувала
  (каталог із `episodes/*/voice/voices.json`); новий голос → етап `voice` зупиняється «потрібен кастинг». `true` — усе читає диктор.
- `voice_build.py` лише пришвидшує до 140; якщо фактичний темп відхиляється > 10 % — попередження в результаті етапу.

## Обкладинка Long (STYLE-GUIDE §11; Shorts — без ChatGPT, власник 2026-11-23)
Агент робить чорнову без написів і промпт → етап `thumbnail` стає `needs_human` з нотаткою `WAIT_FILE: …/cover-gpt.png`.
Власник кладе доопрацьований у ChatGPT файл (`cover-gpt.png/.jpg/.webp`) → при наступному `tick` оркестратор сам повертає
етап у чергу → накладання тексту. AI-генерацію зображень агенти не використовують (заборона власника) — ChatGPT робить сам власник.

## Захист канону й стилю
Агентні сесії (`agentic.py`) працюють у теці студії з обмеженим набором інструментів (`Read/Edit/Write/Glob/Grep/Skill`
+ Bash лише для `npx remotion`, `node`, `npx tsc`, `py -3.12`, `ffmpeg`, `ffprobe`). До й після сесії знімається SHA1 захищених файлів:
`channel/palette.json`, `style-bible.md`, `STYLE-GUIDE.md`, `continuity.md`, `storyboard-rules.md`, `channel/characters/`,
`video/src/two-d/rig/`, `video/src/lib/palette.ts`. Зміна будь-якого → етап блокується «сесія змінила канон…» (людина перевіряє diff).
Ліміт Claude Code → повтор пізніше; немає входу → людина. Сесій Claude на день — бюджет `claude_sessions` (12).
Remotion на Windows лишає в Temp бандл ~0.5 ГБ на кожен `still`/`render`/`compositions`: після сесії, `motion_check` і списку
композицій Studio OS видаляє бандли, що з'явилися за цей час (`agentic.remotion_temp`), — лише коли не працює жоден процес Remotion.

## Що потрібно від власника
1. **`YOUTUBE_API_KEY` у `.env`** — для справжніх даних трендів (Google Cloud Console → APIs & Services → увімкнути
   *YouTube Data API v3* → Credentials → API key). Безкоштовна квота 10 000 од./день; один звіт ≈ 11 пошуків × 100 + дрібні
   запити ≈ 1 200 од. Без ключа агент працює, але теми оцінюються лише за знанням ніші, актуальність = «немає даних».
2. Ліміт знаків ElevenLabs у `[budgets.elevenlabs_chars] limit` (0 = перевіряється лише залишок тарифу через API).

## Як запустити (перший прогін — покроково, режим manual)
```powershell
$P = "tools/studio-venv/Scripts/python.exe"
& $P -X utf8 -m studio_os start S20261124-1800 research ; & $P -X utf8 -m studio_os work --once
& $P -X utf8 -m studio_os approve S20261124-1800 research      # або reject … "коментар"
# далі так само: planning → script → voice → storyboard → animation → edit → thumbnail
```
Перший прогін варто робити на дальньому слоті (дедлайни найближчих уже минули — оркестратор позначить їх `late`).

## Тести
`tools/studio-venv/Scripts/python.exe -m pytest tests/studio_os -q` — 50 тестів: конфіг/розклад/черга/оркестратор (Етап 2),
адаптери Claude і YouTube (кеш, квота, помилки), research (з ключем і без, фільтр вигаданих доказів), стратег (різноманіття жанрів,
reject), сценарист (файли, штампи, хронометраж, лише диктор), голос (збирання з готових реплік, закріплений True Crime, кастинг),
розкадровка (повтор до проходження перевірки), захист канону, очікування файлу ChatGPT, перевірка композицій.
Автофікстура `no_real_claude` гарантує, що жоден тест не витрачає ліміт підписки.
