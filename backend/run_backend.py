"""Точка входа для Nuitka: то же, что ``python -m app``.

Nuitka не любит компилировать ``__main__.py`` из пакета, поэтому для сборки
используется отдельный файл. В режиме разработки по-прежнему работает ``-m app``.
"""

import os

import uvicorn

from app.main import create_app

PORT = int(os.getenv("PF_PORT", "8765"))
app = create_app(PORT)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info", access_log=False)
