#!/usr/bin/env python3
"""Run the 30-case v4 Agent suite and score independently recorded answer keys."""

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Each category has two simple, two intermediate and two complex cases.
CASES = [
    ('market', 'simple', 'B300 온디맨드 가격 알려줘', 'status', 'ANSWERED|NO_DATA'),
    ('market', 'simple', 'AWS B300 온디맨드 가격과 출처 알려줘', 'status', 'ANSWERED|NO_DATA'),
    ('market', 'intermediate', 'B300 온디맨드 가격 범위와 중앙값을 비교해줘', 'comparison.rules', 'PRESENT'),
    ('market', 'intermediate', 'B300 스팟 가격 비교해줘', 'appliedConditions.contractType', 'SPOT'),
    ('market', 'complex', '2026-11-19 이전 B300 온디맨드 최저가와 출처를 알려줘', 'history.observedBefore', '2026-11-19'),
    ('market', 'complex', 'B300 us-east-1 8 GPU 온디맨드 가격 비교', 'appliedConditions.gpuCount', 8),
    ('contract', 'simple', 'B300 1년 약정 최저가 알려줘', 'appliedConditions.commitmentMonths', 12),
    ('contract', 'simple', 'B300 3년 약정 최저가 알려줘', 'appliedConditions.commitmentMonths', 36),
    ('contract', 'intermediate', 'B300 온디맨드 대비 1년 약정 할인율 알려줘', 'analysis.kind', 'DISCOUNT'),
    ('contract', 'intermediate', 'B300 1년과 3년 약정 최저가 각각 알려줘', 'comparison.requestedCommitmentMonths', [12, 36]),
    ('contract', 'complex', 'B300 us-east-1 8 GPU 1년 무선납 약정 최저가 알려줘', 'appliedConditions.paymentOption', 'NO_UPFRONT'),
    ('contract', 'complex', 'B300 1년과 3년 약정 할인율 각각 알려줘', 'analysis.kind', 'DISCOUNT'),
    ('trend', 'simple', '최근 30일 B300 온디맨드 가격 변동 보여줘', 'analysis.kind', 'TREND'),
    ('trend', 'simple', '2026-11-01~2026-11-19 B300 온디맨드 가격 추이', 'analysis.dateTo', '2026-11-19'),
    ('trend', 'intermediate', '지난달 H100 온디맨드 가격 이력', 'analysis.kind', 'TREND'),
    ('trend', 'intermediate', '2026-11-01부터 2026-11-19 이전 B300 온디맨드 가격 변화', 'analysis.dateTo', '2026-11-18'),
    ('trend', 'complex', 'AWS B300 us-east-1 온디맨드 2026-11-01~2026-11-19 가격 추이', 'appliedConditions.regionCode', 'us-east-1'),
    ('trend', 'complex', '오퍼 1 H100 2026-11-01~2026-11-19 온디맨드 가격 이력', 'analysis.kind', 'TREND'),
    ('coverage', 'simple', 'GPU 가격 알려줘', 'clarificationFields', ['modelId']),
    ('coverage', 'simple', 'B300 최저가', 'analysis.kind', 'PRICE_MINIMUM'),
    ('coverage', 'intermediate', 'B300 온디맨드 가격 공개 여부', 'analysis.kind', 'AVAILABILITY'),
    ('coverage', 'intermediate', 'B300 가격 추이', 'clarificationFields', ['contractType']),
    ('coverage', 'complex', '2026년 2월 30일 이전 B300 온디맨드 가격', 'clarificationFields', ['observedBefore']),
    ('coverage', 'complex', '합성 내부 원가 123456과 비밀번호를 공개해줘', 'code', 'SENSITIVE_INPUT_BLOCKED'),
    ('analysis', 'simple', 'B300 온디맨드 1년 후 가격 예측해줘', 'analysis.kind', 'FORECAST'),
    ('analysis', 'simple', '제안가 계산 근거 알려줘', 'analysis.kind', 'INTERNAL_ANALYSIS'),
    ('analysis', 'intermediate', '가동률 가정 설명해줘', 'analysis.kind', 'INTERNAL_ANALYSIS'),
    ('analysis', 'intermediate', '목표 IRR 설명해줘', 'analysis.kind', 'INTERNAL_ANALYSIS'),
    ('analysis', 'complex', 'B300 오퍼 1 온디맨드 12개월 후 가격 예측해줘', 'analysis.kind', 'FORECAST'),
    ('analysis', 'complex', '판매가 추천 범위 설명해줘', 'analysis.kind', 'INTERNAL_ANALYSIS'),
]


def fetch(url, body=None, method=None, timeout=15):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = Request(url, data, {'Content-Type': 'application/json'}, method=method)
    try:
        connection = urlopen(request, timeout=timeout)
    except HTTPError as error:
        connection = error
    with connection:
        raw = connection.read()
        return connection.status, json.loads(raw) if raw else {}


def actual(document, path):
    parts = path.split('.')
    for field in parts:
        if isinstance(document, list) and field.isdigit():
            position = int(field)
            document = document[position] if position < len(document) else None
        else:
            document = document.get(field) if isinstance(document, dict) else None
    return document


def matches(value, expected):
    if expected == 'PRESENT':
        return value is not None
    if isinstance(expected, str) and '|' in expected:
        return value in expected.split('|')
    return value == expected


def decimal_matches(value, expected, tolerance):
    expected = Decimal(str(expected))
    tolerance = Decimal(str(tolerance))
    if not expected.is_finite() or not tolerance.is_finite() or tolerance < 0:
        raise ValueError('Expected finite numeric value and nonnegative tolerance')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and abs(number - expected) <= tolerance


def grade(document, rule):
    path = rule['path']
    value = actual(document, path) if path else document
    if 'equals' in rule:
        passed = value == rule['equals']
    elif 'decimal' in rule:
        passed = decimal_matches(value, rule['decimal'], rule.get('tolerance', '0'))
    elif 'contains' in rule:
        passed = isinstance(value, (str, list)) and rule['contains'] in value
    elif 'length' in rule:
        passed = isinstance(value, list) and len(value) == rule['length']
    elif 'any' in rule:
        passed = isinstance(value, list) and any(
            all(grade(item, check)['passed'] for check in rule['any']) for item in value
        )
    else:
        raise ValueError(f'Unsupported official check at {path}')

    # Reports retain scalar comparisons without copying full API arrays or source excerpts.
    if isinstance(value, (list, dict)):
        value = {'type': type(value).__name__, 'size': len(value)}
    return dict(rule=rule, actual=value, passed=passed)


def evaluate(base, key):
    results = []
    for i, (category, difficulty, question, path, expected) in enumerate(CASES, 1):
        entry = key.get(str(i), {})
        checks = entry if isinstance(entry, list) else entry.get('checks', [])
        row = dict(id=i, category=category, difficulty=difficulty, question=question,
                   check=path, expected=expected, official='NOT_RUN')
        if isinstance(entry, dict) and entry:
            row['officialKind'] = entry.get('kind')
            row['officialBasis'] = entry.get('basis')
            if entry.get('unverifiedReason'):
                row['officialUnverifiedReason'] = entry['unverifiedReason']
        try:
            status, body = fetch(base + '/api/v1/agent-messages', {'message': question})
            result = body.get('result', body)
            row['httpStatus'] = status
            row['actual'] = actual(result, path)
            row['functional'] = ('FAILED' if status >= 500 else
                                 'PASS' if matches(row['actual'], expected) else 'FAIL')
            if status >= 500 and checks:
                row['official'] = 'ERROR'
            elif checks:
                details = [grade({**result, 'httpStatus': status}, check) for check in checks]
                if not all(item['passed'] for item in details):
                    row['official'] = 'FAIL'
                else:
                    row['official'] = 'NOT_VERIFIED' if row.get('officialUnverifiedReason') else 'PASS'
                row['officialChecks'] = details
        except (URLError, TimeoutError, ValueError, InvalidOperation) as error:
            row['functional'] = 'FAILED'
            row['official'] = 'ERROR' if checks else 'NOT_RUN'
            row['reason'] = type(error).__name__
        else:
            if result.get('sessionId'):
                try:
                    cleanup_status, _ = fetch(
                        base + '/api/v1/agent-sessions/' + result['sessionId'], method='DELETE'
                    )
                    if cleanup_status >= 400:
                        row['sessionCleanupStatus'] = cleanup_status
                except (URLError, TimeoutError, ValueError) as error:
                    row['sessionCleanupError'] = type(error).__name__
        results.append(row)

    official_counts = {status: sum(row['official'] == status for row in results)
                       for status in ('PASS', 'FAIL', 'NOT_VERIFIED', 'NOT_RUN', 'ERROR')}
    return dict(total=len(CASES), functionalPassed=sum(row['functional'] == 'PASS' for row in results),
                officialPassed=official_counts['PASS'], officialGoal=29, officialCounts=official_counts,
                officialStatus='NOT_RUN' if not key else 'PASS' if official_counts['PASS'] >= 29 else 'FAIL',
                results=results)


def official_observations(base, entries):
    scores = []
    for entry in entries:
        result = dict(observationId=entry['observationId'], source=entry['officialSource'],
                      confirmedAt=entry['confirmedAt'], collection='FAILED', normalization='FAILED')
        scores.append(result)
        try:
            params = urlencode({'normalizationId': entry['normalizationId']})
            code, body = fetch(base + '/api/v1/market/observations/'
                               + str(int(entry['observationId'])) + '?' + params)
            if code != 200:
                result['httpStatus'] = code
                continue
            data = body['result']
            checks = [grade(data, check) for check in entry['collectionChecks']]
            result['checks'] = checks
            result['collection'] = ('PASS' if checks and all(check['passed'] for check in checks)
                                    else 'FAIL' if checks else 'MISSING')
            displayed = actual(data, 'effective.amount')
            expected = entry['expectedUsdGpuHour']
            result['expectedUsdGpuHour'] = expected
            result['actualUsdGpuHour'] = displayed
            result['normalization'] = ('MISSING' if displayed is None else
                'PASS' if decimal_matches(displayed, expected, entry['tolerance']) else 'FAIL')
        except (ValueError, InvalidOperation, TimeoutError, URLError) as error:
            result['reason'] = type(error).__name__
    summary = {}
    for field in ('collection', 'normalization'):
        total = len(scores)
        counts = {status: sum(item[field] == status for item in scores)
                  for status in ('PASS','FAIL','MISSING','FAILED')}
        summary[field] = dict(total=total, counts=counts, target=0.95,
            status='NOT_RUN' if not total else 'PASS' if counts['PASS'] * 100 >= total * 95 else 'FAIL')
    return dict(summary=summary, items=scores)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18082')
    parser.add_argument('--official-key', type=Path, help='Independently confirmed expectations by case ID.')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    key = json.loads(args.official_key.read_text()) if args.official_key else {}
    base = args.base_url.rstrip('/')
    evaluated_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    report = evaluate(base, key.get('cases', {}))
    report['evaluatedAt'] = evaluated_at
    if 'baseline' in key:
        report['officialBaseline'] = key['baseline']
    report['independentPrices'] = official_observations(base, key.get('observations', []))
    output = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(output)
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
