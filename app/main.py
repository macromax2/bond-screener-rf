#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скринер облигаций рынка РФ -- FastAPI-сервер.

Запуск:
    cd screener
    .venv/bin/uvicorn app.main:app --port 8000
    # в облаке (порт подставляет хостинг):
    uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}

Эндпоинты:
    GET  /                    -- веб-интерфейс скринера
    GET  /api/screener        -- все бумаги + справочники + мета
    GET  /api/presets         -- готовые стратегии
    GET  /api/bond/{secid}    -- карточка бумаги + история
    GET  /api/curve           -- кривая доходности ОФЗ
    POST /api/refresh         -- принудительное обновление данных
    GET  /api/export          -- выгрузка отфильтрованных строк в CSV
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from datetime import datetime, timedelta, time as dtime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from zoneinfo import ZoneInfo

from . import analytics, daily, moex, store

app = FastAPI(title="Скринер облигаций РФ", version="2.0")

MSK = ZoneInfo("Europe/Moscow")
UPDATE_AT = dtime(19, 5)  # время пересборки снапшота, МСК (после закрытия)

# --------------------------------------------------------------------------
# Состояние в памяти: последний дневной снапшот из базы
# --------------------------------------------------------------------------

_state: dict = {
    "bonds": [],           # список записей скринера
    "curve": [],           # кривая ОФЗ
    "benchmarks": [],
    "meta": {},            # информация о загрузке
    "trade_date": None,    # торговый день снапшота (YYYY-MM-DD)
    "loaded_at": 0.0,      # когда снапшот загружен в память
}
_state_lock = threading.Lock()
_refreshing = False


def _apply_snapshot(snap: dict) -> None:
    """Положить снапшот в память."""
    meta = dict(snap.get("meta") or {})
    meta.setdefault("trade_date", snap.get("trade_date"))
    with _state_lock:
        _state["bonds"] = snap.get("bonds") or []
        _state["curve"] = snap.get("curve") or []
        _state["meta"] = meta
        _state["trade_date"] = snap.get("trade_date")
        _state["loaded_at"] = time.time()


def _rebuild(force: bool = False) -> None:
    """Пересобрать дневной снапшот и сохранить в базу."""
    global _refreshing
    with _state_lock:
        if _refreshing:
            return
        _refreshing = True
    try:
        snap = daily.build_snapshot(force=force)
        _apply_snapshot(snap)
    finally:
        _refreshing = False


def _ensure_loaded() -> bool:
    """Гарантировать, что в памяти есть снапшот. True -- если уже был."""
    with _state_lock:
        if _state["bonds"]:
            return True
    snap = store.latest_snapshot()
    if snap:
        _apply_snapshot(snap)
        return True
    return False


def _catch_up_if_missed() -> None:
    """Догнать пропущенную дневную пересборку после пробуждения сервера.

    На бесплатных хостингах процесс засыпает при простое: если 19:05 МСК
    прошло, пока сервера не было, штатный планировщик ушёл бы спать до
    следующих суток, и данные остались бы «вчерашними». Сверяем дату
    последней сборки с ближайшим закрытым торговым днём и пересобираем,
    если она устарела. Пока идёт сборка, продолжает отдаваться старый
    снапшот -- пустых ответов для посетителей нет.
    """
    try:
        if store.get_kv("last_build_date") == datetime.now(MSK).date().isoformat():
            return  # сегодня уже собирали
        want = daily.resolve_trade_date()
        with _state_lock:
            have = _state.get("trade_date")
        if have == want:
            return  # снапшот уже за ближайший закрытый день
        _rebuild()
    except Exception:  # noqa: BLE001 -- не должен ронять сервер
        pass


# --------------------------------------------------------------------------
# Планировщик: пересборка раз в день после закрытия (19:05 МСК)
# --------------------------------------------------------------------------

def _scheduler() -> None:
    """Фоновый цикл: раз в сутки обновляет снапшот после закрытия биржи.

    Работает 24/7 внутри процесса сервера. Если обновление пропущено
    (ноутбук спал), следующий проход цикла догонит -- снапшот строится
    за ближайший закрытый торговый день.
    """
    while True:
        try:
            now = datetime.now(MSK)
            # ближайшее 19:05 МСК
            target = datetime.combine(now.date(), UPDATE_AT, tzinfo=MSK)
            if now >= target:
                target += timedelta(days=1)
            time.sleep(max(30.0, (target - now).total_seconds()))

            # после пробуждения проверяем, не обновлялись ли уже сегодня
            if store.get_kv("last_build_date") == datetime.now(MSK).date().isoformat():
                continue  # сегодня уже обновлялись
            _rebuild()
        except Exception:  # noqa: BLE001 -- планировщик не должен падать
            time.sleep(300)


# --------------------------------------------------------------------------
# Справочники для фронтенда
# --------------------------------------------------------------------------

def _facets(bonds: list[dict]) -> dict:
    """Уникальные значения для выпадающих фильтров."""
    def uniq(key):
        vals = sorted({b.get(key) for b in bonds if b.get(key) is not None},
                      key=lambda x: str(x))
        return list(vals)

    emitents = sorted({b["emitent"] for b in bonds if b.get("emitent")})
    return {
        "groups": uniq("group"),
        "type_keys": uniq("type_key"),
        "currencies": uniq("currency"),
        "coupon_kinds": [k for k in ("фикс", "фикс?", "плав", "нулевой", "—")
                         if k in set(b.get("coupon_kind") for b in bonds)],
        "statuses": uniq("status"),
        "emitents": emitents,
        "ytm_range": _rng(bonds, "ytm"),
        "years_range": _rng(bonds, "years"),
        "price_range": _rng(bonds, "price"),
        "dur_range": _rng(bonds, "duration"),
        "liq_range": _rng(bonds, "liq"),
        "turnover_range": _rng(bonds, "turnover"),
        "cy_range": _rng(bonds, "current_yield"),
        "spread_ofz_range": _rng(bonds, "spread_ofz"),
    }


def _rng(bonds: list[dict], key: str):
    vals = [b[key] for b in bonds if b.get(key) is not None]
    if not vals:
        return [None, None]
    return [round(min(vals), 2), round(max(vals), 2)]


def _stats(bonds: list[dict]) -> dict:
    clean = [b for b in bonds if not b.get("anomaly")]
    ytm = sorted(b["ytm"] for b in clean if b.get("ytm") is not None)
    if not ytm:
        return {}
    def pct(p):
        i = min(len(ytm) - 1, max(0, int(p * len(ytm))))
        return round(ytm[i], 2)
    traded = [b for b in bonds if b.get("traded_today")]
    turnover = sum(b.get("turnover") or 0 for b in bonds)
    return {
        "count": len(bonds),
        "count_clean": len(clean),
        "anomalies": len(bonds) - len(clean),
        "traded_today": len(traded),
        "ytm_median": pct(0.5),
        "ytm_p25": pct(0.25),
        "ytm_p75": pct(0.75),
        "turnover_total_mln": round(turnover / 1e6, 1),
    }


# --------------------------------------------------------------------------
# Маршруты API
# --------------------------------------------------------------------------

@app.on_event("startup")
def _startup() -> None:
    # 1) мгновенно отдаём то, что уже есть в базе
    if not _ensure_loaded():
        # база пуста (первый запуск / чистый диск в облаке) --
        # собираем в фоне, посетители получают дружелюбный503 до готовности
        threading.Thread(target=_rebuild, daemon=True).start()
    else:
        # сервер мог проспать 19:05 (хостинг засыпает при простое) --
        # догоняющая пересборка, если дата снапшота устарела
        threading.Thread(target=_catch_up_if_missed, daemon=True).start()
    # 2) запускаем ежедневный планировщик (19:05 МСК)
    threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/api/screener")
def api_screener(refresh: int = 0):
    """Полный дневной снапшот скринера (из базы).

    По умолчанию отдаёт последний сохранённый снапшот мгновенно.
    refresh=1 -- принудительно пересобрать за ближайший закрытый день.
    """
    if refresh:
        _rebuild(force=True)
    elif not _ensure_loaded():
        raise HTTPException(503, "Снапшот ещё не собран, повторите через минуту")

    with _state_lock:
        bonds = _state["bonds"]
        trade_date = _state.get("trade_date")
        meta_src = dict(_state.get("meta") or {})
        loaded_at = _state.get("loaded_at") or 0.0

    meta = {
        "trade_date": trade_date,
        "trade_date_human": _human_date(trade_date),
        "built_iso": meta_src.get("built_iso"),
        "load_seconds": meta_src.get("load_seconds"),
        "errors": meta_src.get("errors") or [],
        "total_bonds": meta_src.get("total_bonds"),
        "from_history": meta_src.get("from_history"),
        "update_at": UPDATE_AT.strftime("%H:%M"),
        "loaded_iso": datetime.fromtimestamp(loaded_at).isoformat(timespec="seconds")
        if loaded_at else None,
        "refreshing": _refreshing,
        "dates": store.list_dates()[:10],
    }
    return {
        "meta": meta,
        "stats": _stats(bonds),
        "facets": _facets(bonds),
        "bonds": bonds,
    }


def _human_date(iso: str | None) -> str:
    """Дата торгов для отображения: '21 октября 2026 (среда)'."""
    if not iso:
        return ""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return iso
    months = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    weekdays = ["понедельник", "вторник", "среда", "четверг",
                "пятница", "суббота", "воскресенье"]
    return f"{d.day} {months[d.month - 1]} {d.year} ({weekdays[d.weekday()]})"


@app.get("/api/dates")
def api_dates():
    """Список сохранённых дневных снапшотов."""
    return {"dates": store.list_dates(), "latest": store.get_kv("last_trade_date")}


@app.get("/api/presets")
def api_presets():
    return {"presets": analytics.PRESETS}


@app.get("/api/curve")
def api_curve():
    with _state_lock:
        return {"curve": _state["curve"], "benchmarks": [],
                "tradedate": _state.get("trade_date")}


@app.get("/api/bond/{secid}")
def api_bond(secid: str, days: int = Query(180, ge=7, le=730)):
    """Карточка бумаги: подробные параметры + история торгов."""
    with _state_lock:
        bond = next((b for b in _state["bonds"] if b["secid"] == secid), None)
    if not bond:
        raise HTTPException(404, f"Бумага {secid} не найдена в скринере")

    desc = {}
    try:
        desc = moex.fetch_description(secid)
    except Exception:  # noqa: BLE001
        pass
    history = []
    try:
        history = moex.fetch_history(secid, bond.get("board") or "TQCB", days)
    except Exception:  # noqa: BLE001
        pass

    extra = {
        "qualified": desc.get("ISQUALIFIEDINVESTORS"),
        "coupon_freq": desc.get("COUPONFREQUENCY"),
        "has_default": desc.get("HASDEFAULT"),
        "tech_default": desc.get("HASTECHNICALDEFAULT"),
        "early_repayment": desc.get("EARLYREPAYMENT"),
        "issue_date": desc.get("ISSUEDATE"),
        "typename": desc.get("TYPENAME"),
        "regnumber": desc.get("REGNUMBER") or bond.get("isin"),
        "description_full": desc.get("ISSUENAME"),
        "issuer": desc.get("NAME"),
    }
    return {"bond": bond, "extra": extra, "history": history,
            "history_days": days}


@app.post("/api/refresh")
def api_refresh():
    """Принудительная пересборка снапшота за ближайший закрытый день.

    Запускается в фоне (сборка занимает ~2 минуты: история по доскам).
    Статус -- через /api/health (поле refreshing) или /api/screener.
    """
    if _refreshing:
        return {"ok": True, "refreshing": True}
    threading.Thread(target=_rebuild, kwargs={"force": True}, daemon=True).start()
    return {"ok": True, "refreshing": True,
            "note": "сборка запущена в фоне, обновите страницу через 2-3 минуты"}


@app.get("/api/export")
def api_export(format: str = "csv"):
    """Выгрузка текущего снапшота (все бумаги) в CSV.

    Фильтрация выполняется на клиенте; экспорт отфильтрованных строк
    фронтенд собирает сам из /api/screener.
    """
    with _state_lock:
        bonds = _state["bonds"]
    if not bonds:
        raise HTTPException(503, "Данные ещё не загружены, повторите позже")
    if format != "csv":
        raise HTTPException(400, "Поддерживается только csv")

    fields = ["secid", "isin", "name", "full", "emitent", "group", "currency",
              "price", "full_price", "ytm", "ytm_taxed", "current_yield",
              "duration", "years", "matdate", "coupon_pct", "coupon_kind",
              "coupon_per_year", "next_coupon", "liq", "turnover", "trades",
              "spread_ofz", "issue_size", "flags", "status"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for b in bonds:
        row = dict(b)
        row["flags"] = "; ".join(b.get("flags") or [])
        w.writerow(row)
    data = buf.getvalue().encode("utf-8-sig")  # BOM для Excel
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="bonds_screener_{stamp}.csv"'},
    )


# --------------------------------------------------------------------------
# Веб-интерфейс
# --------------------------------------------------------------------------

STATIC_DIR = moex.ROOT / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# Health-check для отладки
@app.get("/api/health")
def health():
    with _state_lock:
        bonds = len(_state["bonds"])
        trade_date = _state.get("trade_date")
        loaded = _state.get("loaded_at") or 0.0
    return {"ok": True, "bonds": bonds, "trade_date": trade_date,
            "age_sec": round(time.time() - loaded, 1) if loaded else None,
            "db": str(moex.DATA_DIR / "screener.db")}
