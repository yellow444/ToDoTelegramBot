"""Проверка Telegram WebApp initData (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = 24 * 3600) -> dict:
    """Возвращает распарсенные поля (user — dict). Бросает InitDataError при неверной подписи."""
    if not init_data:
        raise InitDataError("пустой initData")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("нет hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("неверная подпись")
    auth_date = int(pairs.get("auth_date", "0"))
    if max_age and time.time() - auth_date > max_age:
        raise InitDataError("initData устарел")
    if "user" in pairs:
        pairs["user"] = json.loads(pairs["user"])
    return pairs


def sign_init_data(fields: dict, bot_token: str) -> str:
    """Подписывает initData (для тестов и локальной отладки)."""
    from urllib.parse import urlencode

    data = {k: (json.dumps(v) if isinstance(v, dict) else str(v)) for k, v in fields.items()}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)
