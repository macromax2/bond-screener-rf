#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кредитные рейтинги эмитентов облигаций.

Зачем: поле rating/rating_band в карточке бумаги и фильтр «Только госбумаги»
в скринере. Рейтинг — один из главных
сигналов надёжности бумаги для консервативного инвестора.

ИСТОЧНИК ДАННЫХ (важно про честность):
    Бесплатный MOEX ISS кредитные рейтинги НЕ отдаёт. Единственный
    авторитетный бесплатный источник -- рейтинговый репозитарий ЦБ
    (ratings.cbr.ru), но он:
      * отдаёт данные через Bitrix-component-AJAX с проверкой КАПЧИ
        на каждом поисковом запросе (res.data.captchaResult в своём JS),
      * не имеет публичного REST/JSON API и bulk-выгрузки.
    Поэтому автоматический сбор рейтингов со всех ~3200 бумаг не
    автоматизируется надёжно (потребовалось бы решение капчи на запрос).

    Модуль устроен так, чтобы это ограничение НЕ ломало фильтр:
      1) реестр рейтингов хранится в data/ratings.json (кэш на диске);
      2) есть чистая точка интеграции fetch_external() -- туда подставляется
         любой будущий источник (API агентства, выгрузка ЦБ, cbonds и т.п.);
      3) пока внешнего источника нет, рейтинги выводятся из ТИПА ЭМИТЕНТА
         (эмитент-инфERENCE) -- это консервативная оценка, честно помеченная
         источником "inferred", а не выдуманные цифры.

Вывод: rating (строка национальной шкалы, напр. "ruAA"), rating_band
("high" | "mid" | "low" | None), rating_source ("external" | "inferred" | None).
"""
from __future__ import annotations

import json
import pathlib
import time

from . import moex

RATINGS_CACHE = moex.DATA_DIR / "ratings.json"

# Национальная шкала РФ: сортируемость по надёжности (чем выше -- тем лучше).
# Строки сравниваются лексикографически в порядке убывания надёжности через
# _rank(). Ниже -- отображение в балл для фильтра «Только госбумаги».
_HIGH_PREFIXES = ("ruAAA", "ruAA", "ruA")  # ruAAA, ruAA+/-, ruA+/-
_MID_PREFIXES = ("ruBBB", "ruBB", "ruB+", "ruB")  # инвестиционный/спекулятивный низ


def _rank(rating: str | None) -> int:
    """Грубый балл надёжности по национальной шкале (выше = лучше)."""
    if not rating:
        return -1
    r = rating.upper().replace("(RU)", "")
    order = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-",
             "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
             "B+", "B", "B-", "CCC", "CC", "C", "D"]
    for i, base in enumerate(order):
        if r == base or r == "RU" + base or r.endswith(base):
            return len(order) - i
    return -1


def band(rating: str | None) -> str | None:
    """Полоса для фильтра: high (A и выше), mid (BBB..B), low (ниже), None.

    Основана на _rank(), чтобы логика была единой по всей шкале.
    """
    r = _rank(rating)
    if r < 0:
        return None
    # шкала в _rank(): 20=AAA .. 1=D; high -- это A- и выше (rank >= 14)
    if r >= 14:
        return "high"
    if r >= 5:
        return "mid"
    return "low"


def _infer_from_type(type_key: str | None, group: str | None) -> tuple[str | None, str]:
    """Консервативный вывод рейтинга из типа эмитента (не выдумка, а оценка).

    Госсектор de-facto находится на вершине национальной шкалы:
      * ОФЗ            -- суверенный, гарантия государства  -> ruAAA
      * субфедеральные -- субъект РФ, как правило ruAA..ruAAA
      * муниципальные  -- муниципалитет, как правило ruA..ruAA
    Корпоративные/биржевые выводить нельзя (слишком широкий разброс) -> None.
    """
    tk = type_key or ""
    if tk == "ofz_bond":
        return "ruAAA", "inferred"
    if tk == "subfederal_bond":
        return "ruAA", "inferred"
    if tk == "municipal_bond":
        return "ruA", "inferred"
    return None, "inferred"


def fetch_external() -> dict[str, dict]:
    """Точка интеграции с внешним источником рейтингов.

    Сейчас -- заглушка: возвращает пустой реестр. Сюда подставляется будущий
    источник (API агентства / выгрузка ЦБ / cbonds), когда он будет доступен
    и разрешён. Ключ -- ISIN (основной ключ сопоставления с бумагами MOEX).
    """
    return {}


def load() -> dict:
    """Загрузить кэш реестра: {ts, items:{isin:{rating, agency, ...}}}."""
    try:
        if RATINGS_CACHE.exists():
            payload = json.loads(RATINGS_CACHE.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("items"), dict):
                return payload
    except Exception:  # noqa: BLE001 -- битый кэш -> пустой реестр
        pass
    return {"ts": 0, "items": {}}


def save(items: dict[str, dict]) -> None:
    moex.DATA_DIR.mkdir(parents=True, exist_ok=True)
    RATINGS_CACHE.write_text(
        json.dumps({"ts": time.time(), "items": items}, ensure_ascii=False),
        encoding="utf-8")


def refresh(force: bool = False) -> dict[str, dict]:
    """Обновить реестр из внешнего источника (если он подключён)."""
    items = fetch_external()
    if items:
        save(items)
        return items
    return load().get("items", {})


def resolve(bond: dict, external: dict | None = None) -> dict:
    """Определить рейтинг для одной бумаги скринера.

    Приоритет: внешний реестр по ISIN -> вывод по типу эмитента -> None.
    Возвращает {rating, rating_band, rating_source}.
    """
    isin = bond.get("isin")
    if external is None:
        external = load().get("items", {})
    if isin and isin in external:
        rec = external[isin]
        rating = rec.get("rating")
        return {
            "rating": rating,
            "rating_band": band(rating),
            "rating_source": "external",
        }
    rating, src = _infer_from_type(bond.get("type_key"), bond.get("group"))
    return {
        "rating": rating,
        "rating_band": band(rating),
        "rating_source": src if rating else None,
    }
