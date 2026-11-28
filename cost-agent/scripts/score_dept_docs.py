"""부서별 목데이터 인수 채점 — 기준표 칸 정확도, 사업기획팀 검산 대비, LLM 전송 내용 마스킹 누출.

    python scripts/score_dept_docs.py            → samples-depts/*.pdf 채점 결과 출력

검산 대비는 BE 없이 기준표만으로 계산한다 (감가상각 정액 + 시설 + 운영비) ÷ (GPU × 8,760h × 가동률).
BE 원가 계산 결과와의 대조는 에이전트(SIMULATE 단계)가 따로 한다.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.docs import DocKind, classify_doc, extract_fields  # noqa: E402
from app.mapping.fields_llm import build_prompt  # noqa: E402
from app.mapping.llm import build_user_prompt  # noqa: E402
from app.mapping.rules import RulesMapper  # noqa: E402
from app.mapping.templates import TemplateStore  # noqa: E402
from app.costsheet import FIELDS  # noqa: E402
from app.parsing import extract  # noqa: E402
from app.service import BillService  # noqa: E402
from app.sheetmerge import FACILITY_KEYS, build_sheet, cross_checks  # noqa: E402

BANNED = ["솔빛컴퓨팅", "SIM-261126-02", "SIM-OPS-1026", "울산"]


def _same(a, b) -> bool:
    if isinstance(b, (int, float)) and isinstance(a, (int, float)):
        return abs(a - b) <= 0.005 * abs(b) if b else a == b
    return str(a).replace(" ", "") == str(b).replace(" ", "")


def leaks(docs) -> list[str]:
    """LLM에 보낼 내용(칸 위치 매핑·청구서 분류)에 회사명·견적번호·사업장·금액·파일명이 있으면 목록으로."""
    keys = [f.key for f in FIELDS if f.kind != "result"]
    out = []
    for d in docs:
        rows = [r for r in d.rows if r.numbers]
        for prompt in (build_prompt(d, d.rows, keys), build_user_prompt(d)):
            for bad in BANNED + [d.file_name]:
                if bad in prompt:
                    out.append(f"{d.file_name}: '{bad}'")
            for r in rows:
                for v in r.numbers.values():
                    if abs(v) >= 1000 and (f"{v:,.0f}" in prompt or f"{v:.0f}" in prompt):
                        out.append(f"{d.file_name}: 금액 {v:,.0f}")
    return sorted(set(out))


def score(docs, key: dict, workdir: Path) -> dict:
    svc = BillService(RulesMapper(), TemplateStore(workdir))
    hits, bills = [], []
    for d in docs:
        kind, _ = classify_doc(d)
        hits += extract_fields(d, kind, d.file_name)
        if kind == DocKind.BILL:
            bills.append((d.file_name, svc.ingest(d)))
    sheet = build_sheet(hits, bills)
    c = sheet.cells
    wrong = {k: (c[k].value, v) for k, v in key["cells"].items() if c[k].status != "filled" or not _same(c[k].value, v)}
    num = lambda k: float(c[k].value) if c[k].status == "filled" else 0.0  # noqa: E731
    annual = num("acquisition") / max(num("depreciation_years"), 1) + sum(num(k) for k in FACILITY_KEYS) + num("operations")
    hours = num("gpu_count") * 8760 * num("utilization")
    hourly = annual / hours if hours else 0.0
    plan = num("plan_hourly_cost")
    return {"accuracy": 1 - len(wrong) / len(key["cells"]), "wrong": wrong, "plan_hourly_krw": round(hourly, 2),
            "plan_ok": bool(plan) and abs(hourly - plan) <= 0.01 * plan, "leaks": leaks(docs),
            "cross_checks": cross_checks(sheet)}


def main() -> None:
    d = ROOT / "samples-depts"
    docs = [extract(p.name, p.read_bytes()) for p in sorted(d.glob("*.pdf"))]
    key = json.loads((d / "answer_key.json").read_text(encoding="utf-8"))
    r = score(docs, key, Path(tempfile.mkdtemp()))
    print(f"기준표 칸 정확도 {r['accuracy']:.0%} ({len(key['cells']) - len(r['wrong'])}/{len(key['cells'])})")
    print(f"검산 대비 시간당 원가 {r['plan_hourly_krw']:,.0f}원 · 사업기획팀 {key['plan']['plan_hourly_cost']:,}원 — "
          f"{'일치' if r['plan_ok'] else '차이'}")
    print(f"마스킹 누출 {len(r['leaks'])}건", *r["leaks"], sep="\n  ")
    print("교차 확인", *r["cross_checks"], sep="\n  ")


if __name__ == "__main__":
    main()
