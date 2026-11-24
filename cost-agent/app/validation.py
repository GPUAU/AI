"""매핑 결과를 코드로 검증하고 BE 원가 항목으로 합친다. (LLM 판단을 그대로 믿지 않는 단계)"""

from __future__ import annotations

from collections import defaultdict

from .models import (
    CostItemDraft,
    Currency,
    Issue,
    IssueLevel,
    ItemStatus,
    ItemType,
    LineRole,
    MappedLine,
    Period,
    Reconciliation,
)

# IDC 청구서에 보통 있어야 하는 큰 비용. 없으면 '다른 계약에 포함됐는지' 확인을 요청한다.
BILL_CORE_ITEMS: dict[str, tuple[ItemType, ...]] = {
    "전력": (ItemType.ELECTRICITY,),
    "공간·상면": (ItemType.RACK_FACILITY, ItemType.RENT),
    "네트워크": (ItemType.NETWORK_STORAGE,),
    "운영·유지보수": (ItemType.OPERATIONS,),
}
LOW_CONFIDENCE = 0.7
OTHER_SHARE_LIMIT = 0.2


def _tolerance(total: float, currency: Currency | None) -> float:
    base = 1.0 if currency == Currency.KRW else 0.01
    return max(base, abs(total) * 0.001)


def reconcile(lines: list[MappedLine], currency: Currency | None) -> tuple[Reconciliation, list[Issue]]:
    issues: list[Issue] = []
    cost_sum = sum(l.amount or 0 for l in lines if l.role == LineRole.COST)
    discount = sum(-abs(l.amount or 0) for l in lines if l.role == LineRole.DISCOUNT)
    tax = sum(l.amount or 0 for l in lines if l.role == LineRole.TAX) or None
    totals = [l for l in lines if l.role == LineRole.TOTAL and l.amount is not None]
    subtotals = [l for l in lines if l.role == LineRole.SUBTOTAL and l.amount is not None]
    # 가장 큰 총액 줄을 문서 총액으로 본다 (요약 시트와 상세 시트에 같은 총액이 있을 수 있음)
    total = max((l.amount for l in totals), default=None)

    candidates: list[tuple[str, float]] = []
    net = cost_sum + discount
    candidates.append(("원가 항목 합계", cost_sum))
    if discount:
        candidates.append(("원가 항목 합계 − 할인", net))
    if tax:
        candidates.append(("원가 항목 합계 + 부가세", cost_sum + tax))
        if discount:
            candidates.append(("원가 항목 합계 − 할인 + 부가세", net + tax))

    targets = [("청구 총액", total)] if total is not None else []
    targets += [("소계·공급가액", s.amount) for s in subtotals]
    if not targets:
        rec = Reconciliation(cost_sum=cost_sum, tax=tax, matched=None, basis="총액 줄 없음")
        issues.append(Issue(level=IssueLevel.WARNING, code="NO_TOTAL",
                            message="청구서에서 총액 줄을 찾지 못해 금액 합계를 대조하지 못했습니다. 누락된 줄이 없는지 확인하세요."))
        return rec, issues

    for target_name, target in targets:
        for name, value in candidates:
            if abs(value - target) <= _tolerance(target, currency):
                return Reconciliation(cost_sum=cost_sum, document_total=target, tax=tax, matched=True,
                                      difference=round(value - target, 2), basis=f"{name} = {target_name}"), issues

    target_name, target = targets[0]
    diff = round(cost_sum - target, 2)
    issues.append(Issue(
        level=IssueLevel.WARNING, code="TOTAL_MISMATCH",
        message=f"원가 항목 합계({cost_sum:,.2f})가 {target_name}({target:,.2f})와 {diff:+,.2f} 차이 납니다. "
                "빠지거나 잘못 분류된 줄이 있는지 확인하세요.",
    ))
    return Reconciliation(cost_sum=cost_sum, document_total=target, tax=tax, matched=False,
                          difference=diff, basis="불일치"), issues


def check_lines(lines: list[MappedLine]) -> list[Issue]:
    issues: list[Issue] = []
    for l in lines:
        if l.role == LineRole.COST and l.amount is None:
            issues.append(Issue(level=IssueLevel.ERROR, code="NO_AMOUNT", ref=l.ref,
                                message=f"'{l.label}' 줄의 금액 칸을 찾지 못했습니다."))
        elif l.role == LineRole.COST and l.amount is not None and l.amount < 0:
            issues.append(Issue(level=IssueLevel.WARNING, code="NEGATIVE_COST", ref=l.ref,
                                message=f"'{l.label}' 금액이 음수입니다. 할인 줄인지 확인하세요."))
        if l.role != LineRole.IGNORE and l.confidence < LOW_CONFIDENCE:
            issues.append(Issue(level=IssueLevel.WARNING, code="LOW_CONFIDENCE", ref=l.ref,
                                message=f"'{l.label}' → {l.item_type.value if l.item_type else l.role.value} 분류가 확실하지 않습니다. ({l.reason})"))
    return issues


def check_coverage(lines: list[MappedLine]) -> list[Issue]:
    present: set[ItemType] = set()
    for l in lines:
        if l.role == LineRole.COST and l.item_type:
            present.add(l.item_type)
            present.update(l.included_items)
    issues = []
    for name, types in BILL_CORE_ITEMS.items():
        if not present.intersection(types):
            issues.append(Issue(level=IssueLevel.WARNING, code="MISSING_CORE_ITEM",
                                message=f"청구서에 '{name}' 비용이 없습니다. 다른 계약에 포함됐거나 별도 청구인지 확인하세요. "
                                        "큰 비용이 빠지면 원가 전체를 믿을 수 없습니다."))
    issues.append(Issue(level=IssueLevel.INFO, code="ACQUISITION_OUTSIDE_BILL",
                        message="GPU·서버 취득비는 IDC 청구서에 없는 항목입니다. 원가 계산 때 별도로 입력하세요."))
    cost_sum = sum(abs(l.amount or 0) for l in lines if l.role == LineRole.COST)
    other = sum(abs(l.amount or 0) for l in lines if l.role == LineRole.COST and l.item_type == ItemType.OTHER)
    if cost_sum and other / cost_sum > OTHER_SHARE_LIMIT:
        issues.append(Issue(level=IssueLevel.WARNING, code="OTHER_TOO_LARGE",
                            message=f"'기타'로 분류된 금액이 전체의 {other / cost_sum:.0%}입니다. 실제 항목으로 다시 분류하세요."))
    return issues


def aggregate(lines: list[MappedLine], currency: Currency | None, default_period: Period | None
              ) -> tuple[list[CostItemDraft], list[Issue]]:
    """BE 규칙: 같은 항목은 한 번만, 기간은 YEAR(운영비) 또는 ONE_TIME(취득비)만 가능.

    - 같은 항목의 여러 줄은 합친다.
    - 월 청구는 ×12 해서 연간으로 바꾼다.
    - 1회성 비용은 취득비(ACQUISITION)에 합친다.
    - 다른 줄에 묶인 항목은 INCLUDED_IN_OTHER로 표시한다 (금액 없음).
    """
    issues: list[Issue] = []
    yearly: dict[ItemType, float] = defaultdict(float)
    refs: dict[ItemType, list[str]] = defaultdict(list)
    included: dict[ItemType, ItemType] = {}
    converted_month = False
    for l in lines:
        if l.role != LineRole.COST or l.item_type is None or l.amount is None:
            continue
        period = l.period or default_period or Period.MONTH
        item = l.item_type
        amount = l.amount
        if period == Period.ONE_TIME:
            if item != ItemType.ACQUISITION:
                issues.append(Issue(level=IssueLevel.INFO, code="ONE_TIME_TO_ACQUISITION", ref=l.ref,
                                    message=f"'{l.label}'은 1회성 비용이라 초기 투자(취득비)에 합쳤습니다."))
            item = ItemType.ACQUISITION
        elif period == Period.MONTH:
            amount *= 12
            converted_month = True
        yearly[item] += amount
        refs[item].append(l.ref)
        for inc in l.included_items:
            included.setdefault(inc, l.item_type)
    if converted_month:
        issues.append(Issue(level=IssueLevel.INFO, code="MONTH_TO_YEAR",
                            message="월 청구 금액을 12배 해서 연간 운영비로 바꿨습니다. (한 달 청구서 기준 추정)"))
    if any(l.role == LineRole.DISCOUNT for l in lines):
        issues.append(Issue(level=IssueLevel.INFO, code="DISCOUNT_EXCLUDED",
                            message="할인 줄은 원가에서 빼지 않았습니다. 일시 할인이면 그대로, 계약 할인이면 담당자가 반영하세요."))

    drafts: list[CostItemDraft] = []
    for item, amount in yearly.items():
        drafts.append(CostItemDraft(
            item_type=item, status=ItemStatus.USER_INPUT, amount=round(amount, 2), currency=currency,
            period=Period.ONE_TIME if item == ItemType.ACQUISITION else Period.YEAR, source_refs=refs[item],
        ))
    for item, parent in included.items():
        if item not in yearly:
            drafts.append(CostItemDraft(item_type=item, status=ItemStatus.INCLUDED_IN_OTHER, included_in=parent))
    if currency is None and drafts:
        issues.append(Issue(level=IssueLevel.ERROR, code="NO_CURRENCY",
                            message="통화(원/달러)를 알 수 없습니다. 통화를 지정하세요."))
    return drafts, issues
