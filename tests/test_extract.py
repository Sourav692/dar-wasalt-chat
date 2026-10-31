import json
from copy import deepcopy

import pytest

from crawler.extract import clean_listing

# Synthetic card with the same shape as a Wasalt __NEXT_DATA__ listing. PII values are fake.
CARD = {
    "id": 5898667,
    "propertyInfo": {
        "title": "Apartment 196.09 SQM with 4 Bedrooms",
        "slug": "apartment-19609-sqm-with-4-bedrooms-5898667",
        "propertyFor": "sale",
        "salePrice": 680000,
        "expectedRent": "",
        "rentFreq": "",
        "currencyType": "SAR",
        "city": "Riyadh",
        "district": "Dirab",
        "territory": "West Riyadh",
        "propertySubType": "Apartment",
        "managedBy": "Fake Agent Name",
    },
    "attributes": [
        {"key": "noOfBedrooms", "name": "Bedrooms", "value": 4},
        {"key": "builtUpArea", "name": "Built-Up Area", "value": "196.09", "unit": "SQM"},
    ],
    "contactDetails": {"phoneNumber": "500000001", "whatsappNumber": "500000001"},
    "propertyOwner": {"resEmployee": "Fake Owner", "regaAdvLicNo": "7200000000"},
    "leadContactInfo": {"ownerName": "Fake Owner"},
}


def test_listing_url_and_core_fields():
    item = clean_listing(CARD, "2026-10-31")
    assert item["url"] == (
        "https://wasalt.sa/en/property/sale/apartment-19609-sqm-with-4-bedrooms-5898667"
    )
    assert item["meta"]["price"] == 680000
    assert item["meta"]["city"] == "Riyadh"
    assert item["meta"]["bedrooms"] == 4
    assert "Riyadh" in item["text"] and "4" in item["text"]


def test_no_personal_data_leaks():
    blob = json.dumps(clean_listing(CARD, "2026-10-31"), ensure_ascii=False)
    for secret in ("500000001", "Fake Owner", "Fake Agent Name", "7200000000"):
        assert secret not in blob


def test_card_without_slug_is_dropped():
    card = {**CARD, "propertyInfo": {**CARD["propertyInfo"], "slug": None}}
    assert clean_listing(card, "2026-10-31") is None


def test_rent_uses_expected_rent_and_preserves_period():
    card = deepcopy(CARD)
    card["propertyInfo"].update(
        propertyFor="rent", expectedRent="45,000", rentFreq="Monthly", currencyType="sar"
    )
    item = clean_listing(card, "2026-10-31")
    assert item["meta"]["price"] == 45000
    assert item["meta"]["currency"] == "SAR"
    assert item["meta"]["purpose"] == "rent"
    assert item["meta"]["rent_frequency"] == "monthly"
    assert "/rent/" in item["url"]
    assert "monthly" in item["text"]
    assert "680000" not in json.dumps(item)


@pytest.mark.parametrize("purpose", ["sale", "rent"])
def test_missing_price_never_falls_back_to_other_purpose(purpose):
    card = deepcopy(CARD)
    card["propertyInfo"].update(propertyFor=purpose, salePrice=100000, expectedRent=30000)
    card["propertyInfo"]["salePrice" if purpose == "sale" else "expectedRent"] = None
    item = clean_listing(card, "2026-10-31")
    assert "price" not in item["meta"]
    assert "Asking price" not in item["text"]
    assert "Rent:" not in item["text"]


@pytest.mark.parametrize("period", [None, "", "Call Fake Agent Name"])
def test_unknown_rent_period_is_not_assumed(period):
    card = deepcopy(CARD)
    card["propertyInfo"].update(propertyFor="rent", expectedRent=30000, rentFreq=period)
    item = clean_listing(card, "2026-10-31")
    assert "rent_frequency" not in item["meta"]
    assert "period unspecified" in item["text"]
    assert "Fake Agent Name" not in json.dumps(item)


@pytest.mark.parametrize("price", [0, -1, True, "NaN", float("inf"), "1,23", {}, []])
def test_invalid_prices_are_omitted(price):
    card = deepcopy(CARD)
    card["propertyInfo"]["salePrice"] = price
    item = clean_listing(card, "2026-10-31")
    assert "price" not in item["meta"]
    json.dumps(item, allow_nan=False)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("propertyFor", "lease"),
        ("city", ""),
        ("propertySubType", None),
        ("slug", "../contact"),
        ("slug", "a?phone=500000001"),
        ("city", {"name": "Riyadh"}),
    ],
)
def test_incomplete_or_invalid_cards_are_dropped(key, value):
    card = deepcopy(CARD)
    card["propertyInfo"][key] = value
    assert clean_listing(card, "2026-10-31") is None


@pytest.mark.parametrize("card", [None, [], {}, {"propertyInfo": []}])
def test_malformed_cards_are_dropped(card):
    assert clean_listing(card, "2026-10-31") is None


def test_only_allowlisted_structured_fields_reach_output():
    card = deepcopy(CARD)
    card["propertyInfo"].update(title="Call 500000001", description="Fake Owner", rentFreq="daily")
    card["attributes"].append({"key": "phoneNumber", "value": "500000001"})
    item = clean_listing(card, "2026-10-31")
    assert set(item["meta"]) == {
        "purpose",
        "city",
        "district",
        "territory",
        "property_type",
        "price",
        "currency",
        "bedrooms",
        "area_sqm",
    }
    assert item["meta"]["area_sqm"] == 196.09
    assert "500000001" not in json.dumps(item)
    assert "Fake Owner" not in json.dumps(item)


def test_optional_attributes_and_units():
    card = deepcopy(CARD)
    card["attributes"] = [
        None,
        {"key": "noOfBedrooms", "value": "0"},
        {"key": "builtUpArea", "value": 1000, "unit": "sqft"},
    ]
    item = clean_listing(card, "2026-10-31")
    assert item["meta"]["bedrooms"] == 0
    assert "area_sqm" not in item["meta"]
    card["attributes"] = None
    assert clean_listing(card, "2026-10-31") is not None


def test_fractional_bedrooms_are_omitted():
    card = deepcopy(CARD)
    card["attributes"] = [{"key": "noOfBedrooms", "value": 2.5}]
    assert "bedrooms" not in clean_listing(card, "2026-10-31")["meta"]


def test_clean_text_fixes_extraction_artifacts():
    from crawler.extract import clean_text

    raw = (
        "- Property TypeSeafront Residences\n- Area (sqm)70 to 365\n"
        "<u>Oman</u> &amp;amp; UK\n\\_\\_\\_\\_\n"
        "We use cookies to enhance your browsing experience\nuse of cookies"
    )
    assert (
        clean_text(raw)
        == "- Property Type: Seafront Residences\n- Area (sqm): 70 to 365\nOman & UK"
    )


def test_boilerplate_lines_removed_across_pages():
    from crawler.extract import BOILERPLATE_MIN_DOCS, drop_boilerplate

    body = "Unique content line that is long enough to stay in the doc. " * 5
    docs = [{"text": f"We use cookies.\n{body}{i}"} for i in range(BOILERPLATE_MIN_DOCS)]
    out = drop_boilerplate(docs)
    assert len(out) == BOILERPLATE_MIN_DOCS
    assert all("cookies" not in d["text"] for d in out)
