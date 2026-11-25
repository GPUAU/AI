"""담당자가 확인한 매핑을 양식별 템플릿으로 저장하고, 같은 양식이 다시 오면 LLM 없이 적용한다."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel

from ..models import Currency, ExtractedDocument, ItemType, LineRole, Period, RowMapping, SheetRow


def normalize_label(text: str) -> str:
    """금액·날짜·수량처럼 달마다 바뀌는 숫자를 지운 항목명."""
    t = text.lower()
    t = re.sub(r"[0-9][0-9,.\-/:]*", "#", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def row_key(row: SheetRow) -> str:
    return f"{row.sheet}|{normalize_label(row.label)}"


def fingerprint(doc: ExtractedDocument) -> str:
    """숫자가 있는 줄의 항목명 집합으로 양식을 식별한다. 금액이 달라져도 같은 양식이면 같은 값."""
    keys = sorted({row_key(r) for r in doc.rows if r.numbers and r.label})
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


class TemplateRule(BaseModel):
    role: LineRole
    item_type: ItemType | None = None
    amount_col: int | None = None
    included_items: list[ItemType] = []
    period: Period | None = None


class Template(BaseModel):
    fingerprint: str
    file_name: str
    vendor: str | None = None
    currency: Currency | None = None
    default_period: Period | None = None
    rules: dict[str, TemplateRule]


class TemplateStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def _path(self, fp: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", fp):
            raise ValueError("잘못된 템플릿 식별자")
        return self.dir / f"{fp}.json"

    def get(self, fp: str) -> Template | None:
        p = self._path(fp)
        if not p.exists():
            return None
        return Template.model_validate_json(p.read_text(encoding="utf-8"))

    def save(self, template: Template) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(template.fingerprint).write_text(
            template.model_dump_json(indent=2), encoding="utf-8"
        )

    def list(self) -> list[Template]:
        if not self.dir.exists():
            return []
        return [Template.model_validate_json(p.read_text(encoding="utf-8")) for p in sorted(self.dir.glob("*.json"))]


def apply_template(doc: ExtractedDocument, template: Template) -> list[RowMapping] | None:
    """모든 숫자 줄이 템플릿에 있으면 매핑을 돌려준다. 하나라도 없으면 None (양식이 바뀜)."""
    result = []
    for row in doc.rows:
        rule = template.rules.get(row_key(row))
        if rule is None:
            if row.numbers and row.label:
                return None
            result.append(RowMapping(ref=row.ref, role=LineRole.IGNORE, confidence=1, reason="템플릿: 숫자 없는 줄"))
            continue
        col = rule.amount_col if rule.amount_col in row.numbers else None
        result.append(
            RowMapping(
                ref=row.ref,
                role=rule.role,
                item_type=rule.item_type,
                amount_col=col,
                included_items=rule.included_items,
                period=rule.period,
                confidence=1.0 if (col is not None or rule.role == LineRole.IGNORE) else 0.3,
                reason="담당자가 확인한 템플릿",
            )
        )
    return result


def template_from_lines(doc: ExtractedDocument, mappings: dict[str, RowMapping], fp: str,
                        vendor: str | None, currency: Currency | None, period: Period | None) -> Template:
    rules: dict[str, TemplateRule] = {}
    for row in doc.rows:
        m = mappings.get(row.ref)
        if m is None or not row.label:
            continue
        rules[row_key(row)] = TemplateRule(
            role=m.role, item_type=m.item_type, amount_col=m.amount_col,
            included_items=m.included_items, period=m.period,
        )
    return Template(fingerprint=fp, file_name=doc.file_name, vendor=vendor, currency=currency,
                    default_period=period, rules=rules)


def dumps(t: Template) -> str:
    return json.dumps(t.model_dump(mode="json"), ensure_ascii=False)
