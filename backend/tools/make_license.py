"""Выпуск и проверка лицензионных ключей (Ed25519).

Примеры:

    python tools/make_license.py init
    python tools/make_license.py hwid
    python tools/make_license.py issue --sub "ООО Ромашка" --hwid HWID-... --days 365
    python tools/make_license.py issue --sub "Иван" --hwid HWID-A --hwid HWID-B --days 0
    python tools/make_license.py verify PF1.xxx.yyy

Приватный ключ хранится в ``backend/license_keys/ed25519_private.pem`` и не
должен попадать в установщик или репозиторий. Публичный ключ (base64) нужно
передать сборке: ``scripts/build-backend.ps1 -PublicKey <base64>``.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.license import (  # noqa: E402
    PREFIX,
    LicenseError,
    evaluate_payload,
    hardware_id,
    verify_key,
)

BASE = Path(__file__).resolve().parents[1]
KEYS = BASE / "license_keys"
PRIVATE = KEYS / "ed25519_private.pem"
PUBLIC = KEYS / "ed25519_public.txt"


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def cmd_init(_: argparse.Namespace) -> int:
    if PRIVATE.exists():
        print(f"Приватный ключ уже есть: {PRIVATE}")
    else:
        KEYS.mkdir(parents=True, exist_ok=True)
        private = Ed25519PrivateKey.generate()
        PRIVATE.write_bytes(
            private.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        print(f"Создан приватный ключ: {PRIVATE}")

    public = _public_b64()
    PUBLIC.write_text(public + "\n", encoding="utf-8")
    print(f"Публичный ключ: {PUBLIC}")
    print(f"PF_LICENSE_PUBKEY={public}")
    return 0


def _load_private() -> Ed25519PrivateKey:
    if not PRIVATE.exists():
        raise SystemExit(f"Нет приватного ключа {PRIVATE}. Сначала: make_license.py init")
    return serialization.load_pem_private_key(PRIVATE.read_bytes(), password=None)  # type: ignore[return-value]


def _public_b64() -> str:
    public = _load_private().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64e(public)


def cmd_hwid(_: argparse.Namespace) -> int:
    print(hardware_id())
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    if not args.hwid:
        raise SystemExit("Укажите --hwid (можно несколько) или --any-hwid")
    hwids = list(args.hwid) if not args.any_hwid else []
    payload = {
        "v": 1,
        "sub": args.sub,
        "hwids": hwids,
        "exp": (date.today() + timedelta(days=args.days)).isoformat() if args.days else None,
        "features": args.feature or ["ozon"],
        "iat": date.today().isoformat(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = _b64e(raw)
    signature = _load_private().sign(f"{PREFIX}.{body}".encode("ascii"))
    token = f"{PREFIX}.{body}.{_b64e(signature)}"

    # Самопроверка: ключ должен проходить ту же проверку, что и в приложении.
    verify_key(token, _public_b64())
    if hwids:
        evaluate_payload(verify_key(token, _public_b64()), hwids[0])

    print(token)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(token + "\n", encoding="utf-8")
        print(f"\nЗаписано: {out}", file=sys.stderr)
    print(
        f"\nАбонент: {args.sub}; HWID: {', '.join(hwids) or 'любой'}; "
        f"срок: {payload['exp'] or 'бессрочно'}",
        file=sys.stderr,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    public = args.pubkey or (PUBLIC.read_text(encoding="utf-8").strip() if PUBLIC.exists() else "")
    if not public:
        raise SystemExit("Нет публичного ключа: укажите --pubkey или выполните init")
    try:
        payload = verify_key(args.key, public)
        info = evaluate_payload(payload, args.hwid or hardware_id())
    except LicenseError as exc:
        print(f"НЕГОДЕН: {exc}")
        return 1
    print("Ключ верен")
    print(json.dumps({**payload, **info}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Лицензионные ключи PulseMarket")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="создать пару ключей Ed25519").set_defaults(func=cmd_init)
    sub.add_parser("hwid", help="показать HWID этого компьютера").set_defaults(func=cmd_hwid)

    issue = sub.add_parser("issue", help="выпустить ключ")
    issue.add_argument("--sub", required=True, help="имя клиента")
    issue.add_argument("--hwid", action="append", default=[], help="HWID (можно несколько)")
    issue.add_argument("--any-hwid", action="store_true", help="без привязки к машине")
    issue.add_argument("--days", type=int, default=365, help="срок в днях, 0 — бессрочно")
    issue.add_argument("--feature", action="append", default=[], help="возможность (по умолчанию ozon)")
    issue.add_argument("--out", help="сохранить ключ в файл")
    issue.set_defaults(func=cmd_issue)

    verify = sub.add_parser("verify", help="проверить ключ")
    verify.add_argument("key")
    verify.add_argument("--pubkey", help="публичный ключ base64")
    verify.add_argument("--hwid", help="проверить для другого HWID")
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
