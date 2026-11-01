import pytest

from evals.common import parse_json, percentile
from evals.redteam import PHONE, digits
from evals.retrieval_eval import satisfies


def test_parse_json_finds_the_object_inside_prose_and_fences():
    reply = 'Here is my grade:\n```json\n{"pass": true, "why": "ok"}\n```'
    assert parse_json(reply) == {"pass": True, "why": "ok"}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_percentile():
    assert percentile([3, 1, 2], 0.5) == 2
    assert percentile([1, 2, 3, 4, 100], 0.95) == 100


def test_phone_numbers_compared_by_digits():
    [found] = PHONE.findall("Call +971 4 247 8999 today")
    assert digits(found) == "97142478999"


def test_golden_matching_is_case_insensitive_and_anchors_with_dollar():
    chunk = {
        "url": "https://darglobal.co.uk/press/Trump-International-Oman-in-AIDA",
        "kind": "page",
    }
    assert satisfies(chunk, {"urls": ["aida"]})
    assert not satisfies(
        {"url": "https://wasalt.sa/en/corporate", "kind": "page"}, {"urls": ["wasalt.sa/en$"]}
    )
    listing = {"url": "u", "kind": "listing", "meta": {"city": "Dammam", "purpose": "sale"}}
    assert satisfies(listing, {"listing": {"city": "Dammam"}})
    assert not satisfies(listing, {"listing": {"city": "Riyadh"}})
