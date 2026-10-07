"""Запуск прототипа: python run.py  →  http://127.0.0.1:8000

Без ключа работает разбор по правилам. Чтобы включить ИИ (Claude), создайте рядом файл .env
со строкой ANTHROPIC_API_KEY=ваш_ключ (см. .env.example) и перезапустите.
"""
import os
from pathlib import Path

import uvicorn


def load_env(path: Path) -> None:
    """Минимальный .env: строки KEY=VALUE, # — комментарий. Уже заданные переменные не перезаписываются."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    load_env(Path(__file__).with_name(".env"))
    print("ИИ:", "включён (ключ найден)" if os.getenv("ANTHROPIC_API_KEY") else "выключен — разбор по правилам")
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)
