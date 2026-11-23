-- ERD 초안 스키마 기준 정보: AWS 수집 대상 (ddl.sql 적용 후 실행)
-- 여러 번 실행해도 같은 행을 다시 만들지 않는다.

INSERT INTO provider (provider_name, provider_type, official_url)
VALUES ('AWS', 'hyperscaler', 'https://aws.amazon.com')
ON CONFLICT (provider_name) DO NOTHING;

INSERT INTO gpu_model (model_name, manufacturer, generation)
VALUES ('B300', 'NVIDIA', 'Blackwell Ultra'), ('B200', 'NVIDIA', 'Blackwell'),
       ('GB300', 'NVIDIA', 'Blackwell Ultra'), ('H100', 'NVIDIA', 'Hopper')
ON CONFLICT (model_name) DO NOTHING;

-- OR-02 이용 조건 확인 기록. 가격 파일은 공개 Price List API, 스팟은 사용자 계정의 IAM 키로 호출하는 EC2 API다.
-- AWS 웹페이지 크롤링(스팟 가격 페이지의 spot.json 등)은 AWS Site Terms가 robots·데이터 수집 도구를 금지해 쓰지 않는다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('aws_ec2_price_list', 'official_api', 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/',
        'file', 'allowed', '2026-11-09T00:00:00Z',
        'https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-the-aws-price-list-bulk-api.html'),
       ('aws_savings_plans_price_list', 'official_api',
        'https://pricing.us-east-1.amazonaws.com/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/',
        'file', 'allowed', '2026-11-09T00:00:00Z',
        'https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-the-aws-price-list-bulk-api.html'),
       ('aws_ec2_spot_api', 'official_api', 'https://ec2.{region}.amazonaws.com/?Action=DescribeSpotPriceHistory',
        'api', 'allowed', '2026-11-16T00:00:00Z', 'https://aws.amazon.com/agreement/')
ON CONFLICT (source_name) DO NOTHING;

-- 환산 규칙. 월 730시간은 요구사항 명세 DR-02·03 기준이다.
INSERT INTO normalization_rule (rule_version, monthly_hours, formula_description, precision_policy)
VALUES ('v1', 730,
        '시간 단위: 원본 ÷ GPU 수(GPU 1장 가격이면 나누지 않음). 월: ÷ 730 ÷ GPU 수. '
        '선납+시간당: (선납금 ÷ (약정개월 × 730) + 시간당) ÷ GPU 수. 선납 반영 요율: 요율 ÷ GPU 수(명목 없음). '
        '약정기간 100% 사용 가정, 최소물량은 단가에 넣지 않음',
        '저장은 원본 정밀도(반올림 없음). 표시는 USD 소수 둘째 자리, KRW 원 단위')
ON CONFLICT (rule_version) DO NOTHING;

-- 수집 대상: 리전 × 모델 × 출처 × 계약. GB300은 EC2 가격 파일 등재 여부만 확인한다.
INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, rr.region, v.contract, v.description
FROM (VALUES
    ('aws_ec2_price_list', 'on_demand', 'EC2 가격 파일 온디맨드 (Linux, 공유 테넌시, 사용 중 용량)'),
    ('aws_ec2_price_list', 'reserved_instances', 'EC2 가격 파일 Reserved Instances 1·3년 (Linux, 공유 테넌시)'),
    ('aws_savings_plans_price_list', 'savings_plans', 'Compute·EC2 Instance Savings Plans 1·3년 (Linux, 공유 테넌시)'),
    ('aws_ec2_spot_api', 'spot', 'DescribeSpotPriceHistory 가용영역별 최신 스팟 가격 (Linux/UNIX)')
) AS v (source, contract, description)
CROSS JOIN (VALUES ('B300'), ('B200'), ('H100')) AS mm (model)
CROSS JOIN (VALUES ('us-east-1'), ('us-west-2'), ('ap-northeast-2')) AS rr (region)
JOIN provider p ON p.provider_name = 'AWS'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter = rr.region AND t.contract_filter = v.contract);

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, rr.region, 'on_demand',
       'EC2 가격 파일에 GB300 인스턴스가 등재됐는지만 확인 (가격 미수집)'
FROM (VALUES ('us-east-1'), ('us-west-2'), ('ap-northeast-2')) AS rr (region)
JOIN provider p ON p.provider_name = 'AWS'
JOIN gpu_model m ON m.model_name = 'GB300'
JOIN data_source s ON s.source_name = 'aws_ec2_price_list'
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter = rr.region AND t.contract_filter = 'on_demand');

-- 공식 과거 자료(FR-56). 자체 관측과 출처 유형을 구분하도록 출처·수집 대상을 따로 둔다.
-- 같은 공개 Price List API의 과거 버전과, 같은 IAM 키로 호출하는 스팟 가격 기간 조회(최근 90일)다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('aws_ec2_price_list_history', 'official_api', 'https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/index.json',
        'file', 'allowed', '2026-11-17T00:00:00Z',
        'https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-the-aws-price-list-bulk-api.html'),
       ('aws_savings_plans_price_list_history', 'official_api',
        'https://pricing.us-east-1.amazonaws.com/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/current/index.json',
        'file', 'allowed', '2026-11-17T00:00:00Z',
        'https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-the-aws-price-list-bulk-api.html'),
       ('aws_ec2_spot_price_history', 'official_api', 'https://ec2.{region}.amazonaws.com/?Action=DescribeSpotPriceHistory',
        'api', 'allowed', '2026-11-17T00:00:00Z', 'https://aws.amazon.com/agreement/')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, rr.region, v.contract, v.description
FROM (VALUES
    ('aws_ec2_price_list_history', 'on_demand', 'EC2 가격 파일 과거 월별 버전의 온디맨드 (Linux, 공유 테넌시, 사용 중 용량)'),
    ('aws_ec2_price_list_history', 'reserved_instances', 'EC2 가격 파일 과거 월별 버전의 Reserved Instances 1·3년'),
    ('aws_savings_plans_price_list_history', 'savings_plans', 'Savings Plans 가격 파일 과거 버전(달마다 마지막 버전)의 Compute·EC2 Instance SP'),
    ('aws_ec2_spot_price_history', 'spot', 'DescribeSpotPriceHistory 최근 90일 가격 변경 기록 (Linux/UNIX, 가용영역별)')
) AS v (source, contract, description)
CROSS JOIN (VALUES ('B300'), ('B200'), ('H100')) AS mm (model)
CROSS JOIN (VALUES ('us-east-1'), ('us-west-2'), ('ap-northeast-2')) AS rr (region)
JOIN provider p ON p.provider_name = 'AWS'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter = rr.region AND t.contract_filter = v.contract);

-- B300이 출시된 그 밖의 리전(B300만). 상파울루·런던·하이데라바드·GovCloud(US-East).
-- 스팟은 이 계정에서 조회되는 상파울루·런던만 둔다. 하이데라바드는 계정에서 리전을 켜지 않아(옵트인) 스팟 API가
-- 인증 오류를 내고, GovCloud는 별도 파티션 계정이 필요하다. 가격 파일은 공개라 네 곳 모두 수집한다.
INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, v.region, v.contract, v.description
FROM (VALUES
    ('aws_ec2_price_list', 'on_demand', 'EC2 가격 파일 온디맨드 (Linux, 공유 테넌시, 사용 중 용량)'),
    ('aws_ec2_price_list', 'reserved_instances', 'EC2 가격 파일 Reserved Instances 1·3년 (Linux, 공유 테넌시)'),
    ('aws_savings_plans_price_list', 'savings_plans', 'Compute·EC2 Instance Savings Plans 1·3년 (Linux, 공유 테넌시)'),
    ('aws_ec2_price_list_history', 'on_demand', 'EC2 가격 파일 과거 월별 버전의 온디맨드 (Linux, 공유 테넌시, 사용 중 용량)'),
    ('aws_ec2_price_list_history', 'reserved_instances', 'EC2 가격 파일 과거 월별 버전의 Reserved Instances 1·3년'),
    ('aws_savings_plans_price_list_history', 'savings_plans', 'Savings Plans 가격 파일 과거 버전(달마다 마지막 버전)의 Compute·EC2 Instance SP')
) AS v0 (source, contract, description)
CROSS JOIN (VALUES ('sa-east-1'), ('eu-west-2'), ('ap-south-2'), ('us-gov-east-1')) AS rr (region)
CROSS JOIN LATERAL (SELECT v0.source, v0.contract, v0.description, rr.region) AS v
JOIN provider p ON p.provider_name = 'AWS'
JOIN gpu_model m ON m.model_name = 'B300'
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter = v.region AND t.contract_filter = v.contract);

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, rr.region, 'spot', v.description
FROM (VALUES
    ('aws_ec2_spot_api', 'DescribeSpotPriceHistory 가용영역별 최신 스팟 가격 (Linux/UNIX)'),
    ('aws_ec2_spot_price_history', 'DescribeSpotPriceHistory 최근 90일 가격 변경 기록 (Linux/UNIX, 가용영역별)')
) AS v (source, description)
CROSS JOIN (VALUES ('sa-east-1'), ('eu-west-2')) AS rr (region)
JOIN provider p ON p.provider_name = 'AWS'
JOIN gpu_model m ON m.model_name = 'B300'
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter = rr.region AND t.contract_filter = 'spot');

-- 매일 수집은 가격 조회 API로 한다(대상 상품만, 수 KB). 가격 파일 출처와 같은 값이다.
-- 필요한 IAM 권한: pricing:GetProducts, savingsplans:DescribeSavingsPlansOfferingRates
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('aws_ec2_pricing_api', 'official_api', 'https://api.pricing.us-east-1.amazonaws.com/ (AWSPriceListService.GetProducts)',
        'api', 'allowed', '2026-11-17T00:00:00Z',
        'https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_pricing_GetProducts.html'),
       ('aws_savings_plans_api', 'official_api', 'https://savingsplans.amazonaws.com/DescribeSavingsPlansOfferingRates',
        'api', 'allowed', '2026-11-17T00:00:00Z',
        'https://docs.aws.amazon.com/savingsplans/latest/APIReference/API_DescribeSavingsPlansOfferingRates.html')
ON CONFLICT (source_name) DO NOTHING;

-- 가격 파일의 현재 수집 대상과 같은 범위(모델·리전·계약)를 API 출처로 만든다
INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT t.provider_id, t.gpu_model_id, api.source_id, t.region_filter, t.contract_filter,
       replace(replace(t.scope_description, 'EC2 가격 파일', '가격 조회 API(GetProducts)'),
               'Compute·EC2 Instance Savings Plans', 'Savings Plans 요율 API: Compute·EC2 Instance Savings Plans')
FROM collection_target t
JOIN data_source s ON s.source_id = t.source_id
JOIN data_source api ON api.source_name = CASE s.source_name WHEN 'aws_ec2_price_list' THEN 'aws_ec2_pricing_api'
                                                             WHEN 'aws_savings_plans_price_list' THEN 'aws_savings_plans_api' END
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target x
    WHERE x.provider_id = t.provider_id AND x.gpu_model_id = t.gpu_model_id AND x.source_id = api.source_id
      AND x.region_filter = t.region_filter AND x.contract_filter = t.contract_filter);

-- 가격 파일 출처의 매일 수집은 끈다. 과거 버전 적재(별도 출처)와 API 장애 시 대체용으로 대상은 남긴다.
-- 다시 켜서 쓰려면 init 뒤에 enabled를 바꾼다(init이 이 문장을 다시 실행한다).
UPDATE collection_target t SET enabled = false
FROM data_source s
WHERE s.source_id = t.source_id AND s.source_name IN ('aws_ec2_price_list', 'aws_savings_plans_price_list') AND t.enabled;

-- Azure ----------------------------------------------------------------------------------------------------------
-- 요구사항 모델 기준: H100(ND96isr_H100_v5), GB200·GB300(VM 단위, NVL72 랙과 다른 비교 집단)은 가격을 수집하고,
-- B300·VR200은 등재 여부만 확인한다. H200은 MVP 범위 밖이라 넣지 않는다. 리전은 지정하지 않는다(API에 나온 전 리전).
INSERT INTO provider (provider_name, provider_type, official_url)
VALUES ('Azure', 'hyperscaler', 'https://azure.microsoft.com')
ON CONFLICT (provider_name) DO NOTHING;

INSERT INTO gpu_model (model_name, manufacturer, generation)
VALUES ('GB200', 'NVIDIA', 'Blackwell'), ('VR200', 'NVIDIA', 'Vera Rubin')
ON CONFLICT (model_name) DO NOTHING;

-- Azure 예약은 약정 총액만 공개한다(DR-006 '약정총액만'). v1에 이 구조를 더한 규칙이다.
INSERT INTO normalization_rule (rule_version, monthly_hours, formula_description, precision_policy)
VALUES ('v2', 730,
        '시간 단위: 원본 ÷ GPU 수(GPU 1장 가격이면 나누지 않음). 월: ÷ 730 ÷ GPU 수. '
        '선납+시간당: (선납금 ÷ (약정개월 × 730) + 시간당) ÷ GPU 수. 선납 반영 요율: 요율 ÷ GPU 수(명목 없음). '
        '약정 총액: 총액 ÷ (약정개월 × 730) ÷ GPU 수(명목 없음). 약정기간 100% 사용 가정, 최소물량은 단가에 넣지 않음',
        '저장은 원본 정밀도(반올림 없음). 표시는 USD 소수 둘째 자리, KRW 원 단위')
ON CONFLICT (rule_version) DO NOTHING;

-- OR-02 이용 조건: Retail Prices API는 Microsoft APIs Terms of Use를 따른다(2026-11-08 확인, 2026-11-09 사용자 승인).
-- 내부 분석 용도이며 원시 데이터를 외부에 재판매·재배포하지 않는다.
-- 스냅샷 저장소는 제3자가 같은 API 응답을 매일 공개한 것이다. 코드 라이선스(GPL-3.0) 외에 데이터 재사용 조건을
-- 밝히지 않아 미확인으로 둔다. 사양 문서는 수기로 확인한 값의 근거다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('azure_retail_prices_api', 'official_api', 'https://prices.azure.com/api/retail/prices',
        'api', 'allowed', '2026-11-08T14:42:11Z', 'https://learn.microsoft.com/en-us/legal/microsoft-apis/terms-of-use'),
       ('azure_retail_prices_snapshot', 'aggregator', 'https://github.com/sbroenne/azureretailprices-exporter/releases',
        'file', 'unknown', NULL, 'https://github.com/sbroenne/azureretailprices-exporter'),
       ('azure_vm_size_docs', 'official_page',
        'https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/',
        'manual', 'unknown', NULL, 'https://learn.microsoft.com/en-us/legal/termsofuse')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, v.contract, s2.label || ' ' || v.description
FROM (VALUES
    ('on_demand', '종량제(Pay-as-you-go) Linux, 대표 미터 리전, 전 리전'),
    ('spot', 'Spot Linux, 대표 미터 리전, 전 리전'),
    ('reservations', 'Reserved VM Instances 1·3·5년 약정 총액, 전 리전'),
    ('savings_plans', 'Savings Plan for Compute 1·3년 시간당 요율, 전 리전')
) AS v (contract, description)
CROSS JOIN (VALUES ('H100'), ('GB200'), ('GB300')) AS mm (model)
CROSS JOIN (VALUES ('azure_retail_prices_api', 'Retail Prices API'),
                   ('azure_retail_prices_snapshot', 'Retail Prices API 일별 스냅샷(GitHub, 집계)')) AS s2 (source, label)
JOIN provider p ON p.provider_name = 'Azure'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = s2.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter = v.contract);

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, NULL, s2.label || '에 ' || mm.model || ' VM이 등재됐는지만 확인 (가격 미수집)'
FROM (VALUES ('B300'), ('VR200')) AS mm (model)
CROSS JOIN (VALUES ('azure_retail_prices_api', 'Retail Prices API'),
                   ('azure_retail_prices_snapshot', 'Retail Prices API 일별 스냅샷(GitHub, 집계)')) AS s2 (source, label)
JOIN provider p ON p.provider_name = 'Azure'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = s2.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter IS NULL);

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, NULL,
       'Microsoft Learn VM 크기 문서의 GPU 수·VRAM·vCPU·메모리·상호연결 (수기 확인 근거)'
FROM (VALUES ('H100'), ('GB200'), ('GB300')) AS mm (model)
JOIN provider p ON p.provider_name = 'Azure'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = 'azure_vm_size_docs'
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id);

-- Verda ----------------------------------------------------------------------------------------------------------
-- 공개 API(인증 없음)로 B300·GB300·B200·H200·H100 인스턴스의 지역별 온디맨드·스팟과 약정 할인율을 수집한다.
-- H200은 요구사항 MVP 표시 범위 밖이지만 세대별 가격 추이(생애주기) 분석용으로 수집한다(2026-11-18 사용자 결정).
-- GB300은 1·2·4 GPU 인스턴스(NVL72 랙 트레이의 일부 구성)만 있다. 클러스터(cluster-types)는 수집하지 않는다.
INSERT INTO provider (provider_name, provider_type, official_url)
VALUES ('Verda', 'gpu_specialist', 'https://verda.com')
ON CONFLICT (provider_name) DO NOTHING;

INSERT INTO gpu_model (model_name, manufacturer, generation)
VALUES ('H200', 'NVIDIA', 'Hopper')
ON CONFLICT (model_name) DO NOTHING;

-- Verda 약정은 할인율만 공개한다. v2에 할인율 가정 시나리오를 더한 규칙이다.
INSERT INTO normalization_rule (rule_version, monthly_hours, formula_description, precision_policy)
VALUES ('v3', 730,
        '시간 단위: 원본 ÷ GPU 수(GPU 1장 가격이면 나누지 않음). 월: ÷ 730 ÷ GPU 수. '
        '선납+시간당: (선납금 ÷ (약정개월 × 730) + 시간당) ÷ GPU 수. 선납 반영 요율: 요율 ÷ GPU 수(명목 없음). '
        '약정 총액: 총액 ÷ (약정개월 × 730) ÷ GPU 수(명목 없음). '
        '할인율만 공개: 같은 실행 온디맨드 시간당 × (1 − 할인율) ÷ GPU 수(가정 시나리오, 명목 없음). '
        '약정기간 100% 사용 가정, 최소물량은 단가에 넣지 않음',
        '저장은 원본 정밀도(반올림 없음). 표시는 USD 소수 둘째 자리, KRW 원 단위')
ON CONFLICT (rule_version) DO NOTHING;

-- OR-02 이용 조건(2026-11-18 확인): Verda 약관(2026-10-30 개정)은 악의적 자동 사용·서비스 복제·재판매를 금지하고
-- 공개 가격 조회를 금지하지 않는다. 두 API는 공식 명세에서 인증 없음(security: [])이다. verda.com·docs.verda.com
-- robots.txt는 Allow: /. 내부 분석 용도이며 원시 데이터를 외부에 재배포하지 않는다.
-- Wayback 저장본은 Internet Archive 이용 조건(학술·연구 중심, 비상업 확인)을 원문으로 확인하지 못해 미확인으로 둔다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('verda_instance_types_api', 'official_api', 'https://api.verda.com/v1/instance-types',
        'api', 'allowed', '2026-11-18T00:00:00Z', 'https://verda.com/terms-and-conditions'),
       ('verda_long_term_periods_api', 'official_api', 'https://api.verda.com/v1/long-term/periods/instances',
        'api', 'allowed', '2026-11-18T00:00:00Z', 'https://verda.com/terms-and-conditions'),
       ('verda_product_pages', 'official_page', 'https://verda.com/',
        'manual', 'allowed', '2026-11-18T00:00:00Z', 'https://verda.com/terms-and-conditions'),
       ('verda_api_wayback', 'aggregator', 'https://web.archive.org/web/*/api.datacrunch.io/v1/instance-types',
        'api', 'unknown', NULL, 'https://archive.org/about/terms.php')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, v.contract, v.description
FROM (VALUES
    ('verda_instance_types_api', 'on_demand', 'instance-types 지역별 Pay As You Go 시간당 가격 (전 지역, 기밀 컴퓨팅 포함)'),
    ('verda_instance_types_api', 'spot', 'instance-types 지역별 Spot 시간당 가격 (전 지역, 기밀 컴퓨팅 포함)'),
    ('verda_long_term_periods_api', 'commitment', 'long-term/periods/instances 기간별 할인율을 같은 실행 온디맨드에 적용한 가정 시나리오 (선납)'),
    ('verda_api_wayback', 'on_demand', 'Wayback 저장본(instance-types API 응답)의 Pay As You Go 가격 (집계, 과거 이력)'),
    ('verda_api_wayback', 'spot', 'Wayback 저장본(instance-types API 응답)의 Spot 가격 (집계, 과거 이력)')
) AS v (source, contract, description)
CROSS JOIN (VALUES ('B300'), ('GB300'), ('B200'), ('H200'), ('H100')) AS mm (model)
JOIN provider p ON p.provider_name = 'Verda'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter = v.contract);

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, NULL,
       'verda.com 모델 페이지의 구성(1x·2x·4x·8x with NVLink)·NVLink 대역폭·VRAM, 지역 문서 (수기 확인 근거)'
FROM (VALUES ('B300'), ('GB300'), ('B200'), ('H200'), ('H100')) AS mm (model)
JOIN provider p ON p.provider_name = 'Verda'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = 'verda_product_pages'
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id);

-- Nebius ---------------------------------------------------------------------------------------------------------
-- 온디맨드는 공식 문서 가격표(마크다운), 스팟은 공개 스팟 가격 API(인증 없음, 30일 이력 제공)로 수집한다.
-- 상품은 플랫폼 × 프리셋(1·8 GPU) × 지역. 약정은 영업 문의라 상태만 기록한다. H200은 Verda와 같은 이유로 수집한다.
-- GB300·GB200 NVL72는 가격표에 없고(영업 문의) 이번 범위에 넣지 않는다.
INSERT INTO provider (provider_name, provider_type, official_url)
VALUES ('Nebius', 'gpu_specialist', 'https://nebius.com')
ON CONFLICT (provider_name) DO NOTHING;

-- OR-02 이용 조건(2026-11-18 확인): Terms of Use(2025-03-31)·AUP에 자동 접근·스크래핑·벤치마크 금지 조항이 없고
-- 서비스 재판매만 금지한다. 스팟 가격 문서는 가격과 이력이 공개이며 API 호출에 인증이 필요 없다고 밝힌다.
-- nebius.com·docs.nebius.com robots.txt는 허용. 내부 분석 용도이며 원시 데이터를 외부에 재배포하지 않는다.
-- 문서 Wayback 저장본은 Verda와 같이 Internet Archive 이용 조건을 원문으로 확인하지 못해 미확인으로 둔다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('nebius_pricing_docs', 'official_page', 'https://docs.nebius.com/compute/resources/pricing.md',
        'page', 'allowed', '2026-11-18T00:00:00Z', 'https://docs.nebius.com/legal/terms-of-use/'),
       ('nebius_spot_price_api', 'official_api', 'https://api.nebius.cloud/billing/v1alpha1/spot-price',
        'api', 'allowed', '2026-11-18T00:00:00Z', 'https://docs.nebius.com/signup-billing/spot-prices'),
       ('nebius_spot_price_history_api', 'official_api', 'https://api.nebius.cloud/billing/v1alpha1/spot-price/list-series',
        'api', 'allowed', '2026-11-18T00:00:00Z', 'https://docs.nebius.com/signup-billing/spot-prices'),
       ('nebius_vm_types_docs', 'official_page', 'https://docs.nebius.com/compute/virtual-machines/types.md',
        'manual', 'allowed', '2026-11-18T00:00:00Z', 'https://docs.nebius.com/legal/terms-of-use/'),
       ('nebius_pricing_docs_wayback', 'aggregator',
        'https://web.archive.org/web/*/docs.nebius.com/compute/resources/pricing.md',
        'api', 'unknown', NULL, 'https://archive.org/about/terms.php')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, v.contract, v.description
FROM (VALUES
    ('nebius_pricing_docs', 'on_demand', '공식 문서 가격표의 온디맨드 GPU 시간 가격 (USD, 플랫폼 × 프리셋 × 지역)'),
    ('nebius_pricing_docs', 'commitment', '장기 약정은 영업 문의(가격 미공개) 상태 기록'),
    ('nebius_spot_price_api', 'spot', 'spot-price 공개 API의 SKU(플랫폼 × 지역)별 현재 스팟 가격'),
    ('nebius_spot_price_history_api', 'spot', 'spot-price/list-series 최근 30일 1시간 간격 마지막 가격 (과거 이력)'),
    ('nebius_pricing_docs_wayback', 'on_demand', 'Wayback 문서 가격표 저장본의 온디맨드 가격 (집계, 과거 이력)'),
    ('nebius_pricing_docs_wayback', 'spot', 'Wayback 문서 가격표 저장본의 고정 선점형 가격 (집계, 과거 이력, 시작가 제외)')
) AS v (source, contract, description)
CROSS JOIN (VALUES ('B300'), ('B200'), ('H200'), ('H100')) AS mm (model)
JOIN provider p ON p.provider_name = 'Nebius'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter = v.contract);

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, NULL,
       'VM 유형 문서의 플랫폼·프리셋(GPU 수·vCPU·RAM)·GPU 메모리·InfiniBand, 지역 문서의 국가 (수기 확인 근거)'
FROM (VALUES ('B300'), ('B200'), ('H200'), ('H100')) AS mm (model)
JOIN provider p ON p.provider_name = 'Nebius'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = 'nebius_vm_types_docs'
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id);

-- AWS 사양 근거 ---------------------------------------------------------------------------------------------------
-- 가격 파일에 GPU 모델명과 GPU 간 연결 방식이 없어 공식 문서(인스턴스 사양 표, 제품 페이지, 출시 블로그)로 보완한다.
-- 사람이 열람해 확인한 값이다(자동 수집 아님). AWS Site Terms의 자동 수집 금지와 충돌하지 않는다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('aws_ec2_instance_docs', 'official_page', 'https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html',
        'manual', 'allowed', '2026-11-18T00:00:00Z', 'https://aws.amazon.com/terms/')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, NULL,
       'AWS 인스턴스 사양 문서의 GPU 모델·GPU 수·vCPU·메모리와 GPU 간 연결(NVLink·NVSwitch) (수기 확인 근거)'
FROM (VALUES ('B300'), ('B200'), ('H100')) AS mm (model)
JOIN provider p ON p.provider_name = 'AWS'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = 'aws_ec2_instance_docs'
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id);

-- 랙스케일·출시 예정 모델 공개 상태 (FR-027) ------------------------------------------------------------------------
-- 자동 확인: Verda instance-types·cluster-types API, Nebius 공식 가격표에 VR200(Rubin)이 등재됐는지 매일 본다.
-- 수기 확인: 공식 상품·가격 페이지·발표를 월 1회 열어 견적 문의·가격 미공개 상태를 근거와 함께 남긴다(erd/status.py).
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('verda_status_pages', 'official_page', 'https://verda.com/', 'manual', 'allowed', '2026-11-18T00:00:00Z',
        'https://verda.com/terms-and-conditions'),
       ('nebius_status_pages', 'official_page', 'https://nebius.com/prices', 'manual', 'allowed', '2026-11-18T00:00:00Z',
        'https://docs.nebius.com/legal/terms-of-use/')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, NULL, v.description
FROM (VALUES
    ('Verda', 'VR200', 'verda_instance_types_api', 'instance-types·cluster-types에 VR200(Rubin)이 등재됐는지만 확인 (가격 미수집)'),
    ('Nebius', 'VR200', 'nebius_pricing_docs', '공식 가격표에 VR200(Vera Rubin)이 등재됐는지만 확인 (가격 미수집)'),
    ('Verda', 'GB300', 'verda_status_pages', 'GB300 NVL72 랙(랙스케일) 공개 상태 수기 확인 (월 1회, 공식 페이지)'),
    ('Verda', 'VR200', 'verda_status_pages', 'VR200 NVL72 공개 상태 수기 확인 (월 1회, 공식 발표·페이지)'),
    ('Nebius', 'GB300', 'nebius_status_pages', 'GB300 NVL72 랙(랙스케일) 공개 상태 수기 확인 (월 1회, 가격 페이지)'),
    ('Nebius', 'GB200', 'nebius_status_pages', 'GB200 NVL72 랙(랙스케일) 공개 상태 수기 확인 (월 1회, 가격 페이지)'),
    ('Nebius', 'VR200', 'nebius_status_pages', 'VR200(Vera Rubin) NVL72 공개 상태 수기 확인 (월 1회, 공식 발표)')
) AS v (provider, model, source, description)
JOIN provider p ON p.provider_name = v.provider
JOIN gpu_model m ON m.model_name = v.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter IS NULL);

-- AWS·Azure 랙스케일(NVL72)·VR200 수기 상태 (FR-027) -----------------------------------------------------------------
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('aws_status_pages', 'official_page', 'https://aws.amazon.com/ec2/capacityblocks/pricing/', 'manual', 'allowed',
        '2026-11-18T00:00:00Z', 'https://aws.amazon.com/terms/'),
       ('azure_status_pages', 'official_page', 'https://azure.microsoft.com/', 'manual', 'allowed',
        '2026-11-18T00:00:00Z', 'https://learn.microsoft.com/en-us/legal/termsofuse')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, NULL, v.description
FROM (VALUES
    ('AWS', 'GB200', 'aws_status_pages', 'P6e-GB200 UltraServer(GB200 NVL72) 공개 상태 수기 확인 (월 1회, Capacity Blocks 가격표)'),
    ('AWS', 'GB300', 'aws_status_pages', 'P6e-GB300 UltraServer(GB300 NVL72) 공개 상태 수기 확인 (월 1회)'),
    ('AWS', 'VR200', 'aws_status_pages', 'VR200(Vera Rubin) NVL72 공개 상태 수기 확인 (월 1회, 공식 발표)'),
    ('Azure', 'GB300', 'azure_status_pages', 'GB300 NVL72(ND GB300 v6) 공개 상태 수기 확인 (월 1회)'),
    ('Azure', 'GB200', 'azure_status_pages', 'GB200 NVL72 랙 단위 공개 상태 수기 확인 (월 1회, VM 가격은 별도 수집)'),
    ('Azure', 'VR200', 'azure_status_pages', 'VR200(Vera Rubin) NVL72 공개 상태 수기 확인 (월 1회, 공식 발표)')
) AS v (provider, model, source, description)
JOIN provider p ON p.provider_name = v.provider
JOIN gpu_model m ON m.model_name = v.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter IS NULL);

-- Oracle Cloud(OCI) -----------------------------------------------------------------------------------------------
-- 공개 정가 API의 'OCI - Compute - GPU - {모델}' 품목(GPU 1장·시간, Pay As You Go, 리전 단일가)을 수집한다.
-- 종합 클라우드(hyperscaler): GPU 외에 범용 VM·스토리지·DB·네트워크를 폭넓게 판매한다.
-- 상품은 모델별 베어메탈 구성(BM.GPU.B300.8 등). GB300·GB200은 NVL72 랙의 4 GPU 노드(BM.GPU.GB300.4·GB200.4)다.
-- 스팟(선점형)·약정 가격은 API에 없어 대상에 넣지 않는다. 선점형 인스턴스(온디맨드의 50%)는 Preemptible Instances 문서의
-- 지원 구성(Supported Shapes)에 GPU가 VM.GPU2(P100)·VM.GPU3(V100)만 있어 대상 모델에는 해당하지 않는다(2026-11-20 확인).
-- 과거 이력: Wayback의 정가 API 응답 저장본(2026-05부터)과 SkyPilot 카탈로그(OCI vms.csv 커밋 이력, H100·H200은
-- 2025-01부터, Blackwell 계열은 2026-11부터). SkyPilot의 OCI 수집기(fetch_oci.py)는 같은 정가 API로 카탈로그를 만든다.
-- 카탈로그 저장소에 LICENSE 파일이 없어 이용 조건은 미확인이다(2026-11-20 확인). oracle.com 가격 페이지는 가격 칸을
-- 브라우저에서 같은 API로 채워 HTML 저장본에 값이 없다.
INSERT INTO provider (provider_name, provider_type, official_url)
VALUES ('Oracle', 'hyperscaler', 'https://www.oracle.com/cloud/')
ON CONFLICT (provider_name) DO NOTHING;

-- OR-02 이용 조건(2026-11-20 확인): OCI 문서 'Estimate Your Monthly Cost'의 'Accessing List Pricing for OCI Products'가
-- 이 API를 curl·프로그래밍 방식으로 정가를 조회하는 방법으로 안내한다(인증 없음, partNumber·currencyCode 파라미터).
-- 공식 안내된 프로그램용 조회라 Oracle 웹사이트 이용 약관의 자동화 수단 접근 제한과 별개로 본다. 하루 1회 호출,
-- 내부 분석 용도이며 원시 데이터를 외부에 재배포하지 않는다. Shapes 문서는 사람이 열람해 확인한 사양 근거다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('oracle_price_list_api', 'official_api', 'https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/',
        'api', 'allowed', '2026-11-20T00:00:00Z',
        'https://docs.oracle.com/en-us/iaas/Content/Billing/Tasks/signingup_topic-Estimating_Costs.htm'),
       ('oracle_compute_shapes_docs', 'official_page',
        'https://docs.oracle.com/en-us/iaas/Content/Compute/References/computeshapes.htm',
        'manual', 'allowed', '2026-11-20T00:00:00Z', 'https://www.oracle.com/legal/terms/'),
       ('oracle_price_list_api_wayback', 'aggregator',
        'https://web.archive.org/web/*/apexapps.oracle.com/pls/apex/cetools/api/v1/products/*',
        'api', 'unknown', NULL, 'https://archive.org/about/terms.php'),
       ('oracle_skypilot_catalog', 'aggregator',
        'https://github.com/skypilot-org/skypilot-catalog/tree/master/catalogs',
        'file', 'unknown', NULL, 'https://github.com/skypilot-org/skypilot-catalog')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, v.contract, v.description
FROM (VALUES
    ('oracle_price_list_api', 'on_demand', '공개 정가 API의 GPU 1장·시간 Pay As You Go 정가 (USD, 리전 단일가)'),
    ('oracle_price_list_api_wayback', 'on_demand', 'Wayback 정가 API 응답 저장본의 Pay As You Go 정가 (집계, 과거 이력, USD 전체 목록만)'),
    ('oracle_skypilot_catalog', 'on_demand', 'SkyPilot 카탈로그 OCI vms.csv 커밋별 노드 시간당 가격 (집계, 과거 이력, 커밋 시각 기준)'),
    ('oracle_compute_shapes_docs', NULL, 'Compute Shapes 문서의 베어메탈 GPU 구성(GPU 수·GPU 메모리·CPU 메모리·로컬 디스크·RDMA, 수기 확인 근거)')
) AS v (source, contract, description)
CROSS JOIN (VALUES ('B300'), ('GB300'), ('B200'), ('GB200'), ('H200'), ('H100')) AS mm (model)
JOIN provider p ON p.provider_name = 'Oracle'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter IS NOT DISTINCT FROM v.contract);

-- Scaleway --------------------------------------------------------------------------------------------------------
-- 공개 카탈로그 API의 GPU Instance(B300-SXM-8, H100-SXM-8/4/2, H100 PCIe 1/2)와 Elastic Metal H100 시간당 요금을 수집한다.
-- 종합 클라우드(hyperscaler): 일반 VM·베어메탈·오브젝트 스토리지·관리형 DB·Kubernetes·서버리스를 판매한다.
-- 가격은 EUR(Instance 분 단위). USD 환산은 수출입은행 교차환율(규칙 v4). Elastic Metal 월 요금·설치비와 Dedibox는 기한 없는
-- 월 구독의 1회 설치비라 약정으로 환산하면 단가가 왜곡돼 수집하지 않는다. 스팟·약정 상품은 없다.
INSERT INTO provider (provider_name, provider_type, official_url)
VALUES ('Scaleway', 'hyperscaler', 'https://www.scaleway.com')
ON CONFLICT (provider_name) DO NOTHING;

-- 분 단가·비USD 통화를 v3에 더한 규칙. 교차환율은 관측일(KST) 이하 최신 기준일(원화 환산과 같은 규칙).
INSERT INTO normalization_rule (rule_version, monthly_hours, formula_description, precision_policy)
VALUES ('v4', 730,
        'v3에 더해: 분 단위는 × 60으로 시간 단가. 비USD 통화는 한국수출입은행 매매기준율 교차환율'
        '({통화}→KRW ÷ USD→KRW, 관측일(KST) 이하 최신 기준일, 두 통화 같은 기준일)로 USD. 쓴 환율 ID·기준일은 calculation_inputs에 기록',
        '저장은 원본 정밀도(교차환율 나눗셈은 28자리 유효숫자). 표시는 USD 소수 둘째 자리, KRW 원 단위')
ON CONFLICT (rule_version) DO NOTHING;

-- OR-02 이용 조건(2026-11-20 확인): 공식 Go SDK(github.com/scaleway/scaleway-sdk-go, api/product_catalog/v2alpha1)의
-- PublicCatalogAPI.ListPublicCatalogProducts가 "List all available products in the Scaleway catalog ... prices"로 이 공개
-- 조회를 제공한다(인증 없음). api.scaleway.com/robots.txt의 Disallow: /는 크롤링 규칙으로 보고, 하루 1회 저빈도 호출·내부 분석
-- 용도이며 원시 데이터를 재배포하지 않는다. 버전이 v2alpha1(알파)이라 형식이 바뀌면 검증 실패로 남긴다.
-- www.scaleway.com(가격 페이지·문서)은 BunnyCDN 봇 확인(Bunny Shield)으로 자동 접근을 막아 매일 수집은 API만 쓴다. 가격 페이지의
-- 사양(gpuRam·useNvlink)은 Wayback 저장본(2026-11-18)을 사람이 확인한 값이다. 과거 가격은 Wayback 저장본(집계, 미확인)만 쓴다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('scaleway_product_catalog_api', 'official_api',
        'https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products', 'api', 'allowed',
        '2026-11-20T00:00:00Z', 'https://github.com/scaleway/scaleway-sdk-go/tree/master/api/product_catalog/v2alpha1'),
       ('scaleway_gpu_pricing_page', 'official_page', 'https://www.scaleway.com/en/pricing/gpu/', 'manual', 'allowed',
        '2026-11-20T00:00:00Z', 'https://www.scaleway.com/en/contracts/'),
       ('scaleway_gpu_pricing_wayback', 'aggregator', 'https://web.archive.org/web/*/www.scaleway.com/en/pricing/gpu/',
        'api', 'unknown', NULL, 'https://archive.org/about/terms.php')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, v.contract, v.description
FROM (VALUES
    ('scaleway_product_catalog_api', 'on_demand', '공개 카탈로그 API의 GPU Instance 분 단가·Elastic Metal 시간 단가 (EUR, zone별)'),
    ('scaleway_gpu_pricing_wayback', 'on_demand', 'Wayback 가격 페이지 저장본의 GPU Instance 가격 (집계, 과거 이력)'),
    ('scaleway_gpu_pricing_page', NULL, '가격 페이지 내장 JSON의 GPU 메모리(gpuRam)·NVLink(useNvlink) (수기 확인 근거)')
) AS v (source, contract, description)
CROSS JOIN (VALUES ('B300'), ('H100')) AS mm (model)
JOIN provider p ON p.provider_name = 'Scaleway'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter IS NOT DISTINCT FROM v.contract);

-- Hyperstack (NexGen Cloud) -----------------------------------------------------------------------------------------
-- pricebook(GPU 종류별 장당 시간가, 리전 단일가)과 flavors(리전별 구성)로 B300·B200·H200·H100 온디맨드·스팟을 수집한다.
-- GPU 특화(gpu_specialist): NexGen Cloud가 유럽·북미 자체 데이터센터에서 직접 운영한다. 예약가는 API에 없어 수집하지 않는다.
INSERT INTO provider (provider_name, provider_type, official_url)
VALUES ('Hyperstack', 'gpu_specialist', 'https://www.hyperstack.cloud')
ON CONFLICT (provider_name) DO NOTHING;

-- OR-02 이용 조건(2026-11-22 확인, agy 교차 검증): 공식 문서(docs.hyperstack.cloud/docs/billing/pricebook)가 "Current list
-- prices for every GPU ... are published on the Pricebook API"로 안내하고 공식 Python·Go SDK가 같은 조회를 제공한다(API 키,
-- 분당 500회 제한). 하루 1회 호출, 내부 분석 용도, 원시 데이터 재배포 없음. 계정 계약 할인가(discount_applied)는 T&C 8.1
-- 기밀 소지가 있어 적재하지 않는다. 웹페이지는 자동 접근 금지 문구가 없고 IP 조항(2.1·6.4)만 있어 매일 수집하지 않고
-- 사양 근거는 사람이 확인한 문구만 쓴다. 과거 가격은 Wayback 가격 페이지 저장본(집계, 미확인)이다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('hyperstack_api', 'official_api', 'https://infrahub-api.nexgencloud.com/v1/pricebook', 'api', 'allowed',
        '2026-11-22T00:00:00Z', 'https://docs.hyperstack.cloud/docs/billing/pricebook'),
       ('hyperstack_product_pages', 'official_page', 'https://www.hyperstack.cloud/gpu-pricing', 'manual', 'allowed',
        '2026-11-22T00:00:00Z', 'https://www.hyperstack.cloud/terms-and-conditions'),
       ('hyperstack_gpu_pricing_wayback', 'aggregator', 'https://web.archive.org/web/*/www.hyperstack.cloud/gpu-pricing',
        'page', 'unknown', NULL, 'https://archive.org/about/terms.php')
ON CONFLICT (source_name) DO NOTHING;

INSERT INTO collection_target (provider_id, gpu_model_id, source_id, region_filter, contract_filter, scope_description)
SELECT p.provider_id, m.gpu_model_id, s.source_id, NULL, v.contract, v.description
FROM (VALUES
    ('hyperstack_api', 'on_demand', ARRAY['B300', 'B200', 'H200', 'H100'], 'pricebook GPU 장당 시간가 × flavor(리전별 구성)'),
    ('hyperstack_api', 'spot', ARRAY['B300', 'B200', 'H200', 'H100'], 'pricebook 스팟 GPU 장당 시간가 × 스팟 flavor (없는 모델은 출시 감지용)'),
    ('hyperstack_gpu_pricing_wayback', 'on_demand', ARRAY['B300', 'B200', 'H200', 'H100'], 'Wayback 가격 페이지 저장본의 온디맨드 GPU 장당 시간가 (집계, 과거 이력)'),
    ('hyperstack_gpu_pricing_wayback', 'spot', ARRAY['H100'], 'Wayback 가격 페이지 저장본의 스팟 GPU 장당 시간가 (집계, 과거 이력)'),
    ('hyperstack_product_pages', NULL, ARRAY['B300', 'B200', 'H200', 'H100'], '상품·가격 페이지의 GPU 메모리·NVLink 문구 (수기 확인 근거)')
) AS v (source, contract, models, description)
CROSS JOIN LATERAL unnest(v.models) AS mm (model)
JOIN provider p ON p.provider_name = 'Hyperstack'
JOIN gpu_model m ON m.model_name = mm.model
JOIN data_source s ON s.source_name = v.source
WHERE NOT EXISTS (
    SELECT 1 FROM collection_target t
    WHERE t.provider_id = p.provider_id AND t.gpu_model_id = m.gpu_model_id AND t.source_id = s.source_id
      AND t.region_filter IS NULL AND t.contract_filter IS NOT DISTINCT FROM v.contract);

-- 환율 (한국수출입은행) ----------------------------------------------------------------------------------------------
-- 현재환율 API(data=AP01)의 매매기준율을 응답 통화 전부에 대해 '{통화}→KRW'로 적재한다(erd/fx/collect.py).
-- OR-02 이용 조건(2026-11-20 확인, 같은 날 사용자 승인): API 페이지의 이용허락조건 표시는 공공누리 미적용이다
-- (공공누리 1~4유형 아님, 자유 이용 허락 아님). 인증키를 발급받아 호출하도록 공개한 API이고, 환율은 내부 환산에만 쓰며
-- 원시 데이터를 외부에 재배포하지 않는다. 하루 1,000회 제한, 영업일 11시 전후 갱신. 인증키는 KOREAEXIM_API_KEY로 주입한다.
INSERT INTO data_source (source_name, source_type, base_url, collection_method, terms_status, terms_checked_at, terms_url)
VALUES ('koreaexim_exchange_api', 'fx_source', 'https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON',
        'api', 'allowed', '2026-11-20T00:00:00Z', 'https://www.koreaexim.go.kr/ir/HPHKIR020M01?apino=2&viewtype=C')
ON CONFLICT (source_name) DO NOTHING;
