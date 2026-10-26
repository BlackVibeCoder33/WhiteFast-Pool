"""Генератор минимального sing-box native JSON для WhiteFast-Pool (Hiddify).

Ключ `route_final` в профиле управляет маршрутизацией:
- "direct" (по умолчанию) — route.final="direct", маршрутизацию строит
  Hiddify UI (Bypass Russia и т.п.).
- "proxy" — route.final=selector "proxy-auto" со всеми прокси. Весь
  трафик через прокси. Работает из коробки без настройки Hiddify UI.

Использует convert_proxy() из core/singbox.py для маппинга Mihomo-прокси
в sing-box outbound.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from core.singbox import convert_proxy


def _build_inbounds() -> list[dict[str, Any]]:
    """TUN inbound. sniff=True на случай, если Hiddify использует JSON
    как полный конфиг. Если переопределит своим — блок будет проигнорирован.
    """
    return [
        {
            "type": "tun",
            "tag": "tun-in",
            "inet4_address": "172.19.0.1/30",
            "auto_route": True,
            "strict_route": True,
            "sniff": True,
            "sniff_override_destination": True,
        }
    ]


def _build_outbounds(
    proxies: list[dict[str, Any]],
    add_selector: bool = False,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Конвертирует прокси в outbounds. Возвращает (outbounds, tags, selector_tag).

    Тег = оригинальное имя прокси. Используется json_to_uri.py как
    #fragment в URI-списке и отображается в Hiddify как имя сервера.

    Если add_selector=True — добавляется outbound type=selector с тегом
    "proxy-auto" (список всех proxy-tags, default = первый). route.final=
    "proxy-auto" направляет весь трафик через прокси, Hiddify может
    переключать сервер в UI без правки JSON.
    """
    outbounds: list[dict[str, Any]] = []
    tags: list[str] = []
    seen_tags: set[str] = set()

    for i, p in enumerate(proxies):
        base_name = str(p.get("name") or "").strip()
        if not base_name:
            base_name = f"proxy-{i}"
        tag = base_name
        n = 0
        while tag in seen_tags:
            n += 1
            tag = f"{base_name} [{n}]"
        seen_tags.add(tag)

        ob = convert_proxy(p, tag)
        if ob is None:
            logger.warning(
                f"builder_white: пропущен неподдерживаемый прокси {p.get('name')}"
            )
            continue
        outbounds.append(ob)
        tags.append(tag)

    selector_tag: str | None = None
    if add_selector and tags:
        selector_tag = "proxy-auto"
        while selector_tag in seen_tags:
            selector_tag = f"{selector_tag}-auto"
        outbounds.append(
            {
                "type": "selector",
                "tag": selector_tag,
                "outbounds": list(tags),
                "default": tags[0],
            }
        )

    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})
    return outbounds, tags, selector_tag


def build_white_config(
    proxies: list[dict[str, Any]],
    profile_config: dict[str, Any] | None = None,
    base_config: dict[str, Any] | None = None,
) -> str:
    """Собирает минимальный sing-box native JSON для Hiddify.

    :param proxies: отфильтрованные прокси.
    :param profile_config: секция профиля из config.yaml. Ключ `route_final`
        определяет маршрутизацию: "direct" (по умолчанию) или "proxy".
    :param base_config: опциональный шаблон для мержа.
    :return: JSON-строка.
    """
    if not proxies:
        raise ValueError("build_white_config: пустой список прокси")

    route_final = str((profile_config or {}).get("route_final", "direct")).lower()
    full_tunnel = route_final == "proxy"

    outbounds, proxy_tags, selector_tag = _build_outbounds(
        proxies, add_selector=full_tunnel
    )
    if not proxy_tags:
        raise ValueError("build_white_config: ни один прокси не сконвертирован")

    if full_tunnel and selector_tag:
        final = selector_tag
    else:
        final = "direct"

    config: dict[str, Any] = {
        "log": {"level": "warn"},
        "inbounds": _build_inbounds(),
        "outbounds": outbounds,
        "route": {"final": final},
        "experimental": {"cache_file": {"enabled": True}},
    }

    if base_config:
        for key, value in base_config.items():
            if key not in config:
                config[key] = value

    profile_name = (profile_config or {}).get("name", "?")
    logger.info(
        f"builder_white: профиль '{profile_name}' — "
        f"{len(proxy_tags)} прокси, route.final={final}"
        f"{' (full-tunnel)' if full_tunnel else ''}"
    )
    return json.dumps(config, ensure_ascii=False, indent=2)