"""인수 테스트: AI Factory 규모 상세 청구서 5종이 정답 연간 원가의 ±1% 안에 들어오는가.
(설계 §4A-8 · samples-detailed/answer_key.json)"""
import importlib.util

import pytest

from app.parsing import extract
from tests.conftest import BUILDERS, ROOT

_spec = importlib.util.spec_from_file_location("score_detailed_samples", ROOT / "scripts" / "score_detailed_samples.py")
_score_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_score_mod)
score = _score_mod.score


@pytest.mark.xfail(reason="1순위 구현 중 — Task 9에서 표시를 지운다", strict=False)
@pytest.mark.parametrize("name", list(BUILDERS))
def test_detailed_sample_annual_cost_within_1pct(name, detailed, service):
    path, key = detailed[name]
    r = score(path, key, service)
    assert r["errors"] == []
    assert r["run_ratio"] == pytest.approx(1.0, abs=0.01)
    assert r["acq"][0] == pytest.approx(r["acq"][1], rel=0.01, abs=1)


@pytest.mark.xfail(reason="1순위 구현 중", strict=False)
def test_turning_off_interpretations_reproduces_todays_error(detailed, service):
    ratios = {n: score(*detailed[n], service, off_all=True)["run_ratio"] for n in detailed}
    assert ratios["bill_r5_quarterly_no_currency.xlsx"] == pytest.approx(3.0, rel=0.01)
    assert ratios["bill_r4_prorated_first_month_krw.xlsx"] == pytest.approx(0.553, rel=0.02)
    # 해석이 하나라도 있어야 '해제'가 의미 있다 (지금은 해석 기능이 없어 이 조건에서 실패)
    assert all(getattr(score(*detailed[n], service)["draft"], "interpretations", []) for n in detailed)


def test_original_samples_have_no_interpretations(service):
    for p in sorted((ROOT / "samples").iterdir()):
        d = service.ingest(extract(p.name, p.read_bytes()))
        assert getattr(d, "interpretations", []) == [] and getattr(d, "bench", []) == []
