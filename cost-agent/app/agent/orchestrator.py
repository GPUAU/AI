"""원가 에이전트.

목표: 청구서 한 장으로 '근거 있는 제안가와 견적 근거서'를 만든다.
다음 단계는 코드(상태기계)가 정하고, LLM은 판단이 필요한 곳(분류·재분류)에서만 부른다.
스스로 점검해 다시 시도하고(총액 불일치 → 재분류, 시장 범위 이탈 → 마진 조정 후 재계산),
사람만 정할 수 있는 것은 질문 카드로 묻는다.
"""

from __future__ import annotations

import uuid

from ..be_client import BackendClient, BackendError, build_request
from ..models import (
    BillDraft,
    CostItemDraft,
    Currency,
    ExtractedDocument,
    IssueLevel,
    ItemStatus,
    ItemType,
    LineRole,
    Period,
    SimulationInput,
)
from ..service import BillService
from ..validation import BILL_CORE_ITEMS
from .market import margin_for_price, market_view, offer_prices
from .models import (
    STEP_TITLES,
    AgentSession,
    Answer,
    BoardStep,
    Event,
    FieldSpec,
    Goal,
    Option,
    Question,
    Status,
    Step,
    TermPrice,
)

MAX_REMAP = 2
ITEM_KO = {"ELECTRICITY": "전기료", "RACK_FACILITY": "랙·상면", "RENT": "임차료", "NETWORK_STORAGE": "네트워크·스토리지",
           "OPERATIONS": "운영·유지보수", "MEMORY": "메모리", "GPU": "GPU 임차", "ACQUISITION": "취득비", "OTHER": "기타"}


class AgentError(RuntimeError):
    pass


class CostAgent:
    def __init__(self, bills: BillService, backend: BackendClient):
        self.bills = bills
        self.backend = backend
        self.sessions: dict[str, AgentSession] = {}
        self._docs: dict[str, ExtractedDocument] = {}

    # ------------------------------------------------------------------ 진입점
    def start(self, doc: ExtractedDocument, goal: Goal) -> AgentSession:
        s = AgentSession(id=uuid.uuid4().hex[:12], goal=goal,
                         board=[BoardStep(key=k, title=t) for k, t in STEP_TITLES.items()])
        self.sessions[s.id] = s
        self._docs[s.id] = doc
        self._log(s, Step.ANALYZE, "think", f"목표: 청구서로 원가·제안가·{'/'.join(map(str, goal.terms_years))}년 가격과 근거서를 만든다.")
        return self._run(s)

    def answer(self, session_id: str, ans: Answer) -> AgentSession:
        s = self.get(session_id)
        q = s.question
        if q is None or q.id != ans.question_id:
            raise AgentError("지금 답할 질문이 아닙니다.")
        s.question = None
        handler = getattr(self, f"_answer_{q.step.value.lower()}")
        try:
            handler(s, q, ans)
        except AgentError:
            s.question = q  # 잘못된 입력이면 같은 질문을 다시 보여 준다
            raise
        return self._run(s)

    def get(self, session_id: str) -> AgentSession:
        if session_id not in self.sessions:
            raise KeyError(session_id)
        return self.sessions[session_id]

    # ------------------------------------------------------------------ 상태기계
    def _run(self, s: AgentSession) -> AgentSession:
        guard = 0
        while s.step != Step.DONE and s.question is None:
            guard += 1
            if guard > 20:
                raise AgentError("진행 단계가 반복되어 멈췄습니다.")
            getattr(self, f"_step_{s.step.value.lower()}")(s)
        return s

    def _go(self, s: AgentSession, step: Step) -> None:
        s.step = step
        if step != Step.DONE:
            self._board(s, step, Status.RUNNING)

    # 1) 청구서 분석 ------------------------------------------------------
    def _step_analyze(self, s: AgentSession) -> None:
        if s.draft is None:
            self._do_ingest(s)
        d = s.draft
        if d.currency is None:
            s.question = self._q_currency(s)
            return
        rec = d.reconciliation
        if rec.matched is False and s.remap_tries < MAX_REMAP:
            self._do_remap(s)
            return  # 다시 점검
        if rec.matched is False:
            s.question = self._q_mismatch(s)
            return
        self._finish_analyze(s)
        self._go(s, Step.MISSING)

    # 단계 동작 (워크플로·그래프 에이전트가 함께 쓴다) ---------------------
    def _do_ingest(self, s: AgentSession) -> None:
        self._board(s, Step.ANALYZE, Status.RUNNING)
        self._log(s, Step.ANALYZE, "tool", "청구서를 읽고 각 줄을 원가 항목으로 분류합니다 (금액·숫자는 가리고 AI에 전송).")
        s.draft = self.bills.ingest(self._docs[s.id])
        d = s.draft
        via = {"llm": "AI", "rules": "키워드 규칙", "template": "저장된 양식 템플릿"}.get(d.mapper, d.mapper)
        self._log(s, Step.ANALYZE, "result", f"{via}로 {sum(1 for l in d.lines if l.role == LineRole.COST)}개 원가 줄을 분류했습니다.")

    def _do_remap(self, s: AgentSession, hypothesis: str = "") -> bool:
        """총액이 안 맞을 때 피드백을 붙여 다시 분류한다. AI 분류를 쓸 수 없으면 False."""
        d = s.draft
        s.remap_tries += 1
        self._log(s, Step.ANALYZE, "think",
                  f"원가 합계가 청구 총액과 맞지 않습니다. 분류를 다시 검토합니다 ({s.remap_tries}/{MAX_REMAP}회).")
        feedback = self._remap_feedback(d) + (f"\n[에이전트 가설] {hypothesis}" if hypothesis else "")
        redone = self.bills.remap(d.id, feedback)
        if redone is not None:
            s.draft = redone
            self._log(s, Step.ANALYZE, "result",
                      "재분류 결과 총액이 일치합니다." if redone.reconciliation.matched else "재분류 후에도 총액이 맞지 않습니다.")
            return True
        self._log(s, Step.ANALYZE, "warn", "AI 재분류를 쓸 수 없어 (규칙·템플릿 분류) 담당자 확인으로 넘깁니다.")
        s.remap_tries = MAX_REMAP
        return False

    def _finish_analyze(self, s: AgentSession) -> None:
        d = s.draft
        rec = d.reconciliation
        errors = [i for i in d.issues if i.level == IssueLevel.ERROR]
        status = Status.WARNING if errors or rec.matched is None else Status.DONE
        summary = ("총액 대조 일치" if rec.matched else "총액 줄 없음") + (f" · 재분류 {s.remap_tries}회" if s.remap_tries else "")
        self._board(s, Step.ANALYZE, status, summary, [i.message for i in d.issues if i.level != IssueLevel.INFO][:5])

    def _q_currency(self, s: AgentSession) -> Question:
        self._board(s, Step.ANALYZE, Status.WAITING, "통화 확인 대기")
        self._log(s, Step.ANALYZE, "ask", "청구서에서 통화(원·달러) 표시를 찾지 못해 담당자에게 확인합니다.")
        return Question(id=self._qid(), step=Step.ANALYZE, title="청구서 금액의 통화를 알려 주세요",
                        text="머리글이나 금액에 원·달러 표시가 없어 판단하지 못했어요. 잘못 고르면 원가가 크게 틀립니다.",
                        options=[Option(value="currency:KRW", label="원 (KRW)"),
                                 Option(value="currency:USD", label="달러 (USD)")])

    def _q_mismatch(self, s: AgentSession) -> Question:
        rec = s.draft.reconciliation
        self._board(s, Step.ANALYZE, Status.WAITING, "총액 불일치 · 확인 필요")
        self._log(s, Step.ANALYZE, "ask", "분류를 다시 검토해도 총액이 맞지 않아 담당자에게 확인합니다.")
        return Question(
            id=self._qid(), step=Step.ANALYZE, title="청구 총액과 원가 합계가 맞지 않아요",
            text=f"원가 합계가 청구서 총액과 {rec.difference:+,.0f} 차이 납니다. 빠진 줄이나 잘못 분류된 줄이 있을 수 있어요. "
                 "아래 분류표에서 고친 뒤 다시 시작하거나, 차이를 알고 그대로 진행할 수 있어요.",
            options=[Option(value="proceed", label="차이를 확인했고 그대로 진행"),
                     Option(value="stop", label="여기서 멈추고 분류표 수정")])

    def _answer_analyze(self, s: AgentSession, q: Question, ans: Answer) -> None:
        if (ans.choice or "").startswith("currency:"):
            cur = Currency(ans.choice.split(":", 1)[1])
            s.draft = self.bills.set_currency(s.draft.id, cur)
            self._log(s, Step.ANALYZE, "answer", f"통화: {cur.value}")
            return
        if ans.choice not in ("proceed", "stop"):
            raise AgentError("선택지에서 골라 주세요.")
        self._log(s, Step.ANALYZE, "answer", "그대로 진행" if ans.choice == "proceed" else "분류표 수정을 위해 중단")
        if ans.choice == "proceed":
            s.mismatch_ok = True
            self._board(s, Step.ANALYZE, Status.WARNING, "총액 불일치를 알고 진행")
            self._go(s, Step.MISSING)
        else:
            self._board(s, Step.ANALYZE, Status.ERROR, "담당자가 중단 · 분류표 수정 후 다시 시작")
            s.step = Step.DONE

    @staticmethod
    def _remap_feedback(d: BillDraft) -> str:
        r = d.reconciliation
        direction = "크다" if (r.difference or 0) > 0 else "작다"
        lines = [f"- {l.ref}: {l.role.value}" + (f"/{l.item_type.value}" if l.item_type else "")
                 for l in d.lines if l.role != LineRole.IGNORE]
        return (f"원가(COST)로 분류한 줄의 합계가 청구 총액보다 {direction}. "
                "소계·부가세·총액·할인 줄을 원가로 넣었거나, 원가 줄을 제외(IGNORE)했거나, 금액 칸을 잘못 골랐을 수 있다. "
                "아래 현재 판단을 검토해 모든 줄을 다시 분류하라.\n" + "\n".join(lines))

    # 2) 누락 확인 --------------------------------------------------------
    def _missing_groups(self, s: AgentSession) -> list[tuple[str, tuple[ItemType, ...]]]:
        missing = set(s.draft.missing_items) if s.draft else set()
        return [(name, types) for name, types in BILL_CORE_ITEMS.items()
                if missing.intersection(types) and name not in s.missing_answers]

    def _step_missing(self, s: AgentSession) -> None:
        groups = self._missing_groups(s)
        if not groups:
            self._finish_missing(s)
            self._go(s, Step.ASSUMPTIONS)
            return
        s.question = self._q_missing(s, groups[0][0])

    def _q_missing(self, s: AgentSession, name: str) -> Question:
        present = sorted({c.item_type.value for c in s.draft.cost_items if c.status == ItemStatus.USER_INPUT
                          and c.item_type != ItemType.ACQUISITION})
        self._board(s, Step.MISSING, Status.WAITING, f"'{name}' 확인 대기")
        self._log(s, Step.MISSING, "ask", f"청구서에 '{name}' 비용이 없습니다. 큰 비용이 빠지면 원가 전체를 믿을 수 없어 담당자에게 확인합니다.")
        return Question(
            id=self._qid(), step=Step.MISSING, title=f"'{name}' 비용이 청구서에 없어요",
            text="다른 계약으로 따로 내는지, 다른 요금에 포함됐는지 알려 주세요. 모르면 '이번엔 제외'를 고르면 결과에 '불완전 원가'로 표시합니다.",
            options=[Option(value="amount", label="별도 계약 금액 입력"),
                     Option(value="included", label="다른 요금에 포함됨"),
                     Option(value="exclude", label="이번엔 제외")],
            fields=[
                FieldSpec(name="amount", label="금액", show_for=["amount"]),
                FieldSpec(name="currency", label="통화", type="select", value=(s.draft.currency or Currency.KRW).value,
                          options=[Option(value="KRW", label="원"), Option(value="USD", label="달러")], show_for=["amount"]),
                FieldSpec(name="period", label="기간", type="select", value="MONTH",
                          options=[Option(value="MONTH", label="월"), Option(value="YEAR", label="연")], show_for=["amount"]),
                FieldSpec(name="parent", label="포함된 요금", type="select", value=present[0] if present else None,
                          options=[Option(value=p, label=ITEM_KO.get(p, p)) for p in present], show_for=["included"]),
            ])

    def _finish_missing(self, s: AgentSession) -> None:
        n = len(s.missing_answers)
        self._board(s, Step.MISSING, Status.DONE if not n else Status.WARNING,
                    "빠진 큰 비용 없음" if not n else f"{n}건 담당자 확인 완료",
                    [f"{k}: {self._missing_text(v)}" for k, v in s.missing_answers.items()])

    def _answer_missing(self, s: AgentSession, q: Question, ans: Answer) -> None:
        name = q.title.split("'")[1]
        types = BILL_CORE_ITEMS[name]
        rec = {"choice": ans.choice, "types": [t.value for t in types], **ans.values}
        if ans.choice == "amount":
            try:
                rec["amount"] = float(ans.values.get("amount"))
            except (TypeError, ValueError) as exc:
                raise AgentError("금액을 숫자로 입력하세요.") from exc
            if rec["amount"] < 0:
                raise AgentError("금액은 0 이상이어야 합니다.")
        if ans.choice == "included" and not ans.values.get("parent"):
            raise AgentError("포함된 요금을 고르세요.")
        s.missing_answers[name] = rec
        self._log(s, Step.MISSING, "answer", f"{name}: {self._missing_text(rec)}")

    @staticmethod
    def _missing_text(rec: dict) -> str:
        if rec["choice"] == "amount":
            return f"별도 계약 {rec['amount']:,.0f} {rec.get('currency', '')} / {'월' if rec.get('period') == 'MONTH' else '연'}"
        if rec["choice"] == "included":
            return f"{ITEM_KO.get(rec.get('parent'), rec.get('parent'))} 요금에 포함"
        return "이번엔 제외 (불완전 원가)"

    # 3) 가정값 -----------------------------------------------------------
    def _step_assumptions(self, s: AgentSession) -> None:
        if s.assumptions.get("confirmed"):
            self._go(s, Step.SIMULATE)
            return
        s.question = self._q_assumptions(s)

    def _q_assumptions(self, s: AgentSession) -> Question:
        try:
            models = self.backend.models()
        except BackendError:
            models = []
        fx_value, fx_hint = None, "원화 비용을 달러로 바꿀 때 사용"
        try:
            fx = self.backend.latest_fx()
            if fx:
                fx_value, fx_hint = float(fx["rate"]), f"한국수출입은행 {fx.get('referenceDate', '')} 기준 (BE 환율 API)"
        except BackendError:
            pass
        needs_fx = any(c.currency == Currency.KRW for c in s.draft.cost_items) or any(
            v.get("currency") == "KRW" for v in s.missing_answers.values())
        g = s.goal
        fields = [
            FieldSpec(name="model_id", label="GPU 모델", type="select", value=g.model_id,
                      options=[Option(value=m["id"], label=m["name"]) for m in models], hint="시장 가격 비교 기준"),
            FieldSpec(name="gpu_count", label="GPU 수", value=g.gpu_count, hint="이 청구서의 시설에서 운영할 GPU 수"),
            FieldSpec(name="acquisition_amount", label="서버·GPU 취득비", hint="청구서에 없는 값 · 견적서 기준으로 입력"),
            FieldSpec(name="acquisition_currency", label="취득비 통화", type="select", value="USD",
                      options=[Option(value="USD", label="달러"), Option(value="KRW", label="원")]),
            FieldSpec(name="depreciation_years", label="감가상각 연수", value=5,
                      hint="제안값 5년: 미래에셋 「AI Factory의 경제학」(2026-05) IT 장비 가정"),
            FieldSpec(name="utilization", label="유료 가동률 (%)", value=70,
                      hint="제안값 70%: 같은 보고서는 75% 가정 · 보수적으로 낮춤"),
            FieldSpec(name="pricing_method", label="마진 방식", type="select", value="REVENUE_MARGIN",
                      options=[Option(value="REVENUE_MARGIN", label="매출 기준 이익률"), Option(value="COST_MARKUP", label="원가 가산율")]),
            FieldSpec(name="target_rate", label="목표 마진 (%)", value=20, hint="제안값 20%: 같은 보고서의 목표 마진 예시"),
        ]
        if needs_fx:
            fields.append(FieldSpec(name="fx_krw_per_usd", label="환율 (원/달러)", value=fx_value, hint=fx_hint))
        prev = s.assumptions or {}
        if prev:  # 다시 묻는 경우: 담당자가 넣었던 값을 그대로 채워 둔다
            back = {"utilization": lambda x: round(x * 100, 4), "target_rate": lambda x: round(x * 100, 4)}
            for f in fields:
                if prev.get(f.name) not in (None, ""):
                    f.value = back.get(f.name, lambda x: x)(prev[f.name])
        self._board(s, Step.ASSUMPTIONS, Status.WAITING, "가정값 확인 대기")
        self._log(s, Step.ASSUMPTIONS, "ask", "청구서에 없는 값(GPU 수·취득비)과 가정값을 근거와 함께 제안합니다.")
        return Question(id=self._qid(), step=Step.ASSUMPTIONS, title="청구서에 없는 값을 확인해 주세요",
                        text="회색 제안값은 근거가 있는 출발점이에요. 실제 사업 조건에 맞게 고치세요.",
                        fields=fields, submit_label="이 값으로 계산")

    def _answer_assumptions(self, s: AgentSession, q: Question, ans: Answer) -> None:
        v = {f.name: ans.values.get(f.name, f.value) for f in q.fields}
        try:
            a = {
                "model_id": str(v["model_id"]), "gpu_count": int(v["gpu_count"]),
                "acquisition_amount": float(v["acquisition_amount"]), "acquisition_currency": v["acquisition_currency"],
                "depreciation_years": int(v["depreciation_years"]), "utilization": float(v["utilization"]) / 100,
                "pricing_method": v["pricing_method"], "target_rate": float(v["target_rate"]) / 100,
                "fx_krw_per_usd": float(v["fx_krw_per_usd"]) if v.get("fx_krw_per_usd") not in (None, "") else None,
            }
        except (TypeError, ValueError, KeyError) as exc:
            raise AgentError("필수 값을 모두 숫자로 입력하세요.") from exc
        if not a["model_id"] or a["model_id"] == "None" or a["gpu_count"] <= 0 or not 0 < a["utilization"] <= 1:
            raise AgentError("GPU 모델, GPU 수(1 이상), 가동률(0~100%)을 확인하세요.")
        names = {o.value: o.label for o in q.fields[0].options}
        a["model_name"] = names.get(a["model_id"], a["model_id"])
        a["sources"] = {f.name: (f.hint if str(ans.values.get(f.name, f.value)) == str(f.value) and f.hint else "담당자 입력")
                        for f in q.fields}
        a["confirmed"] = True
        s.assumptions = a
        self._log(s, Step.ASSUMPTIONS, "answer",
                  f"{a['model_name']} {a['gpu_count']}장 · 취득비 {a['acquisition_amount']:,.0f} {a['acquisition_currency']} · "
                  f"가동률 {a['utilization']:.0%} · 목표 마진 {a['target_rate']:.0%}")
        self._board(s, Step.ASSUMPTIONS, Status.DONE, f"{a['model_name']} {a['gpu_count']}장 · 마진 {a['target_rate']:.0%}")

    # 4) 원가 계산 --------------------------------------------------------
    def _final_items(self, s: AgentSession) -> list[CostItemDraft]:
        items = [c.model_copy() for c in s.draft.cost_items]
        have = {c.item_type for c in items}
        for rec in s.missing_answers.values():
            t = ItemType(rec["types"][0])
            if t in have:
                continue
            if rec["choice"] == "amount":
                amount = rec["amount"] * (12 if rec.get("period") == "MONTH" else 1)
                items.append(CostItemDraft(item_type=t, status=ItemStatus.USER_INPUT, amount=round(amount, 2),
                                           currency=Currency(rec.get("currency", "KRW")), period=Period.YEAR))
            elif rec["choice"] == "included":
                items.append(CostItemDraft(item_type=t, status=ItemStatus.INCLUDED_IN_OTHER,
                                           included_in=ItemType(rec["parent"])))
            else:
                items.append(CostItemDraft(item_type=t, status=ItemStatus.NOT_INCLUDED))
            have.add(t)
        return items

    def _step_simulate(self, s: AgentSession) -> None:
        self._go(s, Step.MARKET if self._do_simulate(s) else Step.ASSUMPTIONS)

    def _do_simulate(self, s: AgentSession) -> bool:
        """BE 원가 계산. 실패하면 가정값을 미확정으로 돌리고 False."""
        a = s.assumptions
        s.final_items = self._final_items(s)
        inp = SimulationInput(
            model_id=a["model_id"], gpu_count=a["gpu_count"], acquisition_amount=a["acquisition_amount"],
            acquisition_currency=Currency(a["acquisition_currency"]), depreciation_years=a["depreciation_years"],
            utilization=a["utilization"], pricing_method=a["pricing_method"], target_rate=a["target_rate"],
            fx_krw_per_usd=a.get("fx_krw_per_usd"))
        self._log(s, Step.SIMULATE, "tool", "BE 원가 계산 API를 호출합니다 (계산식은 BE 한 곳에만 있음).")
        try:
            s.request = build_request(s.final_items, inp)
            s.simulation = self.backend.simulate(s.request)
        except BackendError as exc:
            detail = "; ".join(f"{e.get('field')}: {e.get('reason')}" for e in (exc.errors or []) if isinstance(e, dict))
            self._board(s, Step.SIMULATE, Status.ERROR, "원가 계산 실패", [str(exc), detail] if detail else [str(exc)])
            self._log(s, Step.SIMULATE, "warn", f"원가 계산 실패: {exc} {detail}")
            s.assumptions["confirmed"] = False
            return False
        r = s.simulation
        missing = [ITEM_KO.get(m, "조달 금리" if m == "FINANCING" else m) for m in r.get("missingItems", [])]
        self._log(s, Step.SIMULATE, "result",
                  f"원가 ${float(r['hourlyCost']):.3f} · 제안가 ${float(r['candidatePrice']):.3f} /GPU·시간")
        self._board(s, Step.SIMULATE, Status.WARNING if missing else Status.DONE,
                    f"원가 ${float(r['hourlyCost']):.2f} · 제안가 ${float(r['candidatePrice']):.2f}",
                    [f"BE가 미반영으로 본 항목: {', '.join(missing)}"] if missing else [])
        s.sim_basis = self._sim_basis(s)
        return True

    @staticmethod
    def _sim_basis(s: AgentSession) -> str:
        """원가 계산에 쓴 입력의 지문. 가정값·누락 답이 바뀌면 다시 계산해야 한다."""
        import json
        a = {k: v for k, v in s.assumptions.items() if k not in ("sources", "confirmed")}
        return json.dumps([a, s.missing_answers], sort_keys=True, default=str)

    # 5) 시장 비교 --------------------------------------------------------
    def _step_market(self, s: AgentSession) -> None:
        if self._do_market(s) and not s.margin_adjusted:
            s.question = self._q_market(s)
            return
        self._finish_market(s)
        self._go(s, Step.TERMS)

    def _do_market(self, s: AgentSession) -> bool:
        """시장 공개가격과 비교한다. 제안가가 시장 범위를 벗어나면 True."""
        a, r = s.assumptions, s.simulation
        cand = float(r["candidatePrice"])
        self._log(s, Step.MARKET, "tool", f"{a['model_name']} 온디맨드 공개 가격을 조회합니다.")
        try:
            samples = offer_prices(self.backend.offers(a["model_id"]))
        except BackendError as exc:
            samples = []
            self._log(s, Step.MARKET, "warn", f"시장 가격 조회 실패: {exc}")
        s.market = market_view(a["model_name"], samples, cand)
        s.market_basis = s.sim_basis
        m = s.market
        if not m.count:
            return False
        self._log(s, Step.MARKET, "result", f"공개가격 {m.count}개(사업자 {m.providers}곳) · {m.position}")
        return cand > m.max or cand < m.min

    def _finish_market(self, s: AgentSession) -> None:
        m = s.market
        if not m or not m.count:
            self._board(s, Step.MARKET, Status.WARNING, "비교할 공개 가격 없음")
            return
        cand = float(s.simulation["candidatePrice"])
        out_of_range = cand > m.max or cand < m.min
        self._board(s, Step.MARKET, Status.DONE if not out_of_range else Status.WARNING, m.position,
                    [f"최저 ${m.min:.2f} · 중앙 ${m.median:.2f} · 최고 ${m.max:.2f}"])

    def _q_market(self, s: AgentSession) -> Question:
        a, r, m = s.assumptions, s.simulation, s.market
        cand = float(r["candidatePrice"])
        need = margin_for_price(float(r["hourlyCost"]), m.median, a["pricing_method"])
        self._board(s, Step.MARKET, Status.WAITING, m.position)
        above = cand > m.max
        self._log(s, Step.MARKET, "think",
                  f"제안가가 시장 {'최고가보다 높아' if above else '최저가보다 낮아'} 마진 조정을 제안합니다.")
        opts = [Option(value="keep", label="그대로 유지", description="근거서에 시장 범위 밖이라고 표시")]
        if need is not None and 0 < need < 0.95:
            opts.insert(0, Option(value="median", label=f"시장 중앙값에 맞추기 (마진 {need:.0%})",
                                  description=f"제안가를 ${m.median:.2f} 근처로 다시 계산"))
        elif above:
            # 원가 자체가 시장 중앙값보다 높다: 마진으로는 못 맞춘다 → 가정(취득비·가동률 등)을 다시 본다
            opts.insert(0, Option(value="assumptions", label="가정값 다시 확인",
                                  description=f"원가 ${float(r['hourlyCost']):.2f}가 시장 중앙값보다 높아 마진 조정으로는 맞출 수 없어요"))
        return Question(
            id=self._qid(), step=Step.MARKET,
            title=f"제안가가 시장 {'최고가보다 높아요' if above else '최저가보다 낮아요'}",
            text=f"제안가 ${cand:.2f} · 공개가격 ${m.min:.2f}~${m.max:.2f} (중앙값 ${m.median:.2f}). "
                 + ("이 가격이면 수주가 어렵거나, 원가 가정을 다시 봐야 할 수 있어요." if above
                    else "마진을 더 확보할 여지가 있어요."),
            options=opts)

    def _answer_market(self, s: AgentSession, q: Question, ans: Answer) -> None:
        s.margin_adjusted = True
        if ans.choice == "assumptions":
            self._log(s, Step.MARKET, "answer", "원가 가정값을 다시 확인합니다.")
            s.assumptions["confirmed"] = False
            s.goal.model_id, s.goal.gpu_count = s.assumptions.get("model_id"), s.assumptions.get("gpu_count")
            self._board(s, Step.MARKET, Status.PENDING, "가정값 재확인 후 다시 비교")
            self._go(s, Step.ASSUMPTIONS)
        elif ans.choice == "median":
            a, r = s.assumptions, s.simulation
            need = margin_for_price(float(r["hourlyCost"]), s.market.median, a["pricing_method"])
            old = a["target_rate"]
            a["target_rate"] = round(need, 4)
            a.setdefault("sources", {})["target_rate"] = f"시장 중앙값에 맞춰 에이전트가 조정 (원래 {old:.0%})"
            self._log(s, Step.MARKET, "answer", f"마진을 {old:.0%} → {need:.0%}로 바꿔 다시 계산합니다.")
            self._board(s, Step.ASSUMPTIONS, Status.DONE, f"{a['model_name']} {a['gpu_count']}장 · 마진 {need:.0%} (시장 조정)")
            self._go(s, Step.SIMULATE)
        else:
            self._log(s, Step.MARKET, "answer", "제안가를 그대로 유지합니다.")
            self._board(s, Step.MARKET, Status.WARNING, s.market.position + " · 유지")
            self._go(s, Step.TERMS)

    # 6) 기간별 가격 ------------------------------------------------------
    def _step_terms(self, s: AgentSession) -> None:
        if not s.goal.terms_years:
            self._board(s, Step.TERMS, Status.DONE, "요청 없음")
            self._go(s, Step.REPORT)
            return
        s.question = self._q_terms(s)

    def _q_terms(self, s: AgentSession) -> Question:
        self._board(s, Step.TERMS, Status.WAITING, "할인율 입력 대기")
        self._log(s, Step.TERMS, "ask", "장기계약 할인율은 입찰 상황마다 달라 담당자에게 받습니다.")
        defaults = {3: 15, 5: 25}
        return Question(
            id=self._qid(), step=Step.TERMS, title="장기계약 할인율을 정해 주세요",
            text="단독 입찰이면 낮게, 경쟁 입찰이면 높게 잡는 경우가 많아요. 회색 값은 예시입니다.",
            fields=[FieldSpec(name=f"discount_{y}", label=f"{y}년 계약 할인율 (%)", value=defaults.get(y, 10),
                              hint="예시값 · 실제 입찰 조건으로 수정") for y in s.goal.terms_years],
            submit_label="기간별 가격 계산")

    def _answer_terms(self, s: AgentSession, q: Question, ans: Answer) -> None:
        cand = float(s.simulation["candidatePrice"])
        s.terms = []
        for y in s.goal.terms_years:
            try:
                d = float(ans.values.get(f"discount_{y}", next(f.value for f in q.fields if f.name == f"discount_{y}"))) / 100
            except (TypeError, ValueError) as exc:
                raise AgentError("할인율을 숫자로 입력하세요.") from exc
            if not 0 <= d < 1:
                raise AgentError("할인율은 0~100% 사이여야 합니다.")
            s.terms.append(TermPrice(years=y, discount_rate=d, price=round(cand * (1 - d), 4)))
        self._log(s, Step.TERMS, "answer", " · ".join(f"{t.years}년 할인 {t.discount_rate:.0%}" for t in s.terms))
        self._price_terms(s)
        self._go(s, Step.REPORT)

    def _price_terms(self, s: AgentSession) -> None:
        """담당자가 정한 할인율로 기간별 가격을 (다시) 계산한다. 제안가가 바뀌면 다시 부른다."""
        cand = float(s.simulation["candidatePrice"])
        s.terms = [TermPrice(years=t.years, discount_rate=t.discount_rate, price=round(cand * (1 - t.discount_rate), 4))
                   for t in s.terms]
        cost = float(s.simulation["hourlyCost"])
        below = [t for t in s.terms if t.price < cost]
        self._log(s, Step.TERMS, "result", " · ".join(f"{t.years}년 ${t.price:.3f}" for t in s.terms))
        self._board(s, Step.TERMS, Status.WARNING if below else Status.DONE,
                    " · ".join(f"{t.years}년 ${t.price:.2f}" for t in s.terms),
                    [f"{t.years}년 가격이 원가(${cost:.2f})보다 낮습니다" for t in below])

    # 7) 근거서 -----------------------------------------------------------
    def _step_report(self, s: AgentSession) -> None:
        self._do_report(s)
        self._go(s, Step.DONE)

    def _do_report(self, s: AgentSession) -> None:
        r, a, m, d = s.simulation, s.assumptions, s.market, s.draft
        lines = [
            f"청구서 '{d.file_name}'을 {({'llm': 'AI', 'rules': '키워드 규칙', 'template': '저장된 양식'}).get(d.mapper, d.mapper)}로 분류했고 총액 대조는 "
            + ("일치" if d.reconciliation.matched else "불일치(담당자 확인 후 진행)" if d.reconciliation.matched is False else "불가")
            + (f", AI 재분류 {s.remap_tries}회" if s.remap_tries else "") + ".",
            f"{a['model_name']} {a['gpu_count']}장 기준 GPU·시간당 원가 ${float(r['hourlyCost']):.3f}, "
            f"제안가 ${float(r['candidatePrice']):.3f} (목표 마진 {a['target_rate']:.0%}).",
        ]
        for k, v in s.missing_answers.items():
            lines.append(f"청구서에 없던 '{k}'는 담당자 확인: {self._missing_text(v)}.")
        if "FINANCING" in r.get("missingItems", []):
            lines.append("조달 금리는 입력하지 않아 원가에서 빠져 있습니다 (마진이 실제보다 높게 보일 수 있음).")
        if m and m.count:
            lines.append(f"시장 공개가격 {m.count}개 기준 {m.position} (중앙값 ${m.median:.2f}).")
        if s.margin_adjusted and a.get("sources", {}).get("target_rate", "").startswith("시장 중앙값"):
            lines.append("제안가가 시장 범위를 벗어나 에이전트가 마진을 조정해 다시 계산했습니다.")
        for t in s.terms:
            lines.append(f"{t.years}년 계약가 ${t.price:.3f} (할인 {t.discount_rate:.0%}).")
        s.summary = lines
        s.report_ready = True
        self._log(s, Step.REPORT, "result", "견적 근거서(엑셀)를 만들었습니다.")
        self._board(s, Step.REPORT, Status.DONE, "엑셀 다운로드 가능")

    # ------------------------------------------------------------------ 공통
    def _board(self, s: AgentSession, step: Step, status: Status, summary: str | None = None,
               details: list[str] | None = None) -> None:
        b = next(x for x in s.board if x.key == step)
        b.status = status
        if summary is not None:
            b.summary = summary
        if details is not None:
            b.details = details

    @staticmethod
    def _log(s: AgentSession, step: Step, kind: str, message: str) -> None:
        s.events.append(Event(step=step, kind=kind, message=message))

    @staticmethod
    def _qid() -> str:
        return uuid.uuid4().hex[:8]
