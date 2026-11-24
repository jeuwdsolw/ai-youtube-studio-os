from studio_os.core import budget, db, queue

EXCL = frozenset({"remotion", "claude"})


def test_migrations_are_versioned(con):
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert db.migrate(con) == len(db.MIGRATIONS)  # повторно — без змін


def test_enqueue_idempotent(con, fclock):
    a = queue.enqueue(con, "x", "k1")
    b = queue.enqueue(con, "x", "k1")
    assert a == b
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_claim_respects_priority_and_kinds(con, fclock):
    queue.enqueue(con, "a", "low", priority=90)
    hi = queue.enqueue(con, "a", "high", priority=5)
    queue.enqueue(con, "b", "other", priority=0)
    j = queue.claim(con, "w1", kinds={"a"})
    assert j["id"] == hi and j["status"] == "running" and j["attempts"] == 1


def test_exclusive_resource_blocks_parallel_render(con, fclock):
    r1 = queue.enqueue(con, "render", "r1", resources=["remotion"])
    queue.enqueue(con, "render", "r2", resources=["remotion"])
    other = queue.enqueue(con, "voice", "v1", resources=["elevenlabs"])
    assert queue.claim(con, "w1", exclusive=EXCL)["id"] == r1
    # другий рендер чекає; задача з іншим ресурсом іде
    assert queue.claim(con, "w2", exclusive=EXCL)["id"] == other
    assert queue.claim(con, "w3", exclusive=EXCL) is None
    queue.complete(con, r1, "w1")
    assert queue.claim(con, "w3", exclusive=EXCL)["key"] == "r2"


def test_retry_with_backoff_then_dead(con, fclock):
    jid = queue.enqueue(con, "x", "k", max_attempts=2)
    queue.claim(con, "w")
    assert queue.fail(con, jid, "w", "мережа") == "queued"
    assert queue.claim(con, "w") is None            # затримка ще не минула
    fclock.advance(seconds=31)
    assert queue.claim(con, "w")["id"] == jid
    assert queue.fail(con, jid, "w", "знову") == "dead"
    assert queue.retry_dead(con, jid)
    assert queue.claim(con, "w")["attempts"] == 1


def test_wrong_worker_cannot_complete(con, fclock):
    jid = queue.enqueue(con, "x", "k")
    queue.claim(con, "w1")
    assert not queue.complete(con, jid, "intruder")
    assert queue.complete(con, jid, "w1")


def test_expired_lease_recovered_after_restart(con, fclock):
    jid = queue.enqueue(con, "x", "k", resources=["claude"])
    queue.claim(con, "dead-worker", lease_seconds=60, exclusive=EXCL)
    fclock.advance(seconds=30)
    assert queue.heartbeat(con, jid, "dead-worker", 60)   # живий воркер продовжує оренду
    fclock.advance(seconds=61)                           # «процес упав»
    assert queue.recover_expired(con) == [jid]
    fclock.advance(seconds=31)
    j = queue.claim(con, "new-worker", exclusive=EXCL)    # замок claude звільнено
    assert j["id"] == jid and j["attempts"] == 2


def test_budget_period_and_limit(con, cfg, fclock):
    assert budget.allowed(con, cfg, "claude_sessions", 12)
    budget.spend(con, cfg, "claude_sessions", 12)
    assert not budget.allowed(con, cfg, "claude_sessions")
    fclock.advance(days=1)                               # новий день — новий ліміт
    assert budget.allowed(con, cfg, "claude_sessions")
    assert budget.allowed(con, cfg, "elevenlabs_chars", 10**9)  # limit = 0 → без блоку


def test_backup_rotation(con, cfg, fclock):
    for _ in range(3):
        db.backup(con, cfg.backup_dir, keep=2)
        fclock.advance(seconds=1)
    assert len(list(cfg.backup_dir.glob("studio-*.db"))) == 2
