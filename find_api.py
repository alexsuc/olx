from __future__ import annotations
"""
Строит OLX_API_URL из обычной ссылки на страницу поиска OLX.

Запуск:
    python find_api.py "https://www.olx.ua/uk/nedvizhimost/.../poltava/?currency=UAH&search%5B...%5D=..."

Скрипт сам ничего не ломает: только скачивает страницу, ищет в ней ID категории/региона/города,
переводит фильтры страницы в параметры API и проверяет получившийся запрос.
"""
import json
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlparse

try:
    from curl_cffi import requests  # притворяется браузером Chrome (обходит 403)
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    import requests
    IMPERSONATE = {}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8",
}


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def extract_state(html: str):
    m = re.search(r'__PRERENDERED_STATE__\s*=\s*("(?:[^"\\]|\\.)*")', html)
    if not m:
        return None
    try:
        data = json.loads(json.loads(m.group(1)))
        return data
    except Exception:
        return None


def find_ids(html: str):
    """Ищем category_id / region_id / city_id в состоянии страницы."""
    state = extract_state(html)
    if state:
        for d in walk(state):
            cat = d.get("category")
            loc = d.get("location")
            if isinstance(cat, dict) and isinstance(loc, dict) and cat.get("id"):
                city = (loc.get("city") or {}).get("id")
                region = (loc.get("region") or {}).get("id")
                if city and region:
                    return cat["id"], region, city

    # запасной вариант: регулярки по «размэскированному» тексту страницы
    text = html.replace('\\"', '"').replace("\\u0022", '"')

    def rx(*patterns):
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return int(m.group(1))
        return None

    return (
        rx(r'"category_id"\s*:\s*"?(\d+)', r'"categoryId"\s*:\s*"?(\d+)',
           r'"category"\s*:\s*\{\s*"id"\s*:\s*"?(\d+)'),
        rx(r'"region_id"\s*:\s*"?(\d+)', r'"regionId"\s*:\s*"?(\d+)',
           r'"region"\s*:\s*\{\s*"id"\s*:\s*"?(\d+)'),
        rx(r'"city_id"\s*:\s*"?(\d+)', r'"cityId"\s*:\s*"?(\d+)',
           r'"city"\s*:\s*\{\s*"id"\s*:\s*"?(\d+)'),
    )


def diagnose(html: str):
    """Сохраняем страницу и показываем куски вокруг ключевых слов."""
    with open("page.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\n--- ДИАГНОСТИКА (страница сохранена в page.html) ---")
    text = html.replace('\\"', '"')
    for marker in ["__PRERENDERED_STATE__", "__NEXT_DATA__", "category_id", "categoryId",
                   "cityId", "city_id", "regionId", "region_id", '"category"',
                   '"city"', "api/v1/offers", "application/ld+json"]:
        idx = [m.start() for m in re.finditer(re.escape(marker), text)]
        print(f"\n[{marker}] найдено: {len(idx)}")
        for i in idx[:2]:
            print("   ...", text[max(0, i - 80): i + 160].replace("\n", " "), "...")


def page_filters_to_api(page_url: str) -> list[tuple[str, str]]:
    """search[filter_float_price:to]=16000 -> filter_float_price:to=16000 и т.п."""
    out = []
    for key, val in parse_qsl(urlparse(page_url).query):
        m = re.fullmatch(r"search\[(.+?)\]((?:\[\d*\])?)", key)
        if m:
            out.append((m.group(1) + m.group(2), val))
        elif key == "currency":
            out.append((key, val))
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit('Использование: python find_api.py "<ссылка на страницу OLX>"')
    page_url = sys.argv[1]

    r = requests.get(page_url, headers=HEADERS, timeout=30, **IMPERSONATE)
    print("HTTP", r.status_code, "| размер страницы:", len(r.text))
    if r.status_code != 200:
        print("Ответ сервера (начало):\n", r.text[:500])
        sys.exit("OLX не пустил скрипт (см. подсказки в чате).")

    category_id, region_id, city_id = find_ids(r.text)
    print("category_id =", category_id, "| region_id =", region_id, "| city_id =", city_id)
    if not all((category_id, region_id, city_id)):
        diagnose(r.text)
        sys.exit(
            "\nНе удалось найти все ID. Пришлите в чат вывод «ДИАГНОСТИКИ» выше."
        )

    params = [
        ("offset", "0"),
        ("limit", "40"),
        ("category_id", str(category_id)),
        ("region_id", str(region_id)),
        ("city_id", str(city_id)),
        ("sort_by", "created_at:desc"),
    ] + page_filters_to_api(page_url)
    api_url = "https://www.olx.ua/api/v1/offers/?" + urlencode(params, safe=":")

    test = requests.get(api_url, headers=HEADERS, timeout=30, **IMPERSONATE)
    print("Проверка API: HTTP", test.status_code)
    if test.status_code == 200:
        offers = test.json().get("data", [])
        print(f"Объявлений в выдаче: {len(offers)}")
        for o in offers[:5]:
            print(" -", o.get("title"), "|", o.get("url"))

    print("\nВаш OLX_API_URL:\n")
    print(api_url)


if __name__ == "__main__":
    main()
