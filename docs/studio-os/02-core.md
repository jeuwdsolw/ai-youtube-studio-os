# Studio OS — Етап 2: ядро (2026-11-23)

Статус: **реалізовано й протестовано** (31 тест, `tests/studio_os/`). Агентів конвеєра ще немає (Етап 3) —
оркестратор показує такі етапи як «агент ще не реалізовано», а не пропускає їх.

## Запуск
```powershell
# одноразово (вже зроблено): py -3.12 -m venv tools/studio-venv; tools/studio-venv/Scripts/python.exe -m pip install tzdata pytest
$P = "tools/studio-venv/Scripts/python.exe"
& $P -X utf8 -m studio_os status        # огляд: режим, STOP, лічильники, найближчі публікації
& $P -X utf8 -m studio_os calendar      # календар із 11 етапами кожного відео
& $P -X utf8 -m studio_os tick          # один цикл: відновлення задач, планування, дедлайни, бекап, просування конвеєра
& $P -X utf8 -m studio_os run           # постійний цикл (tick + воркер кожні 30 с)
& $P -X utf8 -m studio_os pause "причина"   # STOP (або створити файл data/STOP); resume — продовжити
& $P -X utf8 -m studio_os mode semi     # manual | semi | auto | conveyor (усе саме, людина — лише фінал)
& $P -X utf8 -m studio_os topic S20261124-1800 "Тема"
& $P -X utf8 -m studio_os start|approve <video> <stage>;  reject <video> <stage> "коментар"
& $P -X utf8 -m studio_os approve <video> <stage> --force   # прийняти етап needs_human/failed після ручної перевірки (без нового прогону; пишеться в журнал)
& $P -X utf8 -m studio_os jobs | events | retry <job> | cancel <job> | backup
& $P -X utf8 -m studio_os cleanup-frames [--apply]
& $P -X utf8 -m pytest tests/studio_os -q
```

## Що зроблено
| Модуль | Файл | Що вміє |
|---|---|---|
| Конфіг | `config/studio.toml`, `studio_os/config.py` | розклад, часовий пояс, дедлайни етапів, режими й погодження, ресурси, бюджети; перевірка помилок |
| БД | `studio_os/core/db.py` | SQLite WAL, версіоновані міграції, налаштування, онлайн-бекап з ротацією (щодня) |
| Черга | `studio_os/core/queue.py` | ідемпотентні задачі, оренда + heartbeat, повтори з експоненційною затримкою, `dead` → людина, ексклюзивні ресурси (`remotion`, `blender`, `claude`, `elevenlabs`, `youtube_upload`), відновлення після падіння |
| Розклад | `studio_os/core/scheduler.py` | слоти 7 Shorts + 3 Long на `plan_horizon_weeks` наперед, коректний перехід літо/зима, дедлайни етапів у місцевому часі, попередження про прострочення, статус `late` замість публікації |
| Бюджети | `studio_os/core/budget.py` | облік за день/тиждень/місяць; сесії Claude, знаки ElevenLabs, квота YouTube |
| Журнал | `studio_os/core/events.py` | таблиця `events` (для дашборду) + `data/logs/studio.log` з ротацією |
| Агенти | `studio_os/agents/base.py` | контракт `Agent.run(ctx, payload) → AgentResult`; `RetryableError` / `BlockedError`; реєстр |
| Оркестратор | `studio_os/orchestrator.py` | режими, погодження/повернення, STOP, блок публікації без QC (критичні помилки блокують за будь-якого бала), автопублікація лише з `auto_publish`, перевірка вільного диска перед рендером (менше `min_free_disk_gb` → задача відкладається на 30 хв без витрати спроби, у журналі — попередження) |
| Обслуговування | `studio_os/agents/maintenance.py` | видалення PNG-кадрів лише після перевіреного кодування (ffprobe: кількість кадрів) |

## Конвеєр (порядок етапів)
research → planning → script → **voice → storyboard** (audio-first, як у студії) → animation → edit → thumbnail → qc → approval → publish.
Етап `thumbnail` на Етапі 3 матиме ручний підетап «власник доопрацьовує в ChatGPT» (STYLE-GUIDE §11).

## Перший запуск на реальних даних (2026-11-23)
- Заплановано 29 слотів (20 Shorts + 9 Long) на 3 тижні, режим `manual`.
- Етапи відео цього тижня вже мають прострочені дедлайни (система їх чесно позначає) — перші відео, які реально можна
  зробити конвеєром, — з наступного тижня. Відео, які ви робите вручну, можна буде прив'язати до слоту (Етап 3: `episode_dir`).
- `cleanup-frames --apply`: видалено 390 PNG з `episode-001/renders/shot01, shot03, shot04` (707 МБ); відео `video/public/ep001/*.mp4` на місці.

## Не зроблено на цьому етапі (свідомо)
- Агенти конвеєра, адаптери Claude Code / Remotion / Blender / ElevenLabs — Етап 3.
- Автозапуск через Планувальник завдань Windows — після Етапу 3 (зараз нема чого запускати автоматично).
- REST API для дашборду — Етап 6.

## Конвеєр у фоні (власниця, 2026-11-24)
Режим `conveyor`: агенти йдуть самі, погодження — лише фіналу (`approval`), після нього — публікація (private + publishAt).
Автозапуск нових слотів: `[production] max_in_progress = 1` (одне відео за раз) і `auto_formats = ["short"]`
(Long — після тестового прогону). Ліміти Claude/YouTube/диска — відкладення задачі, а не зупинка.
Запуск окремим процесом (працює, доки ввімкнено ПК): `Start-Process … -m studio_os run --interval 60`, PID — `data/conveyor.pid`,
вивід — `data/logs/conveyor.*.log`. Зупинити: `python -m studio_os pause "причина"` (або файл `data/STOP`); продовжити — `resume`.
