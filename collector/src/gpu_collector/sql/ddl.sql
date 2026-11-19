-- GPU 시장가격 서비스 ERD 초안 DDL (PostgreSQL 17)
-- 기준: GPU_시장가격_ERD_두_명세서_기준_초안.md (2026-11-16) 5절 컬럼·9절 제약
--
-- 원칙
--   - 테이블·컬럼·필수 여부는 초안 그대로 따른다. 초안의 '필수: 예'는 NOT NULL, '아니요'는 NULL 허용이다.
--   - 초안의 한글 상태값은 영문 코드로 바꿔 CHECK로 묶는다. 대응은 각 컬럼 주석에 적는다.
--   - 시각은 timestamptz(UTC 저장), 금액·비율은 정밀도를 정하지 않은 numeric이다(초안 5절: 정밀도는 별도 확정).
--   - NULL은 모두 '미확인'이다. '없음'·'해당 없음'을 NULL로 적지 않는다(DR-01).
--     예: 최소물량 NULL = 최소물량을 확인하지 못함. 온디맨드 오퍼의 선납 방식 NULL도 미확인으로 표시한다.
--   - 초안 본문의 "검증한다"는 CHECK, 복합 FK, 트리거로 옮긴다.
--   - 초안에 더한 것: 상품·오퍼 식별 UNIQUE(동시 실행 중복 방지), 트리거 함수의 search_path 고정
--     (search_path를 비운 세션, 예를 들어 pg_dump 데이터 복원 중에도 테이블을 찾게 한다).
--
-- 초안에서 미정이라 이 DDL도 정하지 않은 것
--   - 상품의 유일성 키(초안 9절 1번): PRODUCT에 UNIQUE를 두지 않는다
--   - 중복 방지 키 생성 기준(9절 3번): 형식은 두지 않고 UNIQUE만 건다
--   - 월 환산시간 720/730(8절): NORMALIZATION_RULE에 기본 행을 넣지 않는다
--   - 원가·IRR·Agent 대화·평가 기록(6절): 테이블을 만들지 않는다

-- 1. 기준 정보 ---------------------------------------------------------------

CREATE TABLE provider (
    provider_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider_name text NOT NULL UNIQUE,
    -- 종합 클라우드 / GPU 특화 / 미확인
    provider_type text NOT NULL CHECK (provider_type IN ('hyperscaler', 'gpu_specialist', 'unknown')),
    official_url  text
);

CREATE TABLE gpu_model (
    gpu_model_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_name         text NOT NULL UNIQUE,
    manufacturer       text,
    generation         text,                                    -- NULL = 미확인
    release_date       date,                                    -- 출시일. 최초 가격 관측일과 다르다
    release_source_url text,
    CONSTRAINT release_has_source CHECK (release_date IS NULL OR release_source_url IS NOT NULL)
);

-- 이 테이블에 행이 있다고 수집이 허용된 것은 아니다. OR-02 확인 기록용이다.
CREATE TABLE data_source (
    source_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name       text NOT NULL UNIQUE,
    -- 공식 API·공식 페이지·집계 사이트·환율 출처
    source_type       text NOT NULL CHECK (source_type IN ('official_api', 'official_page', 'aggregator', 'fx_source')),
    base_url          text NOT NULL,
    -- API·페이지·파일 등
    collection_method text NOT NULL CHECK (collection_method IN ('api', 'page', 'file', 'manual')),
    -- 허용·불허·미확인
    terms_status      text NOT NULL CHECK (terms_status IN ('allowed', 'not_allowed', 'unknown')),
    terms_checked_at  timestamptz,
    terms_url         text,
    CONSTRAINT terms_decision_has_time CHECK (terms_status = 'unknown' OR terms_checked_at IS NOT NULL)
);

-- 2. 상품과 계약 -------------------------------------------------------------

-- 상품 키 후보는 DR-05(사업자·SKU·모델·구성 유형·서비스 옵션·GPU 수·원본 리전)다.
-- 출처별 안정 키가 정해지지 않아 UNIQUE를 두지 않는다.
-- 구성이 바뀌면 적재를 멈추지 않는다. 기존 행을 덮지 않고 configuration_version을 올린 새 행을 만들어
-- 계속 적재하며, collection_alert에 config_changed를 남긴다(팀 결정: DR-05는 초안의 버전 방식).
CREATE TABLE product (
    product_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider_id             bigint NOT NULL REFERENCES provider (provider_id),
    gpu_model_id            bigint NOT NULL REFERENCES gpu_model (gpu_model_id),
    sku                     text,                               -- 모르면 NULL. 임의 값으로 채우지 않는다
    original_product_name   text,
    original_model_name     text,
    -- 인스턴스·노드·랙스케일·미확인
    system_type             text NOT NULL CHECK (system_type IN ('instance', 'node', 'rack_scale', 'unknown')),
    -- 일반·기밀 컴퓨팅·미확인
    service_option          text NOT NULL CHECK (service_option IN ('standard', 'confidential', 'unknown')),
    gpu_count               integer CHECK (gpu_count > 0),
    vram_gb_per_gpu         numeric CHECK (vram_gb_per_gpu > 0),
    vcpu_count              numeric CHECK (vcpu_count > 0),
    ram_gb                  numeric CHECK (ram_gb > 0),
    storage_description     text,
    -- CPU·RAM·스토리지 값의 기준
    spec_scope              text CHECK (spec_scope IN ('instance', 'node', 'rack')),
    intra_node_interconnect text,                               -- 예: NVLink
    -- 공식 근거 확인 / 미확인 / 해당 없음(GPU 1장)
    interconnect_status     text NOT NULL CHECK (interconnect_status IN ('confirmed', 'unconfirmed', 'not_applicable')),
    inter_node_network      text,                               -- 예: InfiniBand
    region_code             text,                               -- 사업자 원본 리전 코드
    country_code            text CHECK (country_code ~ '^[A-Z]{2}$'),
    configuration_version   integer NOT NULL DEFAULT 1 CHECK (configuration_version >= 1),
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (product_id, provider_id, gpu_model_id),             -- 점검 행의 사업자·모델 일치 검사용
    -- 같은 상품·구성 버전은 한 행만 둔다. 식별 컬럼에 NULL이 있어도 같은 값으로 본다(동시 실행 중복 방지)
    CONSTRAINT product_identity_unique UNIQUE NULLS NOT DISTINCT
        (provider_id, gpu_model_id, original_product_name, region_code, system_type, service_option,
         configuration_version),
    -- 해당 없음은 GPU 1장 상품에만 쓴다. 미확인과 구분한다(초안 5.3 PRODUCT_EVIDENCE)
    CONSTRAINT not_applicable_is_single_gpu CHECK (interconnect_status <> 'not_applicable' OR gpu_count = 1)
);

CREATE INDEX product_provider_model_idx ON product (provider_id, gpu_model_id);

CREATE TABLE offer (
    offer_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id            bigint NOT NULL REFERENCES product (product_id),
    -- 온디맨드·스팟·약정·미확인
    contract_type         text NOT NULL CHECK (contract_type IN ('on_demand', 'spot', 'commitment', 'unknown')),
    contract_product_type text,                                 -- 예약 인스턴스·Savings Plan 등 원본 상품 유형
    commitment_months     integer CHECK (commitment_months > 0),
    -- 선납·부분 선납·무선납
    payment_option        text CHECK (payment_option IN ('all_upfront', 'partial_upfront', 'no_upfront')),
    minimum_quantity      numeric CHECK (minimum_quantity > 0),
    minimum_quantity_unit text,
    terms_version         integer NOT NULL DEFAULT 1 CHECK (terms_version >= 1),
    created_at            timestamptz NOT NULL DEFAULT now(),
    UNIQUE (offer_id, product_id),                              -- 점검 행의 상품·오퍼 일치 검사용
    -- 같은 상품의 같은 계약 조건·약관 버전은 한 행만 둔다(동시 실행 중복 방지)
    CONSTRAINT offer_identity_unique UNIQUE NULLS NOT DISTINCT
        (product_id, contract_type, contract_product_type, commitment_months, payment_option, terms_version),
    CONSTRAINT quantity_has_unit CHECK (minimum_quantity IS NULL OR minimum_quantity_unit IS NOT NULL),
    -- 약정 기간은 약정 오퍼에만 있다
    CONSTRAINT commitment_months_only_for_commitment CHECK (commitment_months IS NULL OR contract_type = 'commitment')
);

CREATE INDEX offer_product_idx ON offer (product_id);

-- 3. 수집과 근거 -------------------------------------------------------------

-- 상품이 아직 없어도 등록한다. 범위가 바뀌면 새 scope_version 행으로 관리한다.
CREATE TABLE collection_target (
    target_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider_id       bigint NOT NULL REFERENCES provider (provider_id),
    gpu_model_id      bigint NOT NULL REFERENCES gpu_model (gpu_model_id),
    source_id         bigint NOT NULL REFERENCES data_source (source_id),
    region_filter     text,                                     -- NULL = 미지정
    contract_filter   text,                                     -- NULL = 미지정
    scope_description text NOT NULL,
    scope_version     integer NOT NULL DEFAULT 1 CHECK (scope_version >= 1),
    enabled           boolean NOT NULL DEFAULT true
);

CREATE INDEX collection_target_provider_model_idx ON collection_target (provider_id, gpu_model_id);

CREATE TABLE collection_run (
    run_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at        timestamptz NOT NULL,
    finished_at       timestamptz,                              -- 실행 중이면 NULL
    -- 진행·성공·부분 실패·실패
    run_status        text NOT NULL CHECK (run_status IN ('running', 'success', 'partial_failure', 'failed')),
    -- 정기·수동·재시도
    trigger_type      text NOT NULL CHECK (trigger_type IN ('scheduled', 'manual', 'retry')),
    collector_version text,
    failure_reason    text,                                     -- 비밀값을 남기지 않는다
    CONSTRAINT finished_after_start CHECK (finished_at IS NULL OR finished_at >= started_at),
    CONSTRAINT running_iff_unfinished CHECK ((run_status = 'running') = (finished_at IS NULL))
);

CREATE INDEX collection_run_started_idx ON collection_run (started_at DESC);

-- 범위 점검은 상품·오퍼 없이, 상품 점검은 상품만, 오퍼 점검은 상품과 오퍼를 함께 연결한다.
CREATE TABLE collection_check (
    check_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id            bigint NOT NULL REFERENCES collection_run (run_id),
    target_id         bigint NOT NULL REFERENCES collection_target (target_id),
    product_id        bigint REFERENCES product (product_id),
    offer_id          bigint,
    -- 대상 범위 / 상품 / 오퍼 점검
    check_scope       text NOT NULL CHECK (check_scope IN ('target', 'product', 'offer')),
    checked_at        timestamptz NOT NULL,
    -- 공개가·견적 문의·로그인 필요·가격 미공개·미확인
    disclosure_status text NOT NULL CHECK (disclosure_status IN ('public', 'contact_sales', 'login_required', 'not_disclosed', 'unknown')),
    -- 성공·경로 미지원·미발견·수집 실패·검증 실패
    processing_status text NOT NULL CHECK (processing_status IN ('success', 'unsupported_path', 'not_found', 'collection_failed', 'validation_failed')),
    scope_completed   boolean NOT NULL,
    reason            text,
    deduplication_key text NOT NULL UNIQUE,                     -- 생성 기준은 협의(초안 9절 3번)
    -- 오퍼는 연결한 상품의 오퍼여야 한다
    FOREIGN KEY (offer_id, product_id) REFERENCES offer (offer_id, product_id),
    UNIQUE (check_id, offer_id),                                -- 관측의 오퍼 일치 검사용
    CONSTRAINT scope_links CHECK (
        (check_scope = 'target'  AND product_id IS NULL     AND offer_id IS NULL) OR
        (check_scope = 'product' AND product_id IS NOT NULL AND offer_id IS NULL) OR
        (check_scope = 'offer'   AND product_id IS NOT NULL AND offer_id IS NOT NULL)
    ),
    -- 범위를 끝까지 확인했을 때만 미발견으로 판정한다
    CONSTRAINT not_found_needs_completed_scope CHECK (processing_status <> 'not_found' OR scope_completed),
    -- 요청·처리 실패를 공식 미공개 등 공개 상태로 바꾸지 않는다
    CONSTRAINT failure_keeps_disclosure_unknown CHECK (
        processing_status NOT IN ('collection_failed', 'validation_failed') OR disclosure_status = 'unknown'
    )
);

CREATE INDEX collection_check_target_idx ON collection_check (target_id, checked_at DESC);
CREATE INDEX collection_check_run_idx ON collection_check (run_id);
CREATE INDEX collection_check_offer_idx ON collection_check (offer_id, checked_at DESC) WHERE offer_id IS NOT NULL;

-- 연결한 상품의 사업자·모델이 수집 대상과 같아야 한다(초안 9절 2번)
CREATE FUNCTION collection_check_matches_target() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
    IF NEW.product_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM product p
        JOIN collection_target t ON t.target_id = NEW.target_id
        WHERE p.product_id = NEW.product_id
          AND p.provider_id = t.provider_id
          AND p.gpu_model_id = t.gpu_model_id
    ) THEN
        RAISE EXCEPTION 'collection_check %: 상품 %의 사업자·모델이 수집 대상 %와 다르다',
            NEW.check_id, NEW.product_id, NEW.target_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER collection_check_matches_target
BEFORE INSERT OR UPDATE OF product_id, target_id ON collection_check
FOR EACH ROW EXECUTE FUNCTION collection_check_matches_target();

-- 한 점검에 공식 원천과 집계 근거를 여러 행으로 연결한다.
CREATE TABLE source_evidence (
    evidence_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_id         bigint NOT NULL REFERENCES collection_check (check_id),
    source_id        bigint NOT NULL REFERENCES data_source (source_id),
    -- 공식 원천 / 집계 / 사양 근거
    evidence_role    text NOT NULL CHECK (evidence_role IN ('official', 'aggregator', 'spec')),
    source_url       text NOT NULL,
    source_version   text,
    excerpt          text,                                      -- 출처 조건이 허용하는 범위에서만
    primary_verified boolean,                                   -- 집계값의 공식 원천 확인 여부. 모르면 NULL
    verified_at      timestamptz,
    captured_at      timestamptz NOT NULL,
    CONSTRAINT verified_at_needs_verified CHECK (verified_at IS NULL OR primary_verified IS TRUE)
);

CREATE INDEX source_evidence_check_idx ON source_evidence (check_id);

CREATE TABLE product_evidence (
    product_id     bigint NOT NULL REFERENCES product (product_id),
    evidence_id    bigint NOT NULL REFERENCES source_evidence (evidence_id),
    attribute_name text NOT NULL CHECK (attribute_name <> ''),  -- 예: gpu_count, intra_node_interconnect
    PRIMARY KEY (product_id, evidence_id, attribute_name)
);

CREATE INDEX product_evidence_evidence_idx ON product_evidence (evidence_id);

-- 노드 내 상호연결은 공식 근거가 있을 때만 '확인'이다(DR-07).
-- 상품과 근거를 같은 트랜잭션에서 넣을 수 있도록 커밋 시점에 검사한다.
CREATE FUNCTION product_interconnect_has_evidence() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    pid bigint;
BEGIN
    IF TG_TABLE_NAME = 'product' THEN
        pid := NEW.product_id;
    ELSE
        pid := OLD.product_id;
    END IF;
    IF EXISTS (SELECT 1 FROM product WHERE product_id = pid AND interconnect_status = 'confirmed')
       AND NOT EXISTS (
           SELECT 1 FROM product_evidence
           WHERE product_id = pid AND attribute_name = 'intra_node_interconnect'
       ) THEN
        RAISE EXCEPTION '상품 %: 상호연결이 confirmed인데 intra_node_interconnect 근거가 없다', pid;
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER product_interconnect_has_evidence
AFTER INSERT OR UPDATE OF interconnect_status ON product
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION product_interconnect_has_evidence();

CREATE CONSTRAINT TRIGGER product_evidence_removal_keeps_interconnect
AFTER DELETE OR UPDATE ON product_evidence
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION product_interconnect_has_evidence();

-- 4. 가격·환산·환율 ----------------------------------------------------------

-- 추가 전용. 가격이 같아도 수집마다 추가하고, 정정은 이전 관측을 가리키는 새 행으로 남긴다.
-- 미발견·실패는 collection_check에만 남기고 0원 관측을 만들지 않는다.
CREATE TABLE price_observation (
    observation_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id                bigint NOT NULL REFERENCES offer (offer_id),
    -- 한 오퍼 점검에 관측은 최대 하나다. 정정 관측도 자기 점검이 필요하다
    check_id                bigint NOT NULL UNIQUE,
    collected_at            timestamptz NOT NULL,
    effective_from          timestamptz,                        -- 사업자가 명시한 적용 시각. 불명이면 NULL
    -- 확정가·시작가·범위·추정
    price_certainty         text NOT NULL CHECK (price_certainty IN ('firm', 'starting_from', 'range', 'estimate')),
    raw_price_text          text,
    original_currency       text CHECK (original_currency ~ '^[A-Z]{3}$'),
    original_time_unit      text,                               -- 시간·월·전체 약정 등 원본 단위
    price_applies_to        text,                               -- GPU·인스턴스·클러스터 등
    raw_terms               text,
    corrects_observation_id bigint,
    correction_reason       text,
    -- 점검이 확인한 오퍼와 같아야 한다
    FOREIGN KEY (check_id, offer_id) REFERENCES collection_check (check_id, offer_id),
    -- 정정은 같은 오퍼의 이전 관측만 가리킨다
    UNIQUE (observation_id, offer_id),
    FOREIGN KEY (corrects_observation_id, offer_id) REFERENCES price_observation (observation_id, offer_id),
    CONSTRAINT correction_has_reason CHECK ((corrects_observation_id IS NULL) = (correction_reason IS NULL)),
    CONSTRAINT correction_not_self CHECK (corrects_observation_id IS DISTINCT FROM observation_id)
);

CREATE INDEX price_observation_offer_idx ON price_observation (offer_id, collected_at DESC);

-- 선납금과 시간당 요금처럼 한 관측의 가격 요소를 각각 보존한다.
CREATE TABLE price_component (
    component_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL REFERENCES price_observation (observation_id),
    -- 표시가·시간 요율·선납금·약정 총액·할인율·범위 하한·범위 상한
    component_type text NOT NULL CHECK (component_type IN (
        'list_price', 'hourly_rate', 'upfront_fee', 'commitment_total', 'discount_rate', 'range_min', 'range_max'
    )),
    amount         numeric CHECK (amount >= 0),                 -- 할인율만 있으면 NULL
    currency       text CHECK (currency ~ '^[A-Z]{3}$'),
    time_unit      text,
    applies_to     text,
    discount_rate  numeric,                                     -- 기준 단위·값 범위는 구현 시 확정
    raw_value      text NOT NULL,
    UNIQUE (component_id, observation_id),
    CONSTRAINT discount_type_matches CHECK (
        (component_type = 'discount_rate') = (discount_rate IS NOT NULL)
    ),
    CONSTRAINT amount_has_currency CHECK (amount IS NULL OR currency IS NOT NULL)
);

CREATE INDEX price_component_observation_idx ON price_component (observation_id);

-- 관측과 가격 요소는 고치거나 지우지 않는다
CREATE FUNCTION reject_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
    RAISE EXCEPTION '%는 추가 전용이다. 정정은 새 관측으로 남긴다', TG_TABLE_NAME;
END $$;

CREATE TRIGGER price_observation_append_only
BEFORE UPDATE OR DELETE ON price_observation
FOR EACH ROW EXECUTE FUNCTION reject_change();

CREATE TRIGGER price_component_append_only
BEFORE UPDATE OR DELETE ON price_component
FOR EACH ROW EXECUTE FUNCTION reject_change();

-- 월 환산시간 720/730은 합의 전이라 기본 행을 넣지 않는다.
CREATE TABLE normalization_rule (
    rule_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_version        text NOT NULL UNIQUE,
    monthly_hours       numeric NOT NULL CHECK (monthly_hours > 0),
    formula_description text NOT NULL,
    precision_policy    text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- 정상 USD 가격의 환산 결과. 사용자 입력 환율·원가·이익률은 넣지 않는다.
CREATE TABLE price_normalization (
    normalization_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id         bigint NOT NULL REFERENCES price_observation (observation_id),
    rule_id                bigint NOT NULL REFERENCES normalization_rule (rule_id),
    nominal_usd_gpu_hour   numeric CHECK (nominal_usd_gpu_hour >= 0),
    effective_usd_gpu_hour numeric CHECK (effective_usd_gpu_hour >= 0),
    -- 계산 성공·계산 불가·가정 시나리오
    result_status          text NOT NULL CHECK (result_status IN ('success', 'not_computable', 'assumption')),
    reason                 text,
    -- 공개 계산 입력의 기록. 구성요소 ID는 FK가 아니라서 아래 트리거로 검증한다.
    -- 형식: {"component_ids": [..], "gpu_count": .., "hours": .., ...}
    calculation_inputs     jsonb NOT NULL CHECK (jsonb_typeof(calculation_inputs) = 'object'),
    calculated_at          timestamptz NOT NULL,
    UNIQUE (observation_id, rule_id),
    CONSTRAINT success_has_value CHECK (
        result_status <> 'success' OR nominal_usd_gpu_hour IS NOT NULL OR effective_usd_gpu_hour IS NOT NULL
    ),
    CONSTRAINT not_computable_has_reason CHECK (
        result_status <> 'not_computable'
        OR (nominal_usd_gpu_hour IS NULL AND effective_usd_gpu_hour IS NULL AND reason IS NOT NULL)
    ),
    CONSTRAINT assumption_has_reason CHECK (result_status <> 'assumption' OR reason IS NOT NULL)
);

CREATE INDEX price_normalization_observation_idx ON price_normalization (observation_id);

-- calculation_inputs의 component_ids가 모두 같은 관측의 가격 요소인지 확인한다(초안 5.4)
CREATE FUNCTION normalization_inputs_belong_to_observation() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    ids jsonb := NEW.calculation_inputs -> 'component_ids';
    bad bigint;
BEGIN
    IF ids IS NULL THEN
        RETURN NEW;
    END IF;
    IF jsonb_typeof(ids) <> 'array' THEN
        RAISE EXCEPTION 'calculation_inputs.component_ids는 배열이어야 한다';
    END IF;
    SELECT x::bigint INTO bad
    FROM jsonb_array_elements_text(ids) AS x
    WHERE NOT EXISTS (
        SELECT 1 FROM price_component c
        WHERE c.component_id = x::bigint AND c.observation_id = NEW.observation_id
    )
    LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION '가격 요소 %는 관측 %에 속하지 않는다', bad, NEW.observation_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER normalization_inputs_belong_to_observation
BEFORE INSERT OR UPDATE OF calculation_inputs, observation_id ON price_normalization
FOR EACH ROW EXECUTE FUNCTION normalization_inputs_belong_to_observation();

-- 자동 조회 환율. 수집 실패 시 다른 값으로 대체하지 않는다.
CREATE TABLE exchange_rate (
    exchange_rate_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id        bigint NOT NULL REFERENCES data_source (source_id),
    base_currency    text NOT NULL CHECK (base_currency ~ '^[A-Z]{3}$'),
    quote_currency   text NOT NULL CHECK (quote_currency ~ '^[A-Z]{3}$'),
    rate             numeric NOT NULL CHECK (rate > 0),
    reference_date   date NOT NULL,
    collected_at     timestamptz NOT NULL,
    source_url       text,
    UNIQUE (source_id, base_currency, quote_currency, reference_date),
    CONSTRAINT distinct_currencies CHECK (base_currency <> quote_currency)
);

-- 자동 USD/KRW 환율만 적용한다. 사용자 입력 환율의 결과는 저장하지 않는다(FR-16).
CREATE TABLE krw_conversion (
    conversion_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalization_id       bigint NOT NULL REFERENCES price_normalization (normalization_id),
    exchange_rate_id       bigint NOT NULL REFERENCES exchange_rate (exchange_rate_id),
    nominal_krw_gpu_hour   numeric CHECK (nominal_krw_gpu_hour >= 0),
    effective_krw_gpu_hour numeric CHECK (effective_krw_gpu_hour >= 0),
    calculated_at          timestamptz NOT NULL,
    UNIQUE (normalization_id, exchange_rate_id)
);

CREATE INDEX krw_conversion_rate_idx ON krw_conversion (exchange_rate_id);

-- 환율 방향이 USD→KRW이고, USD 원본값이 없는 쪽은 원화도 비워야 한다
CREATE FUNCTION krw_conversion_consistent() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
    r exchange_rate%ROWTYPE;
    n price_normalization%ROWTYPE;
BEGIN
    SELECT * INTO r FROM exchange_rate WHERE exchange_rate_id = NEW.exchange_rate_id;
    IF r.base_currency <> 'USD' OR r.quote_currency <> 'KRW' THEN
        RAISE EXCEPTION '환율 %는 USD→KRW가 아니다(%→%)', r.exchange_rate_id, r.base_currency, r.quote_currency;
    END IF;
    SELECT * INTO n FROM price_normalization WHERE normalization_id = NEW.normalization_id;
    IF (n.nominal_usd_gpu_hour IS NULL AND NEW.nominal_krw_gpu_hour IS NOT NULL)
       OR (n.effective_usd_gpu_hour IS NULL AND NEW.effective_krw_gpu_hour IS NOT NULL) THEN
        RAISE EXCEPTION '환산 %: USD 원본값이 없는 항목에 원화 값이 있다', n.normalization_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER krw_conversion_consistent
BEFORE INSERT OR UPDATE ON krw_conversion
FOR EACH ROW EXECUTE FUNCTION krw_conversion_consistent();

-- 5. 운영 기록 (초안의 제안 테이블) ------------------------------------------

-- 운영 로그·별도 저장소를 쓰기로 하면 이 테이블은 만들지 않는다.
CREATE TABLE collection_alert (
    alert_id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id                  bigint REFERENCES collection_run (run_id),
    target_id               bigint REFERENCES collection_target (target_id),   -- 환율 등 GPU 대상이 없으면 NULL
    check_id                bigint REFERENCES collection_check (check_id),     -- 점검 전 실패면 NULL
    source_id               bigint REFERENCES data_source (source_id),
    previous_observation_id bigint REFERENCES price_observation (observation_id),
    current_observation_id  bigint REFERENCES price_observation (observation_id),
    -- 급변(OR-06)·수집 실패·누락·환율 수집 실패·환율 기준일 4일 초과(DR-04)·상품 구성 변경(DR-05)
    alert_type              text NOT NULL CHECK (alert_type IN (
        'price_spike', 'collection_failed', 'missing', 'fx_failed', 'fx_stale', 'config_changed'
    )),
    detected_at             timestamptz NOT NULL,
    rule_version            text,
    reason                  text,
    -- 미확인·확인 중·처리
    handling_status         text NOT NULL DEFAULT 'open' CHECK (handling_status IN ('open', 'investigating', 'resolved')),
    deduplication_key       text NOT NULL UNIQUE,
    -- 급변은 전후 관측이 모두 있어야 한다
    CONSTRAINT spike_has_both_observations CHECK (
        alert_type <> 'price_spike'
        OR (previous_observation_id IS NOT NULL AND current_observation_id IS NOT NULL
            AND previous_observation_id <> current_observation_id)
    )
);

CREATE INDEX collection_alert_detected_idx ON collection_alert (detected_at DESC);
CREATE INDEX collection_alert_open_idx ON collection_alert (handling_status) WHERE handling_status <> 'resolved';
