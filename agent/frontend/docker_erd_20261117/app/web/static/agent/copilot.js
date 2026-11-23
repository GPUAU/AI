/* Use the original Copilot panel with the Spring public-price endpoint. */
const publicPriceExamples = [
  'B300 온디맨드 GPU당 시간 가격과 출처 알려줘',
  'B300 온디맨드 가격의 최저가와 중앙값을 비교해줘',
  '최근 30일 B300 온디맨드 가격 변동 보여줘',
  'B300 1년과 3년 약정 할인율 알려줘',
  'B300 온디맨드 가격 공개 여부 알려줘',
  'B300 온디맨드 1년 후 가격을 예측해줘',
  '미국 AWS B300 온디맨드 가격 알려줘',
  'H100 온디맨드 가격과 수집 시각 알려줘',
  'VR200 온디맨드 가격 알려줘'
];
const springAgentWelcome = $('#agentWelcome');
let springAgentSessionId = null;
let springAgentBusy = false;

function syncSpringCopilotControls() {
  $('#agentReset').disabled = springAgentBusy || !state.hist.length;
  document.querySelectorAll('#askForm button[type="submit"], #exQs button, #histQs button, #feed [data-welcome-ask]')
    .forEach(button => { button.disabled = springAgentBusy; });
}

RENDER.agent = () => {
  $('#agentDesc').textContent = '공개 GPU 가격과 동일 조건의 사업자별 비교 결과를 확인합니다.';
  $('#exQs').innerHTML = publicPriceExamples
    .map(message => `<button type="button" class="qbtn" data-q="${esc(message)}">${esc(message)}</button>`)
    .join('');
  $('#usageBox').textContent = '후속 질문에는 앞서 선택한 조회 조건이 이어집니다. 조건을 처음부터 설정하려면 ‘새 대화’를 누르세요.';
  renderHist();
  syncSpringCopilotControls();
};

async function askSpringPrice(text) {
  const message = text.trim();
  if (!message || springAgentBusy) return;

  const feed = $('#feed');
  if (!feed) return;
  springAgentBusy = true;
  $('#agentSessionStatus').textContent = '';
  $('#agentWelcome')?.remove();
  const question = document.createElement('div');
  question.className = 'msg-u';
  question.textContent = message;
  const reply = document.createElement('div');
  reply.className = 'msg-a';
  reply.setAttribute('aria-live', 'polite');
  reply.textContent = '공개가격 조회 중…';
  feed.append(question, reply);
  const revealReply = () => {
    feed.scrollTop += Math.max(0, reply.getBoundingClientRect().top - feed.getBoundingClientRect().top - 12);
  };
  revealReply();
  syncSpringCopilotControls();
  try {
    const result = await GpuSpringAnalysis.askPrice(message, undefined, springAgentSessionId);
    state.hist.unshift(message);
    renderHist();
    springAgentSessionId = result.sessionId || null;
    const historical = Boolean(result.history?.observedBefore || result.history?.observedOn);
    const label = {
      ANSWERED: ({INTERNAL_PROPOSAL:'내부 제안가 검토', PRICE_REMOVAL:'공개가격 상태 변경'})[result.analysis?.kind]
        || (historical ? '과거 공개가격 관측' : '확인된 공개가격'),
      NO_DATA: result.analysis?.kind === 'MONTH_OVER_MONTH' ? '월간 비교 근거 부족'
        : result.analysis?.kind === 'WEEK_OVER_WEEK' ? '주간 비교 근거 부족'
        : result.analysis?.status === 'INSUFFICIENT_EVIDENCE' || result.analysis?.kind === 'PRICE_REMOVAL' ? '근거 부족'
        : historical ? '과거 가격 기록 없음' : '가격 미확보',
      NEEDS_CLARIFICATION: '질문 조건 확인'
    }[result.status] || '조회 결과';
    if (result.analysis) {
      const disclaimer = result.disclaimer ? '<p>' + esc(result.disclaimer) + '</p>' : '';
      reply.innerHTML = '<div class="hd"><span class="route">' + esc(label) + '</span></div>'
        + '<div class="md"><p>' + esc(result.answer) + '</p>' + renderAgentAnalysis(result.analysis)
        + disclaimer + '</div>';
      paintAgentAnalysis(reply, result.analysis);
      return;
    }
    if (result.comparison) {
      const comparison = result.comparison;
      const assumptions = (result.assumptions || []).map(text => `<p>${esc(text)}</p>`).join('');
      const content = comparison.requestedCommitmentMonths?.length > 1
        ? renderAgentCommitmentComparison(comparison)
        : comparison.groups.map(group => renderAgentComparisonGroup(comparison, group)).join('');
      reply.innerHTML = `<div class="hd"><span class="route">${esc(label)}</span></div>
        <div class="md"><p>${esc(result.answer)}</p>${assumptions}${content}
        ${marketSummaryMetadata(comparison)}<p>${esc(result.disclaimer)}</p></div>`;
      return;
    }
    const rows = (result.results || []).map(item => {
      const conditions = [item.providerName, item.regionCode,
        item.gpuCount == null ? null : item.gpuCount + ' GPU'].filter(Boolean).join(' · ');
      const price = item.priceUsdGpuHour == null ? '가격 미확보' : '$' + item.priceUsdGpuHour;
      return [
        esc(conditions),
        esc(price),
        agentTime(item.collectedAt),
        agentEvidence(GpuSpringAnalysis.sourcesForResult(item, result.sources || []))
      ];
    });
    const answer = rows.length ? String(result.answer ?? '').split(/\r?\n/, 1)[0] : result.answer;
    const listing = rows.length
      ? `<p><small>오퍼별 마지막 공개가격 ${esc(rows.length)}건 · 최근 수집순</small></p>
        ${agentTable(['사업자 · 리전 · GPU 수', '단가 (USD/GPU·시간)', '수집 시각', '출처'], rows)}
        ${result.disclaimer ? '<p>' + esc(result.disclaimer) + '</p>' : ''}`
      : '';
    reply.innerHTML = `<div class="hd"><span class="route">${esc(label)}</span></div>
      <div class="md"><p>${esc(answer)}</p>${listing}</div>`;
  } catch (error) {
    if (error.code === 'SENSITIVE_INPUT_BLOCKED') question.remove();
    if (error instanceof GpuSpringAnalysis.AgentSessionExpiredError) springAgentSessionId = null;
    reply.textContent = error.message;
  } finally {
    springAgentBusy = false;
    syncSpringCopilotControls();
    revealReply();
  }
}

async function resetSpringCopilot() {
  if (springAgentBusy || !state.hist.length) return;
  const status = $('#agentSessionStatus');
  springAgentBusy = true;
  status.textContent = '이전 대화 종료 중…';
  syncSpringCopilotControls();
  try {
    if (springAgentSessionId) await GpuSpringAnalysis.deleteAgentSession(springAgentSessionId);
    springAgentSessionId = null;
    state.hist.length = 0;
    $('#feed').replaceChildren(springAgentWelcome);
    renderHist();
    const input = $('#askIn');
    input.value = '';
    input.focus();
    status.textContent = '새 대화를 시작할 수 있습니다.';
  } catch (error) {
    status.textContent = error.message;
  } finally {
    springAgentBusy = false;
    syncSpringCopilotControls();
  }
}

$('#askForm').addEventListener('submit', event => {
  if (!springAgentBusy) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);
$('#agentReset').onclick = resetSpringCopilot;
ask = askSpringPrice;

const agentUsd = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 10
});
const agentPercent = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 2 });
const agentDiscountReasons = {
  BASE_PRICE_MISSING:'온디맨드 기준가 미확보',
  AMBIGUOUS_BASE_PRICE:'온디맨드 기준가 여러 건',
  CONDITIONS_NOT_COMPARABLE:'최소 물량·선납 방식·환산 기준을 맞출 수 없음'
};
const agentPlanNames = {
  COMPUTE_SAVINGS_PLANS:'Compute Savings Plans',
  EC2_INSTANCE_SAVINGS_PLANS:'EC2 Instance Savings Plans'
};
const agentPaymentOrder = {ALL_UPFRONT:0, PARTIAL_UPFRONT:1, NO_UPFRONT:2};

function renderAgentProductQuote(quote, commitment) {
  const plan = quote.contractProductType;
  const conditions = commitment
    ? [agentPlanNames[plan] || plan, marketPayment[quote.paymentOption] || quote.paymentOption]
        .filter(Boolean).map(esc).join(' · ')
    : '';
  const min = quote.minimumQuantity == null ? '' : '<br>최소 물량 ' + esc(quote.minimumQuantity)
    + ' ' + esc(quote.minimumQuantityUnit);
  const discount = !commitment ? ''
    : quote.discountPercent == null ? '<br>할인율 산정 불가 · ' + esc(agentDiscountReasons[quote.discountReason] || '비교 조건 미확인')
    : '<br>할인율 <b>' + agentPercent.format(quote.discountPercent) + '%</b>';
  return `<details class="agent-product-quote"><summary><b>${agentUsd.format(quote.priceUsdGpuHour)}</b>
    ${conditions ? '<br>' + conditions : ''}${discount}</summary>
    <small>수집 ${agentTime(quote.collectedAt)}${min}<br>${agentEvidence(quote.sources)}</small></details>`;
}

function renderAgentProductPrices(items, terms) {
  if (!items.length) return '';
  const rows = items.map(item => {
    const name = item.productName || item.sku || '상품 ' + item.productId;
    const sku = item.sku && item.sku !== name ? '<br><small>SKU ' + esc(item.sku) + '</small>' : '';
    const location = [item.providerName, item.regionCode || item.countryCode || '리전 미확인',
      item.gpuCount == null ? null : item.gpuCount + ' GPU'].filter(Boolean).map(esc).join(' · ');
    const commitments = [...item.commitments].sort((a, b) =>
      (a.contractProductType || '').localeCompare(b.contractProductType || '')
      || (agentPaymentOrder[a.paymentOption] ?? 3) - (agentPaymentOrder[b.paymentOption] ?? 3));
    const quotes = (values, isCommitment) => values.length
      ? values.map(quote => renderAgentProductQuote(quote, isCommitment)).join('')
      : '공개가 미확보';
    return ['<b>' + esc(name) + '</b><br>' + location + sku,
      quotes(item.onDemand, false), ...terms.map(term => quotes(commitments.filter(quote => quote.months === term), true))];
  });
  const headings = ['상품 · 리전 · GPU 수', '온디맨드',
    ...terms.map(term => (term % 12 === 0 ? term / 12 + '년' : term + '개월') + ' 약정 · 할인율')];
  return '<p>상품 ' + esc(rows.length) + '개 · USD/GPU·시간 · 할인율은 같은 상품의 온디맨드 단가 기준입니다.</p>'
    + '<div class="agent-product-prices">' + agentTable(headings, rows) + '</div>'
    + '<p><small>단가를 펼치면 수집 시각과 출처를 확인할 수 있습니다. 할인율이 음수면 약정 단가가 더 높습니다.</small></p>';
}

function renderAgentComparisonGroup(comparison, group, usdOnly = false) {
  const heading = '<h4>' + esc(marketGroupConditions(group.groupKey)) + '</h4>';
  if (!usdOnly) return '<section>' + heading + marketSummaryContent(comparison, group, false) + '</section>';
  const counts = group.sampleCounts;
  return `<section>${heading}
    <p>최저 ${agentUsd.format(group.minimum)} · 중앙값 ${agentUsd.format(group.median)} · 최고 ${agentUsd.format(group.maximum)} (USD / GPU·시간)</p>
    <p>사업자별 최저 1개 · 사업자 ${esc(counts.providerCount)}곳 / 상품 ${esc(counts.productCount)}개 / 오퍼 ${esc(counts.offerCount)}개 / 관측 ${esc(counts.observationCount)}건</p>
    ${marketRepresentativeTable(group)}</section>`;
}

function renderAgentCommitmentComparison(comparison) {
  const periods = comparison.requestedCommitmentMonths.map(months => ({
    label: months % 12 === 0 ? months / 12 + '년 약정' : months + '개월 약정',
    groups: comparison.groups.filter(group => group.groupKey.commitmentMonths === months)
  }));
  const rows = periods.map(({ label, groups }) => {
    if (!groups.length) return [esc(label), '비교 가능한 공개가격 없음', '0'];
    const min = Math.min(...groups.map(group => Number(group.minimum)));
    const max = Math.max(...groups.map(group => Number(group.maximum)));
    return [esc(label), min === max ? agentUsd.format(min) : agentUsd.format(min) + ' ~ ' + agentUsd.format(max), esc(groups.length)];
  });
  const table = agentTable(['약정기간', '조건별 대표 단가 범위 (USD / GPU·시간)', '가격 조건 수'], rows);
  return '<div class="agent-commitment-overview">' + table + '</div>'
    + '<p><small>리전·상품·선납 방식이 다른 단가를 포함합니다. 기간별 단가를 비교할 때는 상세 조건을 맞춰 확인하세요.</small></p>'
    + periods.filter(({ groups }) => groups.length).map(({ label, groups }) =>
      `<details><summary>${esc(label)} 상세 조건과 출처 (${esc(groups.length)}개)</summary>
        ${groups.map(group => renderAgentComparisonGroup(comparison, group, true)).join('')}</details>`).join('');
}

function agentTable(headings, rows) {
  if (!rows.length) return '';
  return '<div class="mv-tablebox"><table class="mv-table"><thead><tr>'
    + headings.map(label => '<th scope="col">' + esc(label) + '</th>').join('')
    + '</tr></thead><tbody>' + rows.map(row => '<tr>' + row.map(cell => '<td>' + cell + '</td>').join('') + '</tr>').join('')
    + '</tbody></table></div>';
}

function agentTime(value) {
  return value && Number.isFinite(Date.parse(value)) ? esc(new Intl.DateTimeFormat('ko-KR', {
    timeZone: 'Asia/Seoul', dateStyle: 'short', timeStyle: 'short'
  }).format(new Date(value))) + ' KST' : '미확인';
}

function agentEvidence(sources) {
  return (sources || []).flatMap(source => {
    const url = GpuSpringAnalysis.sourceLink(source.sourceUrl);
    return url ? ['<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">'
      + esc(source.sourceName || '공개 출처') + '</a>'] : [];
  }).join(' · ') || '출처 미확인';
}

function agentAnalysisStatus(item) {
  if (item.latestDisclosure?.status === 'NOT_PUBLIC') return '공식 미공개';
  if (item.latestCheck?.processingStatus === 'NOT_FOUND') return '가격 미발견';
  if (['COLLECTION_FAILED', 'FAILED'].includes(item.latestCheck?.processingStatus)) return '최근 수집 실패';
  if (!item.latestCheck?.checkedAt) return '수집 이력 없음';
  if (item.lastSuccessfulObservation && item.lastSuccessfulObservation.effective?.status !== 'OK') return '환산 불가';
  const disclosureLabels = {PUBLIC:'공개 확인', QUOTE_REQUIRED:'견적 문의 필요', LOGIN_REQUIRED:'로그인 필요'};
  return disclosureLabels[item.latestDisclosure?.status] || '가격 상태 미확인';
}

function renderAgentAnalysis(data) {
  switch (data.kind) {
    case 'PRICE_REMOVAL':
      return '<p>확인 기간 ' + esc(data.dateFrom) + ' ~ ' + esc(data.dateTo) + ' KST</p>'
        + agentTable(['상품 · 모델 · 사업자 · 리전', '계약 유형', '마지막 공개가 수집', '가격표 변경 확인', '공개가 출처', '변경 출처'],
          data.items.map(item => [
            [item.productName, item.modelName, item.providerName, item.regionCode].filter(Boolean).map(esc).join(' · '),
            esc(({ON_DEMAND:'온디맨드', SPOT:'스팟', COMMITMENT:'약정'})[item.contractType] || item.contractType),
            agentTime(item.lastCollectedAt), agentTime(item.checkedAt),
            agentEvidence(item.previousSources), agentEvidence(item.sources)
          ]));
    case 'INTERNAL_PROPOSAL':
      return renderAgentProposal(data);
    case 'PRICE_RANGE':
      return renderAgentPriceRange(data);
    case 'PRICE_MINIMUM':
      return renderAgentPriceRange(data, true);
    case 'CONTRACT_COMPARISON':
      return renderAgentProductPrices(data.items, data.requestedCommitmentMonths);
    case 'TYPICAL_PRICE':
      return renderAgentTypicalPrices(data);
    case 'TREND_TIMING':
      return renderAgentTrendTiming(data);
    case 'TREND': {
      const period = '<p>' + esc(data.dateFrom) + ' ~ ' + esc(data.dateTo) + ' KST · '
        + esc(agentContractType(data.contractType)) + ' · USD/GPU·시간</p>';
      const series = data.series || [];
      const graph = series.length
        ? '<p>집계 오퍼 ' + esc(series.length) + '개 · 차트 ' + esc(Math.min(series.length, agentTrendChartLimit)) + '개</p>'
          + '<div class="chartbox" style="height:240px"><canvas data-agent-chart aria-label="가격 관측 시계열"></canvas></div>' : '';
      const limited = data.nextCursor ? '<p>일부 오퍼만 집계했습니다. 사업자나 오퍼 ID로 범위를 좁힐 수 있습니다.</p>' : '';
      return period + graph
        + agentTrendObservations(data)
        + agentTrendChanges(data.changes, data.sources) + limited;
    }
    case 'WEEK_OVER_WEEK':
    case 'MONTH_OVER_MONTH':
      return renderAgentPeriodTrend(data);
    case 'DISCOUNT':
      return agentTable(['사업자','기간','온디맨드 단가','약정 단가','차액','할인율','근거'], data.items.map(item => [
        esc(item.providerName), esc(item.months) + '개월', esc(item.baseline?.effective?.amount ?? '미확보'),
        esc(item.commitment.priceUsdGpuHour), esc(item.difference ?? '계산 불가'),
        item.percent == null ? esc(({AMBIGUOUS_BASE_PRICE:'기준가가 여러 건', CONDITIONS_NOT_COMPARABLE:'조건 불일치',
          BASE_PRICE_MISSING:'기준가 미확보', BASE_PRICE_UNAVAILABLE:'기준가 또는 근거 미확보'})[item.reason] || '계산 불가') : esc(item.percent) + '%',
        agentEvidence(item.baseline?.evidence) + ' · ' + agentEvidence(item.commitment.sources)
      ]));
    case 'AVAILABILITY':
      return agentTable(['수집 대상','가격 상태','최근 점검','마지막 정상 관측','근거'], data.items.map(item => [
        esc(item.scopeDescription || item.targetId), esc(agentAnalysisStatus(item)), agentTime(item.latestCheck?.checkedAt),
        item.lastSuccessfulObservation?.collectedAt ? agentTime(item.lastSuccessfulObservation.collectedAt)
          + ' · ' + esc(item.lastSuccessfulObservation.effective?.amount ?? '단가 미확보') : '관측 없음',
        agentEvidence([...(item.latestDisclosure?.evidence || []), ...(item.evidence || [])])
      ])) + (data.limited ? '<p>수집 대상 50건을 표시했습니다. 사업자나 리전을 지정해 범위를 좁힐 수 있어요.</p>' : '');
    case 'FORECAST': {
      const options = agentTable(['오퍼 ID','사업자','리전'], (data.offers || []).map(row =>
        [esc(row.offerId), esc(row.providerName), esc(row.regionCode)]));
      const coverage = data.availableMonths == null ? '' : '<p>관측 월 ' + esc(data.availableMonths)
        + '개 · 연속 월 ' + esc(data.consecutiveMonths || 0) + '개 · 필요 ' + esc(data.requiredMonths) + '개</p>';
      const series = (data.monthly || []).map(row => [esc(row.month), esc(row.p10), esc(row.p50), esc(row.p90)]);
      const backtest = data.backtest?.horizons || [];
      return '<p>기준 시각 ' + agentTime(data.asOf) + '</p>' + coverage + options
        + agentTable(['예상 월','P10','중앙값','P90'], series)
        + (data.assumptions || []).map(line => '<p>' + esc(line) + '</p>').join('')
        + (data.modelVersion ? '<p>' + esc(data.modelVersion) + ' · 반복 ' + esc(data.paths) + '회 · 시드 ' + esc(data.seed) + '</p>' : '')
        + (data.status === 'INSUFFICIENT_HISTORY' ? '<p><a href="#dashboard">공개가격 대시보드</a> · <a href="#trend">과거 추이</a></p>' : '')
        + (data.historyFrom ? '<p>이력 범위 ' + esc(data.historyFrom) + ' ~ ' + esc(data.historyTo) + '</p>' : '')
        + agentTable(['검증 기간','사례 수','MAE','가격 유지 기준 MAE'], backtest.map(row => [
          esc(row.horizonMonths) + '개월', esc(row.count), esc(row.mae), esc(row.baselineMae)]))
        + (data.backtest && !backtest.some(row => row.horizonMonths === 12) ? '<p>12개월 검증에 필요한 후속 실관측이 부족합니다.</p>' : '')
        + (data.sources?.length ? '<details><summary>가격 이력 출처</summary>' + agentEvidence(data.sources) + '</details>' : '');
    }
    case 'INTERNAL_ANALYSIS': {
      const recommendation = window.GpuDecisionRecommendation?.();
      if (!recommendation) return '<p><a href="#cost">원가·제안가</a> · <a href="#decision">가동률·판매가 검토</a> · <a href="#bizcase">IRR</a></p>';
      return '<p>' + esc(recommendation.method) + '</p>' + agentTable(
        ['시나리오','가동률','목표 IRR 필요가','검토 구간','판정'],
        recommendation.scenarios.map(row => [esc(row.scenario), esc(Number(row.utilization) * 100) + '%',
          esc(row.minimumForTarget ?? '계산 불가'), row.lowerBound == null ? '범위 미확보'
            : esc(row.lowerBound) + ' ~ ' + esc(row.upperBound), esc(row.reason || row.status)]))
        + '<p><a href="#decision">입력 가정과 계산 결과 확인</a></p>';
    }
    default: return '';
  }
}

function renderAgentPriceRange(data, showProviders = false) {
  if (!data.items.length) return '';
  const labels = {MINIMUM:'최저가', MAXIMUM:'최고가', BOTH:'최저가·최고가'};
  const rows = data.items.map(item => {
    const details = item.matches.map(match => {
      const conditions = [match.providerName, match.productName, match.regionCode,
        match.gpuCount == null ? null : match.gpuCount + ' GPU',
        marketPayment[match.paymentOption]].filter(value => value && value !== '해당 없음');
      return '<b>' + conditions.map(esc).join(' · ') + '</b><br><small>수집 '
        + agentTime(match.collectedAt) + ' · ' + agentEvidence(match.sources) + '</small>';
    });
    const providers = [...new Set(item.matches.map(match => match.providerName).filter(Boolean))];
    const providerNames = showProviders && providers.length ? '<b>' + providers.map(esc).join(' · ') + '</b>' : '';
    const collectionTimes = [...new Set(item.matches.map(match => match.collectedAt).filter(Boolean))];
    const collectionTime = showProviders && collectionTimes.length === 1
      ? '<br><small>수집 ' + agentTime(collectionTimes[0]) + '</small>' : '';
    const evidence = details.length === 1 ? details[0]
      : providerNames + collectionTime + '<details><summary>' + esc(details.length) + '건의 상품 조건과 출처</summary><ul>'
        + details.map(detail => '<li>' + detail + '</li>').join('') + '</ul></details>';
    return [labels[item.endpoint], '<b>' + agentUsd.format(item.priceUsdGpuHour) + '</b>', evidence];
  });
  return '<div class="agent-price-range">'
    + agentTable(['구분', '단가 (USD/GPU·시간)', '상품 조건과 출처'], rows)
    + '<p><small>사업자 ' + esc(data.providerCount) + '곳 · 상품 ' + esc(data.productCount)
    + '개 · 가격 항목 ' + esc(data.offerCount) + '건의 마지막 공개 관측을 집계했습니다. 리전과 GPU 구성이 다를 수 있습니다.</small></p></div>';
}

function renderAgentProposal(data) {
  const model = esc(data.modelName);
  const costLink = '<p><a href="#cost/' + encodeURIComponent(data.modelId) + '">' + model + ' 원가·후보가격 계산</a></p>';
  const cost = V02.cost;
  if (!cost || String(cost.modelId) !== String(data.modelId)) {
    return '<p><b>산정 근거 부족</b> · 이 화면 세션에 ' + model + '의 원가·후보가격 계산 결과가 없습니다. '
      + '내부 계산 화면에서 투자·운영비, 가동률과 목표 마진을 입력해 주세요.</p>' + costLink;
  }
  if (cost.incomplete || !Number.isFinite(cost.candidate) || cost.candidate <= 0) {
    return '<p><b>산정 근거 부족</b> · ' + model + '의 내부 계산에서 누락 항목과 후보가격을 확인해 주세요.</p>'
      + (cost.missing?.length ? '<p>미반영 항목 · ' + cost.missing.map(esc).join(', ') + '</p>' : '') + costLink;
  }
  const formula = V02.costInput?.marginType === 'markup' ? '시간당 원가 × (1 + 목표 가산율)' : '시간당 원가 ÷ (1 − 목표 이익률)';
  return '<p><b>내부 가정에 따른 후보가 ' + agentUsd.format(cost.candidate) + ' / GPU·시간</b></p>'
    + '<p>기간 총원가를 판매 가능한 GPU 시간으로 나눈 뒤, ' + formula + ' 방식으로 후보가를 계산했습니다. '
    + '최종 제안 전 동일 조건 시장가격과 목표 IRR을 확인하세요.</p>'
    + '<p><a href="#decision">가동률·IRR과 가격 검토</a></p>';
}

function agentContractType(contract) {
  return ({ON_DEMAND:'온디맨드', SPOT:'스팟', COMMITMENT:'약정'})[contract] || contract || '계약 유형 미확인';
}

function agentObservationPeriod(data) {
  return '<p>' + esc(data.dateFrom) + ' ~ ' + esc(data.dateTo) + ' KST · USD/GPU·시간</p>';
}

function renderAgentTypicalPrices(data) {
  const rows = data.items.map(item => {
    const scope = [agentContractType(item.contractType), item.regionCode,
      item.gpuCount == null ? null : item.gpuCount + ' GPU', '오퍼 ' + item.offerId];
    return [
      '<b>' + esc(item.providerName) + '</b><br><small>' + scope.filter(Boolean).map(esc).join(' · ') + '</small>',
      '<b>' + agentUsd.format(item.medianUsdGpuHour) + '</b>',
      esc(item.observedDays) + ' / ' + esc(data.periodDays) + '일'
    ];
  });
  const headings = ['사업자 · 오퍼', '일별 마지막 단가의 중앙값', '관측 일수'];
  return agentObservationPeriod(data)
    + '<div class="agent-typical-prices">' + agentTable(headings, rows.slice(0, 20))
    + (rows.length > 20 ? '<details><summary>나머지 ' + esc(rows.length - 20) + '개 오퍼</summary>'
      + agentTable(headings, rows.slice(20)) + '</details>' : '') + '</div>'
    + (rows.length ? '<p><small>표의 일수는 비교 가능한 단가가 확인된 날입니다. 관측하지 못한 날은 중앙값에 반영되지 않습니다.</small></p>' : '')
    + (data.limited ? '<p>일부 오퍼만 집계했습니다. 사업자나 오퍼 ID로 범위를 좁힐 수 있습니다.</p>' : '')
    + (rows.length ? '<details><summary>일별 마지막 단가와 출처</summary>'
      + data.items.map(item => '<details><summary>' + esc(item.providerName) + ' · '
        + esc(agentContractType(item.contractType)) + ' · 오퍼 ' + esc(item.offerId) + '</summary>'
        + agentTable(['수집 시각', '단가', '출처'], item.daily.slice(-60).map(point => [
          agentTime(point.collectedAt), agentUsd.format(point.effectiveUsdGpuHour),
          agentEvidence(GpuSpringAnalysis.sourcesForResult(point, data.sources))
        ])) + (item.daily.length > 60 ? '<p>최근 관측일 60일을 표시했습니다.</p>' : '') + '</details>').join('')
      + '</details>' : '');
}

function renderAgentTrendTiming(data) {
  return agentObservationPeriod(data) + data.items.map((section, index) => {
    const series = section.series.filter(offer => offer.points.some(point => point.effectiveUsdGpuHour != null));
    const plotted = Math.min(series.length, agentTrendChartLimit);
    return '<section class="agent-timing-section" data-agent-timing-index="' + index + '"><h4>'
      + esc(agentContractType(section.contractType)) + '</h4>'
      + (plotted ? '<p><small>차트 ' + plotted + ' / ' + esc(series.length)
        + '개 오퍼 · 점은 실제 관측 시각입니다.</small></p>'
        + '<div class="chartbox" style="height:240px"><canvas data-agent-chart aria-label="관측 시각별 공개 가격 선그래프"></canvas></div>' : '')
      + (section.firstChanges.length ? '<p><b>오퍼별 최초 상승·하락 관측</b></p>'
        + agentTrendChanges(section.firstChanges, section.sources)
        : '<p>' + (section.comparablePairs ? '비교 가능한 연속 관측에서 변동이 확인되지 않았습니다.'
          : '비교 가능한 연속 관측이 부족합니다.') + '</p>')
      + agentTrendObservations(section)
      + (section.nextCursor ? '<p>일부 오퍼만 집계했습니다.</p>' : '') + '</section>';
  }).join('') + '<p><small>관측이 비어 있는 긴 구간은 선을 끊었습니다. 표의 시각은 단가가 달라진 것을 확인한 관측 시각입니다.</small></p>';
}

function agentTrendChanges(changes, sources) {
  const rows = changes.slice(0, 100).map(change => [
    [change.providerName, change.regionCode, '오퍼 ' + change.offerId].filter(Boolean).map(esc).join(' · '),
    agentTime(change.before.collectedAt), esc(change.before.effectiveUsdGpuHour),
    agentTime(change.after.collectedAt), esc(change.after.effectiveUsdGpuHour),
    esc(change.difference), change.changePercent == null ? '계산 불가' : esc(change.changePercent) + '%',
    agentEvidence(GpuSpringAnalysis.sourcesForResult(change.before, sources)) + ' → '
      + agentEvidence(GpuSpringAnalysis.sourcesForResult(change.after, sources))
  ]);
  return agentTable(['사업자 · 오퍼','앞선 관측','이전 단가','변경 확인','변경 단가','변동액','변동률','관측 출처'], rows)
    + (changes.length > 100 ? '<p>변경 내역은 처음 100건을 표시했습니다.</p>' : '');
}

function agentPeriodPriceTable(entries, beforeLabel, afterLabel) {
  const mobileLabel = label => '<span class="agent-period-mobile-label" aria-hidden="true">' + esc(label) + '</span>';
  const rows = entries.map(({ change, contractType }) => {
    const conditions = [agentContractType(contractType), change.regionCode,
      change.gpuCount == null ? null : change.gpuCount + ' GPU', '오퍼 ' + change.offerId];
    const delta = Number(change.difference);
    const percent = change.changePercent == null ? '변동률 계산 불가'
      : (delta > 0 ? '+' : '') + esc(change.changePercent) + '%';
    return [
      '<b>' + esc(change.providerName) + '</b><br><small>' + conditions.filter(Boolean).map(esc).join(' · ') + '</small>',
      mobileLabel(beforeLabel) + agentUsd.format(change.before.effectiveUsdGpuHour),
      mobileLabel(afterLabel) + agentUsd.format(change.after.effectiveUsdGpuHour),
      mobileLabel('차액 · 변동률') + '<b>' + (delta > 0 ? '+' : '') + agentUsd.format(delta)
        + '</b><br><small>' + percent + '</small>'
    ];
  });
  return '<div class="agent-period-prices">'
    + agentTable(['사업자 · 오퍼', beforeLabel, afterLabel, '차액 · 변동률'], rows) + '</div>';
}

function agentPeriodChangeChart(comparisons, previous) {
  const changes = comparisons
    .filter(({ change }) => change.changePercent != null)
    .map(entry => ({ ...entry, percent: Number(entry.change.changePercent) }))
    .filter(({ percent }) => Number.isFinite(percent) && percent !== 0)
    .sort((a, b) => Math.abs(b.percent) - Math.abs(a.percent));
  if (!changes.length) return '';

  const displayed = changes.slice(0, 8);
  const maxPercent = Math.abs(displayed[0].percent);
  const rows = displayed.map(({ change, contractType, percent }) => {
    const conditions = [agentContractType(contractType), change.regionCode,
      change.gpuCount == null ? null : change.gpuCount + ' GPU',
      '오퍼 ' + change.offerId].filter(Boolean).map(esc).join(' · ');
    const direction = percent > 0 ? 'increase' : 'decrease';
    const width = 50 * Math.abs(percent) / maxPercent;
    return '<li class="agent-period-change-row">'
      + '<span class="agent-period-change-offer"><b>' + esc(change.providerName) + '</b><small>' + conditions + '</small></span>'
      + '<span class="agent-period-change-track" aria-hidden="true"><span class="agent-period-change-bar ' + direction
      + '" style="--bar-width:' + width.toFixed(4) + '%"></span></span>'
      + '<b class="agent-period-change-value">' + (percent > 0 ? '+' : '') + esc(percent) + '%</b></li>';
  }).join('');

  const subset = changes.length > displayed.length
    ? '<p><small>변동률의 절댓값이 큰 ' + displayed.length + '개를 표시합니다.</small></p>' : '';
  return '<figure class="agent-period-change-chart"><figcaption><b>' + previous + ' 대비 오퍼별 가격 변동률</b></figcaption>'
    + '<p><small>← 하락 · 0% 기준 · 상승 →</small></p><ul>' + rows + '</ul>' + subset + '</figure>';
}

function agentPeriodPriceOverview(data, previous, current) {
  const entriesFor = field => data.items.flatMap(section => (section[field] || [])
    .map(change => ({change, contractType:section.contractType})));
  const largestFirst = (a, b) => Math.abs(Number(b.change.difference)) - Math.abs(Number(a.change.difference));
  const comparisons = data.items.flatMap(section => (section.priceComparisons || section.weeklyChanges)
    .map(change => ({change, contractType:section.contractType}))).sort(largestFirst);
  if (comparisons.length) {
    return agentPeriodChangeChart(comparisons, previous)
      + '<h4>오퍼별 마지막 공개가격 비교</h4>'
      + agentPeriodPriceTable(comparisons.slice(0, 20), previous + ' 단가', current + ' 단가')
      + (comparisons.length > 20 ? '<details><summary>나머지 ' + esc(comparisons.length - 20) + '개 오퍼</summary>'
        + agentPeriodPriceTable(comparisons.slice(20), previous + ' 단가', current + ' 단가') + '</details>' : '');
  }
  const currentChanges = entriesFor('currentChanges');
  const changes = currentChanges.length ? currentChanges : entriesFor('previousChanges');
  if (!changes.length) return '';
  const period = currentChanges.length ? current : previous;
  return '<h4>' + period + ' 내 가격 변동 · 참고</h4>'
    + '<p>같은 오퍼의 연속 관측에서 차액이 큰 순으로 최대 5건을 표시합니다.</p>'
    + agentPeriodPriceTable(changes.sort(largestFirst).slice(0, 5), '변동 전 단가', '변동 후 단가');
}

function renderAgentPeriodTrend(data) {
  const monthly = data.kind === 'MONTH_OVER_MONTH';
  const previous = monthly ? '지난달' : '지난주';
  const current = monthly ? '이번 달' : '이번 주';
  const rows = data.items.map(section => [
    esc(agentContractType(section.contractType)), esc(section.previousOffers) + '개',
    esc(section.currentOffers) + '개', section.comparablePairs
      ? '비교 ' + esc(section.comparablePairs) + '개 · 변동 ' + esc(section.weeklyChanges.length) + '건' : '계산 불가',
    esc(section.previousChanges.length) + '건',
    esc(section.currentChanges?.length || 0) + '건' + (section.limited ? ' · 일부 집계' : '')
  ]);
  return '<p>' + previous + ' ' + esc(data.previousFrom) + ' ~ ' + esc(data.previousTo) + ' · ' + current + ' '
    + esc(data.currentFrom) + ' ~ ' + esc(data.currentTo) + ' KST · USD/GPU·시간</p>'
    + agentPeriodPriceOverview(data, previous, current)
    + '<h4>관측·변동 건수</h4>'
    + agentTable(['계약 유형', previous + ' 관측 오퍼', current + ' 관측 오퍼', previous + ' 대비',
      previous + ' 내 변동', current + ' 내 변동'], rows)
    + (monthly ? '<p><small>가격은 관측일 기준입니다. 나중에 확보한 공식 출처도 조회 시점까지 검증되면 포함됩니다.</small></p>' : '')
    + data.items.map(section => {
      const name = esc(agentContractType(section.contractType));
      const changes = (label, list) => list?.length ? '<details><summary>' + name + ' ' + label + ' '
        + esc(list.length) + '건</summary>' + agentTrendChanges(list, section.sources) + '</details>' : '';
      return changes(previous + ' 대비 관측 시각과 출처', section.priceComparisons || section.weeklyChanges)
        + changes(previous + ' 내 변동과 출처', section.previousChanges)
        + changes(current + ' 내 변동과 출처', section.currentChanges);
    }).join('');
}

function agentTrendObservations(data) {
  const items = (data.series || []).flatMap(series => series.points.map(point => ({
    name: series.providerName, offerId: series.offerId, point
  }))).sort((left, right) => Date.parse(right.point.collectedAt) - Date.parse(left.point.collectedAt));
  if (!items.length) return '';
  return '<details><summary>관측 시각과 출처 · 최근 최대 100건</summary>'
    + agentTable(['사업자·오퍼','관측 시각','관측 가격','출처'], items.slice(0, 100).map(item => [
      esc(item.name) + ' · ' + esc(item.offerId), agentTime(item.point.collectedAt),
      esc(item.point.effectiveUsdGpuHour ?? '환산 불가'),
      agentEvidence(GpuSpringAnalysis.sourcesForResult(item.point, data.sources))])) + '</details>';
}

const agentTrendChartLimit = 8;

function paintAgentAnalysis(reply, data) {
  if (data.kind === 'TREND_TIMING') {
    reply.querySelectorAll('[data-agent-timing-index]').forEach(section =>
      paintAgentAnalysis(section, data.items[Number(section.dataset.agentTimingIndex)]));
    return;
  }
  const canvas = reply.querySelector('[data-agent-chart]');
  if (!canvas || !window.Chart || data.kind !== 'TREND') return;
  const colors = ['#2164a5', '#8154a8', '#1d7562', '#b05e20', '#9f3759'];
  const changed = new Set(data.changes.map(change => change.offerId));
  const chartSeries = data.series.filter(series => series.points.some(point => point.effectiveUsdGpuHour != null))
    .sort((a, b) => Number(changed.has(b.offerId)) - Number(changed.has(a.offerId)))
    .slice(0, agentTrendChartLimit);
  const datasets = chartSeries.map((series, index) => {
    let last = null;
    const points = [];
    for (const point of series.points) {
      const time = Date.parse(point.collectedAt);
      if (!Number.isFinite(time)) continue;
      if (last != null && time - last > 48 * 3600 * 1000) points.push({x:(last + time) / 2, y:null});
      points.push({x:time, y:point.effectiveUsdGpuHour == null ? null : Number(point.effectiveUsdGpuHour)});
      last = time;
    }
    return { label:series.providerName + ' · ' + (series.regionCode || '') + ' · ' + series.offerId,
      data:points, borderColor:colors[index % colors.length], pointRadius:2, spanGaps:false };
  });
  new Chart(canvas, { type:'line', data:{datasets}, options:{animation:false, parsing:false, maintainAspectRatio:false,
    plugins:{tooltip:{callbacks:{title:items => items.length ? agentTime(new Date(items[0].parsed.x).toISOString()) : ''}}},
    scales:{x:{type:'linear',ticks:{callback:value => new Date(value).toLocaleDateString('ko-KR',{timeZone:'Asia/Seoul'})}}}} });
}
