"""청구서 매핑 파이프라인.

엑셀 읽기 → (템플릿 있으면 템플릿 / 없으면 LLM, 실패하면 규칙) → 금액은 코드가 칸에서 읽기
→ 코드 검증(총액 대조·큰 항목 누락·확신도) → BE 원가 항목으로 합치기
"""

from __future__ import annotations

import uuid
from typing import Protocol

from .mapping.llm import LLMMappingError
from .mapping.rules import RulesMapper, detect_period
from .mapping.templates import TemplateStore, apply_template, fingerprint, template_from_lines
from .models import (
    BillDraft,
    CorrectionRequest,
    DocumentMapping,
    ExtractedDocument,
    Issue,
    IssueLevel,
    LLMDisclosure,
    LineRole,
    MappedLine,
    Period,
    RowMapping,
)
from .validation import aggregate, check_coverage, check_lines, reconcile


class Mapper(Protocol):
    name: str

    def map(self, doc: ExtractedDocument) -> DocumentMapping: ...


class BillService:
    def __init__(self, mapper: Mapper, templates: TemplateStore, fallback: Mapper | None = None):
        self.mapper = mapper
        self.fallback = fallback or RulesMapper()
        self.templates = templates
        # MVP: 진행 중인 초안은 메모리에 둔다 (Pod 재시작 시 사라짐). 확정 템플릿만 파일로 저장.
        self._docs: dict[str, ExtractedDocument] = {}
        self._mappings: dict[str, DocumentMapping] = {}
        self._drafts: dict[str, BillDraft] = {}

    # ------------------------------------------------------------ 처음 올릴 때
    def ingest(self, doc: ExtractedDocument) -> BillDraft:
        fp = fingerprint(doc)
        issues: list[Issue] = []
        if doc.truncated:
            issues.append(Issue(level=IssueLevel.WARNING, code="TRUNCATED",
                                message="파일이 길어 앞부분만 읽었습니다. 나머지 줄이 원가에 빠졌을 수 있습니다."))
        template = self.templates.get(fp)
        mapping: DocumentMapping | None = None
        used = ""
        if template:
            rows = apply_template(doc, template)
            if rows is not None:
                mapping = DocumentMapping(vendor=template.vendor, currency=template.currency,
                                          default_period=template.default_period, rows=rows)
                used = "template"
        if mapping is None:
            try:
                mapping = self.mapper.map(doc)
                used = self.mapper.name
                if used == "llm":
                    mapping = self._cross_check(doc, mapping)
            except LLMMappingError as exc:
                issues.append(Issue(level=IssueLevel.WARNING, code="LLM_FAILED",
                                    message=f"AI 분류에 실패해 키워드 규칙으로 분류했습니다. 결과를 꼭 확인하세요. ({exc})"))
                mapping = self.fallback.map(doc)
                used = self.fallback.name
        if mapping.currency is None:
            mapping = mapping.model_copy(update={"currency": doc.currency_hint})
        # 청구 기간은 숫자를 가리기 전 원문에서 코드가 판단한다 (LLM은 가려진 날짜를 볼 수 없음)
        period = detect_period(doc)
        if period and period != mapping.default_period:
            mapping = mapping.model_copy(update={"default_period": period})
        elif mapping.default_period is None:
            mapping = mapping.model_copy(update={"default_period": Period.MONTH})
        draft_id = uuid.uuid4().hex[:12]
        self._docs[draft_id] = doc
        self._mappings[draft_id] = mapping
        draft = self._build(draft_id, doc, mapping, used, fp, issues)
        self._drafts[draft_id] = draft
        return draft

    # ------------------------------------------------------------ 담당자 수정·확정
    def correct(self, draft_id: str, req: CorrectionRequest) -> BillDraft:
        doc, mapping = self._docs[draft_id], self._mappings[draft_id]
        by_ref = {m.ref: m for m in mapping.rows}
        for c in req.corrections:
            if c.ref not in {r.ref for r in doc.rows}:
                raise KeyError(c.ref)
            base = by_ref.get(c.ref) or RowMapping(ref=c.ref, role=LineRole.IGNORE)
            update = {k: v for k, v in c.model_dump(exclude={"ref"}).items() if v is not None}
            if update.get("role") and update["role"] != LineRole.COST:
                update.setdefault("item_type", None)
            row = next(r for r in doc.rows if r.ref == c.ref)
            if base.amount_col is None and row.numbers:
                update["amount_col"] = max(row.numbers)
            by_ref[c.ref] = base.model_copy(update={**update, "confidence": 1.0, "reason": "담당자 수정"})
        mapping = mapping.model_copy(update={"rows": list(by_ref.values())})
        self._mappings[draft_id] = mapping
        prev = self._drafts[draft_id]
        draft = self._build(draft_id, doc, mapping, prev.mapper, prev.fingerprint, [])
        if req.save_template:
            if any(i.level == IssueLevel.ERROR for i in draft.issues):
                draft.issues.append(Issue(level=IssueLevel.WARNING, code="TEMPLATE_NOT_SAVED",
                                          message="오류가 남아 있어 템플릿을 저장하지 않았습니다."))
            else:
                self.templates.save(template_from_lines(
                    doc, {m.ref: m for m in mapping.rows}, prev.fingerprint,
                    mapping.vendor, mapping.currency, mapping.default_period))
                draft.issues.append(Issue(level=IssueLevel.INFO, code="TEMPLATE_SAVED",
                                          message="이 양식을 템플릿으로 저장했습니다. 같은 양식은 다음부터 AI 없이 바로 처리됩니다."))
        self._drafts[draft_id] = draft
        return draft

    def _cross_check(self, doc: ExtractedDocument, mapping: DocumentMapping) -> DocumentMapping:
        """AI 결과를 키워드 규칙과 교차 확인한다. 같은 항목으로 본 줄에서 규칙이 찾은 '포함 항목'을 더한다
        (예: '통합 패키지 (상면·전력·회선·관제 포함)'에서 AI가 전력만 잡았을 때 회선·관제를 보탠다)."""
        rules = {m.ref: m for m in self.fallback.map(doc).rows}
        rows = []
        for m in mapping.rows:
            r = rules.get(m.ref)
            if m.role == LineRole.COST and r and r.role == LineRole.COST and r.included_items:
                extra = [t for t in r.included_items if t not in m.included_items and t != m.item_type]
                if extra:
                    m = m.model_copy(update={"included_items": [*m.included_items, *extra],
                                             "reason": m.reason + f" (+규칙 교차 확인: {', '.join(t.value for t in extra)} 포함)"})
            rows.append(m)
        return mapping.model_copy(update={"rows": rows})

    def remap(self, draft_id: str, feedback: str) -> BillDraft | None:
        """총액이 안 맞을 때 검토 결과를 주고 LLM에 다시 분류시킨다. LLM 매퍼가 아니면 None."""
        if not hasattr(self.mapper, "mask"):
            return None
        doc, prev = self._docs[draft_id], self._drafts[draft_id]
        try:
            mapping = self.mapper.map(doc, feedback=feedback)
        except LLMMappingError:
            return None
        if mapping.currency is None:
            mapping = mapping.model_copy(update={"currency": doc.currency_hint})
        period = detect_period(doc) or mapping.default_period or Period.MONTH
        mapping = mapping.model_copy(update={"default_period": period})
        self._mappings[draft_id] = mapping
        draft = self._build(draft_id, doc, mapping, self.mapper.name, prev.fingerprint, [])
        self._drafts[draft_id] = draft
        return draft

    def set_currency(self, draft_id: str, currency) -> BillDraft:
        """통화를 판단하지 못한 청구서에 담당자가 정한 통화를 적용한다."""
        doc, prev = self._docs[draft_id], self._drafts[draft_id]
        mapping = self._mappings[draft_id].model_copy(update={"currency": currency})
        self._mappings[draft_id] = mapping
        draft = self._build(draft_id, doc, mapping, prev.mapper, prev.fingerprint, [])
        self._drafts[draft_id] = draft
        return draft

    def get(self, draft_id: str) -> BillDraft:
        return self._drafts[draft_id]

    # ------------------------------------------------------------ 공통
    def _build(self, draft_id: str, doc: ExtractedDocument, mapping: DocumentMapping, used: str,
               fp: str, issues: list[Issue]) -> BillDraft:
        by_ref = {m.ref: m for m in mapping.rows}
        lines: list[MappedLine] = []
        for row in doc.rows:
            m = by_ref.get(row.ref)
            if m is None:
                if row.numbers and row.label:
                    issues.append(Issue(level=IssueLevel.WARNING, code="UNMAPPED_ROW", ref=row.ref,
                                        message=f"'{row.label}' 줄이 분류되지 않았습니다."))
                continue
            amount = row.numbers.get(m.amount_col) if m.amount_col is not None else None  # 숫자는 코드가 읽는다
            source = "user" if m.reason == "담당자 수정" else used
            lines.append(MappedLine(
                ref=row.ref, label=row.label, role=m.role,
                item_type=m.item_type if m.role == LineRole.COST else None,
                amount=amount, period=m.period, included_items=m.included_items,
                confidence=m.confidence, reason=m.reason, source=source,
            ))
        rec, rec_issues = reconcile(lines, mapping.currency)
        items, agg_issues = aggregate(lines, mapping.currency, mapping.default_period)
        all_issues = issues + check_lines(lines) + rec_issues + check_coverage(lines) + agg_issues
        missing = [t for i in all_issues if i.code == "MISSING_CORE_ITEM" for t in i.items]
        llm = (LLMDisclosure(destination=mapping.llm_destination or "LLM", masked=bool(mapping.llm_masked),
                             sent=mapping.llm_input) if mapping.llm_input else None)
        return BillDraft(
            id=draft_id, file_name=doc.file_name, fingerprint=fp, mapper=used,
            vendor=mapping.vendor, currency=mapping.currency, period=mapping.default_period,
            lines=lines, reconciliation=rec, cost_items=items, issues=all_issues,
            needs_review=any(i.level in (IssueLevel.ERROR, IssueLevel.WARNING) for i in all_issues),
            missing_items=missing, llm=llm,
        )
