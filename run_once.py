"""Однократный прогон pipeline — для GitHub Actions."""
import asyncio
import json
import sys
from pathlib import Path

import yaml
from loguru import logger

from core.pipeline import run_core_pipeline
from core.singbox import set_anti_dpi


async def run(config: dict) -> None:
    result = await run_core_pipeline(config, is_github=True)

    out_dir = Path("out")
    out_dir.mkdir(exist_ok=True)

    for profile in config.get("subscription_profiles", []):
        path = profile["path"]
        filename = profile.get("filename", f"{path}.json")

        if path in result.json_subscriptions:
            (out_dir / filename).write_text(
                result.json_subscriptions[path], encoding="utf-8"
            )
            logger.info(f"Сохранён {filename} ({result.counts.get(path, 0)} серверов)")

    stats_path = out_dir / "stats.json"
    stats = {
        "total_fetched": result.total_fetched,
        "total_tcp_alive": result.total_tcp_alive,
        "total_speed_passed": result.total_speed_passed,
        "profiles": result.counts,
    }
    stats_path.write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"Статистика сохранена в {stats_path}")

    # Автоматически конвертируем каждый сохранённый JSON в URI-список TXT,
    # чтобы содержимое сразу можно было вставить в Hiddify из буфера обмена.
    try:
        from json_to_uri import convert_file as _json_to_txt
        for json_file in sorted(out_dir.glob("WhiteFast-*.json")):
            try:
                txt = _json_to_txt(json_file)
                with txt.open(encoding="utf-8") as fp:
                    n_lines = sum(1 for _ in fp)
                logger.info(f"Сохранён {txt.name} ({n_lines} URI)")
            except Exception as e:
                logger.warning(f"json_to_uri({json_file.name}) failed: {e}")
    except ImportError:
        logger.warning("json_to_uri.py не найден — .txt не создан")


async def main() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "run_once.log"

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        log_file,
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
    )
    logger.info(f"Логи пишутся в {log_file.resolve()}")

    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    set_anti_dpi(config.get("anti_dpi", {}))
    # set_fetch_mode вызывается внутри run_core_pipeline (по environment)

    await run(config)


if __name__ == "__main__":
    asyncio.run(main())