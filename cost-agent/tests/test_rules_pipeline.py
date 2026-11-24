from app.models import IssueLevel, ItemStatus, ItemType, Period


def codes(draft):
    return {i.code for i in draft.issues}


def items(draft):
    return {c.item_type: c for c in draft.cost_items}


def test_bill_a_domestic(service, doc):
    draft = service.ingest(doc("a"))
    assert draft.reconciliation.matched is True
    it = items(draft)
    assert set(it) == {ItemType.RACK_FACILITY, ItemType.ELECTRICITY, ItemType.NETWORK_STORAGE, ItemType.OPERATIONS}
    assert it[ItemType.ELECTRICITY].amount == 28_800 * 180 * 12  # 월 → 연
    assert all(c.period == Period.YEAR for c in draft.cost_items)
    assert "MISSING_CORE_ITEM" not in codes(draft)


def test_bill_b_bundled_power_is_included_in_rent(service, doc):
    draft = service.ingest(doc("b"))
    it = items(draft)
    assert it[ItemType.RENT].amount == 18_500 * 12
    assert it[ItemType.ELECTRICITY].status == ItemStatus.INCLUDED_IN_OTHER
    assert it[ItemType.ELECTRICITY].included_in == ItemType.RENT
    assert it[ItemType.ELECTRICITY].amount is None
    assert draft.reconciliation.matched is True
    assert not any(i.code == "MISSING_CORE_ITEM" and "전력" in i.message for i in draft.issues)


def test_bill_c_multisheet_discount_tax_and_missing_operations(service, doc):
    draft = service.ingest(doc("c"))
    assert draft.reconciliation.matched is True
    assert "할인" in draft.reconciliation.basis and "부가세" in draft.reconciliation.basis
    assert any(i.code == "MISSING_CORE_ITEM" and "운영" in i.message for i in draft.issues)
    assert items(draft)[ItemType.ACQUISITION].period == Period.ONE_TIME  # (1회) 수수료
    assert draft.needs_review


def test_total_mismatch_is_reported(service, doc):
    d = doc("a")
    # 전력 줄을 지워서 합계와 맞지 않게 만든다
    d = d.model_copy(update={"rows": [r for r in d.rows if "전력" not in r.label]})
    draft = service.ingest(d)
    assert draft.reconciliation.matched is False
    assert "TOTAL_MISMATCH" in codes(draft)
    assert any(i.level == IssueLevel.WARNING and "전력" in i.message for i in draft.issues)
