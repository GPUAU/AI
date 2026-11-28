"""원가 에이전트 (LangGraph).

워크플로(orchestrator.CostAgent)와 달리 **다음에 무엇을 할지 LLM 판단자(controller)가 고른다.**

    decide ──tool call──▶ act ──(질문)──▶ wait(interrupt) ──답변──▶ decide
       ▲                   │
       └──── 관찰 결과 ─────┘          finish → guard 통과 → 근거서 → END

- 숫자는 도구(코드·BE)가 계산한다. 판단자는 도구와 이유만 고르고, 숫자를 직접 넣지 못한다.
- 판단자에게는 금액·식별자 없이 상태와 상대값(비율·위치)만 보낸다.
- finish 를 불러도 guard(코드)가 완료 조건을 다시 확인하고, 모자라면 이유와 함께 돌려보낸다.
- 판단자를 쓸 수 없거나(키 없음·호출 실패) 같은 실패를 되풀이하면 기본 순서(policy)가 다음 도구를 고른다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Callable, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..models import ExtractedDocument, ItemType
from ..validation import BILL_CORE_ITEMS
from .models import STEP_TITLES, AgentSession, Answer, BoardStep, Goal, Question, Status, Step
from .orchestrator import MAX_REMAP, AgentError, CostAgent

MAX_DECISIONS = 40  # 세션 하나에서 판단자가 고를 수 있는 최대 횟수
MAX_FAILS = 2  # 연속으로 '실행 불가'가 나오면 기본 순서로 넘긴다
FINAL = (Status.DONE, Status.WARNING, Status.ERROR)

TOOL_STEP = {"analyze_bill": Step.ANALYZE, "remap_bill": Step.ANALYZE, "simulate_cost": Step.SIMULATE,
             "compare_market": Step.MARKET, "review_price": Step.REVIEW, "finish": Step.REPORT}
ASK_STEP = {"currency": Step.ANALYZE, "interpret": Step.ANALYZE, "mismatch": Step.ANALYZE, "missing": Step.MISSING,
            "assumptions": Step.ASSUMPTIONS, "market": Step.MARKET, "terms": Step.TERMS, "review": Step.REVIEW}
TOOL_KO = {"analyze_bill": "청구서 분석", "remap_bill": "재분류", "simulate_cost": "원가 계산",
           "compare_market": "시장 비교", "review_price": "승인 검토", "finish": "마무리", "ask_user": "담당자 확인"}
ASK_KO = {"currency": "통화", "interpret": "청구서 해석", "mismatch": "총액 불일치", "missing": "누락 비용", "assumptions": "가정값",
          "market": "시장 이탈", "terms": "할인율", "review": "승인 검토"}

SYSTEM_PROMPT = """너는 GPU 클라우드 사업자의 원가 분석 에이전트다.
목표: IDC 청구서 한 장으로 GPU·시간당 원가와 제안가를 계산하고, 시장 공개가격과 비교하고, 장기계약 가격과 견적 근거서를 만든다.

너는 매번 도구 하나를 골라 다음 행동을 정한다. 규칙:
1. 숫자는 도구와 원가 서버가 계산한다. 너는 금액을 보지 못하고(가려짐) 숫자를 직접 정하지 않는다.
2. 모든 도구 호출에 reason을 적는다: 관찰 결과에서 근거를 들어 왜 이 행동인지 한국어 한 문장으로 (예: "차이가 총액의 3%이고 소계 줄이 원가로 들어갔을 수 있어 재분류한다").
3. 원가·가격에 영향을 주는 결정(통화, 누락 비용 처리, 가정값, 시장 범위 이탈 대응, 할인율)은 ask_user로 담당자에게 묻는다.
4. 청구 총액과 원가 합계가 안 맞으면 원인 가설을 세운다. 분류 실수(소계·부가세·할인 줄을 원가로 넣음, 원가 줄 제외, 금액 칸 오인, 신뢰도 낮은 줄)로 설명되면 remap_bill로 다시 분류한다(최대 {max_remap}회). 분류로는 설명되지 않으면(표에 없는 비용이 총액에만 있는 등) 근거를 들어 ask_user(mismatch)로 묻는다.
5. 가정값이나 누락 답이 바뀌면 simulate_cost로 다시 계산하고, 원가가 바뀌면 compare_market을 다시 한다.
6. 제안가가 시장 범위를 벗어나면 ask_user(market)로 담당자 결정을 받는다.
7. 모든 조건이 끝났다고 판단하면 finish를 부른다. 조건이 모자라면 이유가 돌아오니 그에 맞게 다음 행동을 고른다.
8. 도구 결과 끝의 [현재 상태]를 보고 판단한다. '실행 불가'가 오면 같은 호출을 반복하지 말고 이유에 맞게 바꾼다.""".replace(
    "{max_remap}", str(MAX_REMAP))


def _fn(name: str, desc: str, props: dict | None = None) -> dict:
    props = {"reason": {"type": "string", "description": "이 행동을 고른 이유 (한국어 한 문장)"}, **(props or {})}
    return {"type": "function", "function": {
        "name": name, "description": desc, "strict": True,
        "parameters": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}}}


TOOLS = [
    _fn("analyze_bill", "청구서를 읽고 줄마다 원가 항목으로 분류한다(금액은 가린 채 AI 분류, 규칙 교차 확인). 총액 대조·통화·누락을 알려 준다."),
    _fn("remap_bill", "총액이 안 맞을 때 원인 가설을 붙여 청구서를 다시 분류한다.",
        {"hypothesis": {"type": "string", "description": "총액이 안 맞는 이유에 대한 가설 (숫자 없이)"}}),
    _fn("ask_user", "담당자에게 질문 카드를 띄우고 답을 기다린다. 입력칸과 선택지는 시스템이 만든다.",
        {"kind": {"type": "string", "enum": list(ASK_STEP),
                  "description": "currency=통화, interpret=청구서 해석(단위·금액 열·청구 기간) 확인, mismatch=총액 불일치 진행 여부, missing=빠진 큰 비용, assumptions=청구서 밖 가정값, "
                                 "market=시장 범위 이탈 대응, terms=장기계약 할인율, review=승인 검토 발견 사항 처리"}}),
    _fn("simulate_cost", "확정된 원가 항목과 가정값으로 원가 서버에서 GPU·시간당 원가와 제안가를 계산한다."),
    _fn("compare_market", "같은 GPU의 시장 공개가격(사업자별 대표값)과 제안가를 비교한다."),
    _fn("review_price", "원가 분해·손익분기·민감도·업계 구성비로 승인 검토를 하고 발견 사항과 판정(승인 권고/조건부/재검토)을 낸다."),
    _fn("finish", "모든 조건이 끝났을 때 견적 근거서를 만들고 마친다. 조건이 모자라면 이유를 돌려준다."),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


@dataclass
class Action:
    tool: str
    args: dict = field(default_factory=dict)
    reason: str = ""
    by: str = "policy"  # llm | policy
    call_id: str = ""


# 판단자: (messages, tools) -> {"name", "args", "id"} 또는 None(쓸 수 없음)
Controller = Callable[[list[dict], list[dict]], dict | None]


def openai_controller(api_key: str, model: str, timeout: float = 30.0, base_url: str | None = None) -> Controller:
    """OpenAI(또는 호환 sLLM) tool calling 판단자. 실패하면 None을 돌려 기본 순서로 넘어간다."""
    from openai import OpenAI

    from .. import tracing
    client = tracing.wrap_openai(OpenAI(api_key=api_key or "not-needed", timeout=timeout, base_url=base_url or None))

    def decide(messages: list[dict], tools: list[dict]) -> dict | None:
        try:
            from ..mapping.llm import create_with_fallback
            resp = create_with_fallback(client.chat.completions.create, model=model, temperature=0, messages=messages, tools=tools,
                                                  tool_choice="required", parallel_tool_calls=False)
            call = resp.choices[0].message.tool_calls[0]
            return {"name": call.function.name, "args": json.loads(call.function.arguments or "{}"), "id": call.id}
        except Exception:  # noqa: BLE001 - 판단자 실패는 기본 순서로 대체
            return None

    return decide


class GState(TypedDict, total=False):
    session: dict
    messages: list[dict]
    action: dict | None
    fails: int


class GraphCostAgent(CostAgent):
    """LangGraph 기반 원가 에이전트. API는 CostAgent와 같다 (start · answer · get)."""

    def __init__(self, bills, backend, controller: Controller | None = None, checkpointer=None):
        super().__init__(bills, backend)
        self.controller = controller
        self.graph = self._build(checkpointer or MemorySaver())

    # ------------------------------------------------------------------ 진입점
    def start_many(self, docs: list[ExtractedDocument], goal: Goal) -> AgentSession:
        if not docs:
            raise AgentError("문서를 하나 이상 올려 주세요.")
        s = AgentSession(id=uuid.uuid4().hex[:12], goal=goal, engine="graph",
                         board=[BoardStep(key=k, title=t) for k, t in STEP_TITLES.items()])
        self._log(s, Step.ANALYZE, "think", f"목표: 청구서로 원가·제안가·{'/'.join(map(str, goal.terms_years))}년 가격과 근거서를 만든다.")
        self._prepare_docs(s, docs)
        goal_text = (f"목표 GPU 모델 {'지정됨' if goal.model_id else '미정'}, GPU 수 {'지정됨' if goal.gpu_count else '미정'}, "
                     f"장기계약 {'/'.join(map(str, goal.terms_years)) or '없음'}년 가격 필요.")
        state: GState = {"session": s.model_dump(mode="json"), "fails": 0, "action": None,
                         "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": f"새 청구서가 올라왔다. {goal_text}\n{self._status(s)}"}]}
        return self._invoke(s.id, state)

    def answer(self, session_id: str, ans: Answer) -> AgentSession:
        s = self.get(session_id)
        q = s.question
        if q is None or q.id != ans.question_id:
            raise AgentError("지금 답할 질문이 아닙니다.")
        s.question = None
        getattr(self, f"_answer_{q.step.value.lower()}")(s, q, ans)  # 잘못된 입력이면 AgentError → 상태는 그대로
        if s.step != Step.DONE:
            s.step = q.step
        resume = {"session": s.model_dump(mode="json"), "observation": self._answer_text(q, ans)}
        return self._invoke(session_id, Command(resume=resume))

    def get(self, session_id: str) -> AgentSession:
        snap = self.graph.get_state(self._cfg(session_id))
        if not snap or not snap.values.get("session"):
            raise KeyError(session_id)
        return AgentSession.model_validate(snap.values["session"])

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()

    # ------------------------------------------------------------------ 그래프
    def _build(self, checkpointer):
        g = StateGraph(GState)
        g.add_node("decide", self._node_decide)
        g.add_node("act", self._node_act)
        g.add_node("wait", self._node_wait)
        g.add_edge(START, "decide")
        g.add_conditional_edges("decide", lambda st: END if self._load(st).step == Step.DONE else "act", ["act", END])
        g.add_conditional_edges("act", self._route_after_act, ["wait", "decide", END])
        g.add_conditional_edges("wait", lambda st: END if self._load(st).step == Step.DONE else "decide", ["decide", END])
        return g.compile(checkpointer=checkpointer)

    @staticmethod
    def _cfg(session_id: str) -> dict:
        from .. import tracing
        cfg = {"configurable": {"thread_id": session_id}, "recursion_limit": 200}
        if (t := tracing.current()) is not None:  # LangSmith: 가린 추적기만 붙인다 (tracing.py)
            cfg["callbacks"] = t.callbacks()
            cfg["run_name"] = "cost-agent"
            cfg["metadata"] = {"engine": "graph"}
        return cfg

    def _invoke(self, session_id: str, payload) -> AgentSession:
        out = self.graph.invoke(payload, self._cfg(session_id))
        return AgentSession.model_validate(out["session"])

    @staticmethod
    def _load(st: GState) -> AgentSession:
        return AgentSession.model_validate(st["session"])

    def _route_after_act(self, st: GState) -> str:
        s = self._load(st)
        if s.step == Step.DONE:
            return END
        return "wait" if s.question is not None else "decide"

    def _node_decide(self, st: GState) -> dict:
        s = self._load(st)
        msgs = list(st["messages"])
        action = self._choose(s, msgs, st.get("fails", 0))
        s.decisions.append({"tool": action.tool, "kind": action.args.get("kind"), "reason": action.reason, "by": action.by})
        if action.by == "llm" and action.reason:
            what = TOOL_KO.get(action.tool, action.tool) + (f"({ASK_KO.get(action.args.get('kind'), '')})" if action.tool == "ask_user" else "")
            self._log(s, self._step_of(action), "think", f"에이전트 판단 · {what}: {action.reason}")
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": action.call_id, "type": "function",
            "function": {"name": action.tool, "arguments": json.dumps({"reason": action.reason, **action.args}, ensure_ascii=False)}}]})
        return {"session": s.model_dump(mode="json"), "messages": msgs,
                "action": {"tool": action.tool, "args": action.args, "by": action.by, "call_id": action.call_id}}

    def _node_act(self, st: GState) -> dict:
        s = self._load(st)
        a = st["action"]
        step = TOOL_STEP.get(a["tool"]) or ASK_STEP.get(a["args"].get("kind"), s.step)
        if s.step != Step.DONE and step and a["tool"] != "ask_user":
            s.step = step
        obs, ok = self._run_tool(s, a["tool"], a["args"])
        self._sync_board(s)
        msgs = list(st["messages"])
        fails = 0 if ok else st.get("fails", 0) + 1
        if s.question is None:  # 질문이면 답을 받은 뒤 결과를 붙인다
            msgs.append({"role": "tool", "tool_call_id": a["call_id"], "content": f"{obs}\n{self._status(s)}"})
        return {"session": s.model_dump(mode="json"), "messages": msgs, "fails": fails}

    def _node_wait(self, st: GState) -> dict:
        s = self._load(st)
        resumed = interrupt({"question_id": s.question.id if s.question else None})
        s = AgentSession.model_validate(resumed["session"])
        self._sync_board(s)
        msgs = list(st["messages"])
        msgs.append({"role": "tool", "tool_call_id": st["action"]["call_id"],
                     "content": f"담당자 답변: {resumed['observation']}\n{self._status(s)}"})
        return {"session": s.model_dump(mode="json"), "messages": msgs, "fails": 0}

    # ------------------------------------------------------------------ 판단
    def _choose(self, s: AgentSession, msgs: list[dict], fails: int) -> Action:
        llm_turns = sum(1 for d in s.decisions if d["by"] == "llm")
        repeated = len(s.decisions) >= 3 and len({(d["tool"], d.get("kind")) for d in s.decisions[-3:]}) == 1
        if self.controller and fails < MAX_FAILS and llm_turns < MAX_DECISIONS and not repeated:
            out = self.controller(msgs, TOOLS)
            if out and out.get("name") in TOOL_NAMES:
                args = dict(out.get("args") or {})
                reason = str(args.pop("reason", "")).strip()[:300]
                if out["name"] != "ask_user" or args.get("kind") in ASK_STEP:
                    return Action(out["name"], args, reason, "llm", out.get("id") or self._call_id())
        tool, args = self._policy(s)
        return Action(tool, args, "", "policy", self._call_id())

    def _policy(self, s: AgentSession) -> tuple[str, dict]:
        """기본 순서 (워크플로와 같은 판단). 판단자를 쓸 수 없을 때 다음 도구를 고른다."""
        if s.draft is None:
            return "analyze_bill", {}
        if s.draft.currency is None:
            return "ask_user", {"kind": "currency"}
        if self._needs_interpret(s):
            return "ask_user", {"kind": "interpret"}
        if s.draft.reconciliation.matched is False and not s.mismatch_ok:
            if s.remap_tries < MAX_REMAP:
                return "remap_bill", {"hypothesis": ""}
            return "ask_user", {"kind": "mismatch"}
        if self._missing_groups(s):
            return "ask_user", {"kind": "missing"}
        if not s.assumptions.get("confirmed"):
            return "ask_user", {"kind": "assumptions"}
        if not self._sim_current(s):
            return "simulate_cost", {}
        if not self._market_current(s):
            return "compare_market", {}
        if self._market_out(s) and not s.margin_adjusted:
            return "ask_user", {"kind": "market"}
        if s.goal.terms_years and not s.terms:
            return "ask_user", {"kind": "terms"}
        if not self._review_current(s):
            return "review_price", {}
        if self._pending_findings(s):
            return "ask_user", {"kind": "review"}
        return "finish", {}

    def _unmet(self, s: AgentSession) -> list[str]:
        """완료 조건 중 아직 안 된 것 (guard)."""
        out = []
        if s.draft is None:
            return ["청구서 분석 전"]
        if s.draft.currency is None:
            out.append("통화 미확인 → ask_user(currency)")
        elif self._needs_interpret(s):
            out.append("청구서 해석 미확인 → ask_user(interpret)")
        if s.draft.reconciliation.matched is False and not s.mismatch_ok:
            out.append("총액 불일치 미해결 → remap_bill 또는 ask_user(mismatch)")
        if self._missing_groups(s):
            out.append(f"누락 비용 미처리: {', '.join(n for n, _ in self._missing_groups(s))} → ask_user(missing)")
        if not s.assumptions.get("confirmed"):
            out.append("가정값 미확정 → ask_user(assumptions)")
        elif not self._sim_current(s):
            out.append("원가 계산이 최신 입력을 반영하지 않음 → simulate_cost")
        elif not self._market_current(s):
            out.append("시장 비교가 최신 원가를 반영하지 않음 → compare_market")
        elif self._market_out(s) and not s.margin_adjusted:
            out.append("제안가가 시장 범위 밖인데 담당자 결정 없음 → ask_user(market)")
        if s.goal.terms_years and not s.terms:
            out.append("장기계약 할인율 미정 → ask_user(terms)")
        elif s.simulation is not None and not self._review_current(s):
            out.append("승인 검토가 최신 입력을 반영하지 않음 → review_price")
        elif self._pending_findings(s):
            out.append(f"발견 사항 미처리 {len(self._pending_findings(s))}개 → ask_user(review)")
        return out

    # ------------------------------------------------------------------ 도구
    def _run_tool(self, s: AgentSession, tool: str, args: dict) -> tuple[str, bool]:
        try:
            return getattr(self, f"_tool_{tool}")(s, **args), True
        except _Refused as exc:
            self._log(s, s.step if s.step != Step.DONE else Step.REPORT, "warn", f"실행 불가 · {exc}")
            return f"실행 불가: {exc}", False

    def _tool_analyze_bill(self, s: AgentSession) -> str:
        if s.draft is not None:
            raise _Refused("이미 분석했다. 재분류가 필요하면 remap_bill을 쓴다")
        self._do_ingest(s)
        return self._analysis_text(s)

    def _tool_remap_bill(self, s: AgentSession, hypothesis: str = "") -> str:
        if s.draft is None or s.draft.reconciliation.matched is not False:
            raise _Refused("총액이 맞지 않을 때만 재분류한다")
        if s.remap_tries >= MAX_REMAP:
            raise _Refused(f"재분류는 최대 {MAX_REMAP}회. ask_user(mismatch)로 담당자에게 묻는다")
        if not self._do_remap(s, hypothesis[:300]):
            return "AI 재분류를 쓸 수 없다(규칙·템플릿 분류). 담당자 확인이 필요하다."
        return self._analysis_text(s)

    def _tool_ask_user(self, s: AgentSession, kind: str) -> str:
        d = s.draft
        if d is None:
            raise _Refused("청구서를 먼저 분석한다")
        if kind == "currency":
            if d.currency is not None:
                raise _Refused("통화는 이미 확인됐다")
            q = self._q_currency(s)
        elif kind == "interpret":
            if d.currency is None or not self._needs_interpret(s):
                raise _Refused("통화를 먼저 확인한다 (확인할 청구서 해석이 없으면 묻지 않는다)")
            q = self._q_interpret(s)
        elif kind == "mismatch":
            if d.reconciliation.matched is not False or s.mismatch_ok:
                raise _Refused("총액 불일치가 없거나 이미 담당자가 진행을 정했다")
            q = self._q_mismatch(s)
        elif kind == "missing":
            groups = self._missing_groups(s)
            if not groups:
                raise _Refused("처리할 누락 비용이 없다")
            q = self._q_missing(s, [name for name, _ in groups])
        elif kind == "assumptions":
            if d.currency is None or self._needs_interpret(s) or self._missing_groups(s):
                raise _Refused("통화·청구서 해석 확인과 누락 비용 처리를 먼저 끝낸다")
            q = self._q_assumptions(s)
        elif kind == "market":
            if not self._market_current(s) or not self._market_out(s):
                raise _Refused("최신 시장 비교에서 제안가가 범위를 벗어났을 때만 묻는다")
            q = self._q_market(s)
        elif kind == "review":
            if not self._sim_current(s) or not self._review_current(s) or not self._pending_findings(s):
                raise _Refused("최신 승인 검토에 처리할 발견 사항이 있을 때만 묻는다 → review_price")
            q = self._q_review(s)
        elif kind == "terms":
            if not s.goal.terms_years or s.simulation is None:
                raise _Refused("원가 계산을 먼저 끝낸다 (장기계약 요청이 없으면 묻지 않는다)")
            q = self._q_terms(s)
        else:
            raise _Refused(f"알 수 없는 질문 종류: {kind}")
        s.question = q
        s.step = q.step
        return "질문을 띄웠다"

    def _tool_simulate_cost(self, s: AgentSession) -> str:
        if self._needs_interpret(s):
            raise _Refused("청구서 해석을 먼저 확인한다 → ask_user(interpret)")
        if not s.assumptions.get("confirmed"):
            raise _Refused("가정값이 확정되지 않았다 → ask_user(assumptions)")
        if self._missing_groups(s):
            raise _Refused("누락 비용을 먼저 처리한다 → ask_user(missing)")
        if self._sim_current(s):
            raise _Refused("원가 계산이 이미 최신이다")
        if not self._do_simulate(s):
            b = next(x for x in s.board if x.key == Step.SIMULATE)
            return "원가 계산 실패: " + " / ".join(b.details[-1:]) + " → 가정값을 다시 받아야 한다."
        missing = s.simulation.get("missingItems", [])
        return "원가 계산 완료." + (f" 원가 서버가 미반영으로 본 항목: {', '.join(missing)}" if missing else "")

    def _tool_compare_market(self, s: AgentSession) -> str:
        if not self._sim_current(s):
            raise _Refused("최신 원가 계산이 없다 → simulate_cost")
        if self._market_current(s):
            raise _Refused("시장 비교가 이미 최신이다")
        out = self._do_market(s)
        m = s.market
        if not m.count:
            self._finish_market(s)
            return "비교할 시장 공개가격이 없다."
        if not out:
            self._finish_market(s)
            return f"제안가는 시장 범위 안: {m.position} (사업자 {m.providers}곳)."
        need_ok = self._median_reachable(s)
        return (f"제안가가 시장 범위 밖: {m.position}. "
                + ("마진 조정으로 시장 중앙값에 맞출 수 있다." if need_ok else "원가가 시장 중앙값보다 높아 마진 조정만으로는 맞출 수 없다.")
                + (" 담당자가 이미 대응을 정했다." if s.margin_adjusted else " 담당자 결정이 필요하다."))

    def _tool_review_price(self, s: AgentSession) -> str:
        if not self._sim_current(s) or not self._market_current(s):
            raise _Refused("최신 원가 계산과 시장 비교를 먼저 끝낸다")
        if s.goal.terms_years and not s.terms:
            raise _Refused("장기계약 할인율을 먼저 정한다 → ask_user(terms)")
        self._do_review(s)
        ap = s.approval
        return (f"판정 {ap.verdict} · 발견 사항 {', '.join(f.code for f in ap.findings) or '없음'} "
                f"({len(ap.findings)}개, 미처리 {len(self._pending_findings(s))}개)")

    def _tool_finish(self, s: AgentSession) -> str:
        unmet = self._unmet(s)
        if unmet:
            raise _Refused("완료 조건 미충족: " + "; ".join(unmet))
        if s.terms:
            self._price_terms(s)  # 제안가가 바뀌었으면 담당자가 정한 할인율로 다시 계산
        if not any(b.key == Step.MARKET and b.status in FINAL for b in s.board):
            self._finish_market(s)
        self._do_report(s)
        if s.checks_failed:
            s.question = self._q_verify(s)
            s.step = Step.REPORT
            return f"보고서 자동 검증 실패 ({len(s.checks_failed)}개) — 담당자에게 다시 계산을 묻는다"
        s.step = Step.DONE
        return "견적 근거서를 만들고 마쳤다."

    # ------------------------------------------------------------------ 상태·관찰 (금액 없음)
    def _sim_current(self, s: AgentSession) -> bool:
        return s.simulation is not None and s.sim_basis == self._sim_basis(s)

    def _market_current(self, s: AgentSession) -> bool:
        return self._sim_current(s) and s.market is not None and s.market_basis == s.sim_basis

    @staticmethod
    def _market_out(s: AgentSession) -> bool:
        m = s.market
        if not m or not m.count or s.simulation is None:
            return False
        cand = float(s.simulation["candidatePrice"])
        return cand > m.max or cand < m.min

    def _median_reachable(self, s: AgentSession) -> bool:
        from .market import margin_for_price
        need = margin_for_price(float(s.simulation["hourlyCost"]), s.market.median, s.assumptions["pricing_method"])
        return need is not None and 0 < need < 0.95

    def _sync_board(self, s: AgentSession) -> None:
        """끝난 단계를 작업 보드에 반영한다 (순서를 판단자가 정하므로 도구가 끝날 때마다 맞춘다)."""
        d = s.draft
        if d is None or s.step == Step.DONE and s.board[0].status == Status.ERROR:
            return
        board = {b.key: b for b in s.board}
        analysis_ok = (d.currency is not None and not self._needs_interpret(s)
                       and (d.reconciliation.matched is not False or s.mismatch_ok))
        if analysis_ok and board[Step.ANALYZE].status not in FINAL:
            self._finish_analyze(s)
        if analysis_ok and not self._missing_groups(s) and board[Step.MISSING].status not in FINAL:
            self._finish_missing(s)

    def _analysis_text(self, s: AgentSession) -> str:
        from ..models import LineRole
        d = s.draft
        rec = d.reconciliation
        n_cost = sum(1 for l in d.lines if l.role == LineRole.COST)
        role_ko = {"SUBTOTAL": "소계", "TOTAL": "총액", "TAX": "부가세", "DISCOUNT": "할인", "IGNORE": "제목·메모"}
        roles = {}
        for l in d.lines:
            if l.role != LineRole.COST:
                roles[role_ko[l.role.value]] = roles.get(role_ko[l.role.value], 0) + 1
        low = sum(1 for l in d.lines if l.role == LineRole.COST and l.confidence < 0.7)
        if rec.matched is None:
            total = "대조할 총액 줄 없음"
        elif rec.matched:
            total = "일치"
        else:
            base = abs(rec.document_total or 0) or 1
            pct = abs(rec.difference or 0) / base * 100
            total = f"불일치 (원가 합계가 총액보다 {'큼' if (rec.difference or 0) > 0 else '작음'}, 차이 = 총액의 {pct:.1f}%)"
        missing = [n for n, types in BILL_CORE_ITEMS.items() if set(d.missing_items).intersection(types)]
        types = sorted({ITEM_LABEL.get(c.item_type.value, c.item_type.value) for c in d.cost_items})
        tries = f" 재분류 {s.remap_tries}/{MAX_REMAP}회 사용." if rec.matched is False else ""
        return (f"분류 방식 {d.mapper}: 원가 줄 {n_cost}개(신뢰도 낮음 {low}개), 원가 아닌 줄: "
                f"{', '.join(f'{k} {v}' for k, v in roles.items()) or '없음'}. 원가 항목: {', '.join(types) or '없음'}. "
                f"통화: {d.currency.value if d.currency else '미확인'}. 청구 기간: {d.period.value if d.period else '미확인'}. "
                f"총액 대조: {total}.{tries} 빠진 큰 비용: {', '.join(missing) or '없음'}. "
                f"해석 제안: {', '.join(i.code for i in d.interpretations) or '없음'} ({len(d.interpretations)}개, "
                f"{'담당자 확인됨' if s.interpret_ok else '미확인'}). 기준선 경고 {len(d.bench)}개."
                + (f" 승인 검토: 판정 {s.approval.verdict} · 발견 {', '.join(f.code for f in s.approval.findings) or '없음'} "
                   f"(미처리 {len(self._pending_findings(s))}개)." if s.approval else ""))

    def _status(self, s: AgentSession) -> str:
        unmet = self._unmet(s)
        return "[현재 상태] " + ("남은 조건: " + "; ".join(unmet) if unmet else "모든 조건 충족 → finish 가능")

    def _answer_text(self, q: Question, ans: Answer) -> str:
        """담당자 답을 판단자에게 금액 없이 전한다."""
        if q.step == Step.ANALYZE:
            if (ans.choice or "").startswith("currency:"):
                return f"통화 {ans.choice.split(':', 1)[1]}"
            if q.fields and q.fields[0].name.startswith(("interp:", "bench:")):
                off = [f.name.split(":", 1)[1] for f in q.fields
                       if f.name.startswith("interp:") and ans.values.get(f.name, f.value) == "off"]
                stop = any(ans.values.get(f.name, f.value) == "stop" for f in q.fields if f.name.startswith("bench:"))
                return ("청구서 해석 확인: " + (f"해제 {', '.join(off)}" if off else "제안대로 적용")
                        + (" · 담당자가 청구서 다시 보기를 골라 중단" if stop else ""))
            return "총액 차이를 알고 그대로 진행" if ans.choice == "proceed" else "중단 (분류표 수정)"
        if q.step == Step.MISSING:
            ko = {"amount": "별도 계약 금액 입력 (금액 기록됨)", "included": "다른 요금에 포함됨", "exclude": "이번엔 제외 (불완전 원가)",
                  "excluded": "이번엔 제외 (불완전 원가)"}
            parts = []
            for f in q.fields:
                if f.name.startswith("choice:"):
                    g = f.name.split(":", 1)[1]
                    c = ans.choice if ans.choice and not any(k.startswith("choice:") for k in ans.values) \
                        else ans.values.get(f.name, f.value)
                    parts.append(f"'{g}' {ko.get(c, c)}")
                elif f.name.startswith("cell:") and f.type == "select":  # 기준표 충돌 칸
                    c = ans.values.get(f.name, f.value)
                    parts.append(f"'{f.name.split(':', 1)[1]}' {'담당자 직접 입력' if c == 'manual' else '문서 값 선택'}")
            return " · ".join(parts)
        if q.step == Step.ASSUMPTIONS:
            return "가정값 확정 (GPU 모델·수, 취득비, 감가상각, 가동률, 마진 기록됨). 원가를 다시 계산해야 한다."
        if q.step == Step.MARKET:
            return {"median": "시장 중앙값에 맞추기 → 마진이 조정됨, 원가를 다시 계산해야 한다",
                    "assumptions": "가정값을 다시 확인하기로 함 → ask_user(assumptions)",
                    "keep": "제안가 유지 (시장 범위 밖으로 표시)"}.get(ans.choice or "keep", "제안가 유지")
        if q.step == Step.TERMS:
            return "장기계약 할인율 확정, 기간별 가격 계산됨"
        if q.step == Step.REPORT:
            return "자동 검증 실패 → 가정값부터 다시 계산하기로 함 → ask_user(assumptions)"
        if q.step == Step.REVIEW:
            pick = {f.name.split(":", 1)[1]: ans.values.get(f.name, f.value) for f in q.fields if f.name.startswith("finding:")}
            applied = [c for c, v in pick.items() if v == "apply"]
            fixes = [c for c, v in pick.items() if v == "fix"]
            parts = ([f"제안대로 고침 {', '.join(applied)} (기간별 가격이 바뀜 → review_price로 다시 검토)"] if applied else []) + \
                    ([f"돌아가서 고치기 {', '.join(fixes)}"] if fixes else [])
            return "승인 검토: " + (" · ".join(parts) if parts else "막는 것은 사유를 골라 진행, 참고는 확인")
        return ""

    @staticmethod
    def _step_of(a: Action) -> Step:
        return TOOL_STEP.get(a.tool) or ASK_STEP.get(a.args.get("kind"), Step.ANALYZE)

    @staticmethod
    def _call_id() -> str:
        return "call_" + uuid.uuid4().hex[:16]


ITEM_LABEL: dict[str, str] = {t.value: k for k, v in {
    "전기료": ItemType.ELECTRICITY, "랙·상면": ItemType.RACK_FACILITY, "임차료": ItemType.RENT,
    "네트워크·스토리지": ItemType.NETWORK_STORAGE, "운영·유지보수": ItemType.OPERATIONS, "메모리": ItemType.MEMORY,
    "GPU 임차": ItemType.GPU, "취득비": ItemType.ACQUISITION, "기타": ItemType.OTHER}.items() for t in [v]}


class _Refused(Exception):
    """도구의 전제 조건이 맞지 않음. 판단자에게 이유를 돌려준다."""


def build_agent(engine: str, bills, backend, controller: Controller | None = None, checkpointer=None) -> CostAgent:
    if engine == "workflow":
        return CostAgent(bills, backend)
    return GraphCostAgent(bills, backend, controller=controller, checkpointer=checkpointer)


def checkpointer_from(path: str | None):
    """CHECKPOINT_DB가 있으면 SQLite에 세션을 저장한다 (langgraph-checkpoint-sqlite 필요). 없으면 메모리."""
    if not path:
        return MemorySaver()
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


__all__ = ["GraphCostAgent", "openai_controller", "build_agent", "checkpointer_from", "TOOLS", "SYSTEM_PROMPT", "Action"]
