"""Лицензия: подпись Ed25519, HWID, срок, офлайн-допуск."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["PF_HWID"] = "HWID-TEST-AAAA-BBBB"

from app.core import license as lic  # noqa: E402

PRIVATE = Ed25519PrivateKey.generate()
PUBLIC_B64 = base64.urlsafe_b64encode(
    PRIVATE.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
).decode().rstrip("=")


def token(hwids: list[str], days: int = 365, sub: str = "Тест") -> str:
    payload = {
        "v": 1,
        "sub": sub,
        "hwids": hwids,
        "exp": (date.today() + timedelta(days=days)).isoformat() if days else None,
        "features": ["ozon"],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = PRIVATE.sign(f"{lic.PREFIX}.{body}".encode())
    return f"{lic.PREFIX}.{body}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def manager(tmp: Path, server: str = "") -> lic.LicenseManager:
    return lic.LicenseManager(app_data=tmp, public_key=PUBLIC_B64, server=server, timeout=0.6)


def scenario() -> None:
    assert lic.hardware_id() == "HWID-TEST-AAAA-BBBB", lic.hardware_id()

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)

        # Режим разработки: ни ключа, ни сервера.
        dev = lic.LicenseManager(app_data=tmp, public_key="", server="")
        state = dev.status("")
        assert not state["enabled"] and state["valid"] and state["mode"] == "dev", state

        mgr = manager(tmp)
        good = token(["HWID-TEST-AAAA-BBBB"])

        # Ключ для этой машины.
        state = mgr.status(good)
        assert state["valid"] and state["mode"] == "key", state
        assert state["subject"] == "Тест" and state["days_left"] in (364, 365), state

        # Ключ на две машины: этот HWID — один из списка.
        state = mgr.status(token(["HWID-OTHER-1111-2222", "HWID-TEST-AAAA-BBBB"]))
        assert state["valid"] and state["mode"] == "key", state

        # Чужой HWID.
        state = mgr.status(token(["HWID-OTHER-1111-2222"]))
        assert not state["valid"] and "HWID" in (state["error"] or ""), state

        # Истёкший ключ.
        state = mgr.status(token(["HWID-TEST-AAAA-BBBB"], days=-1))
        assert not state["valid"] and "истёк" in (state["error"] or ""), state

        # Подпись не сходится.
        broken = good[:-4] + ("AAAA" if good[-4:] != "AAAA" else "BBBB")
        state = mgr.status(broken)
        assert not state["valid"] and "Подпись" in (state["error"] or ""), state

        # Активация сохраняет ключ; отдельный экземпляр видит его без аргумента.
        activated = mgr.activate(good)
        assert activated["valid"], activated
        assert (tmp / "license.key").read_text(encoding="utf-8").strip() == good
        again = manager(tmp).status()
        assert again["valid"] and again["mode"] == "key", again

        # Офлайн-допуск: сервер недоступен, но проверка была 2 дня назад.
        offline = manager(tmp, server="http://127.0.0.1:9")
        state_path = tmp / "license_state.json"
        state_path.write_text(
            json.dumps({"last_ok": (datetime.now() - timedelta(days=2)).isoformat()}),
            encoding="utf-8",
        )
        state = offline.status(good)
        assert state["valid"] and state["mode"] == "grace", state
        assert state["offline_until"], state

        # ...а 30 дней назад — уже нет.
        state_path.write_text(
            json.dumps({"last_ok": (datetime.now() - timedelta(days=30)).isoformat()}),
            encoding="utf-8",
        )
        state = offline.status(good)
        assert not state["valid"] and state["mode"] == "invalid", state
        assert "недоступен" in (state["error"] or ""), state

        # Совсем без ключа с включённой лицензией.
        empty = manager(tmp)
        (tmp / "license.key").unlink()
        state = empty.status("")
        assert not state["valid"] and state["mode"] == "none", state

    print("license: ok — подпись, HWID, срок, активация, офлайн-допуск 7 дней")


if __name__ == "__main__":
    scenario()
