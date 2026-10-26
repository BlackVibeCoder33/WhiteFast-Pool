"""Гео-детект по IP через ip-api.com/batch с graceful fallback.

Нужен для отсева серверов по стране (exclude_geo), когда имя прокси
не содержит явных гео-подсказок (например, "Node 123" с IP в США).

Если ip-api.com недоступен (блокировка, таймаут, rate-limit) — пробуем
fallback на ipwho.is (по одному IP, без ключа). Если и он не дал
результата, возвращаем прокси без изменений: фильтрация продолжит
работать только по именам. Это осознанный размен: не роняем пайплайн
из-за внешнего API.
"""
import asyncio
import ipaddress
import socket
from typing import Any

import aiohttp
from loguru import logger


BATCH_URL = "http://ip-api.com/batch?fields=query,countryCode"
BATCH_SIZE = 100
RESOLVE_TIMEOUT = 3.0
RESOLVE_CONCURRENCY = 64
HTTP_TIMEOUT = 10.0

# Fallback: ipwho.is — бесплатный, без ключа, JSON, ~10k/мес.
# Формат ответа: {"ip": "1.2.3.4", "success": true, "country_code": "US", ...}
IPWHO_FALLBACK_BASE = "https://ipwho.is"
IPWHO_FALLBACK_CONCURRENCY = 16


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


async def _resolve_one(server: str) -> str | None:
    """Возвращает IP для сервера (IP — как есть, домен — резолвится)."""
    if not server:
        return None
    if _is_ip(server):
        return server
    loop = asyncio.get_event_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(server, None, type=socket.SOCK_STREAM),
            timeout=RESOLVE_TIMEOUT,
        )
        for info in infos:
            candidate = info[4][0]
            if _is_ip(candidate):
                return candidate
    except Exception:
        return None
    return None


async def _resolve_all(
    servers: list[str],
    concurrency: int = RESOLVE_CONCURRENCY,
) -> dict[str, str | None]:
    unique = list({s for s in servers if s})
    if not unique:
        return {}
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(s: str) -> tuple[str, str | None]:
        async with sem:
            return s, await _resolve_one(s)

    pairs = await asyncio.gather(*[_bounded(s) for s in unique])
    return dict(pairs)


async def _batch_lookup(
    session: aiohttp.ClientSession,
    ips: list[str],
) -> dict[str, str]:
    if not ips:
        return {}
    body = [{"query": ip} for ip in ips]
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    try:
        async with session.post(BATCH_URL, json=body, timeout=timeout) as resp:
            if resp.status >= 400:
                logger.warning(f"GeoIP: ip-api.com вернул HTTP {resp.status}")
                return {}
            data = await resp.json()
    except Exception as e:
        logger.warning(f"GeoIP: запрос к ip-api.com не удался: {e}")
        return {}
    if not isinstance(data, list):
        return {}
    result: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        query = item.get("query")
        code = item.get("countryCode")
        if query and code:
            result[query] = str(code).upper()
    return result


async def _lookup_via_ipwho(
    session: aiohttp.ClientSession,
    ips: list[str],
) -> dict[str, str]:
    """Fallback-источник стран: ipwho.is (по одному IP).

    Медленнее, чем batch у ip-api.com, поэтому используется ТОЛЬКО если
    ip-api.com не дал ни одной страны. При 400 IP и конкурентности 16
    это ~25 волн по 1 запросу. Применяется редко — в white-list среде
    geoip выключен вовсе (pipeline отключает по environment=white).
    """
    if not ips:
        return {}
    result: dict[str, str] = {}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    sem = asyncio.Semaphore(IPWHO_FALLBACK_CONCURRENCY)

    async def _one(ip: str) -> None:
        async with sem:
            try:
                async with session.get(
                    f"{IPWHO_FALLBACK_BASE}/{ip}", timeout=timeout
                ) as resp:
                    if resp.status >= 400:
                        return
                    data = await resp.json()
            except Exception:
                return
            if not isinstance(data, dict) or not data.get("success"):
                return
            code = data.get("country_code")
            if code:
                result[ip] = str(code).upper()

    await asyncio.gather(*[_one(ip) for ip in ips])
    return result


async def lookup_countries(
    proxies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Ставит на каждый прокси `_country_code` (ISO 3166-1 alpha-2), если удалось.
    Модифицирует список in-place и возвращает его же для удобства.
    """
    if not proxies:
        return proxies

    servers = [str(p.get("server") or "") for p in proxies]
    resolved = await _resolve_all(servers)

    ip_to_servers: dict[str, list[str]] = {}
    for server, ip in resolved.items():
        if ip:
            ip_to_servers.setdefault(ip, []).append(server)

    unique_ips = list(ip_to_servers.keys())
    if not unique_ips:
        logger.info("GeoIP: ни один server не удалось резолвить в IP")
        return proxies

    logger.info(f"GeoIP: резолвлено {len(unique_ips)} IP, запрашиваю страны...")

    async with aiohttp.ClientSession() as session:
        chunks = [
            unique_ips[i:i + BATCH_SIZE]
            for i in range(0, len(unique_ips), BATCH_SIZE)
        ]
        results = await asyncio.gather(
            *[_batch_lookup(session, chunk) for chunk in chunks],
            return_exceptions=True,
        )

        ip_to_country: dict[str, str] = {}
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"GeoIP: батч упал: {r}")
                continue
            ip_to_country.update(r)

        if not ip_to_country:
            logger.warning(
                "GeoIP: ip-api.com не дал стран — пробую fallback ipwho.is"
            )
            ip_to_country = await _lookup_via_ipwho(session, unique_ips)

    if not ip_to_country:
        logger.warning("GeoIP: ни один источник не дал стран")
        return proxies

    for p in proxies:
        server = str(p.get("server") or "")
        ip = resolved.get(server)
        if ip and ip in ip_to_country:
            p["_country_code"] = ip_to_country[ip]

    known = sum(1 for p in proxies if p.get("_country_code"))
    logger.info(f"GeoIP: страны определены для {known} из {len(proxies)} прокси")
    return proxies