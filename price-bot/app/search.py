"""Поиск по каталогу: «15 про 256», «айфон 16 pro max», «macbook air m3»."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Как клиенты пишут по-русски → как написано в прайсе
SYNONYMS = {
    "айфон": "iphone", "айфоны": "iphone", "ифон": "iphone", "про": "pro", "макс": "max", "мини": "mini",
    "плюс": "plus", "гб": "gb", "тб": "tb", "макбук": "macbook", "мак": "mac", "айпад": "ipad",
    "аирподс": "airpods", "эйрподс": "airpods", "аирпотс": "airpods", "эпл": "apple", "эппл": "apple",
    "вотч": "watch", "самсунг": "samsung", "галакси": "galaxy", "дайсон": "dyson", "сяоми": "xiaomi",
    "ксиоми": "xiaomi", "редми": "redmi", "пиксель": "pixel", "хонор": "honor", "хуавей": "huawei",
    "плейстейшн": "playstation", "пс": "ps", "соньи": "sony", "сони": "sony", "эйр": "air", "ультра": "ultra",
}
TOKEN_RE = re.compile(r"\d+|[^\W\d_]+")
MAX_RESULTS = 15


def tokens(text: str) -> list[str]:
    """«iPhone15Pro 256ГБ» → ['iphone', '15', 'pro', '256', 'gb']"""
    found = TOKEN_RE.findall(text.lower().replace("ё", "е"))
    return [SYNONYMS.get(t, t) for t in found]


@dataclass
class Hit:
    kind: str        # "item" — позиция прайса, "used" — б/у устройство
    id: int
    label: str
    price: int
    path: str
    score: int


def score(query: list[str], haystack: list[str]) -> int | None:
    """Каждое слово запроса должно быть началом какого-то слова товара; точное совпадение ценится выше."""
    total = 0
    for q in query:
        if q in haystack:
            total += 2
        elif any(h.startswith(q) for h in haystack):
            total += 1
        else:
            return None
    return total


def search(db, query: str) -> tuple[list[Hit], int]:
    """(лучшие совпадения, сколько нашлось всего)."""
    q = tokens(query)
    if not q:
        return [], 0
    hits = []
    for cat in db.categories():
        for it in db.items(cat["id"]):
            s = score(q, tokens(f"{cat['title']} {it['section']} {it['name']} {it['note']}"))
            if s is not None:
                path = cat["title"]
                if it["section"] and it["section"].lower() != cat["title"].lower():
                    path += f" › {it['section']}"
                label = " ".join(p for p in (it["name"], it["note"]) if p)
                hits.append(Hit("item", it["id"], label, db.sell_price(it, cat), path, s))
    for r in db.used_items():
        s = score(q, tokens(f"бу б/у used {r['kind']} {r['model']}"))
        if s is not None:
            hits.append(Hit("used", r["id"], f"{r['model']} (б/у)", r["price"], f"♻️ Б/у › {r['kind']}", s + 1))
    # сначала самые точные, среди равных — дешевле
    hits.sort(key=lambda h: (-h.score, h.price))
    return hits[:MAX_RESULTS], len(hits)
