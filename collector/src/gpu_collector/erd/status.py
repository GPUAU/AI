"""랙스케일·출시 예정 모델의 공개 상태 수기 확인 기록 (FR-027: 공식 발표·상품 페이지 월 1회 수기 확인).

자동 수집으로는 '미발견'만 알 수 있어, 사람이 공식 페이지를 열어 확인한 상태(견적 문의·가격 미공개 계열 등)를 근거와
함께 수집 점검(check_scope='target')으로 남긴다. 가격 관측은 만들지 않는다.

- 확인할 때마다 아래 MANUAL 목록의 checked_on·문구를 고치고 `python -m gpu_collector.erd status record`를 실행한다.
  같은 (대상, 확인일)은 다시 기록하지 않는다. 확인 이력은 대상별 점검 행으로 쌓인다(최초 관측 시점·이후 확인 이력).
- 상태: public(공개가) / contact_sales(견적 문의) / not_disclosed(발표됐으나 가격 미공개) / unknown(미확인).
"""
from datetime import datetime, timezone

from . import db

COLLECTOR_VERSION = "gpu-collector 0.1.0 / erd-status-manual 1"

MANUAL = [
    {"provider": "Verda", "model": "GB300", "source": "verda_status_pages", "checked_on": "2026-11-18",
     "disclosure": "contact_sales",
     "reason": "GB300 NVL72 랙(트레이 18개·72 GPU) 단위 가격은 견적 문의. 공개가는 GPU 1개 $14.56/h(트레이 일부 구성)뿐",
     "url": "https://www.verda.com/gb300",
     "quotes": ["Talk to our engineers for multi-tray, single-rack, and multi-rack quotes.",
                "Each rack contains 18 trays with NVLink v5 peer-to-peer connect.", "1x GB300 GPU $14.56/h"]},
    {"provider": "Verda", "model": "VR200", "source": "verda_status_pages", "checked_on": "2026-11-18",
     "disclosure": "not_disclosed",
     "reason": "VR200 NVL72 배포 계획만 발표(2026년 하반기, 유럽·미국·APAC). 가격·주문 경로 미공개",
     "url": "https://verda.com/blog/nvidia-vr200-r200-early-deployments",
     "quotes": ["Verda to deploy NVIDIA VR200 NVL72 and R200 in H2 2026 across Europe, the US, and APAC"]},
    {"provider": "Nebius", "model": "GB300", "source": "nebius_status_pages", "checked_on": "2026-11-18",
     "disclosure": "contact_sales",
     "reason": "가격 페이지의 NVIDIA GB300 NVL72 온디맨드 칸이 'Contact us'(선점형 없음)",
     "url": "https://nebius.com/prices",
     "quotes": ["NVIDIA GB300 NVL72 | 112 vCPUs | 800 GB RAM | — | Contact us",
                "Contact us to request large-scale GPU clusters"]},
    {"provider": "Nebius", "model": "GB200", "source": "nebius_status_pages", "checked_on": "2026-11-18",
     "disclosure": "contact_sales",
     "reason": "가격 페이지의 NVIDIA GB200 NVL72 온디맨드 칸이 'Contact us'(선점형 없음)",
     "url": "https://nebius.com/prices",
     "quotes": ["NVIDIA GB200 NVL72 | 112 vCPUs | 800 GB RAM | — | Contact us"]},
    {"provider": "Nebius", "model": "VR200", "source": "nebius_status_pages", "checked_on": "2026-11-18",
     "disclosure": "not_disclosed",
     "reason": "Vera Rubin NVL72 제공 계획 발표(2026년 하반기 미국·유럽), 2026-10 랙 가동·검증 단계. 가격 미공개",
     "url": "https://nebius.com/newsroom/nebius-to-offer-nvidia-vera-rubin-nvl-72-in-us-and-europe-from-h2-2026",
     "quotes": ["Nebius to offer NVIDIA Vera Rubin NVL72 in US and Europe from H2 2026"]},
    {"provider": "AWS", "model": "GB200", "source": "aws_status_pages", "checked_on": "2026-11-18",
     "disclosure": "public",
     "reason": ("P6e-GB200 UltraServer(GB200 NVL72) Capacity Blocks 공개가: u-p6e-gb200x72 시간당 $761.904(GPU당 $10.582), "
                "US East (Dallas) Local Zone. 선납·공급수요에 따라 주기 갱신(다음 2027-01). Capacity Blocks는 수집 범위 밖이라 관측 미적재"),
     "url": "https://aws.amazon.com/ec2/capacityblocks/pricing/",
     "quotes": ["u-p6e-gb200x72 | US East (Dallas) Local Zone | $761.904 ($10.582 USD) | 72 x B200",
                "u-p6e-gb200x36 | US East (Dallas) Local Zone | $380.952 ($10.582 USD) | 36 x B200",
                "The reservation fee is charged up front at the time you schedule the reservation."]},
    {"provider": "AWS", "model": "GB300", "source": "aws_status_pages", "checked_on": "2026-11-18",
     "disclosure": "not_disclosed",
     "reason": "P6e-GB300 UltraServer(GB300 NVL72) 발표·지원 문서는 있으나 Capacity Blocks 가격표에 GB300 행 없음. 가격 미공개",
     "url": "https://aws.amazon.com/ec2/capacityblocks/pricing/",
     "quotes": ["P6e-GB300 UltraServers, accelerated by NVIDIA GB300 NVL72 (re:Invent 2025)",
                "Capacity Blocks P6e Pricing: u-p6e-gb200x72, u-p6e-gb200x36 (GB300 없음)"]},
    {"provider": "AWS", "model": "VR200", "source": "aws_status_pages", "checked_on": "2026-11-18",
     "disclosure": "not_disclosed",
     "reason": "Rubin GPU 배치 계획만 발표(2027~2028). EC2 인스턴스·가격 없음",
     "url": "https://press.aboutamazon.com/aws/2026/8/aws-and-nvidia-to-deliver-2-million-additional-gpus-and-next-generation-infrastructure-for-agentic-and-physical-ai",
     "quotes": ["AWS plans to deploy an additional 2 million NVIDIA Blackwell Ultra, Rubin and Rubin Ultra GPUs in 2027–2028"]},
    {"provider": "Azure", "model": "GB300", "source": "azure_status_pages", "checked_on": "2026-11-18",
     "disclosure": "not_disclosed",
     "reason": ("ND GB300 v6 VM(GB300 NVL72 기반, VM당 4 GPU)은 GA지만 공개 가격 없음(Retail Prices API에서 2026-04 이후 사라짐). "
                "랙 단위 상품 없음"),
     "url": "https://techcommunity.microsoft.com/blog/azurehighperformancecomputingblog/azure-nd-gb300-v6-now-generally-available---hyper-optimized-for-generative-and-a/4469475",
     "quotes": ["Azure ND GB300 v6 now Generally Available"]},
    {"provider": "Azure", "model": "GB200", "source": "azure_status_pages", "checked_on": "2026-11-18",
     "disclosure": "not_disclosed",
     "reason": "GB200 NVL72 랙 단위 상품·가격 없음. ND GB200 v6 VM(NVL72의 4 GPU 구성) 공개가는 Retail Prices API로 별도 수집 중",
     "url": "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nd-gb200-v6-series",
     "quotes": ["Each ND-GB200-v6 VM is powered by two NVIDIA Grace CPUs and four NVIDIA Blackwell GPUs"]},
    {"provider": "Azure", "model": "VR200", "source": "azure_status_pages", "checked_on": "2026-11-18",
     "disclosure": "not_disclosed",
     "reason": "Vera Rubin NVL72 첫 운영 시스템 데이터센터 납품(2026-08), 순차 배포 중. VM SKU·가격 미공개",
     "url": "https://news.microsoft.com/events/ignite/2026/book-of-news/",
     "quotes": ["In August, we delivered the first production Vera Rubin systems in our datacenters.",
                "Over the next few months, Vera Rubin NVL72 will be rolled out into our modern, liquid-cooled Azure datacenters."]},
]


def record(conn, *, entries=MANUAL, now=None):
    """수기 확인 상태를 기록한다. 반환: 실행 요약."""
    now = now or (lambda: datetime.now(timezone.utc))
    run_id = db.begin_run(conn, now(), "manual", COLLECTOR_VERSION)
    added, skipped = 0, 0
    for entry in entries:
        target = next((t for t in db.enabled_targets(conn, entry["provider"])
                       if t["source_name"] == entry["source"] and t["model_name"] == entry["model"]), None)
        if target is None:
            raise LookupError(f"status target not seeded: {entry['provider']} {entry['model']} {entry['source']}")
        checked_at = datetime.fromisoformat(entry["checked_on"]).replace(tzinfo=timezone.utc)
        check_id, created = db.add_check(conn, run_id, target["target_id"], "target", checked_at, entry["disclosure"],
                                         "success", True, f"manual-status:{target['target_id']}:{entry['checked_on']}",
                                         reason=entry["reason"])
        if not created:
            skipped += 1
            continue
        db.add_evidence(conn, check_id, target["source_id"], "official", entry["url"], f"수기 확인 {entry['checked_on']}",
                        {"quotes": entry["quotes"], "verified_by": "manual", "verified_on": entry["checked_on"]}, now())
        added += 1
    db.finish_run(conn, run_id, now(), "success")
    conn.commit()
    return {"run_id": run_id, "status": "success", "added": added, "skipped": skipped}
