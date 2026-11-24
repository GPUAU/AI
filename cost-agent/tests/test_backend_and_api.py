import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.be_client import BackendClient, BackendError, build_request
from app.config import Settings
from app.main import create_app
from app.models import CostItemDraft, Currency, ItemStatus, ItemType, Period, SimulationInput

ALLOWED_ITEM_FIELDS = {"itemType", "status", "amount", "currency", "period", "region", "referenceDate",
                       "sourceUrl", "includedInItemType"}


def inp(**kw):
    return SimulationInput(model_id="7", gpu_count=64, **kw)


def test_build_request_follows_be_rules(service, doc):
    draft = service.ingest(doc("b"))
    req = build_request(draft.cost_items, inp(acquisition_amount=2_000_000, fx_krw_per_usd=1400))
    types = [i["itemType"] for i in req["items"]]
    assert len(types) == len(set(types))                               # 항목 중복 금지
    for i in req["items"]:
        assert set(i) <= ALLOWED_ITEM_FIELDS                           # 모르는 필드 금지
        assert i.get("period") != "MONTH"                              # 기본 프로필은 MONTH 불가
        if i["itemType"] != "ACQUISITION":
            assert i.get("period") in (None, "YEAR")
        if i["status"] == "INCLUDED_IN_OTHER":
            assert "amount" not in i and i["includedInItemType"] == "RENT"
    acq = next(i for i in req["items"] if i["itemType"] == "ACQUISITION")
    assert acq == {"itemType": "ACQUISITION", "status": "USER_INPUT", "amount": 2_000_000,
                   "currency": "USD", "period": "ONE_TIME"}
    assert req["assumptions"]["interestRateType"] == "NOT_APPLIED"


def test_one_time_bill_cost_is_added_to_acquisition_input():
    items = [CostItemDraft(item_type=ItemType.ACQUISITION, status=ItemStatus.USER_INPUT, amount=1_400_000,
                           currency=Currency.KRW, period=Period.ONE_TIME)]
    req = build_request(items, inp(acquisition_amount=1000, fx_krw_per_usd=1400))
    assert req["items"] == [{"itemType": "ACQUISITION", "status": "USER_INPUT", "amount": 2000.0,
                             "currency": "USD", "period": "ONE_TIME"}]
    with pytest.raises(BackendError):
        build_request(items, inp(acquisition_amount=1000))  # 통화가 섞였는데 환율 없음


def mock_backend(handler):
    return BackendClient("http://be", transport=httpx.MockTransport(handler))


def test_backend_client_success_and_validation_error():
    ok = mock_backend(lambda r: httpx.Response(200, json={"isSuccess": True, "result": {"hourlyCost": "1.23"}}))
    assert ok.simulate({})["hourlyCost"] == "1.23"
    bad = mock_backend(lambda r: httpx.Response(400, json={
        "isSuccess": False, "message": "요청값을 확인해 주세요.", "errors": [{"field": "modelId", "reason": "x"}]}))
    with pytest.raises(BackendError) as e:
        bad.simulate({})
    assert e.value.errors[0]["field"] == "modelId"


def test_api_end_to_end(tmp_path, samples):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"isSuccess": True, "result": {"candidatePrice": "2.50", "unit": "USD/GPU-hour"}})

    settings = Settings(openai_api_key=None, openai_model="x", openai_base_url=None, llm_mask=True, mapper="rules", spring_url="http://be",
                        template_dir=str(tmp_path / "t"), max_upload_bytes=1024 * 1024)
    client = TestClient(create_app(settings, backend=mock_backend(handler)))
    assert client.get("/health").json()["mapper"] == "rules"

    with open(samples["c"], "rb") as f:
        r = client.post("/v1/bills", files={"file": ("bill_c.xlsx", f)})
    assert r.status_code == 200, r.text
    draft = r.json()
    assert "OPERATIONS" in draft["missing_items"]
    card = next(l for l in draft["lines"] if "출입 카드" in l["label"])

    r = client.put(f"/v1/bills/{draft['id']}/lines",
                   json={"corrections": [{"ref": card["ref"], "item_type": "OPERATIONS"}]})
    assert r.status_code == 200
    assert client.get("/v1/templates").json()[0]["rules"] > 0

    r = client.post(f"/v1/bills/{draft['id']}/simulate",
                    json={"model_id": "7", "gpu_count": 64, "acquisition_amount": 2000000, "fx_krw_per_usd": 1400})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["candidatePrice"] == "2.50"
    assert {"RENT", "ELECTRICITY", "NETWORK_STORAGE", "ACQUISITION"} <= {i["itemType"] for i in seen["body"]["items"]}

    assert client.post("/v1/bills", files={"file": ("x.pdf", b"%PDF")}).status_code == 400
    assert client.get("/v1/bills/nope").status_code == 404
    big = b"0" * (1024 * 1024 + 10)
    assert client.post("/v1/bills", files={"file": ("big.csv", big)}).status_code == 413
