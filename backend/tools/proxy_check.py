r"""Проверка прокси перед прогоном: TCP, внешний IP, страна и часовой пояс.

    python tools/proxy_check.py --file C:\app\proxies.txt
    python tools/proxy_check.py --line socks5://1.2.3.4:1080 --ozon
    python tools/proxy_check.py --file proxies.txt --ozon --timeout 25

Без `--file`/`--line` берётся «Список прокси» из настроек приложения.
`--ozon` дополнительно открывает выдачу Ozon и показывает, есть ли челлендж.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.browser.providers import get_playwright, shutdown_playwright  # noqa: E402
from app.browser.proxies import Proxy, parse_proxy  # noqa: E402
from app.core.config import load_settings  # noqa: E402

OZON_URL = "https://www.ozon.ru/search/?text=%D0%BB%D0%B0%D0%BC%D0%BF%D0%BE%D1%87%D0%BA%D0%B0"


def tcp_ok(proxy: Proxy, timeout: float = 5.0) -> tuple[bool, str]:
    hostport = proxy.server.split("://", 1)[-1]
    host, _, port = hostport.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, str(exc)


async def check(line: str, timeout: float, with_ozon: bool) -> None:
    proxy = parse_proxy(line)
    if proxy is None:
        print(f"  [—] не разобран: {line.strip()[:80]}")
        return

    ok, error = await asyncio.to_thread(tcp_ok, proxy)
    if not ok:
        print(f"  [X] {proxy.masked()} — TCP закрыт: {error[:80]}")
        return

    pw = await get_playwright()
    started = time.perf_counter()
    try:
        browser = await pw.chromium.launch(headless=True, proxy=proxy.playwright())
    except Exception as exc:  # noqa: BLE001
        print(f"  [X] {proxy.masked()} — браузер не запустился: {str(exc).splitlines()[0][:120]}")
        return

    page = await browser.new_page()
    try:
        await page.goto("https://ipinfo.io/json", timeout=int(timeout * 1000))
        info = json.loads(await page.inner_text("body"))
        elapsed = int((time.perf_counter() - started) * 1000)
        print(
            f"  [OK] {proxy.masked()} → {info.get('ip')} · {info.get('country')} "
            f"{info.get('city') or ''} · {info.get('timezone')} · {elapsed} мс · "
            f"{info.get('org') or ''}"
        )
        if with_ozon:
            try:
                response = await page.goto(
                    OZON_URL, wait_until="domcontentloaded", timeout=int(timeout * 1000)
                )
                title = (await page.title())[:120]
                body = (await page.inner_text("body"))[:160].replace("\n", " ")
                print(
                    f"       Ozon: HTTP {response.status if response else '—'} · "
                    f"«{title}» · {body}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"       Ozon: не открылся — {str(exc).splitlines()[0][:120]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [X] {proxy.masked()} — не отвечает: {str(exc).splitlines()[0][:140]}")
    finally:
        await browser.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка прокси для PulseMarket")
    parser.add_argument("--file", help="файл со списком прокси (по строке)")
    parser.add_argument("--line", action="append", default=[], help="прокси строкой (можно несколько)")
    parser.add_argument("--timeout", type=float, default=20.0, help="таймаут на прокси, сек")
    parser.add_argument("--ozon", action="store_true", help="дополнительно проверить выдачу Ozon")
    args = parser.parse_args()

    lines = list(args.line)
    if args.file:
        lines += Path(args.file).read_text(encoding="utf-8", errors="ignore").splitlines()
    if not lines:
        settings = load_settings()
        if settings.general.proxies_file:
            print(f"Файл из настроек: {settings.general.proxies_file}")
            lines = Path(settings.general.proxies_file).read_text(
                encoding="utf-8", errors="ignore"
            ).splitlines()
    lines = [line for line in lines if line.strip() and not line.strip().startswith("#")]
    if not lines:
        print("Нет прокси: укажите --file/--line или заполните «Список прокси» в настройках")
        return 2

    print(f"Проверяем {len(lines)} прокси (таймаут {args.timeout:g} с):")
    for line in lines:
        await check(line, args.timeout, args.ozon)
    await shutdown_playwright()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
