#!/usr/bin/env python3
"""
botnadzor.py — сбор и проверка аккаунтов VK по базе Ботнадзора (botnadzor.org).

Только стандартная библиотека Python 3.9+, ничего ставить не нужно.

Команды:
  dump    Скачать ВСЮ базу ботов (то же, что использует расширение) и сохранить в CSV.
          Это самый быстрый и полный способ: ~600 тыс. аккаунтов одним файлом.
  pages   Обойти страницы https://botnadzor.org/accounts?p=N и собрать аккаунты из HTML.
  check   Проверить аккаунты: бот или нет.
            - по локальной копии базы (по умолчанию, без запросов к сайту);
            - --web: GET https://botnadzor.org/account/id{N} и разбор страницы;
            - --access-code КОД: POST в API расширения /inspect-account
              (код выдаёт бот https://t.me/botnadzor_org_bot, действуют лимиты/баллы).

Примеры:
  python3 botnadzor.py dump -o accounts.csv
  python3 botnadzor.py pages --start 1 --end 316 -o pages.csv --delay 1.5
  python3 botnadzor.py check 3333 id2538 durov https://vk.com/id1
  python3 botnadzor.py check --range 1-5000 --only-bots -o result.csv
  python3 botnadzor.py check 3333 --web
  python3 botnadzor.py check 3333 --access-code ВАШ_КОД
"""
from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

FRONTEND = "https://botnadzor.org"
DYNAMIC_API = "https://extension.botnadzor.org/dynamic"
# Зеркала статического API (берутся из root-config.json расширения), в порядке приоритета
STATIC_ALIASES = [
    "https://extension.botnadzor.org/static",
    "https://botnadzor.github.io/extension-data",
    "https://raw.githubusercontent.com/botnadzor/extension-data/main",
]
CACHE_DIR = Path(os.environ.get("BOTNADZOR_CACHE", Path(__file__).resolve().parent / "botnadzor_cache"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ----------------------------------------------------------------- HTTP ----
def http(url: str, *, data: dict | None = None, timeout: int = 60, retries: int = 4,
         accept: str = "*/*") -> tuple[int, bytes, str]:
    """GET (или POST JSON, если передан data). Возвращает (status, body, final_url).
    Повторяет при 429/5xx/сетевых ошибках с нарастающей паузой."""
    body = json.dumps(data).encode() if data is not None else None
    headers = {"User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip",
               "Accept-Language": "ru-RU,ru;q=0.9"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, raw, r.geturl()
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            if e.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or 0) or 5 * (attempt + 1)
                print(f"  ! HTTP {e.code} на {url}, жду {wait} c", file=sys.stderr)
                time.sleep(wait)
                last_err = e
                continue
            return e.code, raw, url
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Не удалось получить {url}: {last_err}")


def fetch_static(suffix: str) -> bytes:
    """Скачивает файл статического API с первого доступного зеркала."""
    for base in STATIC_ALIASES:
        try:
            status, raw, _ = http(base + suffix, timeout=300, retries=2)
            if status == 200 and raw:
                print(f"  {suffix}: {len(raw)/1e6:.1f} МБ с {base}", file=sys.stderr)
                return raw
            print(f"  {base}{suffix}: HTTP {status}", file=sys.stderr)
        except RuntimeError as e:
            print(f"  {e}", file=sys.stderr)
    raise SystemExit(f"Ни одно зеркало не отдало {suffix}")


# ----------------------------------------------------------- база/кэш ------
def update_cache(force: bool = False) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path = CACHE_DIR / "root-config.json"
    remote_cfg = json.loads(fetch_static("/root-config.json"))
    local_cfg = json.loads(cfg_path.read_text("utf-8")) if cfg_path.exists() else {}
    lookup_r = remote_cfg["remoteSystemLookup"]["staticApi"]["listLookup"]
    lookup_l = local_cfg.get("remoteSystemLookup", {}).get("staticApi", {}).get("listLookup", {})
    for list_id in ("accounts", "tags"):
        path = CACHE_DIR / f"{list_id}.jsonl"
        fresh = lookup_r[list_id]["generatedAt"] == lookup_l.get(list_id, {}).get("generatedAt")
        if path.exists() and fresh and not force:
            print(f"  {list_id}: актуален ({lookup_r[list_id]['generatedAt']})", file=sys.stderr)
            continue
        path.write_bytes(fetch_static(f"/lists/{list_id}.jsonl"))
    cfg_path.write_text(json.dumps(remote_cfg, ensure_ascii=False, indent=2), "utf-8")
    print(f"  в базе: {lookup_r['accounts']['itemCount']} аккаунтов, "
          f"обновлена {lookup_r['accounts']['generatedAt']}", file=sys.stderr)


def load_tags() -> dict[str, tuple[str, str]]:
    """tag_id -> (тип, название). Типы: accountCategory, accountSubcategory, region…"""
    tags = {}
    for line in (CACHE_DIR / "tags.jsonl").open(encoding="utf-8"):
        t = json.loads(line)
        tags[str(t[2])] = (t[1], t[3])
    return tags


def iter_accounts():
    """(vk_id, [tag_id, …], nickname|None) для каждой записи базы."""
    for line in (CACHE_DIR / "accounts.jsonl").open(encoding="utf-8"):
        vk_id, tids, *nick = json.loads(line)
        tids = [str(x) for x in (tids if isinstance(tids, list) else [tids])]
        yield vk_id, tids, (nick[0] if nick else None)


def describe(tids: list[str], tags: dict) -> dict[str, str]:
    g = defaultdict(list)
    for t in tids:
        typ, name = tags.get(t, ("unknown", t))
        g[typ].append(name)
    return {
        "category": "; ".join(g.pop("accountCategory", [])),
        "subcategory": "; ".join(g.pop("accountSubcategory", [])),
        "region": "; ".join(g.pop("region", [])),
        "other_tags": "; ".join(f"{k}:{v}" for k, vs in g.items() for v in vs),
    }


def ensure_cache() -> None:
    if not (CACHE_DIR / "accounts.jsonl").exists() or not (CACHE_DIR / "tags.jsonl").exists():
        print("Локальной базы нет, скачиваю…", file=sys.stderr)
        update_cache()


# ----------------------------------------------------------------- dump ----
def cmd_dump(a) -> None:
    update_cache(force=a.force)
    tags = load_tags()
    n = 0
    with open(a.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["vk_id", "vk_url", "nickname", "category", "subcategory", "region",
                    "other_tags", "tag_ids", "botnadzor_url"])
        for vk_id, tids, nick in iter_accounts():
            d = describe(tids, tags)
            dom = f"{'id' if vk_id > 0 else 'club'}{abs(vk_id)}"
            w.writerow([vk_id, f"https://vk.com/{dom}", nick or "", d["category"], d["subcategory"],
                        d["region"], d["other_tags"], ",".join(tids), f"{FRONTEND}/account/{dom}"])
            n += 1
    print(f"Готово: {n} аккаунтов -> {a.output}")


# ---------------------------------------------------------------- pages ----
ACCOUNT_LINK_RE = re.compile(
    r"""<a\b[^>]*href=["'](?:https?://(?:www\.)?botnadzor\.org)?/account/([^"'/?#]+)(?:[?#][^"']*)?["'][^>]*>(.*?)</a>""",
    re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def parse_accounts_page(page_html: str) -> list[dict]:
    """Достаёт со страницы списка все ссылки вида /account/<id123|nickname>."""
    found: dict[str, dict] = {}
    for ident, inner in ACCOUNT_LINK_RE.findall(page_html):
        text = html.unescape(TAG_RE.sub(" ", inner)).split()
        rec = found.setdefault(ident, {"account": ident, "text": ""})
        if text and len(" ".join(text)) > len(rec["text"]):
            rec["text"] = " ".join(text)
    out = []
    for ident, rec in found.items():
        m = re.fullmatch(r"(id|club|public)(\d+)", ident)
        rec["vk_id"] = (int(m.group(2)) * (1 if m.group(1) == "id" else -1)) if m else ""
        rec["vk_url"] = f"https://vk.com/{ident}"
        out.append(rec)
    return out


def cmd_pages(a) -> None:
    seen: set[str] = set()
    empty_streak = 0
    with open(a.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["page", "account", "vk_id", "vk_url", "text"])
        w.writeheader()
        p = a.start
        while a.end is None or p <= a.end:
            url = f"{FRONTEND}{a.path}?p={p}"
            status, raw, _ = http(url, accept="text/html")
            if status != 200:
                print(f"p={p}: HTTP {status}" + (" (возможно, блок фаервола — попробуйте "
                      "с другого IP/реже)" if status == 403 else ""), file=sys.stderr)
                if status in (403, 404):
                    break
            rows = parse_accounts_page(raw.decode("utf-8", "replace")) if status == 200 else []
            new = [r for r in rows if r["account"] not in seen]
            for r in new:
                seen.add(r["account"])
                w.writerow({"page": p, **r})
            f.flush()
            print(f"p={p}: найдено {len(rows)}, новых {len(new)}, всего {len(seen)}", file=sys.stderr)
            if a.dump_html:
                Path(a.dump_html).mkdir(exist_ok=True)
                Path(a.dump_html, f"p{p}.html").write_bytes(raw)
            empty_streak = empty_streak + 1 if not new else 0
            if a.end is None and empty_streak >= 2:   # страницы закончились
                break
            p += 1
            time.sleep(a.delay)
    print(f"Готово: {len(seen)} аккаунтов -> {a.output}")


# ---------------------------------------------------------------- check ----
VK_IN_RE = re.compile(r"(?:https?://)?(?:m\.)?(?:vk\.(?:com|ru)|botnadzor\.org/account)/([^/?#\s]+)", re.I)


def normalize(raw: str) -> tuple[int | None, str | None]:
    """'123' / 'id123' / 'club5' / 'https://vk.com/durov' -> (vk_id | None, nickname | None)."""
    s = raw.strip()
    m = VK_IN_RE.search(s)
    if m:
        s = m.group(1)
    if re.fullmatch(r"-?\d+", s):
        return int(s), None
    m = re.fullmatch(r"(id|club|public|event)(\d+)", s, re.I)
    if m:
        n = int(m.group(2))
        return (n if m.group(1).lower() == "id" else -n), None
    return None, s.lower()


def check_web(vk_id: int | None, nick: str | None, tag_names: list[str]) -> dict:
    """GET https://botnadzor.org/account/<id|nick> и эвристический разбор страницы.
    Ищем на странице названия меток из справочника тегов (они совпадают с тем,
    что сайт показывает), плюс <title>/og:description."""
    dom = nick if vk_id is None else f"{'id' if vk_id > 0 else 'club'}{abs(vk_id)}"
    url = f"{FRONTEND}/account/{dom}"
    status, raw, final = http(url, accept="text/html")
    text = raw.decode("utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    desc = re.search(r"""<meta[^>]+(?:property|name)=["'](?:og:)?description["'][^>]+content=["']([^"']*)""", text, re.I)
    visible = html.unescape(TAG_RE.sub(" ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)))
    marks = sorted({t for t in tag_names if t and t in visible})
    return {
        "web_status": status,
        "web_url": final,
        "web_title": html.unescape(title.group(1).strip()) if title else "",
        "web_description": html.unescape(desc.group(1)) if desc else "",
        "web_marks": "; ".join(marks),
        "web_is_bot": bool(marks) if status == 200 else ("" if status != 404 else False),
    }


def check_api(vk_id: int, code: str) -> dict:
    """POST {DYNAMIC_API}/inspect-account — то же, что делает «инспектор» в расширении."""
    status, raw, _ = http(f"{DYNAMIC_API}/inspect-account",
                          data={"accessCode": code, "vkId": vk_id}, accept="application/json")
    try:
        body = json.loads(raw)
    except ValueError:
        return {"api_status": status, "api_error": raw[:200].decode("utf-8", "replace")}
    if body.get("problem"):
        return {"api_status": status, "api_error": f"{body.get('type')}: {body.get('description')}"}
    lg = body.get("legacy", {})
    return {
        "api_status": status,
        "api_name": lg.get("name") or "",
        "api_mark": lg.get("mark") or "",
        "api_mark_title": (lg.get("mark_title") or "").replace("\n", " | "),
        "api_comments_groups": len(lg.get("comments") or []),
        "api_likes_to_bots": len(lg.get("likes") or []),
        "api_remaining_points": body.get("remainingPointCount", ""),
        "api_raw": json.dumps(body, ensure_ascii=False),
    }


def cmd_check(a) -> None:
    targets: list[str] = list(a.accounts)
    if a.file:
        targets += [l.strip() for l in Path(a.file).read_text("utf-8").splitlines() if l.strip()]
    if a.range:
        lo, hi = map(int, a.range.split("-"))
        targets += [str(i) for i in range(lo, hi + 1)]
    if not targets:
        raise SystemExit("Укажите аккаунты, --file или --range")

    ensure_cache()
    tags = load_tags()
    wanted = [normalize(t) for t in targets]
    want_ids = {i for i, _ in wanted if i is not None}
    want_nicks = {n for _, n in wanted if n is not None}
    by_id, by_nick = {}, {}
    for vk_id, tids, nick in iter_accounts():          # один проход по базе
        if vk_id in want_ids:
            by_id[vk_id] = (tids, nick)
        if nick and nick.lower() in want_nicks:
            by_nick[nick.lower()] = (vk_id, tids, nick)
    tag_names = sorted({name for _, name in tags.values()}, key=len, reverse=True)

    rows = []
    for raw_in, (vk_id, nick) in zip(targets, wanted):
        rec = {"input": raw_in, "vk_id": vk_id or "", "nickname": nick or ""}
        hit = None
        if vk_id is not None and vk_id in by_id:
            hit = (vk_id, *by_id[vk_id])
        elif nick and nick in by_nick:
            hit = by_nick[nick]
        if hit:
            rec.update(vk_id=hit[0], nickname=hit[2] or rec["nickname"], is_bot=True,
                       tag_ids=",".join(hit[1]), **describe(hit[1], tags))
        else:
            rec.update(is_bot=False if vk_id is not None else "unknown (ник не найден в базе)",
                       tag_ids="", category="", subcategory="", region="", other_tags="")
        if a.only_bots and rec["is_bot"] is not True and not (a.web or a.access_code):
            continue
        if a.web:
            rec.update(check_web(rec["vk_id"] or None, rec["nickname"] or None, tag_names))
            time.sleep(a.delay)
        if a.access_code:
            if rec["vk_id"] and int(rec["vk_id"]) > 0:
                rec.update(check_api(int(rec["vk_id"]), a.access_code))
            else:
                rec["api_error"] = "API принимает только числовой id пользователя"
            time.sleep(a.delay)
        rows.append(rec)
        if not a.output:
            verdict = "БОТ" if rec["is_bot"] is True else ("нет в базе" if rec["is_bot"] is False else rec["is_bot"])
            extra = " / ".join(x for x in (rec["category"], rec["subcategory"], rec["region"]) if x)
            line = f"{raw_in:>28}  ->  {verdict}" + (f"  [{extra}]" if extra else "")
            if a.web:
                line += f"\n{'':>32}сайт: HTTP {rec['web_status']}, метки: {rec['web_marks'] or '—'}"
            if a.access_code:
                line += f"\n{'':>32}API: {rec.get('api_mark_title') or rec.get('api_error') or rec.get('api_mark') or '—'}"
            print(line)

    if a.output:
        keys = list(dict.fromkeys(k for r in rows for k in r))
        with open(a.output, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        bots = sum(1 for r in rows if r["is_bot"] is True)
        print(f"Проверено {len(targets)}, ботов в базе: {bots}. Результат -> {a.output}")


# ------------------------------------------------------------------ main ---
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump", help="скачать всю базу ботов в CSV")
    d.add_argument("-o", "--output", default="botnadzor_accounts.csv")
    d.add_argument("--force", action="store_true", help="перекачать, даже если кэш актуален")
    d.set_defaults(fn=cmd_dump)

    p = sub.add_parser("pages", help="обойти botnadzor.org/accounts?p=N")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=None, help="по умолчанию — пока не кончатся страницы")
    p.add_argument("--path", default="/accounts", help="раздел, напр. /accounts/like, /accounts/trol, /accounts/nd")
    p.add_argument("--delay", type=float, default=1.0, help="пауза между запросами, c")
    p.add_argument("--dump-html", metavar="DIR", help="сохранять сырые HTML-страницы в папку")
    p.add_argument("-o", "--output", default="botnadzor_pages.csv")
    p.set_defaults(fn=cmd_pages)

    c = sub.add_parser("check", help="проверить аккаунты: бот или нет")
    c.add_argument("accounts", nargs="*", help="id, id123, club5, ник или ссылка vk.com/…")
    c.add_argument("--file", help="файл со списком (по одному в строке)")
    c.add_argument("--range", help="диапазон id, напр. 1-10000")
    c.add_argument("--web", action="store_true", help="также запросить страницу botnadzor.org/account/…")
    c.add_argument("--access-code", help="код доступа из @botnadzor_org_bot для API /inspect-account")
    c.add_argument("--only-bots", action="store_true", help="выводить только найденных ботов")
    c.add_argument("--delay", type=float, default=1.0, help="пауза между сетевыми запросами, c")
    c.add_argument("-o", "--output", help="сохранить результат в CSV")
    c.set_defaults(fn=cmd_check)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
