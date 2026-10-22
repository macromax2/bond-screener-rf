#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Быстрый тест ядра скринера без запуска сервера.

Проверяет сборку дневного снапшота (данные закрытого дня) и работу
пресетов на нём. Запуск: .venv/bin/python -m app.selftest

Флаг --db читает уже сохранённый снапшот из базы (быстро, без сети).
"""
from __future__ import annotations

import sys
from collections import Counter

from . import analytics, daily, store


def main() -> int:
    use_db = "--db" in sys.argv

    if use_db:
        snap = store.latest_snapshot()
        if not snap:
            print("FAIL: в базе нет снапшотов. Запустите без --db.",
                  file=sys.stderr)
            return 1
        print(f"[—] Снапшот из базы за {snap['trade_date']} "
              f"({len(snap['bonds'])} бумаг)")
        bonds = snap["bonds"]
    else:
        print("[1/2] Сборка дневного снапшота (закрытый торговый день) ...")
        try:
            snap = daily.build_snapshot()
        except Exception as err:  # noqa: BLE001
            print(f"FAIL: сборка не удалась: {err}", file=sys.stderr)
            return 1
        bonds = snap["bonds"]
        print(f"      дата торгов: {snap['trade_date']} | "
              f"бумаг: {len(bonds)}")

    print("[2/2] Пресеты ...")

    with_ytm = [b for b in bonds if b["ytm"] is not None]
    clean = [b for b in with_ytm if not b.get("anomaly")]
    anomalies = [b for b in bonds if b.get("anomaly")]
    traded = [b for b in bonds if b["traded_today"]]
    with_liq = [b for b in bonds if (b.get("liq") or 0) > 0]
    with_turn = [b for b in bonds if (b.get("turnover") or 0) > 0]
    groups = Counter(b["group"] for b in bonds)
    ccy = Counter(b["currency"] for b in bonds)
    sources = Counter(b.get("day_source") for b in bonds)

    print()
    print(f"Записей: {len(bonds)} | с доходностью: {len(with_ytm)} "
          f"| с ликвидностью >0: {len(with_liq)}")
    print(f"Торговались в день снапшота: {len(traded)} "
          f"| с оборотом >0: {len(with_turn)}")
    print(f"Источник итогов дня: {dict(sources)}")
    print("Группы:", dict(groups))
    print("Валюты:", dict(ccy))
    print(f"Аномальные YTM (исключены из статистики): {len(anomalies)}")
    if clean:
        ys = sorted(b["ytm"] for b in clean)
        print(f"YTM: мин {ys[0]:.2f}% | медиана {ys[len(ys)//2]:.2f}% "
              f"| макс {ys[-1]:.2f}%")
    turnover = sum(b.get("turnover") or 0 for b in bonds)
    print(f"Оборот дня: {turnover/1e6:.0f} млн руб.")

    # Ключевая проверка: утром live-оборот нулевой, а в снапшоте
    # закрытого дня он должен быть заполнен у сотен бумаг.
    if not with_liq:
        print("\nFAIL: ни одна бумага не имеет ликвидности -- "
              "итоги дня не подмешались", file=sys.stderr)
        return 1

    print()
    print("Пресеты:")
    failed = []
    for p in analytics.PRESETS:
        rows = analytics.apply_filters(bonds, p["filters"])
        print(f"  {p['title']:26} -> {len(rows):4} бумаг")
        if not rows:
            failed.append(p["title"])

    print()
    print("Топ-10 по доходности среди ликвидных (об. > 1 млн руб.):")
    top = sorted((b for b in bonds
                  if b["ytm"] and 0 < b["ytm"] < 100 and (b["turnover"] or 0) > 1e6
                  and b["currency"] == "SUR" and not b.get("anomaly")),
                 key=lambda b: -b["ytm"])[:10]
    for b in top:
        print(f"  {b['name'][:16]:16} {b['ytm']:6.2f}%  "
              f"дюр {b['duration'] or '-':>5} срок {b['years'] or '-':>5}г  "
              f"цена {b['price'] or '-':>7}  "
              f"{b['group'][:18]:18} {b['emitent'] or ''}"[:120])

    if failed:
        print(f"\nПРЕДУПРЕЖДЕНИЕ: пустые пресеты: {', '.join(failed)}",
              file=sys.stderr)

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
