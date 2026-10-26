import asyncio
import base64
import json
import re
import socket
from html import unescape as html_unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs, quote, unquote

import aiohttp
import yaml
from loguru import logger


SOURCES_CACHE_DIR = Path("sources_cache")


def _safe_cache_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def _looks_like_base64(text: str) -> bool:
    """True, если строка — base64-encoded данные (формат v2rayNG/Hiddify)."""
    if not text or len(text) < 20:
        return False
    cleaned = text.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if len(cleaned) < 20:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", cleaned):
        return False
    return True


def _looks_like_subscription(text: str) -> bool:
    """Похоже ли содержимое на подписку (URI-list, YAML или base64)."""
    if not text or len(text) < 20:
        return False
    has_uri = "://" in text
    has_yaml = "proxies:" in text or "\nmode:" in text
    return has_uri or has_yaml or _looks_like_base64(text)


def _save_source_cache(name: str, text: str) -> None:
    if not _looks_like_subscription(text):
        logger.debug(
            f"Cache skip '{name}': не похоже на подписку "
            f"({len(text)} байт, первые 60: {text[:60]!r})"
        )
        return
    try:
        SOURCES_CACHE_DIR.mkdir(exist_ok=True)
        path = SOURCES_CACHE_DIR / f"{_safe_cache_name(name)}.txt"
        path.write_text(text, encoding="utf-8")
    except Exception as e:
        logger.debug(f"Cache save '{name}' failed: {e}")


def _load_source_cache(name: str) -> str | None:
    try:
        path = SOURCES_CACHE_DIR / f"{_safe_cache_name(name)}.txt"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        if not _looks_like_subscription(text):
            logger.warning(
                f"Cache '{name}': файл есть, но не похож на подписку — игнорирую"
            )
            return None
        return text
    except Exception:
        pass
    return None


async def _try_yandex_fallback(
    session: aiohttp.ClientSession, url: str, name: str
) -> str | None:
    """Проксирование подписки через translate.yandex.ru (whitelisted домен)."""
    if "translate.yandex" in url:
        return None

    encoded = quote(url, safe="")
    yandex_url = f"https://translate.yandex.ru/translate?url={encoded}&lang=de-de"
    logger.info(f"   Fallback: пробую через Yandex.Переводчик")

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with session.get(
            yandex_url, timeout=timeout, headers=_YANDEX_HEADERS
        ) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("Content-Type", "").lower()
            text = await resp.text()
    except Exception as e:
        logger.warning(f"   Yandex fallback '{name}' ошибка: {e}")
        return None

    if "text/html" in ctype:
        extracted = _extract_text_from_html(text)
        if not extracted:
            logger.warning("   Yandex: пустой результат после парсинга HTML")
            return None
        if "://" not in extracted and "proxies:" not in extracted:
            logger.warning(
                "   Yandex: извлечённый текст не похож на конфиг "
                f"(первые 80: {extracted[:80]!r})"
            )
            return None
        return extracted
    return text

_YANDEX_PROXY_HOSTS = {"translate.yandex.ru", "translated.turbopages.org"}

_YANDEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
    "Accept-Language": "ru,en;q=0.9",
}


def _unwrap_yandex_proxy(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc not in _YANDEX_PROXY_HOSTS:
        return None
    params = parse_qs(parsed.query)
    inner = params.get("url", [None])[0]
    if inner:
        return unquote(inner)
    return None


def _extract_text_from_html(html_text: str) -> str:
    cleaned = re.sub(
        r"<script[^>]*>.*?</script>", "", html_text, flags=re.DOTALL | re.IGNORECASE
    )
    cleaned = re.sub(
        r"<style[^>]*>.*?</style>", "", cleaned, flags=re.DOTALL | re.IGNORECASE
    )
    for tag in ("pre", "textarea"):
        m = re.search(
            rf"<{tag}[^>]*>(.*?)</{tag}>",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if m:
            return html_unescape(m.group(1)).strip()
    text = re.sub(r"<[^>]+>", "\n", cleaned)
    text = html_unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def _fetch_one_direct(
    session: aiohttp.ClientSession, url: str, name: str
) -> str:
    logger.info(f"-> Скачиваю '{name}': {url}")
    timeout = aiohttp.ClientTimeout(total=10)

    for attempt in range(2):
        try:
            async with session.get(url, timeout=timeout) as resp:
                resp.raise_for_status()
                text = await resp.text()
            logger.info(f"   получено {len(text)} байт")
            return text
        except aiohttp.ClientResponseError as e:
            if e.status == 429 or e.status >= 500:
                logger.warning(
                    f"   Ошибка {e.status} на '{name}'. Попытка {attempt+1}/2..."
                )
                await asyncio.sleep(1)
            else:
                raise
        except Exception as e:
            logger.warning(
                f"   Ошибка скачивания '{name}': {type(e).__name__}: {e}. "
                f"Попытка {attempt+1}/2..."
            )
            await asyncio.sleep(1)

    raise RuntimeError(f"Не удалось скачать '{name}' после 2 попыток")


async def _fetch_via_yandex(
    session: aiohttp.ClientSession, yandex_url: str, name: str
) -> str:
    inner = _unwrap_yandex_proxy(yandex_url)
    if inner:
        logger.info(f"   Yandex.Прокси: исходный URL {inner}")
        try:
            return await _fetch_one_direct(session, inner, f"{name} (direct)")
        except Exception as e:
            logger.warning(
                f"   Прямой запрос к {inner} не удался ({e}). "
                f"Пробую через Yandex-обёртку..."
            )

    logger.info(f"-> Скачиваю через Yandex.Прокси '{name}': {yandex_url}")
    timeout = aiohttp.ClientTimeout(total=25)

    for attempt in range(2):
        try:
            async with session.get(
                yandex_url, timeout=timeout, headers=_YANDEX_HEADERS
            ) as resp:
                resp.raise_for_status()
                ctype = resp.headers.get("Content-Type", "").lower()
                text = await resp.text()
            if "text/html" in ctype:
                extracted = _extract_text_from_html(text)
                if extracted:
                    logger.info(
                        f"   Yandex.Прокси: извлечено {len(extracted)} байт"
                    )
                    if "://" not in extracted and "proxies:" not in extracted:
                        logger.warning(
                            "   Yandex.Прокси: извлечённый текст не похож на конфиг"
                        )
                    return extracted
                logger.warning("   Yandex.Прокси: HTML пустой после парсинга")
            else:
                logger.info(f"   Yandex.Прокси: получено {len(text)} байт")
                return text
        except aiohttp.ClientResponseError as e:
            if e.status == 429 or e.status >= 500:
                logger.warning(
                    f"   Ошибка {e.status} на Yandex '{name}'. "
                    f"Попытка {attempt+1}/3..."
                )
                await asyncio.sleep(2)
            else:
                raise
        except Exception as e:
            logger.warning(
                f"   Yandex.Прокси ошибка '{name}': {e}. Попытка {attempt+1}/3..."
            )
            await asyncio.sleep(2)

    raise RuntimeError(
        f"Не удалось скачать '{name}' через Yandex после 3 попыток"
    )


_FETCH_MODE = "auto"


async def detect_environment(timeout: float = 20.0) -> str:
    """Определяет среду: 'black' (GitHub доступен) или 'white' (DPI на SNI)."""
    url = "https://raw.githubusercontent.com/Maskkost93/kizyak-vpn-4.0/main/kizyakbeta6.txt"
    try:
        http_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=http_timeout) as resp:
                if resp.status != 200:
                    logger.info(
                        f"[detect] GET {url} → HTTP {resp.status} → env=white"
                    )
                    return "white"
                chunk = await resp.content.read(1024)
                if chunk:
                    logger.info(
                        f"[detect] GET {url} → OK ({len(chunk)} байт) → env=black"
                    )
                    return "black"
                logger.info(
                    f"[detect] GET {url} → пустое тело → env=white"
                )
                return "white"
    except Exception as e:
        logger.info(
            f"[detect] GET {url} → {type(e).__name__}: {e} → env=white"
        )
        return "white"


def set_fetch_mode(mode: str) -> None:
    global _FETCH_MODE
    if mode not in ("auto", "cache_first", "cache_only"):
        logger.warning(f"Неизвестный fetch mode: {mode}, использую 'auto'")
        mode = "auto"
    _FETCH_MODE = mode
    logger.info(f"Fetch mode: {_FETCH_MODE}")


async def _fetch_one(session: aiohttp.ClientSession, url: str, name: str) -> str:
    cached = _load_source_cache(name)

    if _FETCH_MODE == "cache_only":
        if cached:
            logger.info(f"   '{name}': cache_only → кэш ({len(cached)} байт)")
            return cached
        raise RuntimeError(f"'{name}': cache_only, но кэша нет")

    if _FETCH_MODE == "cache_first" and cached:
        logger.info(f"   '{name}': cache_first → кэш ({len(cached)} байт)")
        return cached

    try:
        if _unwrap_yandex_proxy(url):
            text = await _fetch_via_yandex(session, url, name)
        else:
            text = await _fetch_one_direct(session, url, name)
        _save_source_cache(name, text)
        return text
    except Exception as e:
        logger.warning(f"   '{name}': сеть не удалась ({type(e).__name__}: {e})")

    fallback = await _try_yandex_fallback(session, url, name)
    if fallback:
        logger.info(f"   '{name}': через Yandex ({len(fallback)} байт)")
        _save_source_cache(name, fallback)
        return fallback

    if cached:
        logger.warning(f"   '{name}': кэш с диска ({len(cached)} байт)")
        return cached

    raise RuntimeError(f"'{name}': все способы провалились")


def _parse_yaml(text: str, source_name: str) -> list[dict[str, Any]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        logger.error(f"Ошибка YAML в '{source_name}': {e}")
        return []

    if not isinstance(data, dict):
        logger.error(f"'{source_name}': ожидался dict, получен {type(data).__name__}")
        return []

    proxies = data.get("proxies")
    if not proxies:
        logger.warning(f"'{source_name}': ключ 'proxies' отсутствует или пуст")
        return []

    for p in proxies:
        p["_source"] = source_name

    logger.info(f"   из '{source_name}' извлечено {len(proxies)} серверов (YAML)")
    return proxies


SKIP_NAME_PREFIXES = ("[OpenRay]",)


def _parse_uri_list(text: str, source_name: str) -> list[dict[str, Any]]:
    proxies: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for uri in line.split():
            uri = uri.strip()
            if not uri:
                continue
            parsed = _parse_single_uri(uri)
            if not parsed:
                continue
            pname = (parsed.get("name") or "").strip()
            if any(pname.startswith(prefix) for prefix in SKIP_NAME_PREFIXES):
                skipped += 1
                continue
            parsed["_source"] = source_name
            parsed["_raw_uri"] = uri
            proxies.append(parsed)
    suffix = f" (пропущено {skipped} мусорных)" if skipped else ""
    logger.info(
        f"   из '{source_name}' извлечено {len(proxies)} серверов "
        f"(URI list){suffix}"
    )
    return proxies


def _parse_single_uri(uri: str) -> dict[str, Any] | None:
    try:
        if uri.startswith("vmess://"):
            return _parse_vmess_uri(uri)
        if uri.startswith("vless://"):
            return _parse_vless_uri(uri)
        if uri.startswith("trojan://"):
            return _parse_trojan_uri(uri)
        if uri.startswith("ss://"):
            return _parse_ss_uri(uri)
        if uri.startswith("hysteria2://") or uri.startswith("hy2://"):
            return _parse_hysteria2_uri(uri)
    except Exception as e:
        logger.debug(f"Не удалось распарсить URI: {uri[:60]}... ({e})")
    return None


def _decode_b64_tolerant(raw: str) -> bytes | None:
    if not raw:
        return None
    cleaned = raw.strip().replace("\n", "").replace("\r", "").replace(" ", "")

    candidates: list[str] = [cleaned]
    urlsafe = cleaned.replace("-", "+").replace("_", "/")
    if urlsafe != cleaned:
        candidates.append(urlsafe)

    for cand in candidates:
        padded = cand + "=" * (-len(cand) % 4)
        try:
            return base64.b64decode(padded, validate=False)
        except Exception:
            pass
        if len(padded.rstrip("=")) % 4 == 1:
            try:
                return base64.b64decode(padded[:-1], validate=False)
            except Exception:
                pass
    return None


def _parse_vmess_uri(uri: str) -> dict[str, Any] | None:
    b64 = uri[len("vmess://"):]
    raw = _decode_b64_tolerant(b64)
    if raw is None:
        return None

    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding, errors="strict")
        except Exception:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        result: dict[str, Any] = {
            "type": "vmess",
            "server": data.get("add", ""),
            "port": int(data.get("port", 0) or 0),
            "uuid": data.get("id", ""),
            "alterId": int(data.get("aid", 0) or 0),
            "cipher": data.get("scy", "auto"),
            "name": data.get("ps", ""),
            "network": data.get("net", "tcp"),
        }

        ws_host = (data.get("host") or "").strip()
        sni = (data.get("sni") or "").strip()

        if data.get("tls") == "tls":
            result["tls"] = True
            if sni:
                result["servername"] = sni
            elif ws_host:
                result["servername"] = ws_host
            result["skip-cert-verify"] = True

        if data.get("net") == "ws":
            ws: dict[str, Any] = {"path": data.get("path", "/")}
            if ws_host:
                ws["headers"] = {"Host": ws_host}
            result["ws-opts"] = ws
        return result
    return None


def _parse_vless_uri(uri: str) -> dict[str, Any] | None:
    parsed = urlparse(uri)
    if not parsed.hostname:
        return None
    result: dict[str, Any] = {
        "type": "vless",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "uuid": unquote(parsed.username) if parsed.username else "",
    }
    if parsed.fragment:
        result["name"] = unquote(parsed.fragment)
    params = parse_qs(parsed.query)
    if "security" in params and params["security"][0] in ("tls", "reality"):
        result["tls"] = True
    if "flow" in params:
        result["flow"] = params["flow"][0]
    if "type" in params:
        result["network"] = params["type"][0]
    if "sni" in params:
        result["servername"] = params["sni"][0]
    if "fp" in params:
        result["client-fingerprint"] = params["fp"][0]
    if "pbk" in params:
        reality: dict[str, Any] = {"public-key": params["pbk"][0]}
        if "sid" in params:
            reality["short-id"] = params["sid"][0]
        result["reality-opts"] = reality
    if "path" in params and result.get("network") == "ws":
        result["ws-opts"] = {"path": params["path"][0]}
    if "host" in params and result.get("network") == "ws":
        if "ws-opts" not in result:
            result["ws-opts"] = {}
        if "headers" not in result["ws-opts"]:
            result["ws-opts"]["headers"] = {}
        result["ws-opts"]["headers"]["Host"] = params["host"][0]
    return result


def _parse_trojan_uri(uri: str) -> dict[str, Any] | None:
    parsed = urlparse(uri)
    if not parsed.hostname:
        return None
    result: dict[str, Any] = {
        "type": "trojan",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "password": unquote(parsed.username) if parsed.username else "",
    }
    if parsed.fragment:
        result["name"] = unquote(parsed.fragment)
    params = parse_qs(parsed.query)
    if "sni" in params:
        result["sni"] = params["sni"][0]
    if "type" in params:
        result["network"] = params["type"][0]
    if "fp" in params:
        result["client-fingerprint"] = params["fp"][0]
    return result


def _parse_ss_uri(uri: str) -> dict[str, Any] | None:
    parsed = urlparse(uri)
    if not parsed.hostname:
        return None
    result: dict[str, Any] = {
        "type": "ss",
        "server": parsed.hostname,
        "port": parsed.port or 443,
    }
    if parsed.username:
        userinfo = unquote(parsed.username)
        if ":" in userinfo:
            method, password = userinfo.split(":", 1)
            result["cipher"] = method
            result["password"] = password
        else:
            b64 = userinfo + "=" * (-len(userinfo) % 4)
            decoded = base64.b64decode(b64).decode("utf-8")
            if ":" in decoded:
                method, password = decoded.split(":", 1)
                result["cipher"] = method
                result["password"] = password
    if parsed.fragment:
        result["name"] = unquote(parsed.fragment)
    return result


def _parse_hysteria2_uri(uri: str) -> dict[str, Any] | None:
    if uri.startswith("hysteria2://"):
        rest = uri[len("hysteria2://"):]
    elif uri.startswith("hy2://"):
        rest = uri[len("hy2://"):]
    else:
        return None

    parsed = urlparse("hysteria2://" + rest)
    if not parsed.hostname:
        return None

    result: dict[str, Any] = {
        "type": "hysteria2",
        "server": parsed.hostname,
        "port": parsed.port or 443,
        "password": unquote(parsed.username) if parsed.username else "",
        "tls": True,
    }
    if parsed.fragment:
        result["name"] = unquote(parsed.fragment)

    params = parse_qs(parsed.query)
    if "sni" in params:
        result["sni"] = params["sni"][0]
    if "insecure" in params:
        result["skip-cert-verify"] = params["insecure"][0] in ("1", "true")
    if "obfs" in params:
        result["obfs"] = params["obfs"][0]
    if "obfs-password" in params:
        result["obfs-password"] = params["obfs-password"][0]
    return result


def _parse_subscription(text: str, source_name: str) -> list[dict[str, Any]]:
    stripped = text.lstrip()
    if (
        stripped.startswith("proxies:")
        or "\nproxies:" in stripped
        or stripped.startswith("mode:")
        or "\nmode:" in stripped
    ):
        return _parse_yaml(text, source_name)
    if "://" in text:
        return _parse_uri_list(text, source_name)

    if _looks_like_base64(text):
        decoded_bytes = _decode_b64_tolerant(text)
        if decoded_bytes:
            try:
                decoded = decoded_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                decoded = decoded_bytes.decode("latin-1", errors="ignore")
            if "://" in decoded:
                logger.info(
                    f"'{source_name}': base64 URI-list → "
                    f"{len(decoded)} байт после декодирования"
                )
                return _parse_uri_list(decoded, source_name)
            logger.debug(
                f"'{source_name}': base64 OK, но нет '://' в результате"
            )

    logger.warning(
        f"'{source_name}': формат не распознан (нет '://', нет 'proxies:', "
        f"не base64) — пропускаю. Первые 80: {text[:80]!r}"
    )
    return []


_RU_NAME_PATTERNS = [
    re.compile(r"russia", re.IGNORECASE),
    re.compile(r"russian", re.IGNORECASE),
    re.compile(r"россия", re.IGNORECASE),
    re.compile(r"moscow", re.IGNORECASE),
    re.compile(r"москва", re.IGNORECASE),
    re.compile(r"🇷🇺"),
    re.compile(r"\[ru\]", re.IGNORECASE),
    re.compile(r"\bru-\d", re.IGNORECASE),
    re.compile(r"\bru\d{3,}", re.IGNORECASE),
]


def _is_russian_proxy(proxy: dict[str, Any]) -> bool:
    name = str(proxy.get("name") or "")
    if not name:
        return False
    return any(pattern.search(name) for pattern in _RU_NAME_PATTERNS)


def _dedupe(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Дедупликация по (type, server, port, uuid/password, cipher).

    `_is_russian` пересчитывается после сбора дублей: сервер помечается
    РФ, только если ВСЕ его имена матчат РФ-паттернам. Иначе — non-RF
    (один non-RF дубль перекрывает мусорный РФ-тег от другого источника).
    `_all_names` собирает имена всех дублей для geo-фильтра.
    """
    by_key: dict[str, dict[str, Any]] = {}
    dupes = 0
    for p in proxies:
        server = str(p.get("server", "")).lower()
        port = str(p.get("port", ""))
        ptype = str(p.get("type", ""))
        auth = str(p.get("uuid") or p.get("password") or "")
        cipher = str(p.get("cipher", ""))

        key = f"{ptype}:{server}:{port}:{auth}:{cipher}"
        name = str(p.get("name") or "").strip()

        if key in by_key:
            dupes += 1
            survivor = by_key[key]
            if name:
                names = survivor["_all_names"]
                if name not in names:
                    names.append(name)
            continue

        p["_all_names"] = [name] if name else []
        by_key[key] = p

    for survivor in by_key.values():
        names = survivor.get("_all_names", [])
        if not names:
            survivor["_is_russian"] = False
        else:
            survivor["_is_russian"] = all(
                _is_russian_proxy({"name": n}) for n in names
            )

    unique = list(by_key.values())
    logger.info(
        f"Глобальная дедупликация: {len(proxies)} -> {len(unique)} "
        f"(убрано {dupes} дублей)"
    )
    return unique


async def fetch_all(subscriptions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [s for s in subscriptions if s.get("enabled", True)]
    if not active:
        logger.warning("Нет включённых подписок")
        return []

    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_one(session, s["url"], s["name"]) for s in active]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_proxies: list[dict[str, Any]] = []
    for sub, result in zip(active, results):
        if isinstance(result, Exception):
            logger.error(f"Ошибка скачивания '{sub['name']}': {result}")
            continue
        all_proxies.extend(_parse_subscription(result, sub["name"]))

    ru_before = 0
    for p in all_proxies:
        if _is_russian_proxy(p):
            p["_is_russian"] = True
            ru_before += 1

    deduped = _dedupe(all_proxies)

    ru_count = sum(1 for p in deduped if p.get("_is_russian"))
    if ru_count:
        logger.info(
            f"Помечено РФ-серверов до дедупа: {ru_before}, "
            f"после дедупа: {ru_count}"
        )

    return deduped


def group_by_server_port(
    proxies: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple, list[dict[str, Any]]]]:
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for p in proxies:
        key = (p.get("server"), p.get("port"))
        if key not in groups:
            groups[key] = []
        groups[key].append(p)

    representatives: list[dict[str, Any]] = []
    for key, group in groups.items():
        representatives.append(group[0])

    return representatives, groups


def expand_representatives(
    representatives: list[dict[str, Any]],
    expansion_map: dict[tuple, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Возвращает по одному представителю на сервер (без размножения).

    Дубль на `server:port` не проходит независимые тесты и создаёт
    «фантомные» серверы в клиенте — поэтому берём только тестированный
    representative.
    """
    expanded: list[dict[str, Any]] = []
    for rep in representatives:
        expanded.append(rep)
    return expanded