import asyncio
import time
from typing import Any

import aiohttp
from loguru import logger

from core.singbox import run_batch_test


async def _check_service(
    session: aiohttp.ClientSession,
    url: str,
    proxy_url: str,
    timeout: float,
) -> tuple[bool, float, Any]:
    try:
        start = time.perf_counter()
        t = aiohttp.ClientTimeout(total=timeout, sock_connect=8, sock_read=timeout)
        async with session.get(
            url, proxy=proxy_url, timeout=t, allow_redirects=True
        ) as resp:
            elapsed = (time.perf_counter() - start) * 1000
            ok = resp.status < 400
            return ok, round(elapsed, 1), resp.status
    except asyncio.TimeoutError:
        return False, 0.0, "timeout"
    except Exception as e:
        return False, 0.0, type(e).__name__


async def _youtube_download_test(
    session: aiohttp.ClientSession,
    proxy_url: str,
    url: str,
    target_bytes: int,
    timeout: float,
) -> bool:
    total = 0
    try:
        t = aiohttp.ClientTimeout(total=timeout, sock_connect=5, sock_read=timeout)
        async with session.get(url, proxy=proxy_url, timeout=t) as resp:
            if resp.status >= 400:
                return False
            async for chunk in resp.content.iter_chunked(16384):
                total += len(chunk)
                if total >= target_bytes:
                    return True
    except Exception:
        return False
    return total >= target_bytes


async def _run_services(
    session: aiohttp.ClientSession,
    proxy_url: str,
    services: dict[str, Any],
    timeout_default: float,
) -> dict[str, Any]:
    svc_names = list(services.keys())
    tasks = []
    for svc_name in svc_names:
        svc_cfg = services[svc_name]
        url = svc_cfg["url"]
        to = float(svc_cfg.get("timeout_seconds", timeout_default))
        tasks.append(_check_service(session, url, proxy_url, to))
    task_results = await asyncio.gather(*tasks, return_exceptions=True)

    results: dict[str, Any] = {}
    for svc_name, r in zip(svc_names, task_results):
        if isinstance(r, Exception):
            results[svc_name] = {"ok": False, "ms": 0.0, "status": type(r).__name__}
        else:
            ok_s, ms, status = r
            results[svc_name] = {"ok": ok_s, "ms": ms, "status": status}
    return results


def _all_connect_errors(results: dict[str, Any]) -> bool:
    """Все сервисы упали с сетевой ошибкой — стоит попробовать ещё раз.

    Только connect-ошибки, БЕЗ timeout. Run #2 показал: timeout-retry
    удваивает время service-check (4155 × 8 × 15 × 2 / 18 = 55 мин).
    Timeout — это скорее «сервер медленный» или «сервис далеко», retry
    редко помогает, а время тратит сильно.
    """
    if not results:
        return False
    for v in results.values():
        if v.get("ok"):
            return False
    return any(
        str(v.get("status") or "") in (
            "ClientConnectorError",
            "ClientConnectionError",
            "ConnectionResetError",
            "ServerDisconnectedError",
        )
        for v in results.values()
    )


async def _check_one_proxy(
    proxy: dict[str, Any],
    http_port: int,
    session: aiohttp.ClientSession,
    services: dict[str, Any],
    timeout_default: float,
    scfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scfg = scfg or {}

    if proxy.get("_batch_error"):
        proxy["_services"] = {
            svc: {"ok": False, "ms": 0.0, "status": proxy["_batch_error"]}
            for svc in services
        }
        return proxy

    proxy_url = f"http://127.0.0.1:{http_port}"

    results = await _run_services(
        session, proxy_url, services, timeout_default
    )

    retry_on_error = bool(scfg.get("retry_on_connect_error", False))
    if retry_on_error and _all_connect_errors(results):
        # 0.5 с — компромисс: DPI-дроп длится 1-2 с, но 2 с × много серверов
        # = лишние минуты. 0.5 с + retry 1 раз (не более) восстанавливает
        # большинство connect-ошибок без раздувания времени.
        await asyncio.sleep(0.5)
        results = await _run_services(
            session, proxy_url, services, timeout_default
        )

    proxy["_services"] = results
    summary = " ".join(
        f"{k[:3]}={'Y' if v.get('ok') else 'N'}" for k, v in results.items()
    )
    name = (proxy.get("name") or "?")[:40]
    logger.info(f"{summary}  {name}")
    return proxy


def get_service_concurrent(cfg: dict[str, Any], is_github: bool) -> int:
    scfg = cfg.get("service_check", {}) or {}
    if is_github:
        return int(scfg.get("concurrent_github", 8))
    return int(scfg.get("concurrent_pc", 6))


async def service_check_all(
    proxies: list[dict[str, Any]],
    services: dict[str, Any],
    concurrent: int = 6,
    timeout_default: float = 10.0,
    scfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not proxies or not services:
        return proxies

    logger.info(
        f"Service-check: {len(proxies)} серверов, {len(services)} сервисов, "
        f"батчами по {concurrent}"
    )

    async def _tester_bound(proxy, http_port, session):
        return await _check_one_proxy(
            proxy, http_port, session, services, timeout_default, scfg
        )

    results = await run_batch_test(
        proxies, tester_fn=_tester_bound, batch_size=concurrent
    )

    for p in results:
        if "_services" not in p:
            p["_services"] = {
                svc: {"ok": False, "ms": 0.0, "status": p.get("_batch_error", "unknown")}
                for svc in services
            }

    logger.info("Service-check завершён")
    return results