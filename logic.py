import re

INCOME_CAT = "💰 Доход"
OTHER_CAT = "📦 Другое"

CATEGORIES = [
    ("🍔 Еда", ("еда", "їжа", "кофе", "кава", "обед", "обід", "ужин", "вечеря", "завтрак", "сніданок",
                "пицц", "кафе", "ресторан", "продукт", "магазин", "сильпо", "сільпо", "атб", "суши",
                "бургер", "чай", "перекус", "доставк", "шаурм", "вода")),
    ("🚕 Транспорт", ("такси", "таксі", "метро", "автобус", "бензин", "проезд", "проїзд", "маршрутк",
                      "парковк", "uber", "bolt", "поезд", "потяг", "билет", "квиток")),
    ("🎮 Развлечения", ("кино", "кіно", "игр", "гра", "бар", "клуб", "концерт", "подписк", "підписк",
                        "netflix", "spotify", "steam", "стим", "боулинг", "вечеринк")),
    ("🛍 Покупки", ("одежд", "одяг", "обув", "взуття", "кроссовк", "куртк", "подарок", "подарунок",
                    "озон", "wildberries", "amazon", "техник", "телефон")),
    ("🏠 Дом", ("аренд", "оренд", "квартир", "свет", "світло", "газ", "интернет", "інтернет",
                "коммунал", "комунал", "ремонт", "мебел", "меблі")),
    ("💊 Здоровье", ("аптек", "врач", "лікар", "лекарств", "ліки", "зал", "спорт", "стоматолог", "тренировк")),
    ("📚 Учёба", ("курс", "книг", "учёб", "навчан", "обучен", "репетитор")),
    (OTHER_CAT, ()),
]
EXPENSE_CATS = [c for c, _ in CATEGORIES]

_CURR = {"грн", "uah", "руб", "р", "rub", "usd", "eur", "$", "₴", "€", "₽", "zł", "pln"}
_AMOUNT = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d{1,2})?)\s?([кk])?(?!\w)", re.I)


def guess_category(note: str) -> str:
    tokens = re.findall(r"\w+", note.lower())
    for cat, kws in CATEGORIES:
        if kws and any(t.startswith(kws) for t in tokens):
            return cat
    return OTHER_CAT


def parse_entry(text: str):
    """'кофе 150' -> ('expense', 150.0, 'кофе', '🍔 Еда'); '+50к зарплата' -> income."""
    t = text.strip()
    kind = "expense"
    if t[:1] == "+":
        kind, t = "income", t[1:].strip()
    elif t[:1] in ("-", "−"):
        t = t[1:].strip()
    ms = list(_AMOUNT.finditer(t))
    if not ms:
        return None
    m = ms[-1]
    amount = float(m.group(1).replace(",", "."))
    if m.group(2):
        amount *= 1000
    if amount <= 0 or amount > 1e9:
        return None
    note = (t[:m.start()] + " " + t[m.end():]).strip(" -–—,.")
    note = " ".join(w for w in note.split() if w.lower().strip(".") not in _CURR)
    note = re.sub(r"\s+", " ", note) or "Без описания"
    cat = INCOME_CAT if kind == "income" else guess_category(note)
    return kind, amount, note, cat


def fmt(x: float) -> str:
    if abs(x - round(x)) < 0.005:
        s = f"{int(round(x)):,}"
    else:
        s = f"{x:,.2f}"
    return s.replace(",", "\u00a0")


def bar(pct: float, width: int = 10) -> str:
    n = max(0, min(width, round(pct / 100 * width)))
    return "█" * n + "░" * (width - n)
