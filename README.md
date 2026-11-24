# Studio OS — автономна AI-студія горор-Shorts для YouTube

Система агентів, яка веде анімаційний YouTube-канал від теми до запланованої публікації:
тренди → план → сценарій → голос → розкадровка → анімація → монтаж → обкладинка → QC → погодження людиною → YouTube.
Людина погоджує лише фінал; усе інше роблять агенти за календарем слотів (3 Long + 7 Shorts на тиждень, Europe/Kyiv).

> Це публічний зріз **лише системи агентів**. Персонажі, епізоди, рендери, звуки й Remotion-проєкт каналу
> лишаються в приватному репозиторії — тут кілька зразкових файлів каналу, які потрібні тестам і як контекст для агентів.

**English summary.** An autonomous multi-agent pipeline that produces and schedules animated horror Shorts for YouTube.
A Python orchestrator (stdlib + SQLite) drives a job queue of stage agents; the creative stages (script, storyboard,
2D animation in Remotion/TSX, edit) run as headless Claude Code sessions with a strict JSON result schema, while the
deterministic stages (budget, voice via ElevenLabs, QC, YouTube resumable upload with `publishAt`) are plain Python.
A human approves only the final cut. 85 tests, no third-party runtime dependencies.

## Що тут цікавого

| Частина | Що робить | Де |
|---|---|---|
| Оркестратор | 11 етапів відео, режими `manual / semi / auto / conveyor`, ворота погодження, WIP-ліміт, дедлайни від слоту, обмін слотів із усією історією | `studio_os/orchestrator.py` |
| Черга задач | SQLite WAL, оренда з heartbeat, повтори, блокування ресурсів (один рендер Remotion на машину), `defer` без витрати спроб | `studio_os/core/queue.py` |
| Бюджети | денні/місячні ліміти сесій Claude, символів ElevenLabs, квоти YouTube; вичерпано → задача чекає скидання періоду, а не падає | `studio_os/core/budget.py` |
| Агентні сесії | Claude Code headless: системні правила, дозволи інструментів, JSON-схема звіту (`blockers` vs `owner_notes`), перевірка, що композиції справді зареєстровані в Remotion, прибирання temp-бандлів | `studio_os/agents/agentic.py` |
| Агенти етапів | дослідження трендів (YouTube Data API), стратег, сценарист з лінтером, голос, розкадровка, анімація, монтаж, QC, публікація | `studio_os/agents/` |
| QC | 8 зважених модулів (логіка історії, діалоги, утримання, анімація, стиль, звук, монтаж, готовність до YouTube): медіа-перевірки (−14 LUFS, рух «не слайд-шоу», субтитри) + LLM-рецензія → бал і вердикт; критичні знахідки блокують, решта — попередження | `studio_os/qc/` |
| Публікація | OAuth 2.0 installed app (loopback + PKCE), resumable upload із продовженням після обриву (без дублів), `private + publishAt`, обкладинка, SRT, перевірка каналу | `studio_os/adapters/youtube_upload.py`, `studio_os/agents/publish.py` |

Ключове рішення: **стиль тримається кодом, а не промптами** — персонажі є параметричними SVG-ригами, кольори беруться з
палітри, тому агенти не генерують зображення нейромережами (це заборонено конфігом), а пишуть анімацію як код.

Документація етапів (українською): [`docs/studio-os/`](docs/studio-os) — аудит і архітектура, ядро, виробництво, QC, публікація.

## Як це працює

```
calendar slot ─► research ─► planning ─► script ─► voice ─► storyboard ─► animation ─► edit ─► thumbnail ─► qc ─► approval ─► publish
                 (API)       (Claude)    (Claude   (Eleven  (Claude +      (Claude      (Claude   (Claude)    (8 мод.  (людина)    (YouTube
                                          + lint)   Labs)    check_*)      → Remotion)  + check)              + LLM)               publishAt)
```

- `tick` — планувальник створює відео під слоти й ставить у чергу готові етапи; `work` — воркер бере задачі.
- `run` — цикл tick+work (режим `conveyor`: автостарт по одному Shorts, людина погоджує лише фінал).
- Швидкий режим (`strict_checks = false`, `[qc] max_rounds = 1`): кожна перевірка виконується один раз, некритичні знахідки
  йдуть у звіт для людини замість нескінченних циклів переробок.

## Запуск

Потрібен Python 3.12+ (лише стандартна бібліотека). Для тестів — `pytest`.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/studio_os -q          # 85 тестів; мережа в тестах заблокована, Google/Claude — підставні
python -m studio_os status                   # стан студії (створить data/studio.db)
python -m studio_os calendar                 # слоти й дедлайни
```

Реальне виробництво потребує середовища каналу (Remotion-проєкт `video/`, Blender, FFmpeg, Claude Code CLI) і ключів у `.env`
(`ELEVENLABS_API_KEY`, `YOUTUBE_API_KEY`) та OAuth-клієнта YouTube у `data/youtube/` — нічого з цього в репозиторії немає.

## Структура

```
studio_os/            оркестратор, ядро (db, queue, scheduler, budget, events), адаптери, агенти, QC
config/studio.toml    розклад, дедлайни, режими, бюджети, параметри агентів
tests/studio_os/      тести (оркестратор, черга, агенти, QC, публікація)
docs/studio-os/       проєктні документи етапів
scripts/              допоміжні скрипти виробництва, які викликають агенти
channel/, episodes/   мінімальні зразки каналу (фікстури тестів)
```

## Моя роль

Систему спроєктовано й написано в парі з Claude Code: я ставила задачу й обмеження (лише підписка Claude, без генерації
зображень, людина погоджує фінал, без музики в Shorts), приймала архітектурні рішення, тестувала на реальному каналі й
відбраковувала. Перше відео, повністю зроблене агентами, заплановане на YouTube через цю систему.
