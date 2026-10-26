import asyncio
import time
from typing import Any

from loguru import logger


async def _check_one(
    proxy: dict[str, Any],
    timeout: float,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        host = proxy.get("server")
        port = proxy.get("port")
        if not host or not port:
            proxy["_tcp_ok"] = False
            proxy["_tcp_error"] = "no host/port"
            return proxy

        start = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            proxy["_tcp_ok"] = True
            proxy["_tcp_ping_ms"] = round(elapsed_ms, 1)
        except asyncio.TimeoutError:
            proxy["_tcp_ok"] = False
            proxy["_tcp_error"] = "timeout"
        except OSError as e:
            proxy["_tcp_ok"] = False
            proxy["_tcp_error"] = f"oserr:{e.errno}"
        except Exception as e:
            proxy["_tcp_ok"] = False
            proxy["_tcp_error"] = type(e).__name__

        return proxy


async def tcp_check_all(
    proxies: list[dict[str, Any]],
    concurrent: int = 200,
    timeout: float = 4.0,
    skip_types: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not proxies:
        return [], []

    skip_types = skip_types or set()

    to_check: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for p in proxies:
        if p.get("type") in skip_types:
            # UDP/QUIC-протоколы: TCP-чек бессмыслен, помечаем «живыми»
            p["_tcp_ok"] = True
            p["_tcp_ping_ms"] = 0.0
            p["_tcp_skip"] = True
            skipped.append(p)
        else:
            to_check.append(p)

    semaphore = asyncio.Semaphore(concurrent)
    tasks = [_check_one(p, timeout, semaphore) for p in to_check]
    checked = await asyncio.gather(*tasks)

    results = list(checked) + skipped

    alive = [p for p in results if p.get("_tcp_ok")]
    dead = [p for p in results if not p.get("_tcp_ok")]

    logger.info(
        f"TCP-чек: проверено {len(checked)}, пропущено (UDP) {len(skipped)}, "
        f"живых {len(alive)}, отсеяно {len(dead)}"
    )
    return alive, dead