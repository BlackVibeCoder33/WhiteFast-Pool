import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from core.paths import base_dir
from core.builder_white import build_white_config
from core.fetcher import (
    expand_representatives,
    fetch_all,
    group_by_server_port,
)
from core.filter_rank import (
    build_profiles,
    ensure_unique_names,
    sort_by_speed,
)
from core.cache import (
    filter_banned,
    load_cache,
    save_cache,
    update_after_run,
)
from core.cdn_filter import filter_fake_speeds
from core.geoip import lookup_countries
from core.service_check import get_service_concurrent, service_check_all
from core.speed_test import get_speed_concurrent, speed_test_all
from core.tcp_check import tcp_check_all


@dataclass
class PipelineResult:
    json_subscriptions: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    total_fetched: int = 0
    total_tcp_alive: int = 0
    total_speed_passed: int = 0


async def run_core_pipeline(
    config: dict[str, Any], is_github: bool
) -> PipelineResult:
    logger.info("=" * 60)
    logger.info(f"WhiteFast Pipeline: старт ({'GitHub' if is_github else 'PC'})")
    logger.info("=" * 60)
    t_start = time.time()

    # ─── Автоопределение среды (black / white) ───
    from core.fetcher import detect_environment, set_fetch_mode

    env = str(config.get("environment", "auto")).lower()
    if env == "auto":
        env = await detect_environment()
        logger.info(
            f"[env] Автоопределение: {env} "
            f"(TCP+HTTP к raw.githubusercontent.com:443)"
        )
    else:
        logger.info(f"[env] Среда из конфига: {env}")

    if env == "white":
        set_fetch_mode("cache_first")
        config.setdefault("speed_test", {})["mode"] = "white"
        config.setdefault("geoip", {})["enabled"] = False
        logger.info(
            "[env] Применено: fetch=cache_first, "
            "speed_test.mode=white, geoip.enabled=false"
        )
    else:
        set_fetch_mode(config.get("fetch", {}).get("mode", "auto"))
        logger.info(
            f"[env] Применено: fetch={config.get('fetch', {}).get('mode', 'auto')}, "
            f"speed_test.mode={config.get('speed_test', {}).get('mode', 'black')}, "
            f"geoip.enabled={config.get('geoip', {}).get('enabled', True)}"
        )

    proxies_all = await fetch_all(config["subscriptions"])
    logger.info(f"[1] После парсинга: {len(proxies_all)}")
    ru_after_parse = sum(1 for p in proxies_all if p.get("_is_russian"))
    logger.info(f"[1] РФ-серверов после парсинга: {ru_after_parse}")

    udp_types = set(config.get("udp_types", ["hysteria2"]))

    tcp_proxies = [p for p in proxies_all if p.get("type") not in udp_types]
    hy2_proxies = [p for p in proxies_all if p.get("type") in udp_types]
    logger.info(
        f"[1.5] Разделение: TCP {len(tcp_proxies)}, UDP/hy2 {len(hy2_proxies)}"
    )

    tcp_reps, tcp_map = group_by_server_port(tcp_proxies)
    logger.info(f"[1.5] TCP уникальных (server, port): {len(tcp_reps)}")

    tcp_cfg = config.get("tcp_check", {})
    tcp_alive, _ = await tcp_check_all(
        tcp_reps,
        concurrent=tcp_cfg.get("concurrent", 200),
        timeout=float(tcp_cfg.get("timeout_seconds", 3)),
    )
    max_ping = config["thresholds"]["max_ping_ms"]
    tcp_alive = [p for p in tcp_alive if p.get("_tcp_ping_ms", 1e9) <= max_ping]
    logger.info(f"[2] TCP живых и <= {max_ping} мс: {len(tcp_alive)}")

    for p in hy2_proxies:
        p["_tcp_ok"] = True
        p["_tcp_ping_ms"] = 0.0
        p["_tcp_skip"] = True
    logger.info(f"[2] hy2 к service-check: {len(hy2_proxies)}")

    alive = tcp_alive + hy2_proxies
    logger.info(f"[2.5] Всего к service-check: {len(alive)}")

    st = config.get("speed_test", {})
    scfg = config.get("service_check", {})
    svc_concurrent = get_service_concurrent(config, is_github)
    spd_concurrent = get_speed_concurrent(config, is_github)

    cache_cfg = config.get("cache", {}) or {}
    cache_enabled = bool(cache_cfg.get("enabled", False))
    cache_path = base_dir() / cache_cfg.get("cache_file", "out/cache.json")
    cache_data = load_cache(cache_path) if cache_enabled else {"servers": {}}

    if cache_enabled:
        alive = filter_banned(
            alive,
            cache_data,
            float(cache_cfg.get("ban_duration_hours", 2)),
        )

    checked = await service_check_all(
        alive,
        config["services"],
        concurrent=svc_concurrent,
        scfg=scfg,
    )

    def _has_any_white(p: dict[str, Any]) -> bool:
        svc = p.get("_services", {})
        return any(v.get("ok") for v in svc.values())

    relevant = [p for p in checked if _has_any_white(p)]
    logger.info(
        f"[2.5] Прошли хотя бы один white-сервис: {len(relevant)} из {len(checked)}"
    )

    for svc_name in config["services"]:
        ok = sum(
            1 for p in checked if p.get("_services", {}).get(svc_name, {}).get("ok")
        )
        logger.info(f"[2.5] {svc_name}: прошли {ok} из {len(checked)}")

    speed_mode = str(st.get("mode", "black")).lower()
    if speed_mode == "white":
        test_urls = st.get("whitelist_test_urls") or st.get("test_urls")
        min_bytes = int(st.get("min_measure_bytes_whitelist", 500_000))
    else:
        test_urls = st.get("test_urls")
        min_bytes = int(st.get("min_measure_bytes", 300_000))

    if not test_urls:
        test_urls = [
            st.get(
                "test_url",
                "https://dl.google.com/dl/android/studio/install/3.5.2.0/"
                "android-studio-ide-191.5977832-windows.exe",
            )
        ]

    logger.info(
        f"[3] Speed-test режим: {speed_mode} "
        f"(urls={len(test_urls)}, min_bytes={min_bytes})"
    )

    timeout = float(st.get("timeout_seconds", 20))
    retest = int(st.get("retest_count", 2))
    retest_pause = float(st.get("retest_pause_seconds", 0))
    peak_win = float(st.get("peak_window_seconds", 0.5))

    speed_results = await speed_test_all(
        relevant,
        test_urls=test_urls,
        timeout=timeout,
        concurrent=spd_concurrent,
        retest_count=retest,
        retest_pause_seconds=retest_pause,
        min_bytes=min_bytes,
        peak_window_sec=peak_win,
    )

    speed_results = filter_fake_speeds(speed_results)

    geoip_cfg = config.get("geoip", {}) or {}
    if geoip_cfg.get("enabled", True):
        await lookup_countries(speed_results)
    else:
        logger.info("[3] GeoIP: пропуск (geoip.enabled=false)")

    ru_after_speed = sum(1 for p in speed_results if p.get("_is_russian"))
    logger.info(f"[3] РФ-серверов после speed-test: {ru_after_speed}")

    if cache_enabled:
        cache_data = update_after_run(
            speed_results,
            cache_data,
            int(cache_cfg.get("ban_after_consecutive_failures", 3)),
            float(cache_cfg.get("ban_duration_hours", 2)),
        )
        save_cache(cache_path, cache_data)

    min_speed = config["thresholds"]["min_speed_mbps"]
    profiles = config.get("subscription_profiles", [])

    result = PipelineResult()

    if profiles:
        selections = build_profiles(
            speed_results,
            profiles,
            min_speed,
            max_service_latency_ms=config["thresholds"].get("max_service_latency_ms"),
            thresholds_by_type=config.get("thresholds_by_type", {}),
        )
        for profile in profiles:
            path = profile["path"]
            selected = selections.get(path, [])

            selected_tcp = [p for p in selected if not p.get("_tcp_skip")]
            selected_hy2 = [p for p in selected if p.get("_tcp_skip")]
            expanded_tcp = expand_representatives(selected_tcp, tcp_map)
            base_list = list(expanded_tcp) + list(selected_hy2)

            final_list = sort_by_speed(ensure_unique_names(base_list))
            if not final_list:
                logger.warning(f"[profiles] '{path}' пуст — пропуск")
                continue

            result.json_subscriptions[path] = build_white_config(
                final_list, profile
            )
            result.counts[path] = len(final_list)

            logger.info(f"[profiles] '{path}': {len(final_list)} записей")

    elapsed = time.time() - t_start
    logger.info(f"[4] Pipeline завершён за {elapsed:.0f} с")

    result.total_fetched = len(proxies_all)
    result.total_tcp_alive = len(alive)
    result.total_speed_passed = len(speed_results)

    return result