#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Загрузка данных облигационного рынка с MOEX ISS API.

Источники (все бесплатные, без ключей):
  1. markets/bonds/securities  -- все выпуски разом: котировки, доходности,
     дюрация, Z-spread, купоны, оферты (~4 МБ, ~2.5 с)
  2. securities (каталог)       -- тип бумаги (ОФЗ/корпоративная/...),
     эмитент, ИНН, ISIN, номер регистрации
  3. zcyc/securities           -- кривая доходности ОФЗ + эталонные выпуски
  4. securities/{secid}        -- карточка: квал-статус, купонная частота,
     дефолты (лениво, по клику)
  5. history/.../{secid}       -- история торгов для графика (лениво)
"""
from __future__ import annotations

import json
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = "https://iss.moex.com/iss"
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CATALOG_CACHE = DATA_DIR / "catalog.json"
UA = {"User-Agent": "Mozilla/5.0 (bond-screener-rf)"}

# Доски, участвующие в скринере, в порядке приоритета при дублях SECID.
# SPOB (поставка по ОФЗ) дублирует TQOB и нам не нужен.
BOARDS = ["TQCB", "TQOB", "TQOD", "TQOE", "TQOY", "TQRD", "TQIR", "TQNB", "TQNO"]

_lock = threading.Lock()


def _get(url: str, params: dict | None = None, retries: int = 4, timeout: int = 60):
    """GET-запрос к ISS с повторами. Возвращает разобранный JSON."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as err:  # noqa: BLE001 -- любая сетевая ошибка -> повтор
            last_err = err
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"GET {url} не удался после {retries} попыток: {last_err}")


def _table(block) -> list[dict]:
    """ISS-блок {columns, data} -> список словарей."""
    if not block or "data" not in block:
        return []
    cols = block["columns"]
    return [dict(zip(cols, row)) for row in block["data"]]


# --------------------------------------------------------------------------
# 1. Основной снапшот рынка
# --------------------------------------------------------------------------

def fetch_market_snapshot() -> list[dict]:
    """Все выпуски облигационного рынка одним запросом.

    Возвращает объединённые записи securities + marketdata + marketdata_yields,
    по одной на SECID (приоритет основных торговых досок, SPOB пропускаем).
    """
    raw = _get(f"{BASE}/engines/stock/markets/bonds/securities.json",
               {"iss.meta": "off"})
    secs = _table(raw.get("securities"))
    mds = {r["SECID"]: r for r in _table(raw.get("marketdata"))}
    yds = {}
    for r in _table(raw.get("marketdata_yields")):
        # на отдельных досках возможны дубли -- оставляем первый встретившийся
        yds.setdefault(r["SECID"], r)

    board_rank = {b: i for i, b in enumerate(BOARDS)}
    best: dict[str, dict] = {}
    for s in secs:
        secid = s["SECID"]
        board = s.get("BOARDID") or ""
        cur = best.get(secid)
        if cur is None:
            best[secid] = s
            continue
        # оставляем запись с более приоритетной доски; SPOB всегда проигрывает
        r_new = board_rank.get(board, len(BOARDS) + (1 if board == "SPOB" else 0))
        r_old = board_rank.get(cur.get("BOARDID") or "",
                               len(BOARDS) + (1 if (cur.get("BOARDID") or "") == "SPOB" else 0))
        if r_new < r_old:
            best[secid] = s

    out = []
    for secid, s in best.items():
        out.append({
            "sec": s,
            "md": mds.get(secid) or {},
            "yd": yds.get(secid) or {},
        })
    return out


# --------------------------------------------------------------------------
# 2. Каталог: тип бумаги и эмитент (кэш на диске, обновляется раз в сутки)
# --------------------------------------------------------------------------

def _catalog_page(start: int) -> list[dict]:
    raw = _get(f"{BASE}/securities.json", {
        "iss.meta": "off", "engine": "stock", "market": "bonds",
        "is_trading": "1", "limit": 100, "start": start,
    })
    return _table(raw.get("securities"))


def _catalog_all_pages() -> list[dict]:
    """Выгрузка всех страниц каталога параллельно (торгуемых ~3200)."""
    from concurrent.futures import ThreadPoolExecutor

    starts = list(range(0, 5000, 100))  # с запасом до 5000 бумаг
    pages: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_catalog_page, s): s for s in starts}
        for fut, s in futures.items():
            try:
                pages[s] = fut.result()
            except Exception:  # noqa: BLE001 -- сбой одной страницы не критичен
                pages[s] = []
    # пустая страница и все после неё -- конец списка
    rows: list[dict] = []
    for s in sorted(pages):
        page = pages[s]
        if not page:
            break
        rows.extend(page)
    return rows


def fetch_catalog(force: bool = False) -> dict[str, dict]:
    """Каталог торгуемых облигаций: secid -> {type, emitent, inn, isin, ...}.

    Данные справочные, кэшируются на диск на сутки.
    """
    with _lock:
        if not force and CATALOG_CACHE.exists():
            try:
                payload = json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))
                if time.time() - payload.get("ts", 0) < 24 * 3600:
                    return payload["items"]
            except Exception:  # noqa: BLE001 -- битый кэш -> перезагрузка
                pass

    rows = _catalog_all_pages()

    items = {}
    for r in rows:
        items[r["secid"]] = {
            "secid": r.get("secid"),
            "shortname": r.get("shortname"),
            "regnumber": r.get("regnumber"),
            "name": r.get("name"),
            "isin": r.get("isin"),
            "is_traded": r.get("is_traded"),
            "emitent_id": r.get("emitent_id"),
            "emitent": r.get("emitent_title"),
            "inn": r.get("emitent_inn"),
            "type": r.get("type"),
            "group": r.get("group"),
            "primary_board": r.get("primary_boardid"),
        }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_CACHE.write_text(
        json.dumps({"ts": time.time(), "items": items}, ensure_ascii=False),
        encoding="utf-8")
    return items


# --------------------------------------------------------------------------
# 3. Кривая доходности ОФЗ
# --------------------------------------------------------------------------

def fetch_curve() -> dict:
    """Кривая ОФЗ (yearyields) + эталонные выпуски для сравнения."""
    raw = _get(f"{BASE}/engines/stock/zcyc/securities.json", {"iss.meta": "off"})
    curve = [{"period": r["period"], "value": r["value"]}
             for r in _table(raw.get("yearyields"))]
    bench = []
    for r in _table(raw.get("securities")):
        bench.append({
            "secid": r.get("secid"), "shortname": r.get("shortname"),
            "matdate": r.get("expdate"), "price": r.get("trdprice"),
            "yield": r.get("trdyield"), "duration_days": r.get("crtduration"),
        })
    tradedate = None
    rows = _table(raw.get("params"))
    if rows:
        tradedate = rows[0].get("tradedate")
    return {"curve": curve, "benchmarks": bench, "tradedate": tradedate}


# --------------------------------------------------------------------------
# 4. Ключевая ставка ЦБ (для флоатеров)
# --------------------------------------------------------------------------

KEYRATE_CACHE = DATA_DIR / "keyrate.json"


def fetch_keyrate(force: bool = False) -> dict:
    """Текущая ключевая ставка ЦБ и дата её последнего изменения.

    Берём с cbr.ru (HTML-таблица ключевых ставок, отдаётся без капчи).
    Кэш на сутки: ставка меняется примерно раз в 1-2 месяца, так что
    экономим один внешний запрос на каждый пересчёт.
    """
    with _lock:
        if not force and KEYRATE_CACHE.exists():
            try:
                payload = json.loads(KEYRATE_CACHE.read_text(encoding="utf-8"))
                if time.time() - payload.get("ts", 0) < 24 * 3600:
                    return payload
            except Exception:  # noqa: BLE001 -- битый кэш -> перезагрузка
                pass

    import re as _re
    out = {"rate": None, "date": None, "ts": time.time()}
    try:
        url = ("https://www.cbr.ru/hd_base/KeyRate/?UniDbQuery.Posted=True"
               "&UniDbQuery.From=01.01.2024&UniDbQuery.To=31.12.2030")
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
        # строки таблицы: <td>21.10.2026</td><td>14,00</td>
        rows = _re.findall(
            r"<td[^>]*>(\d{2}\.\d{2}\.\d{4})</td>\s*<td[^>]*>([\d,\.]+)</td>",
            html)
        if rows:
            # таблица идёт по убыванию даты -> первая строка и есть текущая ставка
            d, v = rows[0]
            out["date"] = "-".join(reversed(d.split(".")))  # -> 2026-10-21
            out["rate"] = float(v.replace(",", "."))
    except Exception:  # noqa: BLE001 -- нет ставки -> просто не считаем спред
        pass

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEYRATE_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# 5. Карточка бумаги (лениво)
# --------------------------------------------------------------------------

def fetch_description(secid: str) -> dict:
    """Подробное описание выпуска: квал-статус, частота купона, дефолты."""
    raw = _get(f"{BASE}/securities/{urllib.parse.quote(secid)}.json",
               {"iss.meta": "off"})
    info = {}
    for r in _table(raw.get("description")):
        info[r.get("name")] = r.get("value")
    return info


def fetch_history(secid: str, board: str, days: int = 180) -> list[dict]:
    """История торгов бумаги для графика цены и доходности."""
    till = date.today()
    frm = till - timedelta(days=days)
    raw = _get(f"{BASE}/history/engines/stock/markets/bonds/boards/"
               f"{urllib.parse.quote(board)}/securities/"
               f"{urllib.parse.quote(secid)}.json",
               {"iss.meta": "off", "from": frm.isoformat(),
                "till": till.isoformat(), "limit": 300})
    out = []
    for r in _table(raw.get("history")):
        if not r.get("TRADEDATE"):
            continue
        out.append({
            "date": r.get("TRADEDATE"),
            "price": r.get("WAPRICE") or r.get("CLOSE") or r.get("LEGALCLOSEPRICE"),
            "yield": r.get("YIELDATWAP") or r.get("YIELDCLOSE"),
            "value": r.get("VALUE"),
            "trades": r.get("NUMTRADES"),
        })
    return out
