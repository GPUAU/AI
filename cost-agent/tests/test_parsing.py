import pytest

from app.models import Currency
from app.parsing import UnsupportedFile, extract, parse_number


@pytest.mark.parametrize("text,expected", [
    ("1,234,000", 1234000), ("₩1,234,000", 1234000), ("1,234,000원", 1234000),
    ("$12.50", 12.5), ("(5,000)", -5000), ("-500,000", -500000),
    ("2026-10", None), ("40kW", None), ("", None), ("합계", None),
])
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_extract_xlsx_reads_numbers_and_currency(doc):
    d = doc("a")
    assert d.currency_hint == Currency.KRW
    row = next(r for r in d.rows if "전력 사용료" in r.label)
    assert row.numbers[5] == 28_800 * 180


def test_extract_csv():
    d = extract("bill.csv", "항목,금액(원)\n전력 사용료,\"1,000,000\"\n합계,\"1,000,000\"\n".encode())
    assert d.rows[1].numbers == {1: 1_000_000}


def test_rejects_other_types():
    with pytest.raises(UnsupportedFile):
        extract("bill.pdf", b"%PDF")
    with pytest.raises(UnsupportedFile):
        extract("bill.xlsx", b"not a zip")
