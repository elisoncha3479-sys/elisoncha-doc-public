"""
Health-check для Elisoncha Doc бота.

Три слоя:
1. heartbeat() — вызывается из asyncio-джобы каждые HEARTBEAT_INTERVAL секунд.
2. aiohttp /health на 127.0.0.1:HEALTH_PORT — снаружи (docker healthcheck) видит
   200 если heartbeat свежий, 503 если устарел больше STALE_AFTER.
3. watchdog-thread — если heartbeat устарел больше KILL_AFTER, делает os._exit(1).
   Контейнер падает → docker compose с restart: unless-stopped поднимает его заново.

Двойная защита: /health показывает unhealthy в `docker ps`, watchdog гарантирует
автоматический recovery даже если внешний оркестратор не реагирует на unhealthy.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from aiohttp import web

log = logging.getLogger(__name__)

HEALTH_HOST = os.environ.get("HEALTH_HOST", "127.0.0.1")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEALTH_HEARTBEAT_INTERVAL", "20"))
STALE_AFTER = int(os.environ.get("HEALTH_STALE_AFTER", "90"))
KILL_AFTER = int(os.environ.get("HEALTH_KILL_AFTER", "180"))
WATCHDOG_CHECK_INTERVAL = int(os.environ.get("HEALTH_WATCHDOG_INTERVAL", "30"))


@dataclass
class _State:
    started_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)


_state = _State()


def heartbeat() -> None:
    """Обновляет timestamp последней успешной итерации event loop."""
    _state.last_heartbeat = time.time()


def _age_seconds() -> float:
    return time.time() - _state.last_heartbeat


async def _health_handler(request: web.Request) -> web.Response:
    age = _age_seconds()
    uptime = time.time() - _state.started_at
    ok = age <= STALE_AFTER
    payload = {
        "status": "ok" if ok else "stale",
        "heartbeat_age_s": round(age, 1),
        "uptime_s": round(uptime, 1),
        "stale_after_s": STALE_AFTER,
    }
    return web.json_response(payload, status=200 if ok else 503)


def _watchdog_loop() -> None:
    """Отдельный поток. Если heartbeat устарел сильнее KILL_AFTER — суицидируем
    процесс. Запускается в отдельном thread, чтобы не зависеть от состояния
    asyncio loop (если loop замёрз, asyncio-таск проверки не сработает)."""
    while True:
        time.sleep(WATCHDOG_CHECK_INTERVAL)
        age = _age_seconds()
        if age > KILL_AFTER:
            log.critical(
                "Watchdog: heartbeat устарел на %.1fs (>%ds). Завершаем процесс "
                "для рестарта Docker'ом.",
                age,
                KILL_AFTER,
            )
            os._exit(1)


async def start_health_server() -> web.AppRunner:
    """Поднимает aiohttp на 127.0.0.1:HEALTH_PORT и стартует watchdog-thread.
    Вызывается из _post_init бота."""
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HEALTH_HOST, HEALTH_PORT)
    await site.start()
    log.info(
        "Health-сервер: http://%s:%d/health (stale>%ds → unhealthy, >%ds → restart)",
        HEALTH_HOST,
        HEALTH_PORT,
        STALE_AFTER,
        KILL_AFTER,
    )

    watchdog = threading.Thread(
        target=_watchdog_loop, name="health-watchdog", daemon=True
    )
    watchdog.start()
    log.info(
        "Watchdog-thread запущен: проверка каждые %ds, kill при stale>%ds",
        WATCHDOG_CHECK_INTERVAL,
        KILL_AFTER,
    )
    return runner
