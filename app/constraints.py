"""Parse listing constraints ("3-bed villa in Riyadh under 1M SAR") into filters.

Filters only ever apply to Wasalt listing chunks; pages always pass. No match means no filter.
"""

import re
from dataclasses import asdict, dataclass

CITIES = {
    "riyadh": "Riyadh",
    "الرياض": "Riyadh",
    "jeddah": "Jeddah",
    "jiddah": "Jeddah",
    "جدة": "Jeddah",
    "dammam": "Dammam",
    "الدمام": "Dammam",
    "makkah": "Makkah",
    "mecca": "Makkah",
    "مكة": "Makkah",
    "madinah": "Madinah",
    "medina": "Madinah",
    "المدينة": "Madinah",
    "khobar": "Khobar",
    "الخبر": "Khobar",
}
TYPES = {
    "apartment": "Apartment",
    "flat": "Apartment",
    "شقة": "Apartment",
    "شقق": "Apartment",
    "villa": "Villa",
    "فيلا": "Villa",
    "فلل": "Villa",
    "land": "Land",
    "plot": "Land",
    "أرض": "Land",
    "اراضي": "Land",
    "أراضي": "Land",
    "building": "Building",
    "عمارة": "Building",
}
# Arabic proper nouns the English corpus spells in Latin script; added to the BM25 query.
NAMES = {
    "وصلت": "Wasalt",
    "دار جلوبال": "Dar Global",
    "دار غلوبال": "Dar Global",
    "دار الأركان": "Dar Al Arkan",
    "ترامب": "Trump",
    "دبي": "Dubai",
    "قطر": "Qatar",
    "الدوحة": "Doha",
    "عمان": "Oman",
    "مسقط": "Muscat",
    "إسبانيا": "Spain",
    "اسبانيا": "Spain",
    "لندن": "London",
    "السعودية": "Saudi Arabia",
    "الإمارات": "UAE",
    "رأس الخيمة": "Ras Al Khaimah",
}
SALE = re.compile(r"\b(?:sale|buy|buying|purchase)\b|للبيع|شراء")
RENT = re.compile(r"\b(?:rent|rental|renting|lease|to let)\b|للإيجار|للايجار|إيجار|ايجار")
AMOUNT = r"(\d+(?:[.,]\d+)*)\s*(k|m|mn|million|thousand|ألف|الف|مليون)?\b"
MULTIPLIER = {"k": 1e3, "thousand": 1e3, "ألف": 1e3, "الف": 1e3, "m": 1e6, "mn": 1e6}
MULTIPLIER |= {"million": 1e6, "مليون": 1e6}
CURRENCY = r"(?:sar\s+|ريال\s+)?"
BETWEEN = re.compile(rf"(?:between|from|بين)\s+{CURRENCY}{AMOUNT}\s*(?:and|to|-|و)\s*{AMOUNT}")
UNDER = r"under|below|less than|max(?:imum)?|up to|budget(?: of)?|أقل من|اقل من"
OVER = r"over|above|more than|at least|minimum|أكثر من|اكثر من"
MAX_PRICE = re.compile(rf"(?:{UNDER})\s+{CURRENCY}{AMOUNT}")
MIN_PRICE = re.compile(rf"(?:{OVER})\s+{CURRENCY}{AMOUNT}")
BEDS = re.compile(r"(\d+)\s*(\+)?\s*-?\s*(?:bed(?:room)?s?|br|bhk)\b|(\d+)\s*(?:غرف|غرفة)")
MIN_BEDS = re.compile(r"at least|minimum|or more")
EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


@dataclass(frozen=True)
class Filters:
    city: str | None = None
    purpose: str | None = None
    property_type: str | None = None
    bedrooms: int | None = None
    min_bedrooms: int | None = None
    min_price: float | None = None
    max_price: float | None = None

    def __bool__(self) -> bool:
        return any(v is not None for v in asdict(self).values())

    def describe(self) -> str:
        return ", ".join(
            f"{k}={v:,.0f}" if isinstance(v, float) else f"{k}={v}"
            for k, v in asdict(self).items()
            if v is not None
        )

    def matches(self, meta: dict) -> bool:
        """True if a listing's meta satisfies every set filter. Unknown values do not match."""
        for key in ("city", "purpose", "property_type"):
            if getattr(self, key) and meta.get(key) != getattr(self, key):
                return False
        beds, price = meta.get("bedrooms"), meta.get("price")
        if self.bedrooms is not None and beds != self.bedrooms:
            return False
        if self.min_bedrooms is not None and (beds is None or beds < self.min_bedrooms):
            return False
        if self.min_price is not None and (price is None or price < self.min_price):
            return False
        return not (self.max_price is not None and (price is None or price > self.max_price))


def amount(number: str, unit: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * MULTIPLIER.get((unit or "").lower(), 1)


def pattern(key: str) -> str:
    # English keys match whole words with an optional plural; Arabic keys match anywhere,
    # since Arabic attaches prefixes like "بال" ("in the") to the word.
    return rf"\b{re.escape(key)}s?\b" if key.isascii() else re.escape(key)


def first(table: dict, query: str) -> str | None:
    hits = [
        (m.start(), value) for key, value in table.items() if (m := re.search(pattern(key), query))
    ]
    return min(hits)[1] if hits else None


def parse(query: str) -> Filters:
    q = query.lower().translate(EASTERN_DIGITS)
    sale, rent = bool(SALE.search(q)), bool(RENT.search(q))
    purpose = "sale" if sale and not rent else "rent" if rent and not sale else None

    bedrooms = min_bedrooms = None
    if m := BEDS.search(q):
        n = int(m.group(1) or m.group(3))
        if m.group(2) or MIN_BEDS.search(q):
            min_bedrooms = n
        else:
            bedrooms = n

    min_price = max_price = None
    if m := BETWEEN.search(q):
        lo_n, lo_u, hi_n, hi_u = m.groups()
        # "between 500 and 800k": a bare lower bound takes the upper bound's unit
        min_price, max_price = amount(lo_n, lo_u or hi_u), amount(hi_n, hi_u)
    else:
        if m := MAX_PRICE.search(q):
            max_price = amount(*m.groups())
        if m := MIN_PRICE.search(q):
            min_price = amount(*m.groups())

    return Filters(
        city=first(CITIES, q),
        purpose=purpose,
        property_type=first(TYPES, q),
        bedrooms=bedrooms,
        min_bedrooms=min_bedrooms,
        min_price=min_price,
        max_price=max_price,
    )


def english_terms(query: str) -> list[str]:
    """English spellings of Arabic names and city words in the query, for lexical search."""
    return [english for arabic, english in NAMES.items() if arabic in query]


def follow_up(current: Filters, previous: Filters) -> Filters:
    """ "what about Jeddah?" after "villas for sale in Riyadh" -> villas for sale in Jeddah.

    The previous turn fills gaps only when the current turn sets a filter of its own, so an
    unrelated question ("who is the CEO?") does not inherit listing filters.
    """
    if not current:
        return current
    merged = {k: v if v is not None else getattr(previous, k) for k, v in asdict(current).items()}
    return Filters(**merged)
