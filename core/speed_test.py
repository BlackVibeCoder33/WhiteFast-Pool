import asyncio
import time
from typing import Any

import aiohttp
from loguru import logger

from core.singbox import run_batch_test


async def _measure_one(
    proxy: dict[str, Any],
    http_port: int,
    session: aiohttp.ClientSession,
    test_url: str,
    timeout: float,
    min_bytes: int,
    peak_window_sec: float,
) -> dict[str, Any]:
    if proxy.get("_batch_error"):
        return {"mbps": 0.0, "error": proxy["_batch_error"], "bytes": 0}

    proxy_url = f"http://127.0.0.1:{http_port}"
    total = 0
    err = None
    peak_rates: list[float] = []

    try:
        req_timeout = aiohttp.ClientTimeout(
            total=timeout, sock_connect=5, sock_read=timeout
        )
        headers = {"Range": "bytes=0-5000000"}
        start_req_time = time.perf_counter()
        slow_start_detected = False
        
        async with session.get(
            test_url, proxy=proxy_url, timeout=req_timeout, headers=headers
        ) as resp:
            if resp.status >= 400 and resp.status != 206:
                raise RuntimeError(f"http {resp.status}")
                
            window_start = time.perf_counter()
            window_bytes = 0
            async for chunk in resp.content.iter_chunked(16384): # Читаем меньшими кусками для точности
                window_bytes += len(chunk)
                total += len(chunk)
                elapsed = time.perf_counter() - start_req_time
                
                # 5 с / 50 KB → 15 с / 20 KB: Run #2 показал, что
                # единственный сервер, прошедший service-check 6/8
                # (Германия z1q3pv4g78), отсекался на slow_first_50kb.
                # 20 KB / 15 с = 1.3 KB/s — медленный, но живой
                # сервер проходит. Дальше решает min_measure_bytes.
                if elapsed > 15.0 and total < 20_000:
                    raise RuntimeError("slow_first_20kb")
                
                now = time.perf_counter()
                dt = now - window_start
                if dt >= peak_window_sec and window_bytes > 0:
                    peak_rates.append(window_bytes * 8 / dt / 1_000_000)
                    window_start = now
                    window_bytes = 0
                    
            now = time.perf_counter()
            dt = now - window_start
            if window_bytes > 0 and dt > 0.05:
                peak_rates.append(window_bytes * 8 / dt / 1_000_000)

    except asyncio.TimeoutError:
        err = "timeout"
    except Exception as e:
        err = str(e) or type(e).__name__

    if total < min_bytes or not peak_rates:
        return {"mbps": 0.0, "error": err or "too_slow", "bytes": total}

    peak_rates.sort(reverse=True)
    top_n = peak_rates[:3] if len(peak_rates) >= 3 else peak_rates
    mbps = sum(top_n) / len(top_n)

    return {"mbps": round(mbps, 2), "error": None, "bytes": total}


async def speed_test_all(
    proxies: list[dict[str, Any]],
    test_urls: list[str] | str,
    timeout: float = 20.0,
    concurrent: int = 4,
    retest_count: int = 1,
    retest_pause_seconds: float = 0.0,
    min_bytes: int = 300_000,
    peak_window_sec: float = 0.5,
) -> list[dict[str, Any]]:
    if not proxies:
        return []

    if isinstance(test_urls, str):
        test_urls = [test_urls]

    logger.info(
        f"Speed-test: {len(proxies)} серверов, батчами по {concurrent}, "
        f"retest={retest_count}, urls={len(test_urls)}"
    )

    async def _tester_bound(proxy, http_port, session):
        if proxy.get("_batch_error"):
            proxy["_speed_mbps"] = 0.0
            proxy["_speed_error"] = proxy["_batch_error"]
            return proxy

        best_mbps = 0.0
        best_err = None
        best_bytes = 0
        best_url = None
        for attempt in range(max(1, retest_count)):
            if attempt > 0 and retest_pause_seconds > 0:
                await asyncio.sleep(retest_pause_seconds)
            url = test_urls[attempt % len(test_urls)]
            r = await _measure_one(
                proxy, http_port, session, url, timeout,
                min_bytes, peak_window_sec,
            )
            if r["mbps"] > best_mbps:
                best_mbps = r["mbps"]
                best_url = url
            if r["error"] and not best_err:
                best_err = r["error"]
            best_bytes = max(best_bytes, r["bytes"])
            if best_mbps > 0 and attempt + 1 >= retest_count:
                break

        proxy["_speed_mbps"] = best_mbps
        proxy["_speed_error"] = None if best_mbps > 0 else (best_err or "too_slow")
        proxy["_downloaded_bytes"] = best_bytes

        name = (proxy.get("name") or "?")[:45]
        if best_mbps > 0:
            # В лог пишем домен URL, который дал лучший результат. Это
            # позволит в summarize_logs.py видеть, какой именно URL
            # «выигрывает» — а значит, какие whitelist_test_urls работают.
            domain = "?"
            if best_url:
                domain = best_url.split("://", 1)[-1].split("/", 1)[0] or "?"
            logger.info(
                f"{best_mbps:>7.2f} Mbps  ({best_bytes//1024} KB)  "
                f"[{domain}]  {name}"
            )
        else:
            logger.info(
                f"  0.00 Mbps ({(best_bytes/1024):.0f} KB) "
                f"[{proxy['_speed_error']}]  {name}"
            )
        return proxy

    results = await run_batch_test(
        proxies, tester_fn=_tester_bound, batch_size=concurrent
    )

    for p in results:
        if "_speed_mbps" not in p:
            p["_speed_mbps"] = 0.0
            p["_speed_error"] = p.get("_batch_error", "unknown")

    logger.info("Speed-test завершён")
    return results


def get_speed_concurrent(cfg: dict[str, Any], is_github: bool) -> int:
    st = cfg.get("speed_test", {}) or {}
    if is_github:
        return int(st.get("concurrent_github", 8))
    return int(st.get("concurrent_pc", 4))