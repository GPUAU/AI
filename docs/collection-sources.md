# 수집 원천

기준일 2026-11-22. 수집 대상은 GPU를 직접 공급하는 사업자만.

## 이용 조건 판단

- 공식 문서·API 명세가 프로그램 조회를 안내하면 ✅
- 약관에 자동 접근(robot·scraper·data mining) 명시 금지 → ❌
- 지식재산·이용 제한 고지만 있으면 ⚠️ (팀 결정)
- Wayback·GitHub 같은 제3자 저장본은 `aggregator`, `unknown`으로 기록
- 내부 분석용, 원시 데이터 재배포 안 함

## 적재 중

| 사업자 | 현재 | 과거 | 이용 조건 |
| --- | --- | --- | --- |
| AWS | Price List API, Savings Plans API, 스팟 API (IAM 키) | 가격 파일 과거 버전(2023-07~), 스팟 90일 | ✅ 공식 API. 웹 페이지는 ❌(Site Terms: data mining·robots 금지) |
| Azure | Retail Prices API | GitHub 일별 스냅샷(2025-10~, 월 1개 적재) | ✅ |
| Oracle | 정가 API(apexapps) | Wayback API 응답(2026-05~), SkyPilot 카탈로그(2025-01~) | ✅ OCI 문서가 curl 안내. 웹 페이지는 ❌(약관 7조) |
| Scaleway | 카탈로그 API v2alpha1 (EUR → USD) | Wayback 가격 페이지(2024-05~) | ✅ 공식 Go SDK. 웹은 봇 차단 |
| Verda | instance-types, long-term periods API | Wayback API 응답(2026-08~) | ✅ 명세 `security: []` |
| Nebius | 문서 가격표(docs.nebius.com), 스팟 API | 스팟 30일, Wayback 문서(2026-05~) | ✅ 자동 접근 금지 없음 |
| 환율 | 수출입은행 현재환율 API | 같은 API 과거 영업일(2023-07~) | ✅ |

과거 데이터 상세는 각 `collector/src/gpu_collector/erd/*/history.py` 머리 주석.

## 검토했지만 안 쓴 방법

| 사업자 | 방법 | 이유 |
| --- | --- | --- |
| AWS | Wayback spot.json | 리전 최저가라 가용영역 단위와 안 맞음 |
| AWS | Capacity Blocks API | 재고 따라 바뀌는 견적, 계정 한도 막힘 |
| Azure | Price Sheet, Partner Center, Resource Graph 스팟 | 기업·파트너 계정 또는 구독 필요 |
| Verda | 가격 이력 API | 410 제거됨 |
| 공통 | GetDeploying 이력 API | 사업자 단위 통계라 오퍼 특정 불가 |
| Oracle | 가격 페이지 Wayback | 가격을 JS로 채워 HTML에 값 없음 |
| Oracle | gpuhunt 카탈로그 | 보강 기간 짧음(GB200 약 6주) |
| Scaleway | 카탈로그 API Wayback | 첫 페이지(100건)만 저장됨 |

## 검토한 사업자 (미적재)

| 사업자 | 원천 | 상태 |
| --- | --- | --- |
| Hyperstack | pricebook·flavor API(키), 과거는 Wayback 가격 페이지(2023-11~) | 검토 완료. API ✅, 페이지 ⚠️(IP 조항 2.1·6.4). 예약가는 API에 없고 페이지도 "Starting from"이라 제외 예정 |
| OVHcloud | 카탈로그 API(무인증), 과거는 Wayback 가격 페이지 | 보류. B300·B200 판매 상품 없음 |
| Spheron, OpenRelay | — | 제외. 중개·분산 |
| EmpirioLabs, Jarvislabs, Thunder Compute, Runpod | — | 제외. 직접 공급 확인 안 됨 |
