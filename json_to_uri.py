#!/usr/bin/env python3
"""json_to_uri.py — конвертирует WhiteFast JSON-подписки в URI-список (TXT).

Hiddify через буфер обмена принимает только URI-список (vless://, vmess://,
trojan://, ss://, hysteria2:// — по одному на строке). Наш pipeline
сохраняет sing-box native JSON, который в буфер не вставляется.

Скрипт читает out/WhiteFast-*.json и пишет рядом out/WhiteFast-*.txt.
Содержимое .txt копируется в буфер обмена → в Hiddify → «Добавить из
буфера обмена».

Запуск (из корня проекта):
    python json_to_uri.py                              # все out/*.json
    python json_to_uri.py out/WhiteFast-Any.json       # один файл
    python json_to_uri.py out --out-dir out_txt        # в другую папку
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, quote


# ─── helpers ───────────────────────────────────────────────
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_str(s: str) -> str:
    return _b64(s.encode("utf-8"))


def _get_name(ob: dict[str, Any]) -> str:
    tag = str(ob.get("tag") or "")
    if tag and not tag.startswith("proxy-"):
        return tag
    server = ob.get("server", "?")
    port = ob.get("server_port", "?")
    return f"{tag or 'proxy'} {server}:{port}"


def _tls_params(tls: dict[str, Any] | None) -> dict[str, str]:
    if not tls or not tls.get("enabled"):
        return {}
    out: dict[str, str] = {"security": "tls"}
    ro = tls.get("reality")
    if ro and ro.get("enabled"):
        out["security"] = "reality"
        if ro.get("public_key"):
            out["pbk"] = ro["public_key"]
        if ro.get("short_id"):
            out["sid"] = ro["short_id"]
    sni = tls.get("server_name")
    if sni:
        out["sni"] = sni
    utls = tls.get("utls") or {}
    if utls.get("fingerprint"):
        out["fp"] = utls["fingerprint"]
    if tls.get("insecure"):
        out["allowInsecure"] = "1"
    alpn = tls.get("alpn")
    if alpn:
        out["alpn"] = ",".join(alpn) if isinstance(alpn, list) else str(alpn)
    return out


def _transport_params(transport: dict[str, Any] | None) -> dict[str, str]:
    if not transport:
        return {"type": "tcp"}
    t = transport.get("type", "tcp")
    out: dict[str, str] = {"type": t}
    if t == "ws":
        out["path"] = transport.get("path", "/")
        headers = transport.get("headers") or {}
        host = headers.get("Host") or headers.get("host")
        if host:
            out["host"] = host
    elif t == "grpc":
        sn = transport.get("service_name")
        if sn:
            out["serviceName"] = sn
    elif t == "http":
        hosts = transport.get("host") or []
        if hosts:
            out["host"] = ",".join(hosts) if isinstance(hosts, list) else str(hosts)
        path = transport.get("path")
        if path:
            out["path"] = path
    return out


# ─── URI builders ──────────────────────────────────────────
def _vless_uri(ob: dict[str, Any]) -> str:
    uuid = ob.get("uuid", "")
    server = ob.get("server", "")
    port = ob.get("server_port", 443)
    params: dict[str, str] = {}
    params.update(_tls_params(ob.get("tls")))
    params.update(_transport_params(ob.get("transport")))
    if ob.get("flow"):
        params["flow"] = ob["flow"]
    name = _get_name(ob)
    qs = urlencode(params) if params else ""
    frag = quote(name, safe="")
    base = f"vless://{uuid}@{server}:{port}"
    return f"{base}?{qs}#{frag}" if qs else f"{base}#{frag}"


def _vmess_uri(ob: dict[str, Any]) -> str:
    tls = ob.get("tls") or {}
    transport = ob.get("transport") or {}
    net = transport.get("type", "tcp")
    if net == "http":
        net = "tcp"
    vmess_json: dict[str, Any] = {
        "v": "2",
        "ps": _get_name(ob),
        "add": ob.get("server", ""),
        "port": str(ob.get("server_port", 443)),
        "id": ob.get("uuid", ""),
        "aid": str(ob.get("alter_id", 0)),
        "scy": ob.get("security", "auto"),
        "net": net,
        "type": "none",
        "host": "",
        "path": "",
        "tls": "",
        "sni": "",
    }
    if tls.get("enabled"):
        vmess_json["tls"] = "tls"
        if tls.get("server_name"):
            vmess_json["sni"] = tls["server_name"]
        utls = tls.get("utls") or {}
        if utls.get("fingerprint"):
            vmess_json["fp"] = utls["fingerprint"]
    if net == "ws":
        vmess_json["path"] = transport.get("path", "/")
        headers = transport.get("headers") or {}
        host = headers.get("Host") or headers.get("host")
        if host:
            vmess_json["host"] = host
    elif net == "grpc":
        vmess_json["path"] = transport.get("service_name", "")
    raw = json.dumps(vmess_json, ensure_ascii=False, separators=(",", ":"))
    return f"vmess://{_b64_str(raw)}"


def _trojan_uri(ob: dict[str, Any]) -> str:
    password = ob.get("password", "")
    server = ob.get("server", "")
    port = ob.get("server_port", 443)
    params: dict[str, str] = {}
    params.update(_tls_params(ob.get("tls")))
    params.update(_transport_params(ob.get("transport")))
    name = _get_name(ob)
    qs = urlencode(params) if params else ""
    frag = quote(name, safe="")
    base = f"trojan://{quote(password, safe='')}@{server}:{port}"
    return f"{base}?{qs}#{frag}" if qs else f"{base}#{frag}"


def _ss_uri(ob: dict[str, Any]) -> str:
    method = ob.get("method", "")
    password = ob.get("password", "")
    server = ob.get("server", "")
    port = ob.get("server_port", 443)
    name = _get_name(ob)
    userinfo = _b64_str(f"{method}:{password}")
    frag = quote(name, safe="")
    return f"ss://{userinfo}@{server}:{port}#{frag}"


def _hysteria2_uri(ob: dict[str, Any]) -> str:
    password = ob.get("password", "")
    server = ob.get("server", "")
    port = ob.get("server_port", 443)
    tls = ob.get("tls") or {}
    params: dict[str, str] = {}
    if tls.get("server_name"):
        params["sni"] = tls["server_name"]
    if tls.get("insecure"):
        params["insecure"] = "1"
    obfs = ob.get("obfs") or {}
    if obfs.get("type"):
        params["obfs"] = obfs["type"]
    if obfs.get("password"):
        params["obfs-password"] = obfs["password"]
    name = _get_name(ob)
    qs = urlencode(params) if params else ""
    frag = quote(name, safe="")
    base = f"hysteria2://{quote(password, safe='')}@{server}:{port}"
    return f"{base}?{qs}#{frag}" if qs else f"{base}#{frag}"


def outbound_to_uri(ob: dict[str, Any]) -> str | None:
    ptype = ob.get("type")
    try:
        if ptype == "vless":
            return _vless_uri(ob)
        if ptype == "vmess":
            return _vmess_uri(ob)
        if ptype == "trojan":
            return _trojan_uri(ob)
        if ptype == "shadowsocks":
            return _ss_uri(ob)
        if ptype == "hysteria2":
            return _hysteria2_uri(ob)
    except Exception as e:
        print(f"  ! {ptype} {ob.get('tag', '?')}: {type(e).__name__}: {e}",
              file=sys.stderr)
    return None


# ─── file I/O ──────────────────────────────────────────────
def json_to_uris(json_path: Path) -> list[str]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    outbounds = data.get("outbounds") or []
    uris: list[str] = []
    for ob in outbounds:
        if not isinstance(ob, dict):
            continue
        # selector — служебный outbound для full-tunnel, не конвертируется
        # в URI. direct/block/dns — тоже служебные.
        if ob.get("type") in ("direct", "block", "dns", "selector"):
            continue
        uri = outbound_to_uri(ob)
        if uri:
            uris.append(uri)
    return uris


def convert_file(json_path: Path, out_dir: Path | None = None) -> Path:
    uris = json_to_uris(json_path)
    target_dir = out_dir or json_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    txt_path = target_dir / (json_path.stem + ".txt")
    txt_path.write_text(
        "\n".join(uris) + ("\n" if uris else ""), encoding="utf-8"
    )
    return txt_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Конвертирует WhiteFast JSON-подписки в URI-список (TXT) "
                    "для вставки в Hiddify через буфер обмена."
    )
    parser.add_argument(
        "path", nargs="?", default="out",
        help="JSON-файл или директория (по умолчанию out)",
    )
    parser.add_argument(
        "--out-dir", default=None,
        help="куда писать .txt (по умолчанию рядом с .json)",
    )
    args = parser.parse_args()

    src = Path(args.path)
    out_dir = Path(args.out_dir) if args.out_dir else None

    files: list[Path]
    if src.is_file():
        files = [src]
    elif src.is_dir():
        files = sorted(src.glob("WhiteFast-*.json"))
        if not files:
            files = sorted(src.glob("*.json"))
    else:
        print(f"ERROR: путь не найден: {src}", file=sys.stderr)
        return 2

    if not files:
        print(f"ERROR: в {src} нет *.json", file=sys.stderr)
        return 3

    total_servers = 0
    print(f"Найдено файлов: {len(files)}")
    for f in files:
        try:
            txt = convert_file(f, out_dir)
        except Exception as e:
            print(f"  ✗ {f.name}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        with txt.open(encoding="utf-8") as fp:
            n = sum(1 for _ in fp)
        total_servers += n
        print(f"  ✓ {f.name} → {txt.name} ({n} серверов)")
    print(f"Итого: {total_servers} серверов в {len(files)} файлах")
    return 0


if __name__ == "__main__":
    sys.exit(main())