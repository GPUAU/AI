import json

from app.docs import DocKind, extract_fields
from app.mapping.fields_llm import map_fields_llm
from app.privacy import mask_text


def _fake(answer: dict, sent: list):
    def complete(system, user, schema):
        sent.append(user)
        assert schema["properties"]["fields"]  # strict JSON 스키마를 함께 보낸다
        return json.dumps(answer)
    return complete


def test_masked_prompt_has_no_vendor_quote_site_or_amounts(dept_docs):
    sent = []
    map_fields_llm(dept_docs["01"], dept_docs["01"].rows, ["acquisition"], _fake({"fields": []}, sent))
    for bad in ("솔빛컴퓨팅", "SIM-261126-02", "울산", "1,009,000,000", "250,000,000", "01_구매팀"):
        assert bad not in sent[0]
    assert "acquisition" in sent[0] and "공급가액 합계" in sent[0]


def test_llm_answer_is_position_only_and_value_read_by_code(dept_docs):
    d = dept_docs["01"]
    out = map_fields_llm(d, d.rows, ["acquisition"], _fake({"fields": [{"key": "acquisition", "ref": "1쪽!10", "col": 3}]}, []))
    assert out == [("acquisition", "1쪽!10", 3)]
    # extract_fields에 넣으면 값은 코드가 그 칸에서 읽는다
    d2 = d.model_copy(update={"rows": [r for r in d.rows if r.row != 7]})  # 규칙이 못 찾는 상황을 만들려고 품목 줄 하나 뺌
    hits = extract_fields(d2, DocKind.MEMO, "01", complete=_fake(
        {"fields": [{"key": "acquisition", "ref": "1쪽!10", "col": 3}]}, []))
    acq = [h for h in hits if h.key == "acquisition" and h.role == "table"]
    assert acq and acq[0].value.number == 1_009_000_000 and acq[0].via == "llm"


def test_bogus_ref_or_empty_cell_is_dropped(dept_docs):
    d = dept_docs["01"]
    ans = {"fields": [{"key": "acquisition", "ref": "9쪽!99", "col": 3},   # 없는 줄
                      {"key": "acquisition", "ref": "1쪽!10", "col": 1},   # 빈 칸
                      {"key": "gpu_count", "ref": "1쪽!10", "col": 3},     # 요청하지 않은 칸
                      {"key": "acquisition", "ref": "1쪽!6", "col": 4}]}   # 숫자 없는 머리글
    assert map_fields_llm(d, d.rows, ["acquisition"], _fake(ans, [])) == []


def test_wrong_unit_answer_is_dropped(dept_docs):
    d = dept_docs["04"]  # LLM이 '5년' 칸을 취득비라고 답해도 단위가 맞지 않아 버린다
    hits = extract_fields(d.model_copy(update={"rows": [r for r in d.rows if r.row != 6]}), DocKind.MEMO, "04",
                          complete=_fake({"fields": [{"key": "acquisition", "ref": "1쪽!8", "col": 3}]}, []))
    assert not [h for h in hits if h.key == "acquisition"]


def test_mask_hides_company_quote_number_and_site():
    t = mask_text("솔빛컴퓨팅(가상 업체)의 견적 SIM-261126-02 울산 데이터센터 · 수납처 울산시설서비스(가상) · Example Colo Inc.", terms=[])
    for bad in ("솔빛컴퓨팅", "SIM-261126", "울산", "Example Colo"):
        assert bad not in t
    assert "견적" in t and "데이터센터" in t
