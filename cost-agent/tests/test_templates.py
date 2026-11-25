from app.mapping.templates import TemplateStore, fingerprint
from app.models import CorrectionRequest, ItemType, LineCorrection
from app.service import BillService


class ExplodingMapper:
    name = "llm"

    def map(self, doc):
        raise AssertionError("템플릿이 있으면 AI를 부르면 안 된다")


def test_correction_saves_template_and_next_upload_skips_ai(tmp_path, service, doc):
    d = doc("c")
    draft = service.ingest(d)
    card = next(l for l in draft.lines if "출입 카드" in l.label)
    fixed = service.correct(draft.id, CorrectionRequest(corrections=[
        LineCorrection(ref=card.ref, item_type=ItemType.OPERATIONS)]))
    assert any(i.code == "TEMPLATE_SAVED" for i in fixed.issues)
    assert next(l for l in fixed.lines if l.ref == card.ref).source == "user"

    s2 = BillService(ExplodingMapper(), service.templates)
    again = s2.ingest(d)
    assert again.mapper == "template"
    assert next(l for l in again.lines if l.ref == card.ref).item_type == ItemType.OPERATIONS
    assert all(l.confidence == 1.0 for l in again.lines if l.amount is not None)


def test_fingerprint_ignores_amount_changes(doc):
    d = doc("a")
    rows = []
    for r in d.rows:
        rows.append(r.model_copy(update={"numbers": {k: v * 2 for k, v in r.numbers.items()}}))
    assert fingerprint(d) == fingerprint(d.model_copy(update={"rows": rows}))


def test_changed_layout_does_not_use_template(tmp_path, service, doc):
    d = doc("a")
    draft = service.ingest(d)
    service.correct(draft.id, CorrectionRequest())
    extra = d.rows[5].model_copy(update={"ref": "청구서!99", "row": 99, "cells": ["", "신규 항목", "", "", "", "100"],
                                         "numbers": {5: 100.0}})
    changed = d.model_copy(update={"rows": [*d.rows, extra]})
    assert fingerprint(changed) != fingerprint(d)
    assert service.ingest(changed).mapper == "rules"


def test_template_not_saved_when_errors_remain(tmp_path, service, doc):
    d = doc("a")
    draft = service.ingest(d)
    elec = next(l for l in draft.lines if "전력" in l.label)
    # 통화를 알 수 없게 만들면 ERROR
    service._mappings[draft.id] = service._mappings[draft.id].model_copy(update={"currency": None})
    out = service.correct(draft.id, CorrectionRequest(corrections=[LineCorrection(ref=elec.ref)]))
    assert any(i.code == "TEMPLATE_NOT_SAVED" for i in out.issues)
    assert TemplateStore(service.templates.dir).list() == []
