"""Демоверсия: страница, поле ввода и настоящий вызов модели генерации.

Одно приложение на FastAPI, один эндпоинт, одна статическая страница. Всё, что
нужно, чтобы увидеть путь «текст → изображение» целиком и потрогать защиту,
которая в боевой системе размазана по слоям.

Что здесь показано по-настоящему, а не для вида:
  • ключ провайдера читается из окружения и в браузер не отдаётся;
  • запрос проверяется фильтром содержания ДО обращения к провайдеру;
  • частота ограничена — иначе одна открытая вкладка съедает баланс;
  • ошибки провайдера приводятся к понятному тексту, детали остаются в логе.

Без ключа приложение работает в режиме заглушки: интерфейс и все проверки
живые, вместо картинки — placeholder.

Запуск:  python app.py    →    http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from examples.content_guard import BlockedContent, check as guard_check  # noqa: E402

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("demo")

BASE_URL = os.getenv("AI_BASE_URL", "https://api.aimlapi.com")
API_KEY = os.getenv("AI_API_KEY", "")
MODEL = os.getenv("AI_MODEL", "google/nano-banana")

# Демо крутится на чьей-то машине без авторизации, поэтому предел жёсткий.
RATE_LIMIT = int(os.getenv("DEMO_RATE_LIMIT_PER_HOUR", "10"))
_hits: dict[str, list[float]] = {}

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Montix demo", docs_url=None, redoc_url=None)


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=2, max_length=800)


class GenerateOut(BaseModel):
    url: str
    stub: bool = False
    remaining: int


def _rate_limit(client_ip: str) -> int:
    """Сколько попыток осталось. Бросает 429, если лимит выбран."""
    now = time.time()
    fresh = [t for t in _hits.get(client_ip, []) if now - t < 3600]
    _hits[client_ip] = fresh
    remaining = RATE_LIMIT - len(fresh)
    if remaining <= 0:
        wait = int(3600 - (now - min(fresh))) // 60 + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Лимит демо — {RATE_LIMIT} генераций в час. Следующая через {wait} мин.",
        )
    return remaining


def _note_use(client_ip: str) -> None:
    """Засчитываем только состоявшуюся генерацию.

    За наш собственный отказ — по фильтру содержания или из-за сбоя провайдера
    — клиент платить попыткой не должен.
    """
    _hits.setdefault(client_ip, []).append(time.time())


def _friendly(code: int, body: str) -> str:
    low = body.lower()
    if code == 429 or "rate limit" in low:
        return "Провайдер ограничил частоту запросов. Подождите немного."
    if any(w in low for w in ("moderation", "policy", "safety")):
        return "Запрос отклонён модерацией провайдера — измените описание."
    if code >= 500:
        return "Сервис генерации временно недоступен."
    return "Не удалось выполнить генерацию."


async def _call_provider(prompt: str) -> str:
    if not API_KEY:
        await asyncio.sleep(1.2)
        return "/static/stub.svg"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.post(
                f"{BASE_URL}/v1/images/generations",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL, "prompt": prompt},
            )
    except httpx.ReadTimeout as exc:
        # Запрос ушёл, ответа нет: повторять нельзя — работа могла быть оплачена.
        log.warning("provider read timeout")
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "Генерация заняла слишком долго.") from exc
    except httpx.HTTPError as exc:
        log.warning("provider transport error: %s", type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Не удалось связаться с сервисом.") from exc

    if response.status_code >= 400:
        # Полный текст — только в лог. Клиент видит понятную фразу.
        log.warning("provider rejected %s: %s", response.status_code, response.text[:300])
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, _friendly(response.status_code, response.text))

    body = response.json()
    data = body.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("url"):
        return data[0]["url"]
    log.warning("unexpected provider response shape: %s", str(body)[:300])
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Сервис вернул пустой результат.")


@app.post("/api/generate", response_model=GenerateOut)
async def generate(payload: GenerateIn, request: Request) -> GenerateOut:
    client_ip = request.client.host if request.client else "unknown"
    remaining = _rate_limit(client_ip)

    try:
        guard_check(payload.prompt)
    except BlockedContent as exc:
        # 400, а не 500: это не сбой, а осознанный отказ с объяснением.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    url = await _call_provider(payload.prompt)
    _note_use(client_ip)
    log.info("generated prompt_len=%s stub=%s", len(payload.prompt), not API_KEY)
    return GenerateOut(url=url, stub=not API_KEY, remaining=remaining - 1)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Понятный текст вместо разбора Pydantic.

    Сырое «string_too_short / loc: [body, prompt]» клиенту ничего не говорит —
    это то же самое, что показывать ему чужой код ошибки.
    """
    log.info("validation rejected: %s", exc.errors()[:2])
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Напишите хотя бы пару слов о том, что должно быть на изображении."},
    )


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "provider_key": bool(API_KEY), "model": MODEL}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    import uvicorn

    if not API_KEY:
        log.info("AI_API_KEY не задан — работаем в режиме заглушки")
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
