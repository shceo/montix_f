"""Подписанные ссылки на медиафайлы.

Задача: результат генерации должен открываться у владельца и у провайдера,
которому мы отдаём файл на вход, но не у того, кто просто подобрал адрес.

Хранить файлы в открытом каталоге нельзя — идентификаторы предсказуемы и чужие
работы утекут перебором. Проверять сессию на каждый файл тоже не выйдет:
провайдер приходит за файлом со своей стороны, никакой сессии у него нет.

Решение — возможность в самой ссылке: срок жизни плюс подпись, которую можно
проверить, ничего не спрашивая у базы.

    /files/abc.png?expires=1786060800&sig=31cd13ca…

Пример самостоятельный: запускается как есть, ничего из платформы не тянет.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Живёт в переменных окружения. Здесь — заглушка, чтобы пример запускался.
SECRET = b"replace-me-with-a-random-32-byte-secret"

# Неделя. Достаточно, чтобы клиент открыл историю через несколько дней, и
# мало, чтобы утёкшая ссылка не работала вечно.
DEFAULT_TTL = 7 * 24 * 60 * 60


def _signature(path: str, expires: int) -> str:
    """Подпись пути и срока.

    Подписываем ПУТЬ, а не полный адрес: домен может отличаться (внутренний
    адрес для провайдера, публичный для браузера), а право на файл от этого
    не меняется.
    """
    payload = f"{path}:{expires}".encode()
    return hmac.new(SECRET, payload, hashlib.sha256).hexdigest()


def sign(url: str, ttl: int = DEFAULT_TTL) -> str:
    """Возвращает ссылку со сроком жизни и подписью."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    if "sig" in query:
        # Уже подписана — второй раз не трогаем, иначе сломаем чужую подпись.
        return url
    expires = int(time.time()) + ttl
    query["expires"] = str(expires)
    query["sig"] = _signature(parts.path, expires)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def verify(path: str, expires: str | None, sig: str | None) -> bool:
    """Проверяет ссылку. Любая неполнота — отказ."""
    if not expires or not sig:
        return False
    try:
        deadline = int(expires)
    except ValueError:
        return False
    if deadline < time.time():
        return False
    # compare_digest, а не ==: обычное сравнение строк завершается на первом
    # различии, и по времени ответа подпись можно подобрать побайтно.
    return hmac.compare_digest(_signature(path, deadline), sig)


# --- как это выглядит в обработчике запроса ---------------------------------


def serve_file(path: str, query: dict[str, str]):
    """Псевдо-обработчик: показывает порядок проверок."""
    if not verify(path, query.get("expires"), query.get("sig")):
        # 404, а не 403: отказ не должен подтверждать, что такой файл есть.
        return 404, b"Not Found"
    return 200, read_from_storage(path)


def read_from_storage(path: str) -> bytes:  # заглушка для примера
    return b"<file bytes>"


if __name__ == "__main__":
    url = sign("https://example.test/files/abc.png")
    print("подписанная ссылка:\n ", url)

    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    print("\nпроверка честной ссылки:  ", verify(parts.path, q["expires"], q["sig"]))
    print("подделали подпись:        ", verify(parts.path, q["expires"], "0" * 64))
    print("подставили чужой путь:    ", verify("/files/other.png", q["expires"], q["sig"]))
    print("срок вышел:               ", verify(parts.path, "1", q["sig"]))
