from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from loguru import logger


from core.paths import base_dir


import sys # Убедись, что sys импортирован в начале файла

BIN_NAME = "sing-box.exe" if sys.platform == "win32" else "sing-box"
SINGBOX_BIN = base_dir() / "bin" / BIN_NAME
HTTP_PROXY_BASE = 10800
PORT_POOL_SIZE = 32
_ANTI_DPI_CONFIG: dict[str, Any] = {}


def set_anti_dpi(config: dict[str, Any]) -> None:
    global _ANTI_DPI_CONFIG
    _ANTI_DPI_CONFIG = config or {}
BATCH_PAUSE_SEC = 0.3
STARTUP_TIMEOUT_SEC = 15.0


def _is_ip(value: str) -> bool:
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _build_tls(proxy: dict[str, Any]) -> dict[str, Any] | None:
    if not proxy.get("tls"):
        return None
    tls: dict[str, Any] = {"enabled": True}
    sni = proxy.get("servername") or proxy.get("sni")
    if sni:
        tls["server_name"] = sni
    if proxy.get("alpn"):
        tls["alpn"] = proxy["alpn"]
    fp = proxy.get("client-fingerprint")
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    ro = proxy.get("reality-opts")
    if ro:
        tls["reality"] = {
            "enabled": True,
            "public_key": ro.get("public-key", ""),
            "short_id": ro.get("short-id", ""),
        }
        if "utls" not in tls:
            tls["utls"] = {"enabled": True, "fingerprint": "chrome"}
    if proxy.get("skip-cert-verify"):
        tls["insecure"] = True
    elif not ro and not sni and _is_ip(str(proxy.get("server", ""))):
        tls["insecure"] = True

    if _ANTI_DPI_CONFIG.get("enabled") and not ro:
        if _ANTI_DPI_CONFIG.get("fragment"):
            tls["fragment"] = True
        delay = _ANTI_DPI_CONFIG.get("fragment_fallback_delay")
        if delay:
            tls["fragment_fallback_delay"] = delay
        if _ANTI_DPI_CONFIG.get("record_fragment"):
            tls["record_fragment"] = True

    return tls


def _build_transport(proxy: dict[str, Any]) -> dict[str, Any] | None:
    net = proxy.get("network", "tcp")
    if net in ("tcp", "", None):
        return None
    if net == "ws":
        opts = proxy.get("ws-opts", {}) or {}
        ws: dict[str, Any] = {"type": "ws", "path": opts.get("path", "/")}
        headers = opts.get("headers") or {}
        if headers:
            ws["headers"] = headers
        return ws
    if net == "grpc":
        opts = proxy.get("grpc-opts", {}) or {}
        return {"type": "grpc", "service_name": opts.get("grpc-service-name", "")}
    if net in ("h2", "http"):
        opts = proxy.get("h2-opts") or proxy.get("http-opts") or {}
        hosts = opts.get("host") or []
        return {"type": "http", "host": hosts}
    return None


def convert_proxy(proxy: dict[str, Any], tag: str) -> dict[str, Any] | None:
    ptype = proxy.get("type")
    server = proxy.get("server")
    port = proxy.get("port")
    if not server or not port:
        return None

    base = {"tag": tag, "server": server, "server_port": int(port)}
    tls = _build_tls(proxy)
    transport = _build_transport(proxy)

    if ptype == "vless":
        out = {**base, "type": "vless", "uuid": proxy.get("uuid", "")}
        if proxy.get("flow"):
            out["flow"] = proxy["flow"]
        if tls:
            out["tls"] = tls
        if transport:
            out["transport"] = transport
        return out

    if ptype == "vmess":
        out = {
            **base,
            "type": "vmess",
            "uuid": proxy.get("uuid", ""),
            "security": proxy.get("cipher", "auto") or "auto",
            "alter_id": int(proxy.get("alterId", 0) or 0),
        }
        if tls:
            out["tls"] = tls
        if transport:
            out["transport"] = transport
        return out

    if ptype == "trojan":
        out = {**base, "type": "trojan", "password": proxy.get("password", "")}
        if tls:
            out["tls"] = tls
        if transport:
            out["transport"] = transport
        return out

    if ptype == "ss":
        return {
            **base,
            "type": "shadowsocks",
            "method": proxy.get("cipher", ""),
            "password": proxy.get("password", ""),
        }

    if ptype == "hysteria2":
        tls: dict[str, Any] = {"enabled": True}
        sni_h2 = proxy.get("servername") or proxy.get("sni")
        if sni_h2:
            tls["server_name"] = sni_h2
        if proxy.get("alpn"):
            tls["alpn"] = proxy["alpn"]
        if proxy.get("skip-cert-verify"):
            tls["insecure"] = True
        else:
            tls["insecure"] = True
        out_h2: dict[str, Any] = {
            **base,
            "type": "hysteria2",
            "password": proxy.get("password", ""),
            "tls": tls,
        }
        obfs_type = proxy.get("obfs")
        obfs_password = proxy.get("obfs-password")
        if obfs_type and obfs_password:
            out_h2["obfs"] = {"type": obfs_type, "password": obfs_password}
        return out_h2

    return None


def build_single_config(proxy: dict[str, Any], http_port: int) -> dict[str, Any] | None:
    tag = "proxy-0"
    ob = convert_proxy(proxy, tag)
    if ob is None:
        return None
    return {
        "log": {"level": "error"},
        "inbounds": [
            {
                "type": "http",
                "tag": "http-in",
                "listen": "127.0.0.1",
                "listen_port": http_port,
            }
        ],
        "outbounds": [ob, {"type": "direct", "tag": "direct"}],
        "route": {"rules": [], "final": tag},
    }


def write_config(config: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return path


async def _wait_for_port(
    host: str,
    port: int,
    proc: asyncio.subprocess.Process | None = None,
    timeout: float = 10.0,
) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        # Если процесс уже умер — нет смысла ждать порт.
        if proc is not None and proc.returncode is not None:
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=0.5
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            await asyncio.sleep(0.15)
    return False


async def _start_singbox_process(
    config_path: Path, http_port: int
) -> tuple[asyncio.subprocess.Process, Any] | None:
    """Запускает sing-box и ждёт, пока он займёт порт.

    stderr пишется в logs/singbox_<port>.err (режим "wb" — перезаписывается
    на каждом запуске этого воркера). Причина отказа (returncode=1) будет
    видна в этом файле: unsupported cipher/flow/obfs, malformed uuid,
    bad reality public_key и т.п.

    Возвращает (proc, stderr_file) или None. stderr_file закрывает
    вызывающий (_test_one_worker) в finally — иначе fd утечёт на 400 серверах.
    """
    if not SINGBOX_BIN.exists():
        logger.error(f"Не найден бинарник sing-box: {SINGBOX_BIN}")
        return None

    (base_dir() / "logs").mkdir(exist_ok=True)
    stderr_path = base_dir() / "logs" / f"singbox_{http_port}.err"
    stderr_file = open(stderr_path, "wb")

    try:
        proc = await asyncio.create_subprocess_exec(
            str(SINGBOX_BIN.resolve()),
            "run",
            "-c",
            str(config_path.resolve()),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_file,
        )
    except Exception as e:
        stderr_file.close()
        logger.error(f"sing-box spawn failed: {e}")
        return None

    ok = await _wait_for_port(
        "127.0.0.1", http_port, proc=proc, timeout=STARTUP_TIMEOUT_SEC
    )
    if not ok or proc.returncode is not None:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        stderr_file.close()
        logger.debug(
            f"sing-box не поднялся на порту {http_port} "
            f"(returncode={proc.returncode}, см. {stderr_path.name})"
        )
        return None

    return proc, stderr_file


async def _stop_singbox_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    except Exception:
        pass
    
async def _test_one_worker(
    proxy: dict[str, Any],
    port: int,
    worker_id: int,
    tester_fn: TesterFn,
) -> dict[str, Any]:
    cfg = build_single_config(proxy, port)
    if cfg is None:
        proxy["_batch_error"] = "unsupported"
        return proxy

    cfg_path = base_dir() / "out" / f"singbox_{port}.json"
    write_config(cfg, cfg_path)

    started = await _start_singbox_process(cfg_path, port)
    if started is None:
        proxy["_batch_error"] = "singbox_start_failed"
        return proxy
    proc, stderr_file = started

    try:
        async with aiohttp.ClientSession() as session:
            result = await tester_fn(proxy, port, session)
    finally:
        await _stop_singbox_process(proc)
        try:
            stderr_file.close()
        except Exception:
            pass
        await asyncio.sleep(BATCH_PAUSE_SEC)

    return result    


TesterFn = Callable[
    [dict[str, Any], int, aiohttp.ClientSession],
    Awaitable[dict[str, Any]],
]


async def run_batch_test(
    proxies: list[dict[str, Any]],
    tester_fn: TesterFn,
    batch_size: int = 4,
) -> list[dict[str, Any]]:
    if not proxies:
        return []

    num_workers = max(1, batch_size)
    port_pool = max(PORT_POOL_SIZE, num_workers)

    queue: asyncio.Queue = asyncio.Queue()
    for p in proxies:
        queue.put_nowait(p)

    results: list[dict[str, Any]] = []
    results_lock = asyncio.Lock()
    total = len(proxies)
    processed = {"count": 0}

    async def worker(worker_id: int) -> None:
        # Каждому воркеру — фиксированный порт на всё время работы,
        # чтобы два воркера не занимали один порт одновременно.
        port = HTTP_PROXY_BASE + (worker_id % port_pool)
        while True:
            try:
                p = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            try:
                r = await _test_one_worker(p, port, worker_id, tester_fn)
            except Exception as e:
                p["_batch_error"] = f"{type(e).__name__}"
                r = p

            async with results_lock:
                results.append(r)
                processed["count"] += 1
                if processed["count"] % 25 == 0:
                    logger.info(
                        f"  Прогресс: {processed['count']}/{total} "
                        f"({100 * processed['count'] // total}%)"
                    )

    logger.info(
        f"Worker pool: {num_workers} воркеров, {total} серверов, "
        f"порт-пул {port_pool}"
    )

    await asyncio.gather(*[worker(i) for i in range(num_workers)])

    cleanup_dir = base_dir() / "out"
    for f in cleanup_dir.glob("singbox_*.json"):
        try:
            f.unlink()
        except Exception:
            pass

    return results