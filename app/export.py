#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экспорт дневного снапшота в статичный data.json (GitHub Pages).

Статичный режим: фронтенд index.html при отсутствии API (/api/screener
отдаёт 404) загружает ./data.json, лежащий рядом со страницей. Файл
собирается из той же базы и теми же функциями (_facets/_stats/_human_date),
что и ответ сервера, поэтому формат и данные полностью совпадают.

Запуск:
    python -m app.export             # пересобрать снапшот и записать data.json
    python -m app.export --if-stale  # пересобрать, только если за сегодня ещё
                                     # не собирали (ежедневная публикация)
    python -m app.export --no-build  # только выгрузить то, что уже в базе

Расписание публикации (см. publish.sh + launchd):
    19:20 МСК будни -- сразу после серверной пересборки (19:05, просто
    выгружаем готовое) и09:30 ежедневно -- догон, если вечером Мак был
    выключен. Пропущенные запуски launchd выполняет при пробуждении.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import analytics, daily, store
from .main import _facets, _human_date, _stats

OUT = Path(__file__).resolve().parent.parent / "static" / "data.json"


def _snapshot_is_stale() -> bool:
    """Устарел ли снапшот относительно ближайшего закрытого торгового дня.

    Не по календарной дате, а по торговому дню: днём (до 19:05) ближайший
    закрытый день -- вчерашний, и снапшот за него считается свежим.
    """
    snap = store.latest_snapshot()
    if not snap or not snap.get("bonds"):
        return True
    try:
        want = daily.resolve_trade_date()
    except Exception:  # noqa: BLE001 -- сеть недоступна: не мешаем публикации
        return False
    return snap.get("trade_date") != want


def export(no_build: bool = False, if_stale: bool = False) -> Path:
    """Записать data.json. Возвращает путь к файлу."""
    if no_build:
        pass
    elif if_stale and not _snapshot_is_stale():
        print("снапшот уже за ближайший закрытый день -- без пересборки",
              flush=True)
    else:
        daily.build_snapshot()

    snap = store.latest_snapshot()
    if not snap or not snap.get("bonds"):
        raise SystemExit("снапшот пуст -- сначала выполните сборку")

    bonds = snap.get("bonds") or []
    meta_src = dict(snap.get("meta") or {})
    meta = {
        "trade_date": snap.get("trade_date"),
        "trade_date_human": _human_date(snap.get("trade_date")),
        "built_iso": meta_src.get("built_iso"),
        "load_seconds": meta_src.get("load_seconds"),
        "errors": meta_src.get("errors") or [],
        "total_bonds": meta_src.get("total_bonds"),
        "from_history": meta_src.get("from_history"),
        "update_at": "19:05",  # время плановой публикации, МСК
        "loaded_iso": meta_src.get("built_iso"),  # стабильно: без «шума»
        "refreshing": False,
        "dates": store.list_dates()[:10],
        "static": True,  # фронтенд включает статичный режим по этому флагу
    }
    payload = {
        "meta": meta,
        "stats": _stats(bonds),
        "facets": _facets(bonds),
        "bonds": bonds,
        "presets": analytics.PRESETS,  # в статичном режиме пресеты отсюда
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    print(f"data.json: {OUT} ({OUT.stat().st_size / 1e6:.1f} МБ, "
          f"{len(bonds)} бумаг, дата {snap.get('trade_date')})", flush=True)
    return OUT


if __name__ == "__main__":
    export(no_build="--no-build" in sys.argv,
           if_stale="--if-stale" in sys.argv)
