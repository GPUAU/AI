"""USD/GPU·시간 환산. 공통 처리 규칙 6절. 금액은 모두 Decimal이고 반올림하지 않는다."""
from decimal import Decimal

from .model import Normalized

HOURS_PER_MONTH = Decimal(730)
NOMINAL = "nominal_compute"
EFFECTIVE = "effective"


def _not_convertible(item_index, metric, reason):
    return Normalized(item_index=item_index, price_metric=metric, status="not_convertible", reasons=[reason])


def usage_rate(item_index, hourly_instance, gpu_count):
    """온디맨드·스팟. 실효는 명목과 같고 두 지표를 모두 저장한다."""
    reason = _blocking_reason(gpu_count, hourly_instance)
    if reason or hourly_instance <= 0:
        reason = reason or "non_positive_amount"
        return [_not_convertible(item_index, NOMINAL, reason), _not_convertible(item_index, EFFECTIVE, reason)]
    value = hourly_instance / gpu_count
    inputs = {"hourly_instance": str(hourly_instance), "gpu_count": gpu_count}
    return [
        Normalized(item_index, metric, "ok", value, "시간당 인스턴스 요금 / GPU 수", dict(inputs))
        for metric in (NOMINAL, EFFECTIVE)
    ]


def commitment(hourly_index, upfront_index, hourly_instance, upfront, gpu_count, term_months):
    """약정. 반복 요금 행을 대표 관측으로 삼고, 선납금 행은 환산 불가로 남긴다.

    upfront: 선납 없음이 명시되면 Decimal(0), 읽지 못했으면 None.
    """
    combined = [] if upfront_index is None else [upfront_index]
    fee_row = []
    if upfront_index is not None:
        fee_row = [_not_convertible(upfront_index, NOMINAL, "combined_into_effective")]

    reason = _blocking_reason(gpu_count, hourly_instance)
    if not reason and upfront is None:
        reason = "amount_unparsed"
    if not reason and not term_months:
        reason = "term_unknown"
    if reason:
        return [_not_convertible(hourly_index, NOMINAL, reason),
                _not_convertible(hourly_index, EFFECTIVE, reason)] + fee_row

    hours = term_months * HOURS_PER_MONTH
    total = upfront + hourly_instance * hours
    inputs = {
        "hourly_instance": str(hourly_instance),
        "upfront": str(upfront),
        "gpu_count": gpu_count,
        "term_months": term_months,
        "contract_hours": str(hours),
    }

    if hourly_instance > 0:
        nominal = Normalized(hourly_index, NOMINAL, "ok", hourly_instance / gpu_count,
                             "시간당 반복 요금 / GPU 수", dict(inputs))
    else:
        nominal = _not_convertible(hourly_index, NOMINAL, "upfront_only")

    if total > 0:
        effective = Normalized(hourly_index, EFFECTIVE, "ok", total / gpu_count / hours,
                               "(선납금 + 시간당 반복 요금 x 계약 시간) / GPU 수 / 계약 시간",
                               dict(inputs), combined_item_indexes=list(combined))
    else:
        effective = _not_convertible(hourly_index, EFFECTIVE, "non_positive_amount")
    return [nominal, effective] + fee_row


def per_gpu_rate(item_index, hourly_per_gpu):
    """GPU당 공시 가격. 다시 GPU 수로 나누지 않는다. 실효는 명목과 같다."""
    if hourly_per_gpu is None:
        return not_convertible_pair(item_index, "amount_unparsed")
    if hourly_per_gpu <= 0:
        return not_convertible_pair(item_index, "non_positive_amount")
    return [Normalized(item_index, metric, "ok", hourly_per_gpu, "GPU당 시간 요금 그대로",
                       {"hourly_per_gpu": str(hourly_per_gpu)})
            for metric in (NOMINAL, EFFECTIVE)]


def contract_total(item_index, total, gpu_count, term_months):
    """계약 총액만 공개된 약정. 6절에 따라 명목은 환산하지 않는다."""
    nominal = _not_convertible(item_index, NOMINAL, "contract_total_only")
    reason = _blocking_reason(gpu_count, total)
    if reason or total <= 0:
        return [nominal, _not_convertible(item_index, EFFECTIVE, reason or "non_positive_amount")]
    hours = term_months * HOURS_PER_MONTH
    effective = Normalized(item_index, EFFECTIVE, "ok", total / gpu_count / hours, "계약 총액 / GPU 수 / 계약 시간",
                           {"contract_total": str(total), "gpu_count": gpu_count, "term_months": term_months,
                            "contract_hours": str(hours)})
    return [nominal, effective]


def blended_commitment(item_index, blended_hourly, gpu_count, term_months):
    """선납이 이미 반영된 요율. 반복 요금과 선납금을 나눌 수 없어 명목은 계산하지 않는다."""
    nominal = _not_convertible(item_index, NOMINAL, "blended_rate")
    reason = _blocking_reason(gpu_count, blended_hourly)
    if reason or blended_hourly <= 0:
        return [nominal, _not_convertible(item_index, EFFECTIVE, reason or "non_positive_amount")]
    effective = Normalized(item_index, EFFECTIVE, "ok", blended_hourly / gpu_count, "선납 반영 시간당 요율 / GPU 수",
                           {"blended_hourly": str(blended_hourly), "gpu_count": gpu_count, "term_months": term_months})
    return [nominal, effective]


def discount_estimate(item_index, on_demand_hourly, discount_percentage, gpu_count, term_months, base_observation_id):
    """사업자가 할인율만 공개한 약정. 온디맨드에 적용한 계산값이며 선납은 없는 것으로 표기하지 않는다."""
    reason = _blocking_reason(gpu_count, on_demand_hourly)
    if not reason and not 0 <= discount_percentage < 100:
        reason = "invalid_discount"
    if reason or on_demand_hourly <= 0:
        return not_convertible_pair(item_index, reason or "non_positive_amount")
    value = on_demand_hourly * (100 - discount_percentage) / 100 / gpu_count
    inputs = {"estimated": True, "on_demand_hourly": str(on_demand_hourly),
              "discount_percentage": str(discount_percentage), "gpu_count": gpu_count, "term_months": term_months,
              "combined_observation_ids": [base_observation_id]}
    formula = "온디맨드 시간당 요금 x (100 - 할인율) / 100 / GPU 수 (추정)"
    return [Normalized(item_index, metric, "ok", value, formula, dict(inputs)) for metric in (NOMINAL, EFFECTIVE)]


def not_convertible_pair(item_index, reason):
    return [_not_convertible(item_index, NOMINAL, reason), _not_convertible(item_index, EFFECTIVE, reason)]


def _blocking_reason(gpu_count, amount):
    if not gpu_count:
        return "gpu_count_unknown"
    if amount is None:
        return "amount_unparsed"
    if amount < 0:
        return "non_positive_amount"
    return None
