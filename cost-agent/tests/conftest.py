import importlib.util
from pathlib import Path

import pytest

from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.models import ExtractedDocument
from app.parsing import _make_row, detect_currency, extract
from app.service import BillService

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("make_samples", ROOT / "scripts" / "make_samples.py")
make_samples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_samples)


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("samples")
    paths = {"a": d / "bill_a.xlsx", "b": d / "bill_b.xlsx", "c": d / "bill_c.xlsx"}
    make_samples.bill_a(paths["a"])
    make_samples.bill_b(paths["b"])
    make_samples.bill_c(paths["c"])
    return paths


@pytest.fixture
def doc(samples):
    def load(key: str):
        p = samples[key]
        return extract(p.name, p.read_bytes())
    return load


@pytest.fixture
def service(tmp_path) -> BillService:
    return BillService(RulesMapper(), TemplateStore(tmp_path / "templates"))


# ---------------------------------------------------------------- 상세 청구서 샘플 (AI Factory 규모)
_dspec = importlib.util.spec_from_file_location("make_detailed_samples", ROOT / "scripts" / "make_detailed_samples.py")
make_detailed_samples = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(make_detailed_samples)
BUILDERS = make_detailed_samples.BUILDERS


def doc_from_rows(rows: list[list], sheet: str = "s") -> ExtractedDocument:
    """테스트용: 행 목록으로 바로 문서를 만든다 (엑셀 파일 없이)."""
    made = [r for i, values in enumerate(rows, start=1) if (r := _make_row(sheet, i, list(values)))]
    return ExtractedDocument(file_name="t.xlsx", sheets=[sheet], rows=made, currency_hint=detect_currency(made))


@pytest.fixture(scope="session")
def detailed(tmp_path_factory) -> dict[str, tuple[Path, dict]]:
    d = tmp_path_factory.mktemp("detailed")
    return {name: (d / name, build(d / name)) for name, build in BUILDERS.items()}


@pytest.fixture
def detailed_doc(detailed):
    def load(name: str) -> ExtractedDocument:
        p = detailed[name][0]
        return extract(p.name, p.read_bytes())
    return load


@pytest.fixture(scope="session")
def samples_all() -> list[ExtractedDocument]:
    return [extract(p.name, p.read_bytes()) for p in sorted((ROOT / "samples").iterdir())]
