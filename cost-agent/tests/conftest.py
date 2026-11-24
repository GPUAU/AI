import importlib.util
from pathlib import Path

import pytest

from app.mapping.rules import RulesMapper
from app.mapping.templates import TemplateStore
from app.parsing import extract
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
