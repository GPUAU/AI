import pytest

from app.docs import DocKind, classify_doc
from tests.conftest import BUILDERS, doc_from_rows


@pytest.mark.parametrize("code,kind", [("01", "QUOTE"), ("02", "MEMO"), ("03", "BILL"), ("04", "MEMO"),
                                       ("05", "MEMO"), ("06", "MEMO"), ("07", "MEMO"), ("08", "PLAN")])
def test_dept_doc_kinds(dept_docs, code, kind):
    assert classify_doc(dept_docs[code])[0].value == kind


def test_title_beats_body_mentions(dept_docs):
    # 02는 본문에 '견적'이 나오지만 제목은 '명세' → MEMO, 근거에 제목이 보인다
    kind, why = classify_doc(dept_docs["02"])
    assert kind == DocKind.MEMO and "명세" in why


@pytest.mark.parametrize("name", list(BUILDERS))
def test_detailed_bills_are_bills(detailed_doc, name):
    assert classify_doc(detailed_doc(name))[0] == DocKind.BILL


def test_sample_bills_are_bills(samples_all):
    assert all(classify_doc(d)[0] == DocKind.BILL for d in samples_all)


def test_unknown_doc_falls_back_to_bill_with_notice():  # Review Focus 4
    k, why = classify_doc(doc_from_rows([["", "1,000"]]))
    assert k == DocKind.BILL and "확인" in why
