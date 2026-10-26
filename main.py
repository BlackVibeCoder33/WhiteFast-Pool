import asyncio
import socket
import sys
import time
from typing import Any

import yaml
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from core.paths import base_dir
from core.pipeline import run_core_pipeline, PipelineResult
from core.singbox import set_anti_dpi


class PoolState:
    def __init__(self) -> None:
        self.result = PipelineResult()
        self.last_updated: float = 0.0
        self.lock = asyncio.Lock()


state = PoolState()


async def run_pipeline(config: dict[str, Any]) -> None:
    if state.lock.locked():
        logger.warning("Pipeline уже идёт — пропуск итерации")
        return
    async with state.lock:
        try:
            state.result = await run_core_pipeline(config, is_github=False)
            state.last_updated = time.time()

            local_ip = _get_local_ip()
            port = int(config["http_server"]["port"])
            logger.info("=" * 60)
            logger.info("ГОТОВЫЕ ССЫЛКИ ДЛЯ HIDDIFY:")
            for profile in config.get("subscription_profiles", []):
                path = profile["path"]
                if path in state.result.json_subscriptions:
                    count = state.result.counts.get(path, 0)
                    logger.info(f"  {profile['name']} ({count} серверов):")
                    logger.info(f"    http://{local_ip}:{port}/sub/{path}")
            logger.info("=" * 60)
        except Exception as e:
            logger.exception(f"Ошибка pipeline: {e}")


async def handle_sub_profile(request: web.Request) -> web.Response:
    path = request.match_info["profile"]
    if path not in state.result.json_subscriptions:
        available = ", ".join(sorted(state.result.json_subscriptions.keys())) or "(none)"
        return web.Response(
            status=404,
            text=f"Profile '{path}' not found. Available: {available}",
        )
    return web.Response(
        text=state.result.json_subscriptions[path],
        content_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{path}.json"'},
    )


async def handle_status(request: web.Request) -> web.Response:
    if state.last_updated == 0:
        return web.Response(status=503, text="Not ready yet.")
    ago = int(time.time() - state.last_updated)
    lines = ["WhiteFast Pool", f"Last updated: {ago} sec ago", "", "Profiles:"]
    for path in sorted(state.result.json_subscriptions.keys()):
        lines.append(f"  /sub/{path}  →  {state.result.counts.get(path, 0)} servers")
    return web.Response(text="\n".join(lines))


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


async def main() -> None:
    (base_dir() / "logs").mkdir(exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        base_dir() / "logs" / "app.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
    )

    with open(base_dir() / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    set_anti_dpi(config.get("anti_dpi", {}))
    # set_fetch_mode вызывается внутри run_core_pipeline (по environment)

    asyncio.create_task(run_pipeline(config))

    interval = int(config["schedule"]["update_interval_minutes"])
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_pipeline, "interval", minutes=interval, args=[config])
    scheduler.start()
    logger.info(f"Планировщик запущен: обновление каждые {interval} мин")

    app = web.Application()
    app.router.add_get("/sub/{profile}", handle_sub_profile)
    app.router.add_get("/", handle_status)
    app.router.add_get("/status", handle_status)

    host = config["http_server"]["host"]
    port = int(config["http_server"]["port"])

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    local_ip = _get_local_ip()
    logger.info("=" * 60)
    logger.info(f"HTTP-сервер слушает {host}:{port}")
    for profile in config.get("subscription_profiles", []):
        logger.info(
            f"  '{profile['name']}': http://{local_ip}:{port}/sub/{profile['path']}"
        )
    logger.info(f"Статус: http://{local_ip}:{port}/status")
    logger.info("=" * 60)
    logger.info("Ctrl+C — остановка")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    logger.info("Останавливаемся...")
    scheduler.shutdown(wait=False)
    await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass