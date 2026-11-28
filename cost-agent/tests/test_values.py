import math

import pytest

from app.values import parse_value, same_value


@pytest.mark.parametrize("text,num,unit,cur", [
    ("32장", 32, "장", None), ("5년", 5, "년", None), ("70%", 0.7, "%", None),
    ("1,009,000,000원", 1_009_000_000, "원", "KRW"), ("1USD=1,350KRW", 1350, "KRW/USD", "KRW"),
    ("1 USD = 1,350 KRW", 1350, "KRW/USD", "KRW"),
    ("약 1,885원", 1885, "원", "KRW"), ("약1.74USD", 1.74, "USD", "USD"), ("280,320 GPU·시간", 280320, "GPU·시간", None),
    ("GPU 32장", 32, "장", None), ("4대", 4, "대", None), ("24,000kWh", 24000, "kWh", None)])
def test_parse_value(text, num, unit, cur):
    v = parse_value(text)
    assert v is not None and v.number == pytest.approx(num) and v.unit == unit and v.currency == cur


def test_model_name():
    v = parse_value("H100 80GB")
    assert v.text == "H100 80GB" and v.unit == "model" and math.isnan(v.number)


def test_plain_text_is_not_a_value():
    assert parse_value("정액법") is None and parse_value("") is None


def test_units_normalize_for_comparison():  # Review Focus 2
    assert same_value(parse_value("1,009,000,000원"), parse_value("10.09억원"))
    assert same_value(parse_value("5년"), parse_value("60개월"))
    assert same_value(parse_value("1,000만원"), parse_value("10,000,000원"))
    assert not same_value(parse_value("1,009,000,000원"), parse_value("1,100,000,000원"))
    assert not same_value(parse_value("5년"), parse_value("5장"))


def test_dept_pdf_spaces_are_not_control_chars(dept_docs):
    assert all("\x01" not in c for d in dept_docs.values() for r in d.rows for c in r.cells)
    assert dept_docs["02"].rows[6].cells[-1] == "GPU 32장"
