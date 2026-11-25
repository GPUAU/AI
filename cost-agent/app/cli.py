"""터미널에서 청구서 한 개를 매핑해 본다.

    python -m app.cli samples/bill_a_domestic_krw.xlsx            # 키가 있으면 AI, 없으면 규칙
    python -m app.cli samples/bill_b_colo_usd.xlsx --mapper rules
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .config import Settings
from .main import build_service
from .parsing import extract


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--mapper", choices=["auto", "openai", "rules"])
    args = p.parse_args()
    if args.mapper:
        os.environ["MAPPER"] = args.mapper
    service = build_service(Settings.from_env())
    path = Path(args.file)
    draft = service.ingest(extract(path.name, path.read_bytes()))

    print(f"\n[{draft.file_name}] 매핑 방식={draft.mapper} 통화={draft.currency and draft.currency.value} "
          f"기간={draft.period and draft.period.value}")
    print("-" * 96)
    for l in draft.lines:
        if l.role.value == "IGNORE":
            continue
        kind = l.item_type.value if l.item_type else l.role.value
        inc = f" (+포함 {','.join(i.value for i in l.included_items)})" if l.included_items else ""
        amt = f"{l.amount:>16,.2f}" if l.amount is not None else f"{'?':>16}"
        print(f"{l.ref:<14} {l.label[:34]:<36} {kind:<16}{amt}  신뢰도 {l.confidence:.2f}{inc}")
    r = draft.reconciliation
    print("-" * 96)
    print(f"총액 대조: {'일치' if r.matched else ('불일치' if r.matched is False else '대조 불가')} ({r.basis})")
    print("\nBE 원가 항목 (연간 기준):")
    for it in draft.cost_items:
        amt = f"{it.amount:,.2f} {it.currency.value if it.currency else ''} / {it.period.value}" if it.amount is not None else ""
        extra = f" → {it.included_in.value}에 포함" if it.included_in else ""
        print(f"  - {it.item_type.value:<16} {it.status.value:<18} {amt}{extra}")
    print("\n점검 결과:")
    for i in draft.issues:
        print(f"  [{i.level.value}] {i.message}")


if __name__ == "__main__":
    main()
