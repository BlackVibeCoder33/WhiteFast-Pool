"""Обновляет все out/*.json в Gist одним API-запросом."""
import json
import os
import sys
from pathlib import Path

import urllib.error
import urllib.request


def main() -> int:
    token = os.environ.get("GIST_TOKEN")
    gist_id = os.environ.get("GIST_ID")
    if not token or not gist_id:
        print("ERROR: GIST_TOKEN или GIST_ID не заданы", file=sys.stderr)
        return 1

    out_dir = Path("out")
    files = sorted(out_dir.glob("WhiteFast-*.json"))
    if not files:
        print("Нет файлов out/WhiteFast-*.json")
        return 0

    payload_files = {}
    for f in files:
        payload_files[f.name] = {"content": f.read_text(encoding="utf-8")}
        print(f"  + {f.name} ({f.stat().st_size} байт)")

    body = json.dumps({"files": payload_files}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "WhiteFast-Pool",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "ignore")
        print(f"ERROR HTTP {e.code}: {err_body}", file=sys.stderr)
        return 1

    print(f"OK. Обновлено файлов в Gist: {len(data.get('files', {}))}")
    for name in data.get("files", {}):
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())