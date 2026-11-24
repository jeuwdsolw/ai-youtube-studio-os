"""Запуск команди в теці video/ без `cd` (агентні сесії Studio OS: зміну теки Claude Code завжди питає в людини).

    py -3.12 scripts/in_video.py node scripts/motion_check.mjs E19Shot ../episodes/episode-019/previews/motion S01
    py -3.12 scripts/in_video.py npx remotion still Ep019Thumb ../episodes/episode-019/thumbnail/cover.png
    py -3.12 scripts/in_video.py npx tsc --noEmit

Дозволено лише node, `npx remotion`, `npx tsc` (npx не завантажує сторонніх пакетів: --no-install).
"""
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO = Path(__file__).resolve().parents[1] / "video"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    exe, rest = argv[0], argv[1:]
    if exe == "node":
        cmd = [shutil.which("node") or "node", *rest]
    elif exe == "npx" and rest and rest[0] in ("remotion", "tsc"):
        cmd = [shutil.which("npx") or "npx", "--no-install", *rest]
    else:
        print(f"in_video.py: дозволено лише node, npx remotion, npx tsc (отримано: {' '.join(argv[:2])})", file=sys.stderr)
        return 2
    return subprocess.call(cmd, cwd=VIDEO)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
