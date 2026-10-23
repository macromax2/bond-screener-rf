#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Кредитные рейтинги эмитентов облигаций.

Зачем: поле rating в карточке бумаги и колонка «Рейтинг» в скринере.
Рейтинг — один из главных сигналов надёжности бумаги для консервативного
инвестора.

ИСТОЧНИК ДАННЫХ (честность):
    Рейтинговый репозитарий Банка России — ratings.cbr.ru — единственный
    бесплатный и открытый источник, доступный и локально, и с облачных IP
    (АКРА/НКР/НРА недоступны с облака, Эксперт РА отдаёт 429, cbonds платный).

    У репозитория нет публичного REST API — данные отдаются Bitrix-AJAX
    компонента prr.form. Рабочая схема (проверено, капча НЕ требуется):
      1) GET / (+куки) -> bitrix_sessid из HTML и метки фильтра typeGroup;
      2) POST /bitrix/services/main/ajax.php?c=prr.form&action=searchRating
         с полями formSearh=advanced и всеми typeGroup[] -> itemCount;
      3) POST ...action=searchRatingNavigation (pageSize=100, страница N,
         sortingField=objectName -- стабильная сортировка, полный проход
         без потерь) -> itemList[].
      * «{"message":"Array"}» = пустая выдача (серверный баг обработки
        пустого результата), ошибки не считаем.
    Полный реестр ~5600 рейтинговых действий = ~56 запросов ~30-60 сек.

    КАЖДАЯ запись честно маркируется: рейтинг, агентство, дата действия.
    Отзывы (WD) не показываются как действующий рейтинг. Ничего не выдумываем:
    если источника нет -- работает вывод по типу эмитента (inferred).

Хранение: data/ratings.json = {ts, source, items}. Ключи items:
    * ISIN (записи уровня бумаги, objectType TBND/TMGB/...);
    * ИНН  (записи уровня эмитента, objectType BNFC/CBNK/...).

Вывод: rating (нормализованный нац. вид "ruAA+" либо исходный формат
агентства), rating_band (high|mid|low|None), rating_source
("external" | "inferred" | None), rating_agency, rating_date.
"""
from __future__ import annotations

import html as html_mod
import http.cookiejar
import json
import pathlib
import re
import time
import urllib.parse
import urllib.request

from . import moex

RATINGS_CACHE = moex.DATA_DIR / "ratings.json"

CBR = "https://ratings.cbr.ru"
CBR_SOURCE = "ratings.cbr.ru — реестр кредитных рейтингов Банка России"
# Браузерный UA: сайт под ddos-guard, «сырой» urllib-заголовок вызывает отказ.
_CBR_UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}
# Защита от затирания кэша «пустым» ответом (блокировка/изменился формат).
MIN_REGISTRY_ITEMS = 500

# Национальная шкала РФ: сортируемость по надёжности (чем выше -- тем лучше).
# Строки сравниваются лексикографически в порядке убывания надёжности через
# _rank(). Ниже -- отображение в балл для фильтра «Только госбумаги».
_HIGH_PREFIXES = ("ruAAA", "ruAA", "ruA")  # ruAAA, ruAA+/-, ruA+/-
_MID_PREFIXES = ("ruBBB", "ruBB", "ruB+", "ruB")  # инвестиционный/спекулятивный низ

# Нормализация форматов ratingValue к "ruX":
#   "AA+(RU)" | "A-.ru" | "A+|ru|" -> "ruAA+" / "ruA-" / "ruA+"
#   "ruAAA" | "ruAAA.sf" -> "ruAAA"   (".sf" -- субфедеральный, отбрасываем)
#   "AAA(ru.sf)" -> "ruAAA"
# Чистые "AAA"/"A-" (без пометки шкалы) и чужие ("byAAA", "kzBBB-")
# остаются как есть: приводить их к ru-шкале -- было бы враньём.
_RE_RU = re.compile(
    r"^(?:ru(?P<g1>[A-D]{1,3}[+-]?)(?:\.sf)?"
    r"|(?P<g2>[A-D]{1,3}[+-]?)(?:\([Rr][Uu](?:\.[A-Za-z]+)?\)|\.[Rr][Uu]|\|[Rr][Uu]\|))$")

_RE_WD = re.compile(r"(?:^|\s)WD(?:\s|$)|отзыв", re.IGNORECASE)


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


def _norm_rating(value: str | None) -> str | None:
    """Привести ratingValue к единому виду (ruX) либо вернуть исходник.

    Возвращает None для пустых и «Рейтинг отозван».
    """
    if not value:
        return None
    s = value.strip()
    if not s or "отозван" in s.lower():
        return None
    m = _RE_RU.match(s)
    if m:
        grade = (m.group("g1") or m.group("g2")).upper()
        return "ru" + grade
    return s  # чистая шкала без пометки / чужая шкала -- как есть


def _is_withdrawn(action: str | None, value: str | None) -> bool:
    """Рейтинг отозван (WD / «отзыв» / «Рейтинг отозван»)?"""
    if value and "отозван" in value.lower():
        return True
    return bool(action and _RE_WD.search(action))


def _parse_date(s: str | None) -> str:
    """'29.06.2026' -> '2026-06-29' (для сортировки); иначе исходник."""
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", (s or "").strip())
    if not m:
        return ""
    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


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


# --------------------------------------------------------------------------
# Сбор с ratings.cbr.ru
# --------------------------------------------------------------------------

def _opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _request(opener, url: str, data: bytes | None = None, referer: str | None = None,
             retries: int = 3) -> bytes:
    """HTTP-запрос к ЦБ с куками и повторами. Возвращает байты тела."""
    headers = dict(_CBR_UA)
    if referer:
        headers["Referer"] = referer
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["bx-ajax"] = "true"
        headers["Origin"] = CBR
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with opener.open(req, timeout=45) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001 -- любая сетевая ошибка -> повтор
            last_err = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"запрос к {url} не удался после {retries} попыток: {last_err}")


def _session() -> tuple[urllib.request.OpenerDirector, str, list[str]]:
    """Открыть сессию ЦБ: куки, bitrix_sessid и метки фильтра typeGroup."""
    opener = _opener()
    page = _request(opener, f"{CBR}/?disclaimer=1", referer=f"{CBR}/").decode(
        "utf-8", errors="replace")
    m = re.search(r'"bitrix_sessid"\s*:\s*"([a-f0-9]{32})"', page)
    if not m:
        raise RuntimeError("ratings.cbr.ru: не найден bitrix_sessid (блокировка?)")
    sessid = m.group(1)
    labels = [html_mod.unescape(t).strip()
              for t in re.findall(
                  r'name="typeGroup"[^>]*>\s*<label[^>]*>([^<]+)</label>', page)]
    if not labels:
        raise RuntimeError("ratings.cbr.ru: не найдены метки фильтра typeGroup")
    return opener, sessid, labels


def _ajax_post(opener, sessid: str, action: str, fields: list[tuple[str, str]],
               referer: str) -> dict:
    """POST в Bitrix-ajax ЦБ. Возвращает data-словарь ответа.

    Ответ status=error с сообщением 'Array' -- это «0 результатов» на сервере,
    для нас это пустая выдача, не ошибка.
    """
    url = (f"{CBR}/bitrix/services/main/ajax.php?c=prr.form&action={action}"
           f"&mode=ajax&sessid={sessid}")
    body = urllib.parse.urlencode(fields).encode("utf-8")
    raw = _request(opener, url, data=body, referer=referer)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f"ratings.cbr.ru: не JSON в ответе {action}: "
                           f"{raw[:200]!r}") from err
    if payload.get("status") == "success":
        return payload.get("data") or {}
    msgs = [e.get("message", "") for e in (payload.get("errors") or [])]
    if any("Array" in (m or "") for m in msgs):
        return {}  # пустая выдача (серверный баг ЦБ)
    raise RuntimeError(f"ratings.cbr.ru: ошибка {action}: {msgs}")


def fetch_external() -> dict[str, dict]:
    """Полный реестр рейтинговых действий с ratings.cbr.ru.

    Возвращает items: ключ ISIN либо ИНН -> {rating, raw, agency, date,
    action, scope, name}. Действующий рейтинг на ключ -- самое свежее
    НЕ-отозванное действие; если последнее действие -- отзыв, ключ
    в реестр не попадает (нет действующего рейтинга).
    """
    opener, sessid, labels = _session()
    referer = f"{CBR}/?disclaimer=1"

    # 1) сам поиск: даёт itemCount (первую страницу доберём навигацией)
    base_fields = [
        ("fields[formSearh]", "advanced"),
        ("fields[dateFrom]", ""), ("fields[dateTo]", ""),
        ("fields[ratingName]", ""), ("fields[inn]", ""),
        ("fields[isin]", ""), ("fields[koNumber]", ""),
    ]
    base_fields += [("fields[typeGroup][]", lbl) for lbl in labels]
    data = _ajax_post(opener, sessid, "searchRating", base_fields, referer)
    total = int(data.get("itemCount") or 0)
    if total <= 0:
        raise RuntimeError("ratings.cbr.ru: поиск вернул 0 записей")

    # 2) полный проход навигацией: pageSize=100, стабильная сортировка
    #    по objectName (по дате страницы «плывут» и теряют записи)
    records: list[dict] = []
    page, max_pages = 1, total // 100 + 3
    while len(records) < total and page <= max_pages:
        nav = _ajax_post(opener, sessid, "searchRatingNavigation", [
            ("fields[pageSize]", "100"),
            ("fields[pageNumber]", str(page)),
            ("fields[sortingField]", "objectName"),
            ("fields[sortingDirection]", "ascending"),
        ], referer)
        items = nav.get("itemList") or []
        if not items:
            break
        records.extend(items)
        page += 1
        time.sleep(0.25)  # вежливо к репозиторию

    # 3) дедупликация (на границах страниц бывает по 1-2 повтора)
    uniq: dict[tuple, dict] = {}
    for r in records:
        key = (r.get("objectId"), r.get("releaseDate"),
               r.get("ratingAction"), r.get("kraName"))
        uniq.setdefault(key, r)
    if len(uniq) < MIN_REGISTRY_ITEMS:
        raise RuntimeError(
            f"ratings.cbr.ru: собрано только {len(uniq)} записей "
            f"(ожидалось ~{total}) -- не сохраняю, чтобы не затереть кэш")

    # 4) действующий рейтинг на каждый ключ (ISIN у бумаг, ИНН у эмитентов):
    #    самое свежее действие; отзыв (WD) гасит рейтинг, даже если раньше
    #    он действовал. Отозванные записи тоже участвуют в сравнении дат.
    best: dict[str, dict] = {}
    for r in uniq.values():
        isin = (r.get("isin") or "").strip()
        inn = (r.get("inn") or "").strip()
        if isin:
            key, scope = isin, "isin"
        elif inn:
            key, scope = inn, "inn"
        else:
            continue  # суверенные/прочие объекты без ISIN и ИНН
        raw = (r.get("ratingValue") or "").strip()
        action = (r.get("ratingAction") or "").strip()
        if not raw and not action:
            continue
        rating = _norm_rating(raw)
        withdrawn = rating is None or _is_withdrawn(action, raw)
        cand = {
            "rating": rating,  # None = отозван/пусто (сравнение ниже)
            "raw": raw,
            "agency": (r.get("kraName") or "").strip(),
            "date": (r.get("releaseDate") or "").strip(),
            "action": action,
            "scope": scope,
            "name": (r.get("subjectName") or r.get("objectName") or "").strip(),
            "_iso": _parse_date(r.get("releaseDate")),
            "_wd": withdrawn,
        }
        cur = best.get(key)
        if cur is None:
            best[key] = cand
            continue
        # свежесть важнее; при равенстве дат -- консервативно: отзыв и
        # худший рейтинг побеждают
        cand_key = (cand["_iso"], not cand["_wd"], -_rank(cand["rating"]))
        cur_key = (cur["_iso"], not cur["_wd"], -_rank(cur["rating"]))
        if cand_key > cur_key:
            best[key] = cand

    items: dict[str, dict] = {}
    for key, cand in best.items():
        if cand["_wd"] or not cand["rating"]:
            continue  # последнее действие -- отзыв: действующего рейтинга нет
        cand.pop("_iso", None)
        cand.pop("_wd", None)
        items[key] = cand
    if len(items) < MIN_REGISTRY_ITEMS:
        raise RuntimeError(
            f"ratings.cbr.ru: после нормализации {len(items)} ключей -- "
            f"слишком мало, кэш не затираю")
    return items


def load() -> dict:
    """Загрузить кэш реестра: {ts, source, items:{ключ:{rating,...}}}."""
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
        json.dumps({"ts": time.time(), "source": CBR_SOURCE, "items": items},
                   ensure_ascii=False),
        encoding="utf-8")


def refresh(force: bool = False, max_age: float = 20 * 3600) -> dict[str, dict]:
    """Обновить реестр из ЦБ (не чаще max_age, если не force).

    Сетевые ошибки и пустая выдача НЕ затирают существующий кэш.
    """
    cached = load()
    ts = float(cached.get("ts") or 0)
    if not force and cached.get("items") and time.time() - ts < max_age:
        return cached["items"]
    try:
        items = fetch_external()
    except Exception as err:  # noqa: BLE001 -- публикация не должна падать
        print(f"рейтинги: обновление из ЦБ не удалось ({err}); беру кэш",
              flush=True)
        return cached.get("items", {})
    if not items:
        return cached.get("items", {})
    save(items)
    print(f"рейтинги: реестр ЦБ обновлён -- {len(items)} ключей", flush=True)
    # сбросить кэш реестра в analytics: он мог прочитать старую версию
    try:
        from . import analytics as _analytics
        _analytics._external_cache = None
    except Exception:  # noqa: BLE001 -- не критично (первый вызов в процессе)
        pass
    return items


def resolve(bond: dict, external: dict | None = None) -> dict:
    """Определить рейтинг для одной бумаги скринера.

    Приоритет: внешний реестр по ISIN -> по ИНН эмитента -> вывод по типу.
    Возвращает {rating, rating_band, rating_source, rating_agency, rating_date}.
    """
    isin = bond.get("isin")
    inn = str(bond.get("inn") or "").strip()
    if external is None:
        external = load().get("items", {})
    rec = None
    if isin:
        rec = external.get(isin)
    if rec is None and inn:
        rec = external.get(inn)
    if rec:
        rating = rec.get("rating")
        return {
            "rating": rating,
            "rating_band": band(rating),
            "rating_source": "external",
            "rating_agency": rec.get("agency"),
            "rating_date": rec.get("date"),
        }
    rating, src = _infer_from_type(bond.get("type_key"), bond.get("group"))
    return {
        "rating": rating,
        "rating_band": band(rating),
        "rating_source": src if rating else None,
        "rating_agency": None,
        "rating_date": None,
    }


if __name__ == "__main__":
    # Ручное обновление: python -m app.ratings [--force]
    got = refresh(force="--force" in __import__("sys").argv)
    print(f"итого ключей: {len(got)}")
