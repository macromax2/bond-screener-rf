#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Аналитические расчёты поверх сырых данных MOEX.

Что считаем:
  - остаточный срок и дюрацию в годах
  - текущую доходность (купон / цена)
  - доходность к погашению (YTM из ISS; запасной вариант -- расчёт)
  - спред к кривой ОФЗ для рублёвых бумаг
  - доходность после НДФЛ (13%) и с учётом льготы владения 3+ года
  - фактор сложности (амортизация, оферта, плавающий купон и т.д.)
  - коэффициент ликвидности на основе оборота и числа сделок
  - принадлежность к готовым стратегиям (пресеты)
"""
from __future__ import annotations

import math
from datetime import date, datetime

TODAY = date.today()

# Справочник типов ISS -> человекочитаемая группа
TYPE_GROUPS = {
    "ofz_bond": "Государственные (ОФЗ)",
    "subfederal_bond": "Субфедеральные",
    "municipal_bond": "Муниципальные",
    "corporate_bond": "Корпоративные",
    "exchange_bond": "Биржевые (корпоративные)",
}

# Описание фактора сложности -- по мотивам ДОХОДЪ (0..7)
COMPLEXITY_LABELS = {
    0: "Простая (все купоны известны)",
    1: "Оферта владельцев (PUT)",
    2: "Амортизация номинала",
    3: "Плавающий купон",
    4: "Индексируемый номинал",
    5: "Выкуп эмитентом (CALL) / ипотечная",
    6: "Вечная / конвертируемая",
    7: "Высокая сложность / только для квалов",
}


def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_date(s):
    if not s or s in ("0000-00-00",):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def years_between(d: date) -> float | None:
    if d is None:
        return None
    return round((d - TODAY).days / 365.25, 2)


def curve_yield(curve: list[dict], years: float) -> float | None:
    """Интерполяция кривой ОФЗ по сроку (лет)."""
    if not curve or years is None:
        return None
    pts = sorted(((c["period"], c["value"]) for c in curve
                  if c.get("period") is not None and c.get("value") is not None))
    if not pts:
        return None
    if years <= pts[0][0]:
        return round(pts[0][1], 2)
    if years >= pts[-1][0]:
        return round(pts[-1][1], 2)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= years <= x1:
            if x1 == x0:
                return round(y0, 2)
            t = (years - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 2)
    return None


def yield_to_maturity(price_pct, accrued, coupon_value, coupon_pct,
                      face_value, matdate, next_coupon, coupon_period,
                      settle_days: int = 2) -> float | None:
    """Приближённый YTM (%) для бумаг без данных ISS.

    Используется как запасной вариант, когда биржа не посчитала
    эффективную доходность (нет сделок / нет расчёта).
    """
    price = _num(price_pct)
    mat = _parse_date(matdate)
    if price is None or price <= 0 or mat is None:
        return None
    years = (mat - TODAY).days / 365.25
    if years <= 0:
        return None
    fv = _num(face_value) or 1000.0
    # чистая цена в деньгах на номинал 100
    clean = price
    # купоны: годовой купон в % к номиналу
    cp = _num(coupon_pct)
    if cp is None and coupon_value and _num(coupon_period):
        # годовой купон из периодичности
        cp = (_num(coupon_value) or 0) / fv * (365.0 / _num(coupon_period))
    if cp is None:
        cp = 0.0
    # грубая формула: (номинал + сумма купонов - цена) / годы / цена
    coupons_total = cp / 100.0 * fv * max(years, 0.01) / 100.0  # в % от номинала
    acc = _num(accrued) or 0.0
    dirty = clean + acc / fv * 100.0
    gain = (100.0 - clean) + coupons_total
    ytm = (gain / years) / max(dirty, 0.01) * 100.0
    return round(ytm, 2)


def liquidity_score(turnover_rub, trades, spread_pct) -> int:
    """Коэффициент ликвидности 0..100 (упрощённая аналогия ДОХОДЪ).

    Основа -- логарифм оборота за день, штраф за широкий спред,
    небольшой бонус за число сделок.
    """
    turn = _num(turnover_rub) or 0
    tr = _num(trades) or 0
    if turn <= 0 and tr <= 0:
        return 0
    # 100 тыс руб/день ~ 20 баллов, 10 млн ~ 50, 100 млн ~ 75, 1 млрд ~ 95
    score = 0.0
    if turn > 0:
        score = 20 + 18 * math.log10(turn / 1e5 + 1)
    score += min(12.0, 3.0 * math.log10(tr + 1))
    spread = _num(spread_pct)
    if spread is not None:
        if spread > 0.5:
            score -= min(20.0, (spread - 0.5) * 12)
        elif spread <= 0.05:
            score += 4
    return int(max(0, min(100, round(score))))


def complexity_flags(sec: dict) -> tuple[int, list[str]]:
    """Фактор сложности 0..7 и список человекочитаемых особенностей."""
    flags = []
    bond_type = sec.get("BONDTYPE") or ""
    subtype = sec.get("BONDSUBTYPE") or ""
    coupon_details = sec.get("COUPON_DETAILS") or ""
    fv_type = sec.get("FACEVALUE_TYPE") or ""

    has_put = bool(sec.get("PUTOPTIONDATE") and
                   sec.get("PUTOPTIONDATE") != "0000-00-00")
    has_call = bool(sec.get("CALLOPTIONDATE") and
                    sec.get("CALLOPTIONDATE") != "0000-00-00")
    amortized = ("амортизир" in bond_type.lower()
                 or (fv_type and fv_type != "Фиксированный"
                     and "Индексир" not in fv_type))
    floating = ("плавающим" in bond_type.lower()
                or coupon_details in ("Ключевая ставка", "Ставка RUONIA",
                                      "Индекс RUONIA", "ИПЦ",
                                      "Срочная ставка RUONIA 6m"))
    indexed = ("линкер" in bond_type.lower()
               or "индексируемым" in bond_type.lower()
               or fv_type == "Индексированный")
    perpetual = "бессрочн" in subtype.lower()
    convertible = "конвертируем" in bond_type.lower()
    structural = "структурн" in bond_type.lower()
    discount = "дисконтн" in bond_type.lower()

    score = 0
    if has_put:
        score = max(score, 1)
        flags.append("оферта владельцев (PUT)")
    if amortized:
        score = max(score, 2)
        flags.append("амортизация")
    if floating:
        score = max(score, 3)
        flags.append("плавающий купон")
    if indexed:
        score = max(score, 4)
        flags.append("индексируемый номинал")
    if has_call:
        score = max(score, 5)
        flags.append("выкуп эмитентом (CALL)")
    if perpetual or convertible:
        score = max(score, 6)
        flags.append("вечная" if perpetual else "конвертируемая")
    if structural:
        score = max(score, 7)
        flags.append("структурная")
    if discount:
        flags.append("дисконтная")
    if subtype and "оферты" in subtype:
        pass  # уже учтено
    return score, flags


def coupon_kind(sec: dict) -> str:
    """Тип купона: фикс / плав / нулевой / неизвестный."""
    details = sec.get("COUPON_DETAILS") or ""
    bond_type = sec.get("BONDTYPE") or ""
    cp = _num(sec.get("COUPONPERCENT"))
    if "дисконтн" in bond_type.lower() or (cp is not None and cp == 0):
        return "нулевой"
    if details.startswith("Фиксированный с известными"):
        return "фикс"
    if details.startswith("Фиксированный с неизвестными"):
        return "фикс?"
    if details in ("Ключевая ставка", "Ставка RUONIA", "Индекс RUONIA",
                   "ИПЦ", "Срочная ставка RUONIA 6m"):
        return "плав"
    if "плавающим" in bond_type.lower():
        return "плав"
    if "фиксированным" in bond_type.lower():
        return "фикс"
    return "—"


def coupon_per_year(sec: dict) -> float | None:
    period = _num(sec.get("COUPONPERIOD"))
    if not period or period <= 0:
        return None
    return round(365.25 / period, 1)


_external_cache: dict | None = None


def _external_ratings() -> dict:
    """Кэшированный на время процесса внешний реестр рейтингов (ISIN -> запись)."""
    global _external_cache
    if _external_cache is None:
        try:
            from . import ratings as _ratings
            _external_cache = _ratings.load().get("items", {}) or {}
        except Exception:  # noqa: BLE001 -- нет реестра -> пусто
            _external_cache = {}
    return _external_cache


def coupon_base(sec: dict) -> str | None:
    """База плавающего купона: ключевая ставка / RUONIA / ИПЦ.

    MOEX отдаёт её в COUPON_DETAILS («Ключевая ставка», «Ставка RUONIA»,
    «ИПЦ», «Срочная ставка RUONIA 6m»). Для фиксированных бумаг возвращает
    None -- привязка у них ни к чему.
    """
    details = (sec.get("COUPON_DETAILS") or "").strip()
    if not details:
        return None
    low = details.lower()
    if "ключев" in low:
        return "ЦБ"
    if "ruonia" in low:
        return "RUONIA"
    if "ипц" in low or "инфляц" in low:
        return "ИПЦ"
    return details  # прочие экзотические базы показываем как есть


def coupon_spread(sec: dict, keyrate: float | None) -> float | None:
    """Спред плавающего купона над его базой, п.п.

    Считаем только для бумаг на ключевой ставке ЦБ: MOEX не отдаёт формулу
    привязки напрямую, но у флоатеров на КС текущий купон -- это «ставка ЦБ
    + фиксированный спред», поэтому разница текущего купона и ставки ЦБ
    и есть этот спред. Для RUONIA/ИПЦ ближайшую ставку из открытых источников
    взять не вышло, поэтому спред не считаем -- показываем только базу.
    """
    if keyrate is None or coupon_base(sec) != "ЦБ":
        return None
    cp = _num(sec.get("COUPONPERCENT"))
    if cp is None:
        return None
    return round(cp - keyrate, 1)


def offer_dates(sec: dict) -> dict:
    """Все даты «сюрпризов»: оферты владельцев и выкупа эмитентом.

    Оферта (PUT) -- инвестор может вернуть бумагу по номиналу досрочно;
    выкуп (CALL) -- эмитент досрочно гасит выпуск. Обе ловушки: бумага
    «закрывается» раньше срока, и рассчитанная до погашения доходность
    не сбывается. Считаем ближайшую из них.
    """
    def parse(name):
        v = sec.get(name)
        if not v or v == "0000-00-00":
            return None
        d = _parse_date(v)
        return d.isoformat() if d else None

    dates = {k: parse(k) for k in ("OFFERDATE", "PUTOPTIONDATE",
                                  "CALLOPTIONDATE", "BUYBACKDATE")}
    future = [d for d in dates.values()
              if d and _parse_date(d) and _parse_date(d) > TODAY]
    dates["nearest"] = min(future) if future else None
    return dates


def build_bond(item: dict, catalog: dict, curve: list[dict],
               tax_rate: float = 0.13, keyrate: float | None = None) -> dict | None:
    """Сборка одной записи скринера из сырых ISS-данных."""
    sec, md, yd = item["sec"], item["md"], item["yd"]
    secid = sec.get("SECID")
    if not secid:
        return None

    cat = catalog.get(secid) or {}
    board = sec.get("BOARDID")

    matdate = _parse_date(sec.get("MATDATE"))
    years = years_between(matdate)

    # цена: последняя WAP, иначе последняя, иначе предыдущая закрытия
    price = (_num(yd.get("WAPRICE")) or _num(md.get("WAPRICE"))
             or _num(md.get("LAST")) or _num(sec.get("PREVWAPRICE"))
             or _num(md.get("MARKETPRICE")) or _num(sec.get("PREVLEGALCLOSEPRICE")))
    accrued = _num(sec.get("ACCRUEDINT")) or 0.0
    face = _num(sec.get("FACEVALUE")) or 1000.0
    full_price = round(price + accrued / face * 100.0, 4) if price else None

    # доходность: эффективная из ISS -> рыночная -> собственный расчёт
    ytm = (_num(yd.get("EFFECTIVEYIELD")) or _num(yd.get("EFFECTIVEYIELDWAPRICE"))
           or _num(md.get("YIELDATWAPRICE")) or _num(md.get("YIELD"))
           or _num(sec.get("YIELDATPREVWAPRICE")))
    if ytm is None:
        ytm = yield_to_maturity(price, accrued, sec.get("COUPONVALUE"),
                                sec.get("COUPONPERCENT"), face,
                                sec.get("MATDATE"), sec.get("NEXTCOUPON"),
                                sec.get("COUPONPERIOD"))
    ytm_to_offer = (_num(yd.get("YIELDTOOFFER")) or _num(md.get("YIELDTOOFFER")))

    # дюрация: ISS отдаёт в днях
    dur_days = _num(yd.get("DURATION")) or _num(md.get("DURATION"))
    duration = round(dur_days / 365.25, 2) if dur_days else None

    coupon_pct = _num(sec.get("COUPONPERCENT"))
    current_yield = round(coupon_pct / price * 100.0, 2) \
        if coupon_pct is not None and price else None

    # ликвидность
    turnover = (_num(md.get("VALTODAY_RUR")) or _num(md.get("VALUE"))
                or _num(md.get("VALTODAY")) or 0)
    trades = _num(md.get("NUMTRADES")) or 0
    spread = _num(md.get("SPREAD"))
    liq = liquidity_score(turnover, trades, spread)

    # спред к ОФЗ
    ofz = curve_yield(curve, years) if sec.get("FACEUNIT") == "SUR" else None
    spread_ofz = round(ytm - ofz, 2) if (ytm is not None and ofz is not None) else None

    # налоги: 13% -- базовая ставка, 15% -- для дохода свыше 2,4 млн ₽ в год
    taxed = round(ytm * (1 - tax_rate), 2) if ytm is not None else None
    taxed15 = round(ytm * (1 - 0.15), 2) if ytm is not None else None
    # льгота владения 3+ года: бумаги со сроком погашения от 3 лет
    tax_free_ok = years is not None and years >= 3.0

    score, flags = complexity_flags(sec)
    ctype = coupon_kind(sec)
    # амортизация номинала (номинал гасится частями, а не возвращается
    # одним платежом при погашении) -- для фильтра «без амортизации»
    amortized = "амортизация" in flags

    # Выбросы биржевого расчёта. Порог подобран по распределению YTM:
    #   >1000% -- 68 бумаг, 600..1000 -- 5, 300..600 -- 15, 100..300 -- 38.
    # Это бумаги с ценой 2..60 копеек на 100 номинала, как правило в дефолте
    # или у погашения: доходность формально гигантская, но обычному инвестору
    # такая бумага не нужна. Раньше порог был 600%, и 55 «мусорных» выпусков
    # забивали верх таблицы при сортировке по доходности (593%, 578%, 507%...).
    # Ниже 100% остаются реальные ВДО (50..100% -- 63 бумаги) -- их прячем
    # только по явному запросу.
    anomaly = ytm is not None and (ytm > 100.0 or ytm < -50.0)

    # доступность инвесторам: MOEX явно не отдаёт в снапшоте,
    # но структурные и некотируемые часто кваловские -- помечаем по листингу
    listlevel = _num(sec.get("LISTLEVEL"))
    status = md.get("TRADINGSTATUS") or sec.get("STATUS")

    group_key = cat.get("type") or ("ofz_bond" if board == "TQOB" else "corporate_bond")
    group = TYPE_GROUPS.get(group_key, "Прочие")

    # валюта номинала -- по ней фильтруем (SUR/USD/EUR/CNY/CHF)
    ccy = sec.get("FACEUNIT") or sec.get("CURRENCYID") or "SUR"

    # кредитный рейтинг эмитента: внешний реестр по ISIN -> вывод по типу
    from . import ratings as _ratings
    rinfo = _ratings.resolve(
        {"isin": sec.get("ISIN") or cat.get("isin"),
         "type_key": group_key, "group": group},
        _external_ratings())

    # доступность бумаги: ISQUALIFIEDINVESTORS из локального кэша (см.
    # app/qualified.py). "1" = только квал-инвесторы, "0" = доступно всем,
    # None = неизвестно (трактуем как «все», чтобы не прятать хорошие бумаги).
    from . import qualified as _qualified
    q_raw = _qualified.get(secid)
    qualified_only = q_raw == "1"

    return {
        "secid": secid,
        "isin": sec.get("ISIN") or cat.get("isin"),
        "name": sec.get("SHORTNAME"),
        "full": sec.get("SECNAME") or cat.get("name"),
        "emitent": cat.get("emitent"),
        "inn": cat.get("inn"),
        "board": board,
        "type_key": group_key,
        "group": group,
        "currency": ccy,
        "price": price,
        "accrued": round(accrued, 2),
        "full_price": full_price,
        "face": face,
        "ytm": ytm,
        "ytm_taxed": taxed,
        "ytm_taxed15": taxed15,
        "ytm_to_offer": ytm_to_offer,
        # лотность: облигации на MOEX торгуются лотами (обычно 1 шт.),
        # LOTVALUE -- номинальная стоимость лота. Нужно для расчёта «сдачи»
        # в конструкторе портфеля: сколько лотов влезает в бюджет.
        "lotsize": _num(sec.get("LOTSIZE")) or 1,
        "lot_value": _num(sec.get("LOTVALUE")) or face,
        "current_yield": current_yield,
        "duration": duration,
        "years": years,
        "matdate": sec.get("MATDATE") if matdate else None,
        "offerdate": sec.get("OFFERDATE") if sec.get("OFFERDATE") not in (None, "0000-00-00") else None,
        "offer": offer_dates(sec),
        "coupon_pct": coupon_pct,
        "coupon_value": _num(sec.get("COUPONVALUE")),
        "coupon_base": coupon_base(sec) if ctype == "плав" else None,
        "coupon_spread": coupon_spread(sec, keyrate) if ctype == "плав" else None,
        "next_coupon": sec.get("NEXTCOUPON") if sec.get("NEXTCOUPON") not in (None, "0000-00-00") else None,
        "coupon_per_year": coupon_per_year(sec),
        "coupon_kind": ctype,
        "amortized": amortized,
        "qualified": q_raw,
        "qualified_only": qualified_only,
        "rating": rinfo.get("rating"),
        "rating_band": rinfo.get("rating_band"),
        "rating_source": rinfo.get("rating_source"),
        "complexity": score,
        "flags": flags,
        "turnover": round(turnover, 0),
        "trades": trades,
        "spread": spread,
        "liq": liq,
        "ofz_curve": ofz,
        "spread_ofz": spread_ofz,
        "issue_size": _num(sec.get("ISSUESIZE")),
        "issue_value": round((_num(sec.get("ISSUESIZE")) or 0) * face / 1e6, 1),
        "listlevel": listlevel,
        "status": status,
        "traded_today": trades > 0,
        "tax_free_ok": tax_free_ok,
        "anomaly": anomaly,
    }


# --------------------------------------------------------------------------
# Готовые стратегии (пресеты) -- по мотивам ДОХОДЪ и MOEX
# --------------------------------------------------------------------------

PRESETS: list[dict] = [
    {
        "id": "deposit_alt",
        "title": "Альтернатива депозиту",
        "desc": "Простые ликвидные рублёвые бумаги на 0.5–3 года с доходностью выше кривой ОФЗ",
        "filters": {
            "currency": ["SUR"], "years_min": 0.5, "years_max": 3.0,
            "complexity_max": 0, "liq_min": 30, "spread_ofz_min": 0.0,
        },
    },
    {
        "id": "balanced",
        "title": "Сбалансированный",
        "desc": "Качественные бумаги на 1–5 лет: доходность выше средней, ликвидность в порядке",
        "filters": {
            "currency": ["SUR"], "years_min": 1.0, "years_max": 5.0,
            "liq_min": 40, "ytm_min": 12.0, "complexity_max": 3,
        },
    },
    {
        "id": "high_yield",
        "title": "High Yield (ВДО)",
        "desc": "Высокодоходные бумаги со сроком до 5 лет -- повышенный кредитный риск",
        "filters": {
            "currency": ["SUR"], "years_max": 5.0, "ytm_min": 20.0,
            "liq_min": 15,
        },
    },
    {
        "id": "short",
        "title": "Короткие (до года)",
        "desc": "Минимум процентного риска: бумаги со сроком погашения менее 12 месяцев",
        "filters": {"years_max": 1.0, "liq_min": 10},
    },
    {
        "id": "long_fixed",
        "title": "Долгие фикс (лонг-стак)",
        "desc": "Фиксированный купон на 5+ лет -- ставка на снижение ключевой ставки",
        "filters": {
            "currency": ["SUR"], "years_min": 5.0, "coupon_kinds": ["фикс"],
            "complexity_max": 0, "liq_min": 20,
        },
    },
    {
        "id": "ofz",
        "title": "Только ОФЗ",
        "desc": "Государственные бумаги: без кредитного риска, базовый актив рынка",
        "filters": {"type_key": ["ofz_bond"]},
    },
    {
        "id": "tax_free",
        "title": "Налоговая льгота (3+ года)",
        "desc": "Бумаги со сроком погашения от 3 лет -- кандидаты на льготное налогообложение",
        "filters": {"tax_free": True, "currency": ["SUR"], "liq_min": 10},
    },
    {
        "id": "monthly",
        "title": "Ежемесячный купон",
        "desc": "Регулярный денежный поток: выплата купона раз в месяц",
        "filters": {"cpy_min": 11.0, "cpy_max": 13.0, "currency": ["SUR"], "liq_min": 10},
    },
]


def apply_filters(rows: list[dict], f: dict) -> list[dict]:
    """Применение набора фильтров к строкам (общее для пресетов и UI)."""
    out = []
    for r in rows:
        if not _match(r, f):
            continue
        out.append(r)
    return out


def _match(r: dict, f: dict) -> bool:
    def between(val, lo, hi):
        if val is None:
            return False if (lo is not None or hi is not None) else True
        if lo is not None and val < lo:
            return False
        if hi is not None and val > hi:
            return False
        return True

    if "currency" in f and r.get("currency") not in f["currency"]:
        return False
    if "type_key" in f and r.get("type_key") not in f["type_key"]:
        return False
    if "group" in f and r.get("group") not in f["group"]:
        return False
    if not between(r.get("years"), f.get("years_min"), f.get("years_max")):
        return False
    if not between(r.get("ytm"), f.get("ytm_min"), f.get("ytm_max")):
        return False
    if not between(r.get("duration"), f.get("dur_min"), f.get("dur_max")):
        return False
    if not between(r.get("price"), f.get("price_min"), f.get("price_max")):
        return False
    if not between(r.get("current_yield"), f.get("cy_min"), f.get("cy_max")):
        return False
    if not between(r.get("liq"), f.get("liq_min"), f.get("liq_max")):
        return False
    if not between(r.get("spread_ofz"), f.get("spread_ofz_min"), f.get("spread_ofz_max")):
        return False
    if not between(r.get("turnover"), f.get("turnover_min"), f.get("turnover_max")):
        return False
    if not between(r.get("complexity"), None, f.get("complexity_max")):
        return False
    if not between(r.get("coupon_per_year"), f.get("cpy_min"), f.get("cpy_max")):
        return False
    if "coupon_kinds" in f and r.get("coupon_kind") not in f["coupon_kinds"]:
        return False
    if "flags_not" in f:
        for flag in f["flags_not"]:
            if flag in (r.get("flags") or []):
                return False
    if f.get("tax_free") and not r.get("tax_free_ok"):
        return False
    if f.get("traded_today") and not r.get("traded_today"):
        return False
    if r.get("anomaly") and not f.get("include_anomaly"):
        return False
    if f.get("no_default") and r.get("has_default"):
        return False
    return True
