"""Вызов ИИ-модели: то, что отличает боевой запрос от примера из документации.

В документации провайдера обычно так:

    requests.post(url, json={"model": ..., "prompt": ...}).json()["data"][0]["url"]

Этого хватает ровно до первой ошибки. Ниже — что добавляется, когда за каждым
запросом стоят деньги клиента.

1. Ключ только из окружения. В коде и в браузере его нет никогда.
2. Разные таймауты. Картинка делается десятками секунд, видео — минутами;
   один общий таймаут либо рвёт живые запросы, либо часами держит мёртвые.
3. Повтор только там, где он безопасен. Сеть отвалилась на установке
   соединения — повторяем. Ответ уже пришёл с ошибкой — не повторяем: работа
   могла быть выполнена и оплачена.
4. Ключ идемпотентности в заголовке. Провайдер, который его учитывает, вернёт
   ту же работу вместо второй платной. Учитывают не все — проверять надо
   опытом, а не документацией.
5. Ошибка провайдера не показывается клиенту как есть: коды и внутренние
   тексты остаются в логах.

Пример самостоятельный. Без ключа работает в режиме заглушки — видно порядок
шагов и обработку ошибок.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random

log = logging.getLogger("ai")

BASE_URL = os.getenv("AI_BASE_URL", "https://api.aimlapi.com")
API_KEY = os.getenv("AI_API_KEY", "")

# Картинка приходит синхронно, но не мгновенно. Видео — отдельный разговор,
# там опрос статуса, и таймаут на порядок больше.
# Разные фазы разделены намеренно: соединение либо есть сразу, либо его нет,
# а вот ответа честно можно ждать минуты.
IMAGE_TIMEOUT = dict(connect=10.0, read=180.0, write=30.0, pool=10.0)

MAX_ATTEMPTS = 3


class ProviderError(Exception):
    """Провайдер не справился. Текст уже безопасен для показа клиенту."""


def _friendly(status: int, body: str) -> str:
    """Технические детали — в лог, клиенту одна понятная фраза."""
    low = body.lower()
    if status == 429 or "rate limit" in low:
        return "Слишком много запросов — подождите немного и попробуйте снова."
    if "moderation" in low or "policy" in low or "safety" in low:
        return "Запрос отклонён модерацией — измените описание."
    if status >= 500:
        return "Сервис генерации временно недоступен. Попробуйте позже."
    return "Не удалось выполнить генерацию. Попробуйте ещё раз."


async def generate_image(prompt: str, *, model: str, idempotency_key: str) -> str:
    """Возвращает ссылку на готовое изображение."""
    if not API_KEY:
        return await _stub(prompt, model)

    # Импорт внутри функции: без ключа пример должен запускаться и на машине,
    # где httpx не установлен.
    import httpx

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        # Ключ живёт столько же, сколько задача: повтор той же задачи не должен
        # заводить у провайдера вторую платную работу.
        "Idempotency-Key": idempotency_key,
    }
    payload = {"model": model, "prompt": prompt}

    last_transport_error: Exception | None = None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(**IMAGE_TIMEOUT), follow_redirects=True
    ) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(
                    f"{BASE_URL}/v1/images/generations", json=payload, headers=headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # До сервера не дошли — работа точно не начата, повтор безопасен.
                last_transport_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                # Пауза со случайной добавкой: иначе все упавшие запросы
                # вернутся одновременно и добьют провайдера.
                await asyncio.sleep(2**attempt + random.random())
                continue
            except httpx.ReadTimeout as exc:
                # Ответа не дождались, но запрос ушёл. Повторять нельзя:
                # работа могла быть выполнена и оплачена.
                log.warning("ai read timeout model=%s", model)
                raise ProviderError(
                    "Генерация заняла слишком долго. Проверьте историю — "
                    "возможно, результат уже готов."
                ) from exc

            if response.status_code >= 400:
                # Ответ получен: провайдер сказал «нет» осознанно, повтор
                # ничего не изменит и может стоить второй оплаты.
                log.warning(
                    "ai rejected status=%s body=%s", response.status_code, response.text[:300]
                )
                raise ProviderError(_friendly(response.status_code, response.text))

            return _extract_url(response.json())

    log.warning("ai unreachable after %s attempts: %r", MAX_ATTEMPTS, last_transport_error)
    raise ProviderError("Не удалось связаться с сервисом генерации. Попробуйте позже.")


def _extract_url(body: dict) -> str:
    """Форма ответа у провайдеров различается — достаём аккуратно."""
    data = body.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        url = data[0].get("url")
        if url:
            return url
    for key in ("image", "images", "output", "url"):
        value = body.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0]
    # Пустой ответ при HTTP 200 — тоже ошибка, просто менее очевидная.
    raise ProviderError("Сервис вернул пустой результат — попробуйте ещё раз.")


async def _stub(prompt: str, model: str) -> str:
    """Заглушка: пример должен запускаться без ключа."""
    await asyncio.sleep(0.4)
    return f"https://example.test/stub.png?model={model}&len={len(prompt)}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    async def main() -> None:
        url = await generate_image(
            "кот в сапогах из мультфильма, закатный свет, объектив 50 мм",
            model="google/nano-banana",
            idempotency_key="task-1",
        )
        print("готово:", url)
        if not API_KEY:
            print("(ключ AI_API_KEY не задан — это была заглушка)")

    asyncio.run(main())
