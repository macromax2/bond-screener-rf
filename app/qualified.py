"""Квал-статус бумаг: ISQUALIFIEDINVESTORS из MOEX ISS.

MOEX отдаёт это поле ТОЛЬКО поштучно (`/securities/{secid}`), его нет ни в
каталоге (`/securities.json` -- 14 колонок), ни в биржевом снапшоте
(`markets/bonds/securities` -- 47 колонок). То есть для 3176 бумаг пришлось бы
сделать 3176 запросов -- это ~2-3 минуты, платить такой ценой каждый день
нельзя.

Решение: локальный кэш `data/qualified.json`. Прогоняем полную выгрузку один
раз (первый сбор снапшота), дальше докачаем ТОЛЬКО бумаги, которых ещё нет в
кэше -- то есть новые выпуски (единицы в день). Статус бумаги меняется крайне
редко, поэтому кэш живёт долго.

Значения: "1" -- только для квалифицированных инвесторов,
          "0" -- доступно всем,
          отсутствует/None -- не удалось выяснить (ошибка сети).

Принцип безопасности: неизвестное трактуем как «доступно всем». Мы СКРЫВАЕМ
квал-бумаги по умолчанию, поэтому ошибка сети не должна приводить к тому,
что нормальные бумаги пропадут из выдачи -- лучше показать пару кваловских,
чем потерять сотню хороших.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import moex

QUALIFIED_FILE = Path(moex.DATA_DIR) / "qualified.json"

# Меньше воркеров, чем у каталога: ISS отдаёт description поштучно и
# довольно неохотно грузит много параллельных запросов.
_WORKERS = 12
_RETRIES = 2
_TIMEOUT = 25

_cache: dict | None = None


def _read_file() -> dict:
    """Читает кэш с диска {secid: "0"|"1"}."""
    try:
        data = json.loads(QUALIFIED_FILE.read_text(encoding="utf-8"))
        items = data.get("items")
        return items if isinstance(items, dict) else {}
    except Exception:  # noqa: BLE001 -- нет файла или он битый -> пусто
        return {}


def _write_file(items: dict) -> None:
    """Атомарно сохраняет кэш (tmp -> rename), чтобы не порвать при сбое."""
    try:
        QUALIFIED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = QUALIFIED_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"updated_at": time.time(), "items": items},
                       ensure_ascii=False), encoding="utf-8")
        tmp.replace(QUALIFIED_FILE)
    except Exception:  # noqa: BLE001 -- кэш не критичен для работы
        pass


def _fetch_one(secid: str) -> tuple[str, str | None]:
    """Один запрос к ISS -> (secid, "1"|"0"|None). None = сбой, не кэшируем."""
    try:
        info = moex.fetch_description(secid)
    except Exception:  # noqa: BLE001
        return secid, None
    if not info:
        # Пустой ответ (лимит/сбой ISS) -- НЕ записываем "0", иначе навсегда
        # решим, что бумага доступна всем. Вернём None и повторим завтра.
        return secid, None
    return secid, "1" if str(info.get("ISQUALIFIEDINVESTORS")).strip() == "1" else "0"


def resolve(secids: list[str], progress=None, errors: list[str] | None = None) -> dict:
    """Возвращает {secid: "0"|"1"|None}, докачивая в кэш отсутствующие бумаги.

    progress(done, total) -- обратный вызов для лога долгой первой выгрузки.
    """
    global _cache
    items = _read_file()
    missing = [s for s in dict.fromkeys(secids) if s and s not in items]

    if missing:
        done = 0
        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            futures = {ex.submit(_fetch_one, s): s for s in missing}
            for fut in as_completed(futures):
                secid, val = fut.result()
                if val is not None:
                    items[secid] = val
                done += 1
                if progress and done % 250 == 0:
                    progress(done, len(missing))
        _write_file(items)
        if errors is not None:
            lost = len(missing) - len([s for s in missing if s in items])
            if lost:
                errors.append(f"квал-статус: не получено {lost} из {len(missing)} (повтор при следующем сборе)")

    _cache = items
    return items


def get(secid: str):
    """Квал-статус бумаги: "1" | "0" | None (неизвестно).

    Лениво поднимает кэш с диска, если resolve() ещё не вызывали.
    """
    global _cache
    if _cache is None:
        _cache = _read_file()
    return _cache.get(secid)


def stats() -> dict:
    """Для отладки: размер кэша и доля квал-бумаг."""
    items = _read_file()
    qualified = sum(1 for v in items.values() if v == "1")
    return {"cached": len(items), "qualified_only": qualified}
