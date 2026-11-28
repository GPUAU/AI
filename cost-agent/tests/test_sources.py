"""가정값 출처 등급 (설계 §4B-2)."""
from app.agent.sources import grade_of


def test_grades():
    assert grade_of("fx_krw_per_usd", "한국수출입은행 2026-11-23 기준 (BE 환율 API)") == "공개자료"
    assert grade_of("depreciation_years", "제안값 5년: 미래에셋 「AI Factory의 경제학」(2026-05) IT 장비 가정") == "리포트 가정"
    assert grade_of("acquisition_amount", "예시값 · 현대차증권 표 18 GPU 단가 $38,000 · 견적서 값으로 바꾸세요") == "예시값"
    assert grade_of("target_rate", "담당자 입력") == "담당자 입력"
    assert grade_of("pricing_method", "") == "예시값"
