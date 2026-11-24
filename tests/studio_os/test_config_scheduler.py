from datetime import date, datetime, timezone

import pytest

from studio_os import config as config_mod
from studio_os.core import scheduler

UTC = timezone.utc


def test_config_loads_owner_schedule(cfg):
    assert cfg.tz.key == "Europe/Kyiv"
    assert cfg.mode == "manual"
    shorts = [s for s in cfg.slots if s.format == "short"]
    longs = [s for s in cfg.slots if s.format == "long"]
    assert len(shorts) == 7 and len(longs) == 3
    assert {(s.weekday, s.at.strftime("%H:%M")) for s in longs} == {(1, "17:30"), (3, "17:30"), (5, "17:30")}
    assert {s.at.strftime("%H:%M") for s in shorts if s.weekday >= 5} == {"11:00"}
    assert {"remotion", "claude"} <= cfg.exclusive


def test_config_rejects_bad_values(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[studio]\nmode="manual"\n[[schedule]]\nformat="reel"\ndays=["mon"]\ntime="18:00"\n', encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(bad, root=tmp_path)


def test_week_has_7_shorts_and_3_longs(cfg):
    slots = scheduler.slots_between(cfg, date(2026, 11, 23), date(2026, 11, 30))
    assert sum(f == "short" for f, *_ in slots) == 7
    assert sum(f == "long" for f, *_ in slots) == 3


@pytest.mark.parametrize("local, expected_utc", [
    # осінній перехід 2026-10-25: до — UTC+3, після — UTC+2
    (datetime(2026, 10, 24, 17, 30), datetime(2026, 10, 24, 14, 30, tzinfo=UTC)),
    (datetime(2026, 10, 31, 17, 30), datetime(2026, 10, 31, 15, 30, tzinfo=UTC)),
    # весняний перехід 2027-03-28: до — UTC+2, після — UTC+3
    (datetime(2027, 3, 22, 18, 0), datetime(2027, 3, 22, 16, 0, tzinfo=UTC)),
    (datetime(2027, 3, 29, 18, 0), datetime(2027, 3, 29, 15, 0, tzinfo=UTC)),
])
def test_dst_aware_publish_time(cfg, local, expected_utc):
    assert scheduler.local_to_utc(local, cfg.tz) == expected_utc


def test_nonexistent_local_time_shifts_forward(cfg):
    # 2027-03-28 03:30 у Києві не існує (03:00 → 04:00) → 04:30 EEST = 01:30 UTC
    assert scheduler.local_to_utc(datetime(2027, 3, 28, 3, 30), cfg.tz) == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)


def test_plan_is_idempotent_and_skips_past(con, cfg, fclock):
    ids = scheduler.plan(con, cfg, weeks=1)
    # пн 23.11 11:00 Київ: сьогоднішній Short 18:00 ще попереду → 7 + 3
    assert len(ids) == 10
    assert ids[0] == "S20261123-1800"
    assert scheduler.plan(con, cfg, weeks=1) == []
    fclock.advance(hours=8)  # 19:00 Київ — сьогоднішній слот уже минув
    assert con.execute("SELECT COUNT(*) FROM stages WHERE video_id='S20261123-1800'").fetchone()[0] == 11


def test_stage_deadlines_in_local_time(con, cfg, fclock):
    scheduler.plan(con, cfg, weeks=1)
    row = con.execute("SELECT deadline_utc FROM stages WHERE video_id='L20261124-1730' AND stage='script'").fetchone()
    # Long вт 24.11 17:30, script — за 16 днів → 08.11 17:30 Київ = 15:30 UTC
    assert row["deadline_utc"] == "2026-11-08T15:30:00Z"


def test_late_stage_warned_once_and_missed_video_marked(con, cfg, fclock):
    scheduler.plan(con, cfg, weeks=1)
    late = scheduler.check_deadlines(con)
    assert ("L20261124-1730", "script") in late          # дедлайн 08.11 уже минув
    assert scheduler.check_deadlines(con) == []          # попередження лише один раз
    fclock.advance(days=1)
    scheduler.check_deadlines(con)
    st = con.execute("SELECT status FROM videos WHERE id='S20261123-1800'").fetchone()["status"]
    assert st == "late"
