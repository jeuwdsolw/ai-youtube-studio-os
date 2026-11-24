"""Експорт системи агентів Studio OS у публічний репозиторій (портфоліо) — лише список дозволених файлів.

    py -3.12 -X utf8 scripts/export_public_studio_os.py <тека-призначення>

Копіює код (`studio_os/`), тести, конфіг, документацію й мінімальні зразки каналу, потрібні тестам.
НЕ копіює: .env, data/ (БД, токени YouTube), епізоди, відео-проєкт Remotion, звуки, музику, історію git.
Після копіювання перевіряє вміст на секрети/особисті дані й падає, якщо щось знайдено.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

STUDIO = Path(__file__).resolve().parents[1]
TREES = ["studio_os", "tests/studio_os", "docs/studio-os"]
FILES = [
    "config/studio.toml",
    "tests/check_video.py", "tests/check_storyboard.py",
    "scripts/storyboard_build.py", "scripts/voice_build.py", "scripts/pipeline.py", "scripts/in_video.py",
    "scripts/export_srt.py", "scripts/audio/master.py", "scripts/audio/mix_report.py", "scripts/audio/elevenlabs.py", "scripts/audio/elevenlabs_episode.py",
    "scripts/export_public_studio_os.py",
    # зразки каналу — фікстури тестів і контекст для агентів
    "channel/STYLE-GUIDE.md", "channel/storyboard-rules.md", "channel/palette.json",
    "channel/characters/ostap/ostap.json", "channel/locations/room-17.md",
    "episodes/episode-017/script.md", "episodes/episode-017/youtube.md", "episodes/episode-017/voice/voices.json",
]
ROOT_FILES = {"scripts/public_repo/README.md": "README.md", "scripts/public_repo/gitignore": ".gitignore",
              "scripts/public_repo/requirements-dev.txt": "requirements-dev.txt"}
SKIP = re.compile(r"(__pycache__|\.pyc$|\.pytest_cache)")
SECRETS = re.compile(r"GOCSPX-|AIza[0-9A-Za-z_-]{30}|sk_[0-9a-f]{30}|refresh_token\"\s*:\s*\"1//|[A-Za-z0-9._%+-]+@gmail\.com"
                     r"|C:\\\\Users\\\\|STAN LOOSSEMBLE", re.I)


def main(dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    for t in TREES:
        for p in sorted((STUDIO / t).rglob("*")):
            if p.is_file() and not SKIP.search(p.as_posix()):
                copied.append(p.relative_to(STUDIO))
    copied += [Path(f) for f in FILES]
    for rel in copied:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(STUDIO / rel, dst / rel)
    for src, out in ROOT_FILES.items():
        shutil.copy2(STUDIO / src, dst / out)
        copied.append(Path(out))
    bad = []
    for rel in copied:
        try:
            text = (dst / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in SECRETS.finditer(text):
            if rel.name != "export_public_studio_os.py":
                bad.append(f"{rel}: {m.group(0)[:12]}…")
    if bad:
        print("ЗНАЙДЕНО ПІДОЗРІЛЕ — експорт не публікувати:\n  " + "\n  ".join(bad))
        return 1
    print(f"скопійовано {len(copied)} файлів → {dst}; секретів не знайдено")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])) if len(sys.argv) == 2 else (print(__doc__) or 2))
