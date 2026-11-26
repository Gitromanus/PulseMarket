"""Лицензирование: Ed25519-ключ, привязка к HWID, офлайн-допуск 7 дней.

Ключ — самодостаточная строка ``PF1.<payload>.<signature>``:

* payload — JSON (base64url) с полями ``sub``, ``hwids``, ``exp``, ``features``;
* signature — Ed25519-подпись строки ``PF1.<payload>`` (base64url).

Проверка подписи выполняется локально, поэтому приложение работает без
интернета. Если задан сервер лицензий, дополнительно запрашивается онлайн-
подтверждение; при недоступности сервера действует офлайн-допуск 7 дней с
момента последней успешной проверки.

Публичный ключ задаётся одним из способов (по приоритету):

1. переменная окружения ``PF_LICENSE_PUBKEY``;
2. файл ``%APPDATA%/pf-automation/license_pubkey.txt``;
3. файл ``license_pubkey.txt`` рядом с модулем (кладётся в сборку).

Если ключ не задан и сервер не настроен — приложение работает в режиме
разработки без лицензии (чтобы сборка из исходников и тесты не ломались).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import app_data_dir, load_settings

PREFIX = "PF1"
OFFLINE_DAYS = 7
CHECK_TTL_SEC = 300


class LicenseError(Exception):
    """Ключ не принят: текст пригоден для показа оператору."""


# ---------- кодирование ----------


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _norm_hwid(value: str) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 18:
        return key
    return f"{key[:10]}…{key[-6:]}"


# ---------- HWID ----------


def hardware_id() -> str:
    """Стабильный идентификатор машины: MachineGuid + серийник тома + MAC."""
    override = os.getenv("PF_HWID", "").strip()
    if override and not _is_compiled():
        # Подмена HWID нужна тестам и поддержке; в собранном приложении запрещена.
        return override

    parts: list[str] = []
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                parts.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
        except OSError:
            pass
        try:
            import ctypes

            drive = f"{os.getenv('SystemDrive', 'C:')}\\"
            serial = ctypes.c_uint()
            ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(drive), None, 0, ctypes.byref(serial), None, None, None, 0
            )
            if serial.value:
                parts.append(f"{serial.value:08X}")
        except Exception:  # noqa: BLE001 — HWID не должен ронять запуск
            pass

    parts.append(f"{uuid.getnode():012X}")
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest().upper()
    return "HWID-" + "-".join(digest[i : i + 4] for i in range(0, 20, 4))


# ---------- ключ ----------


def _is_compiled() -> bool:
    """Nuitka кладёт ``__compiled__`` в главный модуль собранного приложения."""
    return hasattr(sys.modules.get("__main__"), "__compiled__")


def default_public_key() -> str:
    env = os.getenv("PF_LICENSE_PUBKEY", "").strip()
    if env:
        return env
    for path in (
        app_data_dir() / "license_pubkey.txt",
        Path(sys.executable).with_name("license_pubkey.txt"),
        Path(__file__).with_name("license_pubkey.txt"),
    ):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return ""


def verify_key(token: str, public_key: str) -> dict[str, Any]:
    """Проверяет подпись и возвращает payload. Не проверяет HWID и срок."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    cleaned = "".join((token or "").split())
    parts = cleaned.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenseError("Ключ повреждён: ожидается формат PF1.<данные>.<подпись>")

    try:
        payload_raw = _b64d(parts[1])
        signature = _b64d(parts[2])
        key = Ed25519PublicKey.from_public_bytes(_b64d(public_key))
    except Exception as exc:  # noqa: BLE001 — битый base64/ключ
        raise LicenseError(f"Ключ лицензии не читается: {exc}") from exc

    try:
        key.verify(signature, f"{PREFIX}.{parts[1]}".encode("ascii"))
    except InvalidSignature as exc:
        raise LicenseError("Подпись ключа неверна") from exc

    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LicenseError("Данные ключа повреждены") from exc
    if not isinstance(payload, dict):
        raise LicenseError("Данные ключа повреждены")
    return payload


def evaluate_payload(payload: dict[str, Any], hwid: str) -> dict[str, Any]:
    """Проверяет HWID и срок, возвращает нормализованные поля для UI."""
    allowed = [str(item) for item in payload.get("hwids") or []]
    if allowed and _norm_hwid(hwid) not in {_norm_hwid(item) for item in allowed}:
        raise LicenseError("Ключ выдан для другого компьютера (HWID не совпадает)")

    expires: date | None = None
    days_left: int | None = None
    raw_exp = payload.get("exp")
    if raw_exp:
        try:
            expires = date.fromisoformat(str(raw_exp)[:10])
        except ValueError as exc:
            raise LicenseError("В ключе неверный срок действия") from exc
        days_left = (expires - date.today()).days
        if days_left < 0:
            raise LicenseError(f"Срок действия ключа истёк {expires.isoformat()}")

    features = [str(item) for item in payload.get("features") or []]
    return {
        "subject": payload.get("sub"),
        "expires": expires.isoformat() if expires else None,
        "days_left": days_left,
        "features": features,
    }


# ---------- менеджер ----------


class LicenseManager:
    """Проверка, активация и хранение ключа. Не зависит от FastAPI."""

    def __init__(
        self,
        app_data: Path | None = None,
        public_key: str | None = None,
        server: str = "",
        timeout: float = 4.0,
    ) -> None:
        self.dir = Path(app_data) if app_data is not None else app_data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._public_key = public_key
        self.server = (server or "").strip().rstrip("/")
        self.timeout = timeout

    # ---- пути и хранилище ----

    @property
    def public_key(self) -> str:
        if self._public_key is not None:
            return self._public_key.strip()
        return default_public_key()

    @property
    def key_path(self) -> Path:
        return self.dir / "license.key"

    @property
    def state_path(self) -> Path:
        return self.dir / "license_state.json"

    def hwid(self) -> str:
        return hardware_id()

    def saved_key(self) -> str:
        try:
            return self.key_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def save_key(self, key: str) -> None:
        self.key_path.write_text(key.strip(), encoding="utf-8")

    def _state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **values: Any) -> None:
        state = self._state()
        state.update(values)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---- онлайн-проверка ----

    def _call_server(self, path: str, key: str, hwid: str) -> dict[str, Any]:
        if not self.server:
            raise LicenseError("Сервер лицензий не задан")
        url = f"{self.server}{path}"
        body = json.dumps({"key": key, "hwid": hwid}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except OSError:
                pass
            raise LicenseError(f"Сервер лицензий отклонил ключ (HTTP {exc.code}) {detail}".strip()) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LicenseError(f"Сервер лицензий недоступен: {exc}") from exc

        try:
            answer = json.loads(raw or "{}")
        except ValueError as exc:
            raise LicenseError("Сервер лицензий вернул некорректный ответ") from exc
        if not isinstance(answer, dict) or not answer.get("ok"):
            error = answer.get("error") if isinstance(answer, dict) else None
            raise LicenseError(str(error or "Сервер лицензий отклонил ключ"))
        return answer

    def _online(self, path: str, key: str, hwid: str) -> dict[str, Any]:
        answer = self._call_server(path, key, hwid)
        self._save_state(last_ok=datetime.now().isoformat(timespec="seconds"))
        return answer

    # ---- публичное состояние ----

    def status(self, key: str | None = None, online: bool = True) -> dict[str, Any]:
        hwid = self.hwid()
        token = (key if key is not None else self.saved_key()).strip()
        state = self._state()
        result: dict[str, Any] = {
            "enabled": bool(self.public_key or self.server),
            "valid": False,
            "mode": "none",
            "hwid": hwid,
            "server": self.server,
            "key": mask_key(token),
            "subject": None,
            "expires": None,
            "days_left": None,
            "features": [],
            "error": None,
            "warning": None,
            "offline_until": None,
            "last_check": state.get("last_ok"),
        }

        if not result["enabled"]:
            result.update(
                {
                    "valid": True,
                    "mode": "dev",
                    "warning": "Лицензирование отключено: не задан публичный ключ (режим разработки)",
                }
            )
            return result

        if not token:
            result.update({"mode": "none", "error": "Ключ не активирован"})
            return result

        payload: dict[str, Any] = {}
        if self.public_key:
            try:
                payload = verify_key(token, self.public_key)
                result.update(evaluate_payload(payload, hwid))
            except LicenseError as exc:
                result.update({"mode": "invalid", "error": str(exc)})
                return result

        if not self.server:
            result.update({"valid": True, "mode": "key"})
            return result

        # Онлайн-подтверждение: не чаще раза в CHECK_TTL_SEC.
        last_ok = _parse_ts(state.get("last_ok"))
        cached = _parse_ts(state.get("checked_at"))
        if online and (cached is None or (datetime.now() - cached).total_seconds() > CHECK_TTL_SEC):
            try:
                answer = self._online("/check", token, hwid)
                if answer.get("subject"):
                    result["subject"] = answer["subject"]
                if answer.get("expires"):
                    result["expires"] = answer["expires"]
                self._save_state(checked_at=datetime.now().isoformat(timespec="seconds"))
                result.update({"valid": True, "mode": "online"})
                return result
            except LicenseError as exc:
                error = str(exc)
        else:
            result.update({"valid": True, "mode": "online"})
            return result

        # Сервер недоступен — офлайн-допуск 7 дней с последней успешной проверки.
        if last_ok is not None:
            deadline = last_ok + timedelta(days=OFFLINE_DAYS)
            if datetime.now() <= deadline:
                result.update(
                    {
                        "valid": True,
                        "mode": "grace",
                        "warning": f"{error}. Офлайн-допуск до {deadline:%d.%m.%Y %H:%M}",
                        "offline_until": deadline.isoformat(timespec="seconds"),
                    }
                )
                return result

        result.update({"mode": "invalid", "error": error})
        return result

    def activate(self, key: str, server: str | None = None) -> dict[str, Any]:
        token = "".join((key or "").split())
        if server is not None:
            self.server = server.strip().rstrip("/")
        if not token:
            raise LicenseError("Вставьте ключ лицензии")

        payload: dict[str, Any] = {}
        if self.public_key:
            payload = verify_key(token, self.public_key)
            evaluate_payload(payload, self.hwid())
        elif not self.server:
            raise LicenseError("Публичный ключ не задан: активация недоступна")

        if self.server:
            self._online("/activate", token, self.hwid())

        self.save_key(token)
        self._save_state(checked_at=datetime.now().isoformat(timespec="seconds"))
        return self.status(token)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def current_license() -> dict[str, Any]:
    """Состояние лицензии по текущим настройкам (для API и проверок)."""
    settings = load_settings()
    manager = LicenseManager(server=settings.license.server)
    return manager.status(settings.license.key or None)


def require_license() -> str | None:
    """Возвращает текст ошибки, если работать нельзя; иначе None."""
    state = current_license()
    if state["enabled"] and not state["valid"]:
        return f"Лицензия: {state['error'] or 'ключ не активирован'}"
    return None
