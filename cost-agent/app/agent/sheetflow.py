"""여러 부서 문서 세션 — 원가 기준표를 만들고 문제 칸(빈 핵심 칸·충돌)만 담당자에게 묻는다.

CostAgent(워크플로)와 GraphCostAgent(LangGraph)가 함께 쓴다. 청구서 한 장 세션(s.sheet is None)에는 관여하지 않는다.
"""

from __future__ import annotations

import re
import uuid

from ..benchmarks import power_per_gpu_check
from ..costsheet import SPEC, Source, assumption_values, to_cost_items
from ..docs import UNKNOWN_NOTICE, DocKind, classify_doc, extract_fields
from ..models import BillDraft, CostItemDraft, Currency, ExtractedDocument, IssueLevel, ItemStatus, ItemType, Period, Reconciliation
from ..sheetmerge import INHOUSE, build_sheet, cross_checks

from ..values import Value, same_value
from .models import AgentSession, Answer, FieldSpec, Option, Question, Status, Step

# 비면 원가를 믿을 수 없는 칸 (청구서 핵심 4개 + 취득비). rack은 rack·rent 둘 다 비었을 때만
CORE_CELLS = ("electricity", "rack", "network", "operations", "acquisition")
SHEET_TITLE = "기준표 확인"
MANUAL = "담당자 입력"


class SheetFlow:
    """CostAgent에 섞어 쓰는 기준표 단계. self._docs·self.bills·self.backend·self._log·self._board·self._qid를 쓴다."""

    # ------------------------------------------------------------------ 준비
    def _prepare_docs(self, s: AgentSession, docs: list[ExtractedDocument]) -> None:
        """문서 종류를 정하고 칸을 뽑는다. 청구서 한 장이면 아무것도 하지 않는다 (기존 흐름)."""
        kinds = [(d, *classify_doc(d)) for d in docs]
        if len(docs) == 1 and kinds[0][1] == DocKind.BILL:
            self._docs[s.id] = docs[0]
            return
        complete = getattr(self, "fields_completion", None)
        hits = []
        bills = [d for d, k, _ in kinds if k == DocKind.BILL]
        for d, kind, why in kinds:
            s.docs.append({"name": d.file_name, "kind": kind.value, "reason": why})
            hits += extract_fields(d, kind, d.file_name, complete=complete)
            self._log(s, Step.ANALYZE, "result", f"{d.file_name}: {_KIND_KO[kind]} ({why})")
            if UNKNOWN_NOTICE in why:
                self._log(s, Step.ANALYZE, "warn", f"{d.file_name}: {UNKNOWN_NOTICE} — 청구서로 읽었어요")
        self._hits[s.id] = hits
        s.sheet_hits = [_dump_hit(h) for h in hits]
        if bills:
            s.primary_bill = bills[0].file_name
            self._docs[s.id] = bills[0]
            for d in bills[1:]:
                draft = self.bills.ingest(d)
                s.extra_bills.append((d.file_name, draft))
                # 두 번째부터의 청구서는 해석 카드를 따로 띄우지 않으므로, 문제는 경고로 보이고 기준표 알림에 남긴다
                problems = [i.message for i in draft.issues
                            if i.level != IssueLevel.INFO and i.code not in ("MISSING_CORE_ITEM", "ACQUISITION_OUTSIDE_BILL")]
                problems += [f"해석 확인 필요: {x.message}" for x in draft.interpretations] + [b.message for b in draft.bench]
                entry = next(x for x in s.docs if x["name"] == d.file_name)
                entry["issues"] = problems
                for msg in problems:
                    self._log(s, Step.ANALYZE, "warn", f"{d.file_name}: {msg}")
        else:  # 청구서가 없으면 빈 초안 (분석 단계 질문 없이 기준표로)
            self._docs[s.id] = docs[0]
            s.draft = BillDraft(id=uuid.uuid4().hex[:12], file_name="", fingerprint="", mapper="rules",
                                currency=Currency.KRW, lines=[], reconciliation=Reconciliation(cost_sum=0),
                                cost_items=[], issues=[], needs_review=False)
        s.sheet = build_sheet(hits, [])
        STEP_KO = {b.key: b for b in s.board}
        STEP_KO[Step.MISSING].title = SHEET_TITLE

    # ------------------------------------------------------------------ 기준표
    def _refresh_sheet(self, s: AgentSession) -> None:
        bills = ([(s.primary_bill, s.draft)] if s.primary_bill and s.draft else []) + list(s.extra_bills)
        if s.public_fx is None:
            try:
                fx = self.backend.latest_fx()
                s.public_fx = f"{float(fx['rate']):,.0f}|한국수출입은행 {fx.get('referenceDate', '')}" if fx else ""
            except Exception:  # noqa: BLE001 - 공개값이 없으면 사내 값만
                s.public_fx = ""
        public = {}
        if s.public_fx:
            rate, _, src = s.public_fx.partition("|")
            public["fx_krw_per_usd"] = Source(doc=src, text=rate)
        if s.id not in self._hits:  # 프로세스 재시작 등으로 메모리 값이 없으면 세션에 저장한 값으로
            self._hits[s.id] = [_load_hit(x) for x in s.sheet_hits]
        sheet = build_sheet(self._hits[s.id], bills, public)
        sheet.notices = [f"{d['name']}: {UNKNOWN_NOTICE}" for d in s.docs if UNKNOWN_NOTICE in d["reason"]]
        sheet.notices += [f"{d['name']}: {m}" for d in s.docs for m in d.get("issues", [])]
        for key, a in s.sheet_answers.items():
            _apply(sheet.cells[key], a)
        s.sheet = sheet

    def _sheet_problems(self, s: AgentSession) -> list[str]:
        c = s.sheet.cells
        out = []
        for key in CORE_CELLS:
            empty = c[key].status == "empty" and (key != "rack" or c["rent"].status == "empty")
            if empty and key not in s.sheet_answers:
                out.append(key)
        out += [k for k, cell in c.items() if cell.status == "conflict" and k not in s.sheet_answers and k not in out]
        return out

    def _sheet_missing(self, s: AgentSession) -> list[tuple[str, tuple]]:
        self._refresh_sheet(s)
        return [(k, ()) for k in self._sheet_problems(s)]

    def _q_sheet(self, s: AgentSession, keys: list[str]) -> Question:
        fields: list[FieldSpec] = []
        for key in keys:
            cell, spec = s.sheet.cells[key], SPEC[key]
            owner = f"담당 {cell.owner}" if cell.owner else "담당 부서 확인"
            if cell.status == "conflict":
                opts = [Option(value=_opt_value(x), label=f"{x['text']} ({x['doc']})") for x in cell.candidates]
                part = next((x.get("part") for x in cell.candidates if x.get("part")), None)
                kept = [x["name"] for x in cell.components]
                label = f"{spec.label} · {part}" if part else spec.label
                if kept:
                    owner += f" · 고른 값에 {'·'.join(kept)}를 더해 계산해요"
                fields += [
                    FieldSpec(name=f"cell:{key}", label=f"{label} — 문서마다 값이 달라요", type="select",
                              value=opts[0].value if opts else None, hint=f"{owner} · 자동으로 고르거나 더하지 않았어요",
                              options=opts + [Option(value="manual", label="직접 입력")]),
                    FieldSpec(name=f"amount:{key}", label="직접 입력", required=False, show_for=["manual"],
                              hint="연간 금액(원)" if spec.kind == "cost" and spec.period != "ONE_TIME" else ""),
                ]
            else:
                why = cell.note or "올린 문서에서 찾지 못했어요"
                fields += [
                    FieldSpec(name=f"choice:{key}", label=f"'{spec.label}' 비용", type="select", value="amount",
                              hint=f"{owner} · {why}",
                              options=[Option(value="amount", label="금액 입력"), Option(value="included", label="다른 요금에 포함됨"),
                                       Option(value="excluded", label="이번엔 제외")]),
                    FieldSpec(name=f"cell:{key}", label="금액 (원)", required=False, show_for=["amount"],
                              hint="1회 금액" if spec.period == "ONE_TIME" else "연간 금액"),
                ]
        self._board(s, Step.MISSING, Status.WAITING, f"확인할 칸 {len(keys)}개")
        self._log(s, Step.MISSING, "ask", f"기준표에서 비었거나 문서끼리 다른 칸 {len(keys)}개를 담당자에게 확인합니다: "
                                          + ", ".join(SPEC[k].label for k in keys))
        return Question(id=self._qid(), step=Step.MISSING, title=f"기준표를 확인해 주세요 — 확인할 칸 {len(keys)}개",
                        text="비어 있는 칸은 금액을 넣거나 포함·제외를 고르고, 문서마다 다른 칸은 맞는 값을 골라 주세요.",
                        fields=fields, submit_label="이대로 계속")

    def _answer_sheet(self, s: AgentSession, q: Question, ans: Answer) -> None:
        defaults = {f.name: f.value for f in q.fields}
        get = lambda n: ans.values.get(n, defaults.get(n))  # noqa: E731
        recs: dict[str, dict] = {}
        for f in q.fields:
            kind, _, key = f.name.partition(":")
            label = SPEC[key].label
            if kind == "choice":
                choice = get(f.name)
                if choice == "amount":
                    recs[key] = {"choice": "manual", "value": _number(get(f"cell:{key}"), label)}
                elif choice in ("included", "excluded"):
                    recs[key] = {"choice": choice}
                else:
                    raise _err("선택지에서 골라 주세요.")
            elif kind == "cell" and f.type == "select":
                pick = get(f.name)
                if pick == "manual":
                    recs[key] = {"choice": "manual", "value": _number(get(f"amount:{key}"), label)}
                else:
                    cand = next((x for x in s.sheet.cells[key].candidates if _opt_value(x) == str(pick)), None)
                    if cand is None:
                        raise _err(f"'{label}' 값을 후보에서 고르거나 직접 입력하세요.")
                    recs[key] = {"choice": "pick", "doc": cand["doc"], "row": cand.get("row", 0),
                                 "value": cand["value"] if cand["value"] is not None else cand["text"], "text": cand["text"]}
        for key, rec in recs.items():  # 모두 검사한 뒤 반영
            s.sheet_answers[key] = rec
            spec = SPEC[key]
            if rec["choice"] == "excluded" and spec.be_item:
                s.missing_answers[spec.label] = {"choice": "exclude", "types": [spec.be_item.value], "sheet_key": key}
            self._log(s, Step.MISSING, "answer", f"{spec.label}: {_answer_text(rec)}")
        self._refresh_sheet(s)

    def _finish_sheet(self, s: AgentSession) -> None:
        self._refresh_sheet(s)
        c = s.sheet.cells
        filled = sum(1 for k in SPEC if c[k].status == "filled")
        n = len(s.sheet_answers)
        details = cross_checks(s.sheet) + s.sheet.notices
        warn = n or s.sheet.notices or any("불일치" in d for d in details)
        self._board(s, Step.MISSING, Status.WARNING if warn else Status.DONE,
                    f"기준표 {filled}칸 채움" + (f" · 담당자 확인 {n}칸" if n else ""), details[:6])

    # ------------------------------------------------------------------ 가정값·원가 항목
    def _sheet_prefill(self, s: AgentSession, fields: list[FieldSpec]) -> list[FieldSpec]:
        av = assumption_values(s.sheet)
        c = s.sheet.cells

        def src(key):
            docs = sorted({x.doc for x in c[key].sources})
            return f"{' · '.join(docs)} · 사내 문서" if docs and docs != [MANUAL] else MANUAL

        out = []
        for f in fields:
            if f.name == "inhouse_annual":
                if _has_inhouse(c["operations"]):
                    continue  # 사내 인건비·판관비는 기준표 운영비에 들어 있다
                f.hint = "올린 문서에 사내 인건비·판관비(관리회계팀)가 없어요 — 넣으면 운영비에 더해요 · " + f.hint
            if f.name == "model_id" and "model_name" in av:
                match = _match_model(av["model_name"], f.options)
                if match:
                    f.value, f.hint = match, f"{src('gpu_model')} ({av['model_name']})"
            elif f.name == "acquisition_amount" and c["acquisition"].status == "filled":
                f.value, f.hint = c["acquisition"].value, src("acquisition")
            elif f.name == "acquisition_currency" and c["acquisition"].status == "filled":
                f.value = c["acquisition"].currency or "KRW"
            elif f.name == "annual_interest_rate" and f.name in av:  # 자금팀 문서 금리가 공시 금리보다 우선
                f.value, f.hint = round(float(av[f.name]) * 100, 4), src(f.name)
            elif f.name == "interest_rate_type" and f.name in av:
                f.value = av[f.name]
            elif f.name in ("utilization", "target_rate") and f.name in av:
                f.value, f.hint = round(float(av[f.name]) * 100, 4), src(f.name)
            elif f.name in ("gpu_count", "depreciation_years", "pricing_method", "fx_krw_per_usd") and f.name in av:
                f.value, f.hint = av[f.name], src(f.name) + (f" · {c[f.name].note}" if c[f.name].note else "")
            out.append(f)
        if "fx_krw_per_usd" in av and not any(f.name == "fx_krw_per_usd" for f in out):
            out.append(FieldSpec(name="fx_krw_per_usd", label="환율 (원/달러)", value=av["fx_krw_per_usd"],
                                 hint=src("fx_krw_per_usd")))
        return out

    def _plan_check(self, s: AgentSession) -> None:
        """원가 계산 결과(달러)를 원화로 바꿔 사업기획팀 검산(시간당 원가·판매가)과 대조한다 (±1%). 흐름은 막지 않는다."""
        c = s.sheet.cells if s.sheet else {}
        plan_cost = c["plan_hourly_cost"].value if c and c["plan_hourly_cost"].status == "filled" else None
        fx = (s.assumptions or {}).get("fx_krw_per_usd")
        if plan_cost is None or not fx or not s.simulation:
            s.plan_check = None
            return
        cost = float(s.simulation["hourlyCost"]) * fx
        price = float(s.simulation["candidatePrice"]) * fx
        plan_price = c["plan_price"].value if c["plan_price"].status == "filled" else None
        plan_rate = c["target_rate"].value if c["target_rate"].status == "filled" else None
        if plan_rate is None or abs(float(s.assumptions.get("target_rate", -1)) - float(plan_rate)) > 1e-9:
            plan_price = None  # 마진을 바꿨으면 판매가는 검산과 비교하지 않는다 (원가만)
        ok = abs(cost - plan_cost) <= 0.01 * plan_cost and (plan_price is None or abs(price - plan_price) <= 0.01 * plan_price)
        if ok:
            msg = "사업기획팀 검산과 일치 (산술 검증 — 원가 누락 여부는 보장하지 않음)"
        else:
            msg = (f"사업기획팀 검산과 차이: 시간당 원가 {cost:,.0f}원 · 검산 {plan_cost:,.0f}원 ({(cost / plan_cost - 1) * 100:+.1f}%)"
                   + (f", 판매가 {price:,.0f}원 · 검산 {plan_price:,.0f}원" if plan_price else "")
                   + " — 가정값(가동률·마진·환율)이나 원가 칸이 검산과 다른지 확인해 주세요")
        s.plan_check = {"ok": ok, "hourly_krw": round(cost, 2), "plan_hourly": plan_cost, "price_krw": round(price, 2),
                        "plan_price": plan_price, "message": msg}
        self._log(s, Step.SIMULATE, "result" if ok else "warn", msg)

    def _power_check(self, s: AgentSession, gpu_count: int) -> list:
        """전기료 칸의 원문 줄에 kWh가 있으면 월 kWh ÷ GPU 수를 기준과 비교한다."""
        cell = s.sheet.cells["electricity"] if s.sheet else None
        doc = self._docs.get(s.id)
        if not cell or cell.status != "filled" or doc is None:
            return []
        months = (s.draft.billing_months or 1.0) if s.draft else 1.0
        rows = {r.row: r for r in doc.rows}
        kwh = 0.0
        for src in cell.sources:
            r = rows.get(src.row) if src.doc == doc.file_name else None
            m = _KWH.search(" ".join(r.cells)) if r else None
            kwh += float(m.group(1).replace(",", "")) if m else 0
        return power_per_gpu_check(kwh / months, gpu_count) if kwh else []

    def _sheet_items(self, s: AgentSession) -> list[CostItemDraft]:
        """기준표 → BE 원가 항목. 취득비는 가정값 카드의 값 하나만 쓴다 (be_client가 붙인다)."""
        self._refresh_sheet(s)
        items = [i for i in to_cost_items(s.sheet) if i.item_type != ItemType.ACQUISITION]
        inhouse = float((s.assumptions or {}).get("inhouse_annual") or 0)
        if inhouse > 0 and not _has_inhouse(s.sheet.cells["operations"]):  # 가정값 카드에서 받은 사내 인건비
            ops = next(i for i in items if i.item_type == ItemType.OPERATIONS)
            if ops.status == ItemStatus.USER_INPUT and ops.currency == Currency.KRW:
                ops.amount = round((ops.amount or 0) + inhouse, 2)
            else:
                items = [i for i in items if i.item_type != ItemType.OPERATIONS] + [CostItemDraft(
                    item_type=ItemType.OPERATIONS, status=ItemStatus.USER_INPUT, amount=round(inhouse, 2),
                    currency=Currency.KRW, period=Period.YEAR)]
        return items


def _dump_hit(h) -> dict:
    v = h.value
    return {"key": h.key, "role": h.role, "via": h.via, "source": h.source.model_dump(),
            "value": {"number": None if v.number != v.number else v.number, "unit": v.unit, "currency": v.currency, "text": v.text}}


def _load_hit(d: dict):
    from ..docs import FieldHit
    v = d["value"]
    num = float("nan") if v["number"] is None else v["number"]
    return FieldHit(d["key"], Value(num, v["unit"], v["currency"], v["text"]), Source(**d["source"]), d["role"], d.get("via", "rule"))


def _has_inhouse(cell) -> bool:
    return any(x["name"] in (INHOUSE, MANUAL) for x in cell.components) or (
        cell.status == "conflict" and any(x.get("part") == INHOUSE for x in cell.candidates))


_KWH = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*kWh", re.I)
_KIND_KO = {DocKind.QUOTE: "견적서", DocKind.BILL: "청구서", DocKind.MEMO: "부서 명세", DocKind.PLAN: "검산안"}


def _err(msg: str):
    from .orchestrator import AgentError
    return AgentError(msg)


def _number(v, label: str) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError) as exc:
        raise _err(f"'{label}' 금액을 숫자로 입력하세요.") from exc
    if n < 0:
        raise _err("금액은 0 이상이어야 합니다.")
    return n


def _opt_value(cand: dict) -> str:
    v = cand.get("value")
    return f"{v:.0f}" if isinstance(v, (int, float)) and float(v).is_integer() else str(v if v is not None else cand["text"])


def _match_model(name: str, options: list[Option]) -> str | None:
    n = name.upper().replace(" ", "")
    hits = [o for o in options if o.label and o.label.upper().replace(" ", "") in n]
    return max(hits, key=lambda o: len(o.label)).value if hits else None


def _answer_text(rec: dict) -> str:
    if rec["choice"] == "manual":
        return f"담당자 입력 {rec['value']:,.0f}"
    if rec["choice"] == "pick":
        return f"{rec['doc']} 값 선택"
    return {"included": "다른 요금에 포함", "excluded": "이번엔 제외 (불완전 원가)"}[rec["choice"]]


def _apply(cell, rec: dict) -> None:
    """담당자 답을 칸에 반영하고, 원본 문서 값과 다르면 메모로 남긴다."""
    originals = [x["text"] for x in cell.candidates] or [x.text for x in cell.sources if x.text]
    if rec["choice"] in ("included", "excluded"):
        cell.status, cell.value = rec["choice"], None
        cell.grade = MANUAL
        return
    part = next((x.get("part") for x in cell.candidates if x.get("part")), None)
    if rec["choice"] == "pick":
        chosen = next((x for x in cell.sources if x.doc == rec["doc"]), Source(doc=rec["doc"], row=rec.get("row", 0)))
        cand = next((x for x in cell.candidates if x["doc"] == rec["doc"]), {})
        cell.status, cell.grade = "filled", "사내 문서 · 담당자 선택"
        cell.note = f"후보 {len(cell.candidates)}개 중 {rec['doc']} 값 선택"
        cell.unit, cell.currency = cand.get("unit") or cell.unit, cand.get("currency") or cell.currency
        if part:  # 운영비: 충돌한 구성 요소만 바꾸고 나머지 구성 요소와 더한다
            _set_part(cell, part, float(rec["value"]), [chosen])
        else:
            cell.value, cell.sources = rec["value"], [chosen]
        return
    v = float(rec["value"])
    mine = Value(v, "원", "KRW", "")
    differ = [t for t in originals if not _same_text(mine, t)]
    cell.status, cell.grade = "filled", MANUAL
    cell.unit, cell.currency = cell.unit or "원", cell.currency or "KRW"
    manual = Source(doc=MANUAL, text=f"{v:,.0f}")
    if part or cell.components:
        _set_part(cell, part or MANUAL, v, [manual], replace_all=not part)
    else:
        cell.value, cell.sources = v, [manual]
    if differ:
        cell.note = f"원본 {' · '.join(differ)}과 다름"


def _set_part(cell, name: str, value: float, sources: list, replace_all: bool = False) -> None:
    keep = [] if replace_all else [x for x in cell.components if x["name"] != name]
    cell.components = keep + [{"name": name, "value": value, "sources": [s.model_dump() for s in sources]}]
    cell.value = sum(x["value"] for x in cell.components)
    cell.sources = [s for s in cell.sources if s.doc not in {x["doc"] for x in cell.candidates}] + sources


def _same_text(mine: Value, text: str) -> bool:
    from ..values import parse_value
    v = parse_value(text)
    if v is None:
        return False
    if v.unit == "":
        v = Value(v.number, "원", "KRW", text)
    return same_value(mine, v)
