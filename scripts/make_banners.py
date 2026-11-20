"""Создаёт белые баннеры-заглушки 1280x640 в assets/banners/.

Чтобы заменить баннер, положите PNG с тем же именем (рекомендуемый размер 1280x640).
Если файла нет, экран показывается без картинки.
"""
from pathlib import Path

from PIL import Image

NAMES = ["main", "stars", "premium", "payment", "profile", "balance"]
OUT = Path(__file__).resolve().parent.parent / "assets" / "banners"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        path = OUT / f"{name}.png"
        if path.exists():
            print("skip (exists):", path.name)
            continue
        Image.new("RGB", (1280, 640), "white").save(path, optimize=True)
        print("created:", path.name)


if __name__ == "__main__":
    main()
