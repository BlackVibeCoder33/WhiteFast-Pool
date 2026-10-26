from typing import Any

from loguru import logger


_JUNK_NAME_WORDS = {
    "channel", "free", "all", "server", "vpn", "node",
    "config", "unlimited", "join", "telegram",
}


def _code_to_flag(code: str) -> str:
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


def _is_junk_name(name: str) -> bool:
    n = name.strip()
    if not n:
        return True
    if len(n) < 4 and not any(c.isalpha() for c in n):
        return True
    if n.lower() in _JUNK_NAME_WORDS:
        return True
    return False


def filter_and_rank(
    proxies: list[dict[str, Any]],
    required_services: list[str],
    min_speed_mbps: float,
) -> list[dict[str, Any]]:
    passed: list[dict[str, Any]] = []
    for p in proxies:
        svc = p.get("_services", {})
        if not all(svc.get(req, {}).get("ok") for req in required_services):
            continue
        if p.get("_speed_mbps", 0) < min_speed_mbps:
            continue
        passed.append(p)
    logger.info(
        f"Фильтр (all из {required_services}): {len(passed)} из {len(proxies)}"
    )
    return passed


def build_profiles(
    proxies: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    min_speed_mbps: float,
    max_service_latency_ms: float | None = None,
    thresholds_by_type: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    thresholds_by_type = thresholds_by_type or {}
    result: dict[str, list[dict[str, Any]]] = {}

    for profile in profiles:
        path = profile["path"]
        required = profile["required_services"]
        profile_min_speed = float(profile.get("min_speed_mbps", min_speed_mbps))
        allow_russian = bool(profile.get("allow_russian", False))
        exclude_patterns = [
            str(x).lower() for x in profile.get("exclude_name_contains", []) if x
        ]
        exclude_geo = [str(x).lower() for x in profile.get("exclude_geo", [])]
        exclude_geo_iso = {g.upper() for g in exclude_geo if len(g) == 2 and g.isalpha()}

        selected: list[dict[str, Any]] = []
        excluded_by_pattern = 0
        excluded_russian = 0
        excluded_by_latency = 0
        excluded_by_geo = 0
        excluded_by_missing_service = 0

        for p in proxies:
            svc = p.get("_services", {})

            # 1) РФ-флаг
            if p.get("_is_russian") and not allow_russian:
                excluded_russian += 1
                continue

            # 2) Гео-фильтр
            if exclude_geo:
                all_names = p.get("_all_names") or [p.get("name") or ""]
                all_names = [str(n).lower() for n in all_names if n]

                def _has_geo_match(name: str) -> bool:
                    for geo in exclude_geo:
                        if len(geo) <= 4:
                            import re as _re
                            if _re.search(rf"\b{_re.escape(geo)}\b", name):
                                return True
                            if not geo.isalnum() and geo in name:
                                return True
                        else:
                            if geo in name:
                                return True
                    return False

                if any(_has_geo_match(n) for n in all_names):
                    excluded_by_geo += 1
                    continue

                country = (p.get("_country_code") or "").upper()
                if country and country in exclude_geo_iso:
                    excluded_by_geo += 1
                    continue

            # 3) White-list: ВСЕ сервисы из required профиля должны работать
            if not all(svc.get(req, {}).get("ok") for req in required):
                excluded_by_missing_service += 1
                continue

            # 4) Скорость
            if p.get("_speed_mbps", 0) < profile_min_speed:
                continue

            # 5) Задержка до сервиса
            ptype = p.get("type") or ""
            type_overrides = thresholds_by_type.get(ptype) or {}
            effective_max_lat = type_overrides.get(
                "max_service_latency_ms", max_service_latency_ms
            )
            if effective_max_lat is not None:
                too_slow = False
                for req in required:
                    ms = svc.get(req, {}).get("ms", 0) or 0
                    if ms > effective_max_lat:
                        too_slow = True
                        break
                if too_slow:
                    excluded_by_latency += 1
                    continue

            # 6) Исключение по имени
            if exclude_patterns:
                haystack = f"{p.get('name') or ''} {p.get('server') or ''}".lower()
                if any(pat in haystack for pat in exclude_patterns):
                    excluded_by_pattern += 1
                    continue

            selected.append(p)

        result[path] = selected

        sources: dict[str, int] = {}
        for p in selected:
            s = p.get("_source") or "unknown"
            sources[s] = sources.get(s, 0) + 1
        source_str = ", ".join(f"{k}={v}" for k, v in sources.items())

        excluded_str = ""
        if excluded_by_missing_service:
            excluded_str += f", не все сервисы: {excluded_by_missing_service}"
        if excluded_by_pattern:
            excluded_str += f", исключено по фильтру: {excluded_by_pattern}"
        if excluded_russian:
            excluded_str += f", исключено РФ: {excluded_russian}"
        if excluded_by_latency:
            excluded_str += f", исключено по задержке: {excluded_by_latency}"
        if excluded_by_geo:
            excluded_str += f", исключено по гео: {excluded_by_geo}"

        logger.info(
            f"Профиль '{path}' (all из {required}) "
            f"min_speed={profile_min_speed:g}"
            f"{' +РФ' if allow_russian else ''}: "
            f"{len(selected)} серверов [{source_str}]{excluded_str}"
        )
    return result


def ensure_unique_names(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    for p in proxies:
        base = (p.get("name") or "").strip()
        if _is_junk_name(base):
            code = str(p.get("_country_code") or "").upper()
            flag = _code_to_flag(code) if code else ""
            server = str(p.get("server") or "?")
            port = p.get("port") or "?"
            prefix = f"{flag} {code}".strip() if code else "Server"
            base = f"{prefix} {server}:{port}"
        if not base:
            base = "Server"
        if base not in seen:
            seen[base] = 0
            p["name"] = base
            continue
        seen[base] += 1
        p["name"] = f"{base} [{p.get('server')}:{p.get('port')}]"
    return proxies


def sort_by_speed(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(p: dict[str, Any]) -> float:
        speed = float(p.get("_speed_mbps", 0) or 0)
        streak = int(p.get("_success_streak", 0) or 0)
        stability_bonus = 1.0 + 0.03 * min(streak, 10)
        return speed * stability_bonus

    return sorted(proxies, key=_key, reverse=True)