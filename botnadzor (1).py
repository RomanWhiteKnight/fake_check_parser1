#!/usr/bin/env python3
"""
botnadzor.py — сбор и проверка аккаунтов VK по базе Ботнадзора (botnadzor.org).

Только стандартная библиотека Python 3.9+, ничего ставить не нужно.

Команды:
  dump    Скачать ВСЮ базу ботов (то же, что использует расширение) и сохранить в CSV.
          Это самый быстрый и полный способ: ~600 тыс. аккаунтов одним файлом.
  pages   Обойти страницы https://botnadzor.org/accounts?p=N и собрать аккаунты из HTML.
  site    Проверить аккаунты через сайт: GET https://botnadzor.org/account/id{N}
          и разбор страницы (категория, регион, дата регистрации, комментарии,
          % лайков от ботов, активность, кому ставил лайки).
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
  python3 botnadzor.py site 3333 id2538
  python3 botnadzor.py site --range 3300-3400 --only-bots -o site.csv
  python3 botnadzor.py site 3333 --json
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


# ------------------------------------------------- страница аккаунта -------
def _clean(fragment: str) -> str:
    """HTML-фрагмент -> текст с нормализованными пробелами."""
    t = re.sub(r"(?is)<(script|style|svg)\b.*?</\1>", " ", fragment)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def _num(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def _rsc_payload(page: str) -> str:
    """Склеивает данные Next.js из self.__next_f.push([1, "..."])."""
    out = []
    for chunk in re.findall(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", page, re.S):
        try:
            arr = json.loads(chunk)
        except ValueError:
            continue
        if len(arr) > 1 and isinstance(arr[1], str):
            out.append(arr[1])
    return "".join(out)


def _meta(page: str, key: str) -> str:
    for pat in (rf"""<meta[^>]+(?:name|property)=["']{re.escape(key)}["'][^>]*content=["']([^"']*)""",
                rf"""<meta[^>]+content=["']([^"']*)["'][^>]*(?:name|property)=["']{re.escape(key)}["']"""):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1))
    return ""


def parse_account_page(page: str) -> dict:
    """Разбирает HTML страницы https://botnadzor.org/account/<id>.
    Возвращает словарь: метка (бот/нет), категория, регион, регистрация,
    статистика комментариев и лайков, активность и список комментариев."""
    info: dict = {}
    title_m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    info["title"] = html.unescape(title_m.group(1)).strip() if title_m else ""
    og_title = _meta(page, "og:title")
    m = re.match(r"(.*)\s+\(([^()]+)\)\s*$", og_title)
    info["name"], info["vk_domain"] = (m.group(1), m.group(2)) if m else (og_title, "")

    # "Категория: Бот / Накрутка • Регион: Санкт-Петербург • Регистрация: 05.12.2006"
    desc = _meta(page, "description")
    info["description"] = desc
    fields = {}
    for part in re.split(r"\s*[•\n]\s*", desc):
        if ":" in part:
            k, v = part.split(":", 1)
            fields[k.strip()] = v.strip()
    info["category"] = fields.pop("Категория", "")
    info["region"] = fields.pop("Регион", "")
    info["reg_date"] = fields.pop("Регистрация", "")
    if fields:
        info["other_fields"] = "; ".join(f"{k}: {v}" for k, v in fields.items())
    info["is_bot"] = bool(info["category"])

    text = _clean(page)
    m = re.search(r"Регистрация\s+(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})", text)
    if m:
        info["reg_date"], info["reg_time"] = m.group(1), m.group(2)
    m = re.search(r"\[(\d[\d\s]*)\s+комментари\w*\s+в\s+(\d[\d\s]*)\s+групп", info["title"]) or \
        re.search(r"(\d[\d\s]*)\s+комментари\w*\s+в\s+(\d[\d\s]*)\s+групп", text)
    if m:
        info["comments_count"], info["groups_count"] = _num(m.group(1)), _num(m.group(2))
    m = re.search(r"(\d+)\s*%\s*лайков от ботов\s*\(\s*(\d[\d\s]*?)\s+из\s+(\d[\d\s]*?)\s*\)", text)
    if m:
        info["bot_likes_percent"] = int(m.group(1))
        info["bot_likes"], info["likes_total"] = _num(m.group(2)), _num(m.group(3))
    info["closed_account"] = "закрытый аккаунт" in text

    # Плашка региона: <a href="/region/r6…">Санкт-Петербург <span>1 484</span></a>
    m = re.search(r"""<a[^>]+href=["'][^"']*/region/(r\d+)[^"']*["'][^>]*>(.*?)</a>""", page, re.S)
    if m:
        rt = _clean(m.group(2))
        cm = re.search(r"([\d\s]+)$", rt)
        info["region_id"] = m.group(1)
        if cm and cm.group(1).strip():
            info["region_bots_total"] = _num(cm.group(1))

    # Календарь активности из данных Next.js
    rsc = _rsc_payload(page)
    i = rsc.find('"daysByDateStr":')
    if i >= 0:
        try:
            days, _ = json.JSONDecoder().raw_decode(rsc, i + len('"daysByDateStr":'))
            active = sorted(d for d, v in days.items()
                            if v.get("commentCount") or v.get("likesMadeCount") or v.get("likesGotCount"))
            info["activity_days"] = len(active)
            info["activity_first"] = active[0] if active else ""
            info["activity_last"] = active[-1] if active else ""
            info["activity_comments"] = sum(v.get("commentCount", 0) for v in days.values())
            info["activity_likes_made"] = sum(v.get("likesMadeCount", 0) for v in days.values())
            info["activity_likes_got"] = sum(v.get("likesGotCount", 0) for v in days.values())
            info["activity_renames"] = sum(v.get("renameCount", 0) for v in days.values())
        except ValueError:
            pass

    # Таблицы: «комментарии» и «накрученные лайки» (если их нет в HTML — берём из данных Next.js)
    def tables(src: str) -> tuple[list[dict], list[dict]]:
        comments, liked = [], []
        for tbl in re.findall(r"(?is)<table\b.*?</table>", src):
            head = _clean(" ".join(re.findall(r"(?is)<th[^>]*>(.*?)</th>", tbl)))
            trs = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", tbl)
            if "Дата комментария" in head:
                for tr in trs:
                    link = re.search(r"https://vk\.com/wall-?\d+_\d+[^\"'<\s]*", tr)
                    if not link:
                        continue
                    ctext = _clean(tr)
                    g = re.search(r"/group/([\w.]+)|[?&;]group=([\w.]+)", tr)
                    d = re.search(r"\d{1,2}:\d{2}:\d{2} \d{1,2}\.\d{1,2}\.\d{4}", ctext)
                    t = re.search(r"title=[\"']([^\"']*(?:Комментарий|Ответ|Пост|Запись)[^\"']*)", tr)
                    b = re.search(r"\((\d+)/(\d+)\)", ctext)
                    lk = re.search(r"(\d+)\s*лайков ботов\s*\((\d+)%\)", ctext)
                    comments.append({
                        "link": html.unescape(link.group(0)),
                        "group": (g.group(1) or g.group(2)) if g else "",
                        "date": d.group(0) if d else "",
                        "type": t.group(1) if t else "",
                        "post_comments": int(b.group(1)) if b else "",
                        "post_bots": int(b.group(2)) if b else "",
                        "bot_likes": int(lk.group(1)) if lk else 0,
                    })
            elif "Накручено лайков" in head:
                for tr in trs:
                    acc = re.search(r"""href=["'][^"']*/account/([^"'/?#]+)["'][^>]*>(.*?)</a>""", tr, re.S)
                    if not acc:
                        continue
                    tds = re.findall(r"(?is)<td[^>]*>(.*?)</td>", tr)
                    mark = re.search(r"""title=["']Маркировка:([^"']*)""", tr)
                    liked.append({
                        "account": acc.group(1),
                        "name": _clean(acc.group(2)),
                        "likes_from_this_account": _num(_clean(tds[1])) if len(tds) > 1 and re.search(r"\d", _clean(tds[1])) else "",
                        "mark": re.sub(r"\s+", " ", html.unescape(mark.group(1))).strip() if mark else "",
                        "liked_comments": len(set(re.findall(r"https://vk\.com/wall-?\d+_\d+[^\"'<\s]*", tr))),
                    })
        return comments, liked

    comments, liked = tables(page)
    if not comments and not liked:
        comments, liked = tables(rsc.replace("\\u003c", "<").replace('\\"', '"'))
    info["comments"] = comments
    info["liked_bots"] = liked
    info["liked_bots_count"] = len(liked)
    info["groups"] = "; ".join(sorted({r["group"] for r in comments if r["group"]}))
    return info


def check_web(vk_id: int | None, nick: str | None) -> dict:
    """GET https://botnadzor.org/account/<id|nick> и разбор ответа."""
    dom = nick if vk_id is None else f"{'id' if vk_id > 0 else 'club'}{abs(vk_id)}"
    url = f"{FRONTEND}/account/{dom}"
    status, raw, final = http(url, accept="text/html")
    res = {"web_status": status, "web_url": final}
    if status == 200:
        info = parse_account_page(raw.decode("utf-8", "replace"))
        info.pop("comments", None)
        info.pop("liked_bots", None)
        res.update({f"web_{k}": v for k, v in info.items()})
    elif status == 404:
        res["web_is_bot"] = False
    return res


def cmd_site(a) -> None:
    """Проверка только через сайт: GET /account/id{N} для каждого аккаунта."""
    targets = list(a.accounts)
    if a.file:
        targets += [l.strip() for l in Path(a.file).read_text("utf-8").splitlines() if l.strip()]
    if a.range:
        lo, hi = map(int, a.range.split("-"))
        targets += [str(i) for i in range(lo, hi + 1)]
    if a.from_html:
        pages = [(p, Path(p).read_text("utf-8", "replace")) for p in a.from_html]
    elif not targets:
        raise SystemExit("Укажите аккаунты, --file, --range или --from-html")

    results, comment_rows, liked_rows = [], [], []

    def handle(label: str, status: int, url: str, page: str | None) -> None:
        rec = {"input": label, "http_status": status, "url": url}
        if status == 200 and page is not None:
            info = parse_account_page(page)
            for c in info.pop("comments"):
                comment_rows.append({"input": label, **c})
            for lb in info.pop("liked_bots"):
                liked_rows.append({"input": label, **lb})
            rec.update(info)
        elif status == 404:
            rec.update(is_bot=False, note="страницы нет (404)")
        else:
            rec.update(is_bot="", note=f"HTTP {status}")
        results.append(rec)
        if a.only_bots and rec.get("is_bot") is not True:
            return
        if a.json:
            print(json.dumps(rec, ensure_ascii=False, indent=2))
        elif not a.output:
            if rec.get("is_bot") is True:
                bits = [rec.get("category"), rec.get("region"),
                        f"рег. {rec.get('reg_date','')} {rec.get('reg_time','')}".strip(),
                        f"{rec.get('comments_count', '?')} комм. в {rec.get('groups_count', '?')} группах"]
                if "bot_likes_percent" in rec:
                    bits.append(f"{rec['bot_likes_percent']}% лайков от ботов")
                print(f"{label:>14}  ->  БОТ  {rec.get('name','')} | " + " | ".join(b for b in bits if b))
            elif rec.get("is_bot") is False:
                print(f"{label:>14}  ->  не отмечен  {rec.get('name','')} {rec.get('note','')}".rstrip())
            else:
                print(f"{label:>14}  ->  ?  {rec.get('note','')}")

    if a.from_html:
        for p, page in pages:
            handle(Path(p).name, 200, "", page)
    for t in targets:
        vk_id, nick = normalize(t)
        dom = nick if vk_id is None else f"{'id' if vk_id > 0 else 'club'}{abs(vk_id)}"
        url = f"{FRONTEND}/account/{dom}"
        try:
            status, raw, final = http(url, accept="text/html")
        except RuntimeError as e:
            print(f"{t}: {e}", file=sys.stderr)
            continue
        if status == 403:
            print(f"{t}: HTTP 403 — похоже, фаервол сайта. Увеличьте --delay.", file=sys.stderr)
        handle(t, status, final, raw.decode("utf-8", "replace") if status == 200 else None)
        time.sleep(a.delay)

    if a.output:
        keys = list(dict.fromkeys(k for r in results for k in r))
        with open(a.output, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(r for r in results if not a.only_bots or r.get("is_bot") is True)
        for suffix, rows_, label_ in (("_comments", comment_rows, "Комментарии"),
                                      ("_liked_bots", liked_rows, "Кому ставил лайки")):
            if not rows_:
                continue
            cpath = Path(a.output).with_name(Path(a.output).stem + suffix + ".csv")
            with open(cpath, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows_ for k in r)))
                w.writeheader()
                w.writerows(rows_)
            print(f"{label_} -> {cpath}")
        bots = sum(1 for r in results if r.get("is_bot") is True)
        print(f"Проверено {len(results)}, ботов: {bots}. Результат -> {a.output}")


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
            rec.update(check_web(rec["vk_id"] or None, rec["nickname"] or None))
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
                line += (f"\n{'':>32}сайт: HTTP {rec['web_status']}, "
                         f"{rec.get('web_category') or 'метки нет'}"
                         + (f", {rec['web_comments_count']} комм., {rec.get('web_bot_likes_percent', '?')}% лайков от ботов"
                            if 'web_comments_count' in rec else ""))
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

    w = sub.add_parser("site", help="проверить через сайт: GET botnadzor.org/account/id{N}")
    w.add_argument("accounts", nargs="*", help="id, id123, ник или ссылка vk.com/…")
    w.add_argument("--file", help="файл со списком (по одному в строке)")
    w.add_argument("--range", help="диапазон id, напр. 3000-3500")
    w.add_argument("--from-html", nargs="+", metavar="FILE", help="разобрать сохранённые страницы вместо запроса")
    w.add_argument("--only-bots", action="store_true", help="выводить только ботов")
    w.add_argument("--json", action="store_true", help="печатать полный результат в JSON")
    w.add_argument("--delay", type=float, default=1.5, help="пауза между запросами, c (по умолчанию 1.5)")
    w.add_argument("-o", "--output", help="CSV с результатами (+ отдельный *_comments.csv)")
    w.set_defaults(fn=cmd_site)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
