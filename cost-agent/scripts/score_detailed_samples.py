"""samples-detailed/ 청구서를 지금 분석기로 읽고 정답표와 비교한다.

    python scripts/score_detailed_samples.py            # 키워드 규칙 분류 (LLM 호출 없음)
    python scripts/score_detailed_samples.py --llm      # 설정된 LLM 분류 (금액은 가려서 전송)

줄 분류 정확도, 누락 탐지, 총액 대조, 항목별 연간 원가 오차를 표로 낸다.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import build_service  # noqa: E402
from app.mapping.rules import RulesMapper  # noqa: E402
from app.mapping.templates import TemplateStore  # noqa: E402
from app.models import ItemStatus  # noqa: E402
from app.parsing import extract  # noqa: E402
from app.service import BillService  # noqa: E402

DIR = ROOT / "samples-detailed"


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def find(draft, sheet: str, label: str):
    want = _norm(label)
    # PDF에는 엑셀 시트 이름이 없으므로 항목명으로만 찾는다
    same_sheet = [l for l in draft.lines if sheet is None or l.ref.split("!")[0] == sheet]
    for l in same_sheet:  # 같은 이름이 먼저 (머리글 '공급가액 부가세 합계'가 '합계' 줄을 가로채지 않게)
        if _norm(l.label) == want:
            return l
    for l in same_sheet:
        if want in _norm(l.label) and l.amount is not None:
            return l
    return None


def score(path: Path, key: dict, service: BillService, off_all: bool = False, ocr=None) -> dict:
    """청구서 한 장을 읽고 정답표와 비교한다. off_all=True면 에이전트의 청구서 해석을 모두 해제한 결과를 잰다."""
    name = path.name
    is_pdf = path.suffix.lower() == ".pdf"
    draft = service.ingest(extract(name, path.read_bytes(), ocr=ocr))
    interps = getattr(draft, "interpretations", [])
    if off_all and interps:
        draft = service.set_interpretations(draft.id, {i.code for i in interps})
    ok = wrong = 0
    errors = []
    for k in key["lines"]:
        got = find(draft, None if is_pdf else k["sheet"], k["label"])
        exp = (k["role"], k["item_type"]) if k["role"] == "COST" else (k["role"], None)
        act = (got.role.value, got.item_type.value if got and got.item_type and got.role.value == "COST" else None) if got else ("없음", None)
        # 합계·소계·세금 줄은 역할만 본다
        same = act == exp if k["role"] == "COST" else act[0] == exp[0]
        # 1회성으로 읽혔으면 합칠 때 취득비로 옮겨지므로 정답으로 본다
        if not same and exp == ("COST", "ACQUISITION") and got and got.period and got.period.value == "ONE_TIME":
            same = True
        if same:
            ok += 1
        else:
            wrong += 1
            errors.append(f"{k['label'][:28]}: 정답 {exp[1] or exp[0]} → {act[1] or act[0]}")
    # 항목별 연간 원가 (원화는 원, 달러는 달러). 분석기 결과는 청구서 단위 그대로라 단위 환산 오류도 드러난다.
    got_annual = {c.item_type.value: (c.amount or 0) for c in draft.cost_items if c.status == ItemStatus.USER_INPUT}
    exp_annual = key["expected_annual"]
    exp_run = sum(v for t, v in exp_annual.items() if t != "ACQUISITION")
    got_run = sum(v for t, v in got_annual.items() if t != "ACQUISITION")
    missing_flag = sorted(set(m.value for m in draft.missing_items))
    return {
        "name": name, "mapper": draft.mapper,
        "currency": draft.currency.value if draft.currency else None, "currency_exp": key["currency"],
        "period": draft.period.value if draft.period else None, "period_exp": key["period"],
        "lines": f"{ok}/{ok + wrong}", "errors": errors,
        "total": draft.reconciliation.basis,
        "missing": missing_flag, "missing_exp": key.get("missing_expected", []),
        "run_ratio": (got_run / exp_run) if exp_run else None,
        "acq": (got_annual.get("ACQUISITION", 0), exp_annual.get("ACQUISITION", 0)),
        "draft": draft,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    args = ap.parse_args()
    key = json.loads((DIR / "answer_key.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix="score-templates-"))
    for off_all, title in ((False, "해석 적용"), (True, "해석 모두 해제")):
        rows = []
        for name in key:
            service = build_service(Settings.from_env()) if args.llm else BillService(RulesMapper(), TemplateStore(tmp))
            rows.append(score(DIR / name, key[name], service, off_all=off_all))
        print(f"## {title}\n")
        _print(rows)


def _print(rows: list[dict]) -> None:
    print("| 샘플 | 줄 분류 | 통화 (정답) | 기간 (정답) | 총액 대조 | 누락 표시 (정답) | 연간 운영원가 / 정답 | 1회성 (얻음/정답) |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        ratio = f"{r['run_ratio']:.3g}배" if r["run_ratio"] is not None else "-"
        print(f"| {r['name']} | {r['lines']} | {r['currency']} ({r['currency_exp']}) | {r['period']} ({r['period_exp']}) | "
              f"{r['total']} | {', '.join(r['missing']) or '-'} ({', '.join(r['missing_exp']) or '-'}) | {ratio} | "
              f"{r['acq'][0]:,.0f} / {r['acq'][1]:,.0f} |")
    print()
    for r in rows:
        if r["errors"]:
            print(f"**{r['name']}** ({r['mapper']}) 분류 오류")
            for e in r["errors"]:
                print(f"- {e}")
            print()


if __name__ == "__main__":
    main()
