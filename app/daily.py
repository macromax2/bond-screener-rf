#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка дневного снапшота скринера.

Дневной снапшот -- это состояние рынка за последний ЗАКРЫТЫЙ торговый день.
Он собирается из двух источников MOEX ISS:

  1. markets/bonds/securities  -- статичные параметры выпуска (купон, дата
     погашения, номинал, ISIN) + текущие рыночные доходности/дюрацию.
     После закрытия сессии рыночные данные уже не меняются до утра.
  2. history/.../securities    -- ИТОГИ закрытого дня: оборот, число сделок,
     средневзвешенная цена и доходность. Именно история даёт полный
     оборот за день, в то время как live-поля VALTODAY обнуляются к утру.

Почему это лечит утреннюю проблему: утром (до 10:00) у биржи оборот дня
ещё нулевой, и ликвидность всех бумаг падала в 0 -- пресеты с фильтром
ликвидности становились пустыми. Дневной снапшот хранит итоги закрытого
дня, поэтому утром/ночью/в выходные скринер отдаёт вчерашние полные данные.

Расписание: снапшот пересобирается раз в день после закрытия основной
сессии (19:05 МСК). Вручную -- POST /api/refresh.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import analytics, moex, qualified

MSK = ZoneInfo("Europe/Moscow")

# Доски, итоги которых собираем (тот же список, что и в снапшоте).
# SPOB исключён -- дубль TQOB.
HISTORY_BOARDS = list(moex.BOARDS)


def _day_history_page(board: str, trade_date: str, start: int) -> list[dict]:
    raw = moex._get(
        f"{moex.BASE}/history/engines/stock/markets/bonds/boards/"
        f"{board}/securities.json",
        {"iss.meta": "off", "from": trade_date, "till": trade_date,
         "limit": 100, "start": start},
    )
    return moex._table(raw.get("history"))


def _board_history(board: str, trade_date: str) -> list[dict]:
    """Все строки истории одной доски за день (пагинация по 100)."""
    rows: list[dict] = []
    start = 0
    while True:
        try:
            page = _day_history_page(board, trade_date, start)
        except Exception:  # noqa: BLE001 -- сбой страницы не критичен
            break
        if not page:
            break
        rows.extend(page)
        if len(page) < 100:
            break
        start += 100
        if start > 20000:  # защита от зацикливания
            break
    return rows


def fetch_day_totals(trade_date: str) -> dict[str, dict]:
    """Итоги закрытого дня: secid -> {turnover, trades, wap, ytm, ...}.

    При дублях SECID между досками оставляем запись с более приоритетной
    доски (приоритет -- как в moex.BOARDS).
    """
    board_rank = {b: i for i, b in enumerate(HISTORY_BOARDS)}
    best: dict[str, tuple[int, dict]] = {}

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_board_history, b, trade_date): b
                   for b in HISTORY_BOARDS}
        for fut, board in futures.items():
            try:
                rows = fut.result()
            except Exception:  # noqa: BLE001
                continue
            rank = board_rank.get(board, len(HISTORY_BOARDS))
            for r in rows:
                secid = r.get("SECID")
                if not secid:
                    continue
                cur = best.get(secid)
                if cur is None or rank < cur[0]:
                    best[secid] = (rank, r)

    out: dict[str, dict] = {}
    for secid, (_, r) in best.items():
        out[secid] = {
            "turnover": analytics._num(r.get("VALUE")) or 0.0,
            "trades": analytics._num(r.get("NUMTRADES")) or 0.0,
            "wap": analytics._num(r.get("WAPRICE")),
            "ytm": analytics._num(r.get("YIELDATWAP")),
            "close": analytics._num(r.get("CLOSE") or r.get("LEGALCLOSEPRICE")),
            "duration_days": analytics._num(r.get("DURATION")),
            "zspread": analytics._num(r.get("ZSPREADATWAPRICE")),
            "spread": analytics._num(r.get("ZSPREAD")),
        }
    return out


def last_closed_trade_date(today: date | None = None) -> str:
    """Ближайшая прошедшая торговая дата (сегодня после закрытия, иначе раньше).

    Ориентируется на календарь: будни считаем торговыми, пока история
    не подтвердит обратное. Если за будний день истории нет (выходной
    биржи/праздник), отступаем назад до 7 дней.
    """
    now = datetime.now(MSK)
    d = (today or now.date())
    # основная сессия закрывается 18:40; снапшот берём с 19:05
    if now.hour * 60 + now.minute < 19 * 60 + 5 and d == now.date():
        d = d - timedelta(days=1)
    for _ in range(8):
        if d.weekday() < 5:  # понедельник-пятница
            return d.isoformat()
        d = d - timedelta(days=1)
    return d.isoformat()


def _has_trading(trade_date: str) -> bool:
    """Есть ли сделки за дату (одна лёгкая проверка по TQCB)."""
    try:
        rows = _day_history_page("TQCB", trade_date, 0)
    except Exception:  # noqa: BLE001
        return False
    return any((analytics._num(r.get("NUMTRADES")) or 0) > 0 for r in rows)


def resolve_trade_date(preferred: str | None = None) -> str:
    """Дата закрытого дня, за которую есть реальные торги.

    Начинаем с предпочтительной даты и отступаем назад, пока не найдём
    день с торговлей (переживает праздники и пропущенные обновления).
    """
    d = analytics._parse_date(preferred or last_closed_trade_date())
    if d is None:
        d = date.today() - timedelta(days=1)
    for _ in range(10):
        if _has_trading(d.isoformat()):
            return d.isoformat()
        d = d - timedelta(days=1)
    return d.isoformat()


def build_snapshot(force: bool = False, preferred_date: str | None = None) -> dict:
    """Полная сборка дневного снапшота: рыночные данные + итоги дня.

    Возвращает {trade_date, bonds, curve, meta} и сохраняет в базу.
    """
    t0 = time.time()
    errors: list[str] = []

    trade_date = resolve_trade_date(preferred_date)

    # 1. Рыночный снапшот: статика + текущие доходности/дюрация.
    try:
        snapshot = moex.fetch_market_snapshot()
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f"снапшот рынка недоступен: {err}") from err

    try:
        catalog = moex.fetch_catalog(force=force)
    except Exception as err:  # noqa: BLE001
        catalog = {}
        errors.append(f"каталог: {err}")

    try:
        curve_pack = moex.fetch_curve()
    except Exception as err:  # noqa: BLE001
        curve_pack = {"curve": [], "benchmarks": [], "tradedate": None}
        errors.append(f"кривая: {err}")

    # 1.5 Текущая ключевая ставка ЦБ -- нужна, чтобы у плавающих купонов
    #     показать спред «ЦБ + X%». Не критично: без неё просто не считаем спред.
    keyrate_pack = {"rate": None, "date": None}
    try:
        keyrate_pack = moex.fetch_keyrate(force=force)
    except Exception as err:  # noqa: BLE001
        errors.append(f"ключевая ставка ЦБ: {err}")

    # 1.6 Реестр кредитных рейтингов Банка России (ratings.cbr.ru).
    #     Обновляется не чаще раза в сутки; ошибки не критичны -- остаёмся
    #     на кэше, фильтр и колонка продолжают работать.
    try:
        from . import ratings as _ratings
        _ratings.refresh()
    except Exception as err:  # noqa: BLE001
        errors.append(f"рейтинги ЦБ: {err}")

    # 2. Итоги закрытого дня (оборот, сделки, цены, доходности).
    try:
        totals = fetch_day_totals(trade_date)
    except Exception as err:  # noqa: BLE001
        totals = {}
        errors.append(f"итоги дня {trade_date}: {err}")

    # 2.5 Квал-статус эмитента/бумаги. ISS отдаёт ISQUALIFIEDINVESTORS
    #     только поштучно (не в каталоге и не в биржевом снапшоте), поэтому
    #     держим локальный кэш и докачиваем лишь новые выпуски.
    try:
        secids = [i["sec"].get("SECID") for i in snapshot if i["sec"].get("SECID")]
        qualified.resolve(
            secids,
            progress=lambda d, t: print(f"  квал-статус: {d}/{t}", flush=True),
            errors=errors)
    except Exception as err:  # noqa: BLE001 -- не критично: покажем как «все»
        errors.append(f"квал-статус: {err}")

    # 3. Сборка записей. Итоги дня приоритетнее live-полей для
    #    оборота/сделок/цены/доходности -- это и даёт стабильность утром.
    bonds = []
    for item in snapshot:
        secid = item["sec"].get("SECID")
        day = totals.get(secid)
        if day:
            merged = _merge_day(item, day)
        else:
            merged = item
        b = analytics.build_bond(merged, catalog, curve_pack.get("curve") or [],
                                 keyrate=keyrate_pack.get("rate"))
        if b:
            b["trade_date"] = trade_date
            # источник данных по ликвидности: из итогов дня или из live
            b["day_source"] = "history" if day else "live"
            bonds.append(b)

    meta = {
        "trade_date": trade_date,
        "built_at": time.time(),
        "built_iso": datetime.now(MSK).isoformat(timespec="seconds"),
        "load_seconds": round(time.time() - t0, 1),
        "errors": errors,
        "total_bonds": len(bonds),
        "from_history": sum(1 for b in bonds if b.get("day_source") == "history"),
        "tradedate_curve": curve_pack.get("tradedate"),
        "keyrate": keyrate_pack.get("rate"),
        "keyrate_date": keyrate_pack.get("date"),
    }

    from . import store
    store.save_snapshot(trade_date, bonds, curve_pack.get("curve") or [], meta)
    # фиксируем, что сборка выполнялась сегодня (для планировщика)
    from .store import _db, _lock
    with _lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO kv VALUES ('last_build_date', ?)",
                   (datetime.now(MSK).date().isoformat(),))
        db.commit()
    store.prune()

    return {"trade_date": trade_date, "bonds": bonds,
            "curve": curve_pack.get("curve") or [], "meta": meta}


def _merge_day(item: dict, day: dict) -> dict:
    """Подмешать итоги закрытого дня в рыночную запись.

    Статичные параметры (купон, погашение, номинал) берём из live-снапшота,
    а торговые итоги дня -- из истории: оборот, сделки, WAP и доходность.
    Это гарантирует, что утром ликвидность не обнулится.
    """
    sec = item["sec"]
    md = dict(item.get("md") or {})
    yd = dict(item.get("yd") or {})

    # оборот и сделки -- строго из итогов дня
    md["VALTODAY_RUR"] = day.get("turnover") or 0.0
    md["VALUE"] = day.get("turnover") or 0.0
    md["NUMTRADES"] = day.get("trades") or 0.0

    # средневзвешенная цена дня -- приоритетнее live LAST
    if day.get("wap"):
        md["WAPRICE"] = day["wap"]
        md["LAST"] = day.get("close") or day["wap"]

    # доходность и дюрация дня (если биржа посчитала)
    if day.get("ytm") is not None:
        md["YIELDATWAPRICE"] = day["ytm"]
        yd.setdefault("EFFECTIVEYIELD", None)
    if day.get("duration_days") is not None:
        md["DURATION"] = day["duration_days"]
    if day.get("zspread") is not None:
        md["SPREAD"] = day["zspread"]
    elif day.get("spread") is not None:
        md["SPREAD"] = day["spread"]

    return {"sec": sec, "md": md, "yd": yd}
