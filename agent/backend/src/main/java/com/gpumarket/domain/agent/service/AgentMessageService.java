package com.gpumarket.domain.agent.service;

import com.gpumarket.domain.agent.dto.AgentMessageRequestDto;
import com.gpumarket.domain.agent.dto.AgentMessageResponseDto;
import com.gpumarket.domain.agent.dto.AgentMessageResponseDto.AgentResultDto;
import com.gpumarket.domain.agent.dto.AgentMessageResponseDto.SampleCountsDto;
import com.gpumarket.domain.agent.dto.PublicContextDto;
import com.gpumarket.domain.agent.service.AgentQueryScope.Kind;
import com.gpumarket.domain.agent.service.AgentSessionStore.Conditions;
import com.gpumarket.domain.catalog.dto.CatalogResponseDto.GpuModelDto;
import com.gpumarket.domain.catalog.dto.CatalogResponseDto.ProviderDto;
import com.gpumarket.domain.catalog.service.CatalogService;
import com.gpumarket.domain.market.dto.PublicPriceLookupDto;
import com.gpumarket.domain.market.dto.PublicPriceLookupDto.PublicPriceDto;
import com.gpumarket.domain.market.service.MarketForecastService;
import com.gpumarket.domain.market.service.MarketOfferService;
import java.text.Normalizer;
import java.time.DateTimeException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.regex.MatchResult;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import org.springframework.stereotype.Service;

@Service
public class AgentMessageService {
  private static final String DISCLAIMER = "수집한 공개 가격이며 실제 거래가와 다를 수 있습니다.";
  private static final String COMPARISON_DISCLAIMER =
      "공개가격 기준이며 협상가·대형 고객 할인을 반영하지 않습니다. " + "3~5년 약정은 공개 자료가 제한적일 수 있습니다.";
  private static final Pattern SENSITIVE =
      Pattern.compile(
          "원가|구매가|매입가|마진|비밀번호|비공개|내부\\s*계약|협상가|비밀키|api["
              + " _-]*key|password|secret|sk-[a-z0-9_-]{8,}|\\birr\\b"
              + "|\\b(?:internal|confidential)\\s+(?:(?:purchase|contract)\\s+)?(?:costs?|prices?|rates?)\\b|\\b(?:purchase"
              + " price|profit margins?)\\b");
  private static final Pattern PRICE = Pattern.compile("가격|단가|시세|\\bprices?\\b|\\brates?\\b");
  private static final Pattern ANALYSIS_HELP =
      Pattern.compile(
          "(?:제안가|목표\\s*irr|irr|판매가\\s*추천\\s*범위|추천\\s*범위|가동률)\\s*(?:계산\\s*)?(?:근거|결과|가정)?\\s*(?:설명해줘|알려줘|보여줘)[.?]?");
  private static final Pattern COMPARISON =
      Pattern.compile("비교|가격\\s*범위|중앙값|최저|최고|\\b(?:compar\\w+|median|cheapest|lowest|highest)\\b");
  private static final Pattern COMPARISON_FOLLOW_UP = Pattern.compile("그\\s*중|같은\\s*조건|이번에는|대신");
  private static final String CALENDAR_DATE =
      "(?<![0-9./-])(?:([0-9]{4})\\s*(?:[./-]|년)\\s*)?"
          + "([0-9]{1,2})\\s*(?:[./-]|월)\\s*([0-9]{1,2})\\s*(?:일\\s*)?";
  private static final Pattern OBSERVED_BEFORE =
      Pattern.compile(CALENDAR_DATE + "(?:이전|전)(?:으로|에|의|까지)?(?=\\s|[.!?,]|$)");
  private static final Pattern OBSERVED_ON =
      Pattern.compile(CALENDAR_DATE + "(?:당일|에|자)?(?=\\s|[.!?,]|$)(?!\\s*(?:이전|전|부터|이후|까지|후|뒤|~))");
  private static final Pattern DATE_RANGE = Pattern.compile("부터|이후|사이");
  private static final Pattern CURRENT_PRICE = Pattern.compile("현재|지금|최신");
  private static final Pattern OTHER_INTENT =
      Pattern.compile(
          "예측|전망|추이|이력|변동|원인|할인|평균|내년|내일|내달|작년"
              + "|지난|다음\\s*(?:주|달|월|해)|어제|그제|(?:시간|일|주|개월|년)\\s*(?:후|뒤|전)"
              + "|\\b(?:forecast|history|historical|trend|predict|average"
              + "|tomorrow|yesterday|future|discounts?)\\b"
              + "|\\b(?:last|next)\\s+(?:week|month|year)\\b"
              + "|(?<![a-z0-9])20[0-9]{2}(?:[-/.][0-9]{1,2}|\\s*년)");
  private static final Pattern ON_DEMAND = Pattern.compile("온\\s*디맨드|온\\s*디멘드|on[ _-]*demand");
  private static final Pattern SPOT = Pattern.compile("스팟|\\bspot\\b");
  private static final Pattern COMMITMENT =
      Pattern.compile("약정|장기\\s*계약|\\bcommitment\\b|\\breserved\\b");
  private static final Pattern PAIRED_YEARS =
      Pattern.compile("(?<![0-9./-])([0-9]{1,3})\\s*/\\s*([0-9]{1,3})\\s*년");
  private static final Pattern COMMITMENT_CONVERSION_BASELINE =
      Pattern.compile(
          "(?:온\\s*디[맨멘]드|on[ _-]*demand)\\s*(?:가격|단가)을?\\s*"
              + "(?=[0-9][0-9\\s/년개월과와,]*\\s*(?:장기\\s*계약|약정)\\s*(?:으로|로)\\s*환산)");
  private static final Pattern COMMITMENT_TERM =
      Pattern.compile("(?<![0-9])([0-9]{1,3})\\s*(년|개월)(?=\\s|약정|장기|예약|으로|만|요|과|와|[.!?,·ㆍ/]|$)");
  private static final Map<String, Pattern> PAYMENT_OPTIONS =
      Map.of(
          "ALL_UPFRONT", Pattern.compile("전액\\s*선납"),
          "PARTIAL_UPFRONT", Pattern.compile("일부\\s*선납"),
          "NO_UPFRONT", Pattern.compile("무선납|선납\\s*(?:없음|없이)"));
  private static final Pattern GPU_TOKEN =
      Pattern.compile("(?<![a-z0-9])(?:gb|vr|[habvl])[0-9]{2,4}(?![a-z0-9])");
  private static final Pattern REGION_CODE = Pattern.compile("\\b[a-z]{2}(?:-[a-z]+)+-[0-9]+\\b");
  private static final Pattern AMBIGUOUS_REGION = Pattern.compile("서울|오리건|도쿄|버지니아");
  private static final Pattern NON_USD =
      Pattern.compile(
          "원화|원으로|유로|엔화|엔으로|위안|파운드|[₩€¥£]"
              + "|\\b(?:krw|eur|jpy|cny|gbp|cad|aud|euros?|yen|yuan|won|pounds?)\\b");
  private static final Pattern PER_GPU = Pattern.compile("\\bper\\s+1\\s+(?=gpus?\\b)");
  private static final Map<String, Pattern> SYSTEMS =
      Map.of(
          "INSTANCE", Pattern.compile("인스턴스|\\binstances?\\b"),
          "NODE", Pattern.compile("노드|\\bnodes?\\b"),
          "RACK_SCALE", Pattern.compile("랙|\\brack\\b"));
  private static final Map<String, Pattern> PROVIDER_TYPES =
      Map.of(
          "GENERAL_CLOUD", Pattern.compile("종합\\s*(?:클라우드|사업자)|\\bgeneral\\s+cloud\\b"),
          "GPU_SPECIALIST",
              Pattern.compile("(?:gpu\\s*)?(?:특화|전문)\\s*(?:클라우드|사업자)|\\bgpu\\s+specialists?\\b"));
  private static final Pattern FOLLOW_UP_FILLER =
      Pattern.compile(
          "\\s+|[!?,.…]|그럼|그러면|그\\s*중|이번에는|이번엔|대신|이\\s*조건으로|"
              + "같은\\s*조건으로|에서도|에서|으로|까지|쪽|[은는이가만에도로요]"
              + "|\\b(?:what about|how about|instead|only|and|in|for)\\b");
  private static final Pattern GPU_COUNT =
      Pattern.compile("(?<![a-z0-9])([0-9]{1,4})\\s*(?:장(?!\\s*당)|gpus?\\b)");
  private static final Map<String, Pattern> COUNTRY_ALIASES =
      Map.of(
          "KR", Pattern.compile("한국|국내|대한민국|\\bkorea\\b"),
          "US", Pattern.compile("미국|\\busa\\b|\\bu\\.s\\b"),
          "GB", Pattern.compile("영국|\\buk\\b"));
  private static final Map<String, Pattern> COUNTRIES =
      Arrays.stream(Locale.getISOCountries())
          .collect(Collectors.toUnmodifiableMap(code -> code, AgentMessageService::countryPattern));
  private static final PublicContextDto EMPTY =
      new PublicContextDto(null, null, null, null, null, null, null, null, null);
  private static final Conditions EMPTY_CONDITIONS =
      new Conditions(EMPTY, null, false, false, null, AgentQueryScope.price(), List.of());
  private static final ZoneId KST = ZoneId.of("Asia/Seoul");
  private static final DateTimeFormatter OBSERVED_AT =
      DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm 'KST'").withZone(KST);

  private final CatalogService catalog;
  private final MarketOfferService offers;
  private final AgentSessionStore sessions;
  private final AgentMarketTools tools;

  public AgentMessageService(
      CatalogService catalog,
      MarketOfferService offers,
      AgentSessionStore sessions,
      AgentMarketTools tools) {
    this.catalog = catalog;
    this.offers = offers;
    this.sessions = sessions;
    this.tools = tools;
  }

  public AgentMessageResponseDto answer(AgentMessageRequestDto request) {
    String original = Normalizer.normalize(request.message(), Normalizer.Form.NFKC);
    String question = original.toLowerCase(Locale.ROOT);
    if (ANALYSIS_HELP.matcher(question.strip()).matches()) {
      var analysis =
          Map.<String, Object>of(
              "kind",
              "INTERNAL_ANALYSIS",
              "asOf",
              Instant.now(),
              "items",
              List.of(Map.of("internal", true)));
      String id = request.sessionId();
      if (id == null) id = sessions.create(EMPTY_CONDITIONS);
      else sessions.require(id);
      return analysisReply(EMPTY, analysis).withSession(id, List.of(), List.of());
    }
    if (SENSITIVE.matcher(question).find()) {
      throw new SensitiveInput();
    }
    String sessionId = request.sessionId();
    if (sessionId == null) {
      Lookup lookup = lookup(request, EMPTY_CONDITIONS, original, question);
      String id = sessions.create(firstValue(lookup.conditions(), EMPTY_CONDITIONS));
      return lookup.response().withSession(id, List.of(), List.of());
    }

    var session = sessions.require(sessionId);
    synchronized (session) {
      Conditions previous = sessions.require(sessionId).conditions;
      Lookup lookup = lookup(request, previous, original, question);
      Conditions current = lookup.conditions();
      if (current == null) {
        sessions.complete(sessionId, previous);
        return lookup.response().withSession(sessionId, List.of(), List.of());
      }
      sessions.complete(sessionId, current);
      return followUpResponse(sessionId, lookup.response(), previous, current);
    }
  }

  public void deleteSession(String sessionId) {
    sessions.delete(sessionId);
  }

  private Lookup lookup(
      AgentMessageRequestDto request, Conditions previous, String original, String question) {
    var supplied = request.publicContext() == null ? EMPTY : request.publicContext();
    AgentQueryScope scope;
    try {
      scope = AgentQueryScope.parse(question, LocalDate.now(KST));
    } catch (IllegalArgumentException error) {
      return clarify(supplied, error.getMessage(), "scope");
    }
    if (scope.kind() == Kind.COST_IMPACT) {
      return costImpactReply(new Conditions(EMPTY, null, false, false, null, scope, List.of()));
    }
    if (scope.kind() == Kind.PRICE
        && previous.scope().kind() == Kind.COST_IMPACT
        && isModelOnlyFollowUp(question)) {
      return costImpactReply(previous);
    }
    if (scope.kind() == Kind.PROPOSAL
        || (scope.kind() == Kind.PRICE
            && previous.scope().kind() == Kind.PROPOSAL
            && isModelOnlyFollowUp(question))) {
      return proposalReply(
          supplied, question, scope.kind() == Kind.PROPOSAL ? scope : previous.scope());
    }
    if (scope.kind() == Kind.PRICE_REMOVAL) {
      return priceRemovalReply(supplied, question, scope);
    }
    if (scope.kind() == Kind.PRICE || scope.kind() == Kind.DISCOUNT) {
      var termText = PAIRED_YEARS.matcher(question).replaceAll("$1년 $2년");
      boolean namesBothContracts =
          ON_DEMAND.matcher(question).find() && COMMITMENT.matcher(question).find();
      boolean comparesByProduct = question.contains("상품별");
      long termCount = COMMITMENT_TERM.matcher(termText).results().limit(2).count();
      if (COMMITMENT_CONVERSION_BASELINE.matcher(question).find()
          || (termCount > 0
              && (comparesByProduct
                  || (namesBothContracts && (scope.kind() == Kind.PRICE || termCount == 2))))) {
        scope = new AgentQueryScope(Kind.CONTRACT_COMPARISON, null, null, scope.offerId());
      }
    }
    String strippedQuestion = scope.withoutIntent(question);
    String strippedOriginal = scope.withoutIntent(original);
    return lookupDate(request, previous, strippedOriginal, strippedQuestion, scope);
  }

  private boolean isModelOnlyFollowUp(String question) {
    if (PRICE.matcher(question).find()) return false;
    var models = mentionedModels(question, catalog.getCatalog().models());
    if (models.size() != 1) return false;
    var remainder = removeName(question, models.get(0).name());
    return FOLLOW_UP_FILLER.matcher(remainder.replace("기준", "")).replaceAll("").isEmpty();
  }

  private static Lookup costImpactReply(Conditions conditions) {
    var analysis =
        Map.<String, Object>of(
            "kind", "COST_IMPACT", "status", "INSUFFICIENT_EVIDENCE", "items", List.of());
    return new Lookup(analysisReply(EMPTY, analysis), conditions);
  }

  private Lookup proposalReply(PublicContextDto supplied, String question, AgentQueryScope scope) {
    var available = catalog.getCatalog().models();
    var models = mentionedModels(question, available);
    var conditions = new Conditions(EMPTY, null, false, false, null, scope, List.of());
    if (models.size() > 1
        || (models.size() == 1 && conflict(supplied.modelId(), models.get(0).id()))) {
      return new Lookup(
          clarify(EMPTY, "제안가를 검토할 GPU 모델 한 가지를 알려 주세요.", "modelId").response(), conditions);
    }
    var model =
        models.isEmpty()
            ? available.stream()
                .filter(candidate -> candidate.id().equals(supplied.modelId()))
                .findFirst()
                .orElse(null)
            : models.get(0);
    if (model == null) {
      return new Lookup(
          clarify(EMPTY, "제안가를 검토할 GPU 모델을 알려 주세요.", "modelId").response(), conditions);
    }
    var context = new PublicContextDto(model.id(), null, null, null, null, null, null, null, null);
    var analysis =
        Map.<String, Object>of(
            "kind",
            "INTERNAL_PROPOSAL",
            "modelId",
            model.id(),
            "modelName",
            model.name(),
            "items",
            List.of(model.id()));
    return new Lookup(
        analysisReply(context, analysis, null, model.name()),
        new Conditions(context, null, false, false, null, scope, List.of()));
  }

  private Lookup priceRemovalReply(
      PublicContextDto supplied, String question, AgentQueryScope scope) {
    var available = catalog.getCatalog();
    var models = mentionedModels(question, available.models());
    var conditions = new Conditions(EMPTY, null, false, false, null, scope, List.of());
    if (models.size() > 1 || (models.isEmpty() && GPU_TOKEN.matcher(question).find())) {
      return new Lookup(
          clarify(EMPTY, "공개 여부를 확인할 GPU 모델 한 가지를 확인해 주세요.", "modelId").response(), conditions);
    }
    var filters = new LinkedHashMap<String, String>();
    if (!models.isEmpty()) filters.put("modelId", models.get(0).id());
    else if (supplied.modelId() != null) filters.put("modelId", supplied.modelId());
    var providers =
        available.providers().stream()
            .filter(provider -> mentions(question, provider.name()))
            .map(ProviderDto::id)
            .toList();
    if (!providers.isEmpty()) filters.put("providerIds", String.join(",", providers));
    if (supplied.regionCode() != null) filters.put("regionCode", supplied.regionCode());
    REGION_CODE
        .matcher(question)
        .results()
        .findFirst()
        .ifPresent(match -> filters.put("regionCode", match.group()));
    if (supplied.countryCode() != null) filters.put("countryCode", supplied.countryCode());
    if (supplied.contractType() != null) filters.put("contractType", supplied.contractType());
    var until = scope.to().atStartOfDay(KST).toInstant();
    var items =
        offers.confirmedPriceRemovals(filters, scope.from().atStartOfDay(KST).toInstant(), until);
    var analysis =
        Map.<String, Object>of(
            "kind",
            "PRICE_REMOVAL",
            "items",
            items,
            "asOf",
            until,
            "dateFrom",
            scope.from().toString(),
            "dateTo",
            scope.to().minusDays(1).toString());
    return new Lookup(analysisReply(EMPTY, analysis), conditions);
  }

  private Lookup lookupDate(
      AgentMessageRequestDto request,
      Conditions previous,
      String original,
      String question,
      AgentQueryScope scope) {
    var beforeDates = OBSERVED_BEFORE.matcher(question).results().toList();
    boolean canUseDay =
        scope.kind().supportsExactPriceDate()
            || (scope.kind() == Kind.PRICE
                && previous.scope().kind().supportsExactPriceDate()
                && !PRICE.matcher(question).find());
    var onDates =
        canUseDay
            ? OBSERVED_ON
                .matcher(OBSERVED_BEFORE.matcher(question).replaceAll(" "))
                .results()
                .toList()
            : List.<MatchResult>of();
    var context = request.publicContext() == null ? EMPTY : request.publicContext();
    if (beforeDates.size() + onDates.size() > 1
        || (!beforeDates.isEmpty() && DATE_RANGE.matcher(question).find())) {
      return clarify(context, "조회 기준 날짜를 하나 알려 주세요.", "observedBefore");
    }
    if (beforeDates.isEmpty() && onDates.isEmpty()) {
      return lookup(request, previous, original, question, null, scope);
    }
    boolean exactDay = !onDates.isEmpty();
    var match = exactDay ? onDates.get(0) : beforeDates.get(0);
    LocalDate today = LocalDate.now(KST);
    LocalDate date;
    try {
      int year = match.group(1) == null ? today.getYear() : Integer.parseInt(match.group(1));
      date = LocalDate.of(year, Integer.parseInt(match.group(2)), Integer.parseInt(match.group(3)));
    } catch (DateTimeException error) {
      return clarify(context, "실제 달력 날짜를 알려 주세요. 예를 들어 11/19일 전처럼 입력할 수 있어요.", "observedBefore");
    }
    if (date.isAfter(today) || date.getYear() < 2000 || CURRENT_PRICE.matcher(question).find()) {
      return clarify(context, "현재 가격 또는 조회할 과거 날짜를 한 가지 알려 주세요.", "observedBefore");
    }
    var datePattern = exactDay ? OBSERVED_ON : OBSERVED_BEFORE;
    if (exactDay) {
      var baseScope = scope.kind().supportsExactPriceDate() ? scope : previous.scope();
      scope =
          new AgentQueryScope(
              baseScope.kind(),
              date,
              date.plusDays(1),
              firstValue(scope.offerId(), baseScope.offerId()));
    }
    return lookup(
        request,
        previous,
        datePattern.matcher(original).replaceAll(" "),
        datePattern.matcher(question).replaceAll(" "),
        exactDay ? null : date,
        scope);
  }

  private Lookup lookup(
      AgentMessageRequestDto request,
      Conditions previous,
      String original,
      String question,
      LocalDate explicitBefore,
      AgentQueryScope requestedScope) {
    var context = request.publicContext() == null ? EMPTY : request.publicContext();
    var remembered = previous.publicContext();
    boolean asksCurrent = CURRENT_PRICE.matcher(question).find();
    boolean asksComparison = COMPARISON.matcher(question).find();
    boolean asksPrice =
        PRICE.matcher(question).find() || asksComparison || requestedScope.kind() != Kind.PRICE;
    AgentQueryScope scope =
        requestedScope.kind() == Kind.PRICE
                && !asksCurrent
                && (!asksPrice || COMPARISON_FOLLOW_UP.matcher(question).find())
            ? previous.scope()
            : requestedScope;
    if (requestedScope.offerId() != null && scope.kind() != Kind.PRICE) {
      scope = new AgentQueryScope(scope.kind(), scope.from(), scope.to(), requestedScope.offerId());
    }
    if (scope.from() == null
        && (scope.kind() == Kind.TYPICAL_PRICE || scope.kind() == Kind.TREND_TIMING)) {
      scope = resolveHistoryPeriod(scope, previous.scope(), LocalDate.now(KST));
    }
    if (explicitBefore != null && scope.kind().supportsExactPriceDate() && scope.from() != null) {
      scope = new AgentQueryScope(scope.kind(), null, null, scope.offerId());
    }
    boolean standaloneMinimum =
        requestedScope.kind() == Kind.PRICE_MINIMUM
            && GPU_TOKEN.matcher(question).find()
            && !COMPARISON_FOLLOW_UP.matcher(question).find();
    LocalDate before =
        asksCurrent
                || (standaloneMinimum && explicitBefore == null)
                || scope.kind().defaultsToOnDemandAndSpot()
                || (scope.kind().supportsExactPriceDate() && scope.from() != null)
            ? null
            : firstValue(explicitBefore, previous.observedBefore());
    if ((!asksPrice
            && scope.kind() == Kind.PRICE
            && remembered.contractType() == null
            && !previous.comparison())
        || OTHER_INTENT.matcher(question).find()) {
      return clarify(context, "공개 가격을 확인할 GPU 모델과 조건을 입력해 주세요.", "intent");
    }
    var namedContracts = new ArrayList<String>();
    if (requestedScope.kind() != Kind.CONTRACT_COMPARISON && ON_DEMAND.matcher(question).find()) {
      namedContracts.add("ON_DEMAND");
    }
    if (SPOT.matcher(question).find()) namedContracts.add("SPOT");
    if (COMMITMENT.matcher(question).find()
        || requestedScope.kind() == Kind.CONTRACT_COMPARISON
        || (scope.kind() == Kind.DISCOUNT && namedContracts.isEmpty())) {
      namedContracts.add("COMMITMENT");
    }
    String namedContract = namedContracts.isEmpty() ? null : namedContracts.get(0);
    if (namedContracts.size() > 1
        || (namedContract != null && conflict(context.contractType(), namedContract))) {
      return clarify(context, "조회할 계약 유형 한 가지를 확인해 주세요.", "contractType");
    }
    String selectedContract =
        firstValue(
            namedContract,
            firstValue(
                upper(context.contractType()),
                standaloneMinimum ? null : remembered.contractType()));
    if ("UNKNOWN".equals(selectedContract)) {
      return clarify(context, "온디맨드, 스팟, 약정 중 조회할 계약을 알려 주세요.", "contractType");
    }
    if (scope.kind().defaultsToOnDemandAndSpot()
        && namedContract == null
        && context.contractType() == null
        && previous.assumedContract()) {
      selectedContract = null;
    }
    boolean comparison =
        asksComparison
            || scope.kind().defaultsToOnDemandAndSpot()
            || scope.kind() == Kind.DISCOUNT
            || (previous.comparison()
                && (!asksPrice || COMPARISON_FOLLOW_UP.matcher(question).find()))
            || (selectedContract != null && !"ON_DEMAND".equals(selectedContract));
    var namedRegions =
        REGION_CODE.matcher(question).results().map(match -> match.group()).distinct().toList();
    String namedRegion = namedRegions.isEmpty() ? null : namedRegions.get(0);
    if (namedRegions.size() > 1
        || (namedRegion == null && AMBIGUOUS_REGION.matcher(question).find())
        || (namedRegion != null && conflict(context.regionCode(), namedRegion))) {
      return clarify(context, "조회할 리전 코드 한 가지를 알려 주세요.", "regionCode");
    }

    if (NON_USD.matcher(question).find()) {
      return clarify(context, "현재 달러 기준 GPU당 시간 단가를 조회합니다.", "currency");
    }

    var systems =
        SYSTEMS.entrySet().stream()
            .filter(item -> item.getValue().matcher(question).find())
            .map(Map.Entry::getKey)
            .toList();
    String systemType =
        systems.isEmpty()
            ? firstValue(upper(context.systemType()), remembered.systemType())
            : systems.get(0);
    if (systems.size() > 1 || conflict(context.systemType(), systemType)) {
      return clarify(context, "조회할 상품 구성이 여러 개거나 화면 선택과 다릅니다.", "systemType");
    }

    var available = catalog.getCatalog();
    var namedModels = mentionedModels(question, available.models());
    if (namedModels.size() > 1 || (namedModels.isEmpty() && GPU_TOKEN.matcher(question).find())) {
      return clarify(context, "조회할 GPU 모델 한 가지를 확인해 주세요.", "modelId");
    }

    String modelId =
        namedModels.isEmpty()
            ? firstValue(context.modelId(), remembered.modelId())
            : namedModels.get(0).id();
    if (conflict(context.modelId(), modelId)) {
      return clarify(context, "질문의 GPU 모델과 화면에서 선택한 GPU 모델이 다릅니다.", "modelId");
    }
    var nations =
        COUNTRIES.entrySet().stream()
            .filter(country -> country.getValue().matcher(original).find())
            .map(Map.Entry::getKey)
            .toList();
    String country =
        nations.isEmpty()
            ? firstValue(upper(context.countryCode()), remembered.countryCode())
            : nations.get(0);
    if (nations.size() > 1 || conflict(context.countryCode(), country)) {
      return clarify(context, "조회할 국가가 여러 개거나 화면 선택과 다릅니다.", "countryCode");
    }
    String region = firstValue(namedRegion, context.regionCode());
    if (region == null && Objects.equals(country, remembered.countryCode())) {
      region = remembered.regionCode();
    }

    var count = GPU_COUNT.matcher(PER_GPU.matcher(question).replaceAll("per "));
    boolean mentionsGpuCount = count.find();
    Integer gpuCount = firstValue(context.gpuCount(), remembered.gpuCount());
    if (mentionsGpuCount) {
      int mentioned = Integer.parseInt(count.group(1));
      if (mentioned == 0
          || count.find()
          || (context.gpuCount() != null && context.gpuCount() != mentioned)) {
        return clarify(context, "조회할 상품의 GPU 장수를 확인해 주세요.", "gpuCount");
      }
      gpuCount = mentioned;
    }

    var namedProviders =
        available.providers().stream()
            .filter(provider -> mentions(question, provider.name()))
            .toList();
    if (namedProviders.size() > 1) {
      return clarify(context, "사업자 한 곳을 선택하거나 사업자 이름을 빼고 질문해 주세요.", "providerIds");
    }
    var namedTypes =
        PROVIDER_TYPES.entrySet().stream()
            .filter(item -> item.getValue().matcher(question).find())
            .map(Map.Entry::getKey)
            .toList();
    String requestedType = namedTypes.isEmpty() ? upper(context.providerType()) : namedTypes.get(0);
    if (namedTypes.size() > 1 || conflict(context.providerType(), requestedType)) {
      return clarify(context, "조회할 사업자 유형이 여러 개거나 화면 선택과 다릅니다.", "providerType");
    }

    String type = firstValue(requestedType, remembered.providerType());
    ProviderDto provider =
        namedProviders.isEmpty()
            ? available.providers().stream()
                .filter(item -> item.id().equals(previous.providerId()))
                .findFirst()
                .orElse(null)
            : namedProviders.get(0);
    if (provider != null && !"ALL".equals(type) && conflict(type, provider.type())) {
      if (namedProviders.isEmpty()) {
        provider = null;
      } else if (requestedType == null) {
        type = null;
      } else {
        return clarify(context, "질문의 사업자와 화면에서 선택한 사업자 유형이 다릅니다.", "providerType");
      }
    }

    String providerId = provider == null ? null : provider.id();
    boolean namesContract = namedContract != null;
    boolean assumesContract =
        (!comparison || scope.kind().isPriceExtrema())
            && context.contractType() == null
            && !namesContract
            && (standaloneMinimum
                || remembered.contractType() == null
                || previous.assumedContract());
    boolean defaultsToOnDemand =
        scope.kind().isPriceExtrema() || (scope.kind() == Kind.PRICE && !comparison);
    String contract =
        defaultsToOnDemand ? firstValue(selectedContract, "ON_DEMAND") : selectedContract;
    String termText =
        "COMMITMENT".equals(contract)
            ? PAIRED_YEARS.matcher(question).replaceAll("$1년 $2년")
            : question;
    var namedTerms =
        COMMITMENT_TERM
            .matcher(termText)
            .results()
            .map(match -> Integer.parseInt(match.group(1)) * ("년".equals(match.group(2)) ? 12 : 1))
            .distinct()
            .toList();
    Integer namedTerm = namedTerms.isEmpty() ? null : namedTerms.get(0);
    if (namedTerm != null && !"COMMITMENT".equals(contract)) {
      return clarify(context, "약정 가격인지 미래 가격인지 조회할 기준을 확인해 주세요.", "intent");
    }
    if ((namedTerms.size() > 1 && !"COMMITMENT".equals(contract))
        || namedTerms.stream().anyMatch(term -> term < 1 || term > 120)
        || namedTerms.size() > 6
        || (namedTerm != null
            && context.commitmentMonths() != null
            && !namedTerm.equals(context.commitmentMonths()))) {
      return clarify(context, "조회할 약정 개월 수를 확인해 주세요.", "commitmentMonths");
    }
    Integer months =
        "COMMITMENT".equals(contract)
            ? firstValue(
                namedTerm, firstValue(context.commitmentMonths(), remembered.commitmentMonths()))
            : null;
    var namedPayments =
        PAYMENT_OPTIONS.entrySet().stream()
            .filter(option -> option.getValue().matcher(question).find())
            .map(Map.Entry::getKey)
            .toList();
    String namedPayment = namedPayments.isEmpty() ? null : namedPayments.get(0);
    if (namedPayments.size() > 1
        || (namedPayment != null && conflict(context.paymentOption(), namedPayment))
        || (!"COMMITMENT".equals(contract)
            && (namedPayment != null
                || context.paymentOption() != null
                || context.commitmentMonths() != null))) {
      return clarify(context, "약정의 지불 방식과 계약 유형을 확인해 주세요.", "paymentOption");
    }
    String payment =
        "COMMITMENT".equals(contract)
            ? firstValue(
                namedPayment,
                firstValue(upper(context.paymentOption()), remembered.paymentOption()))
            : null;
    var applied =
        new PublicContextDto(
            modelId, contract, country, systemType, gpuCount, type, region, months, payment);
    var terms =
        !"COMMITMENT".equals(contract)
            ? List.<Integer>of()
            : namedTerms.size() > 1
                ? namedTerms
                : namedTerm == null
                        && context.commitmentMonths() == null
                        && !previous.comparisonTerms().isEmpty()
                    ? previous.comparisonTerms()
                    : months == null ? List.<Integer>of() : List.of(months);
    var conditions =
        new Conditions(applied, providerId, assumesContract, comparison, before, scope, terms);
    if (!asksPrice) {
      boolean namesCondition =
          !namedModels.isEmpty()
              || !nations.isEmpty()
              || !systems.isEmpty()
              || !namedProviders.isEmpty()
              || !namedTypes.isEmpty()
              || namesContract
              || namedRegion != null
              || namedTerm != null
              || namedPayment != null
              || requestedScope.offerId() != null
              || explicitBefore != null
              || (asksCurrent && previous.observedBefore() != null)
              || mentionsGpuCount;
      if ((!namesCondition && conditions.equals(previous))
          || !isConditionFragment(
              PAIRED_YEARS.matcher(original).replaceAll("$1년 $2년"),
              nations,
              namedModels,
              namedProviders)) {
        return clarify(context, "확인할 공개 온디맨드 가격 조건을 입력해 주세요.", "intent");
      }
    }

    var assumptions = new ArrayList<String>();
    if (assumesContract) {
      assumptions.add("계약 유형을 온디맨드로 가정합니다.");
    }
    if (provider != null) {
      assumptions.add("조회 사업자: " + provider.name());
    }
    if (modelId == null) {
      // Retain the confirmed public filters until the user names the missing model.
      var clarification = clarify(applied, "조회할 GPU 모델을 알려 주세요.", "modelId", assumptions);
      return new Lookup(clarification.response(), conditions);
    }
    var model = available.models().stream().filter(item -> item.id().equals(modelId)).findFirst();
    if (model.isEmpty()) {
      return clarify(context, "조회할 GPU 모델을 알려 주세요.", "modelId");
    }

    if (scope.kind() != Kind.PRICE) {
      var analysisFilters = filters(applied, providerId);
      if (scope.kind().isPriceExtrema()) {
        if ("COMMITMENT".equals(contract) && terms.size() != 1) {
          return new Lookup(
              clarify(applied, "가격을 비교할 약정기간 한 가지를 알려 주세요.", "commitmentMonths").response(),
              conditions);
        }
        if (scope.offerId() != null) analysisFilters.put("offerId", scope.offerId());
        var cutoff = before == null ? null : before.atStartOfDay(KST).toInstant();
        boolean minimumOnly = scope.kind() == Kind.PRICE_MINIMUM;
        Map<String, Object> analysis;
        if (scope.from() != null) {
          var start = scope.from().atStartOfDay(KST).toInstant();
          var end = scope.to().atStartOfDay(KST).toInstant();
          analysis =
              new LinkedHashMap<>(
                  minimumOnly
                      ? offers.publicMinimumPrice(analysisFilters, start, end)
                      : offers.publicPriceRange(analysisFilters, start, end));
          analysis.put("observedOn", scope.from().toString());
        } else {
          analysis =
              minimumOnly
                  ? offers.publicMinimumPrice(analysisFilters, cutoff)
                  : offers.publicPriceRange(analysisFilters, cutoff);
        }
        return new Lookup(analysisReply(applied, analysis, before, model.get().name()), conditions);
      }
      if (scope.offerId() != null
          && (scope.kind() == Kind.TREND
              || scope.kind().defaultsToOnDemandAndSpot()
              || scope.kind() == Kind.FORECAST)) {
        analysisFilters.put("offerId", scope.offerId());
      }
      if ((scope.kind() == Kind.TREND || scope.kind() == Kind.FORECAST)
          && analysisFilters.get("contractType") == null) {
        return new Lookup(
            clarify(applied, "온디맨드, 스팟, 약정 중 조회할 계약을 알려 주세요.", "contractType").response(),
            conditions);
      }
      if ((scope.kind() == Kind.TREND || scope.kind() == Kind.FORECAST) && terms.size() > 1) {
        return new Lookup(
            clarify(applied, "이력과 예측에는 약정기간 한 가지를 알려 주세요.", "commitmentMonths").response(),
            conditions);
      }
      if (scope.kind() == Kind.TREND && scope.from() == null) {
        return new Lookup(
            clarify(applied, "조회 시작일과 종료일을 알려 주세요. 예를 들어 11/1~11/19를 입력해 주세요.", "period")
                .response(),
            conditions);
      }
      if (scope.kind().defaultsToOnDemandAndSpot()
          && "COMMITMENT".equals(contract)
          && terms.size() != 1) {
        return new Lookup(
            clarify(applied, "기간 비교에 적용할 약정기간을 한 가지 알려 주세요.", "commitmentMonths").response(),
            conditions);
      }
      if ((scope.kind() == Kind.DISCOUNT || scope.kind() == Kind.CONTRACT_COMPARISON)
          && (!"COMMITMENT".equals(contract) || terms.isEmpty())) {
        return new Lookup(
            clarify(applied, "온디맨드와 비교할 약정기간을 알려 주세요.", "commitmentMonths").response(), conditions);
      }
      if (scope.kind() == Kind.CONTRACT_COMPARISON
          || (scope.kind() == Kind.DISCOUNT && (before != null || scope.from() != null))) {
        Map<String, Object> comparisonResult;
        if (scope.from() != null) {
          var start = scope.from().atStartOfDay(KST).toInstant();
          var end = scope.to().atStartOfDay(KST).toInstant();
          comparisonResult =
              new LinkedHashMap<>(offers.productPricesForTerms(analysisFilters, terms, start, end));
          comparisonResult.put("observedOn", scope.from().toString());
        } else {
          comparisonResult =
              offers.productPricesForTerms(
                  analysisFilters,
                  terms,
                  before == null ? null : before.atStartOfDay(KST).toInstant());
        }
        return new Lookup(
            analysisReply(applied, comparisonResult, before, model.get().name()), conditions);
      }
      if (scope.kind() == Kind.AVAILABILITY && before != null) {
        return new Lookup(
            clarify(applied, "현재 공개 상태를 확인할 조건을 알려 주세요.", "observedBefore").response(), conditions);
      }
      Map<String, Object> analysis =
          switch (scope.kind()) {
            case TREND -> tools.trend(analysisFilters, scope.from(), scope.to());
            case TYPICAL_PRICE -> tools.typicalPrices(analysisFilters, scope.from(), scope.to());
            case TREND_TIMING -> tools.trendTiming(analysisFilters, scope.from(), scope.to());
            case WEEK_OVER_WEEK -> tools.weeklyTrend(analysisFilters, scope.from(), scope.to());
            case MONTH_OVER_MONTH -> tools.monthlyTrend(analysisFilters, scope.from(), scope.to());
            case DISCOUNT -> tools.discounts(analysisFilters, terms);
            case AVAILABILITY -> tools.statuses(analysisFilters);
            case FORECAST ->
                tools.forecast(
                    analysisFilters,
                    before == null ? Instant.now() : before.atStartOfDay(KST).toInstant());
            case PRICE,
                PRICE_RANGE,
                PRICE_MINIMUM,
                CONTRACT_COMPARISON,
                COST_IMPACT,
                PRICE_REMOVAL,
                PROPOSAL ->
                throw new IllegalStateException("Unexpected price intent.");
          };
      return new Lookup(analysisReply(applied, analysis), conditions);
    }
    if (comparison) {
      if (contract == null
          || (previous.assumedContract() && !namesContract && context.contractType() == null)) {
        return new Lookup(
            clarify(applied, "온디맨드, 스팟, 약정 중 조회할 계약을 알려 주세요.", "contractType").response(),
            new Conditions(
                new PublicContextDto(
                    modelId, null, country, systemType, gpuCount, type, region, null, null),
                providerId,
                false,
                true,
                before,
                scope,
                terms));
      }
      if ("COMMITMENT".equals(contract) && months == null) {
        return new Lookup(
            clarify(applied, "조회할 약정기간을 알려 주세요.", "commitmentMonths").response(), conditions);
      }
      return new Lookup(
          comparisonReply(
              model.get(), applied, assumptions, filters(applied, providerId), before, terms),
          conditions);
    }
    var filters = filters(applied, providerId);
    var data =
        before == null
            ? offers.publicPrices(filters)
            : offers.publicPricesBefore(filters, before.atStartOfDay(KST).toInstant());
    var response = reply(model.get(), applied, data, assumptions, before);
    if (before == null && data.items().isEmpty()) {
      var status = tools.statuses(filters);
      if (status.containsKey("kind")) response = response.withAnalysis(status);
    }
    return new Lookup(response, conditions);
  }

  private static AgentQueryScope resolveHistoryPeriod(
      AgentQueryScope requested, AgentQueryScope previous, LocalDate today) {
    if (requested.kind() == Kind.TREND_TIMING) {
      var from = previous.from() == null ? today.minusDays(29) : previous.from();
      var until = previous.to() == null ? today.plusDays(1) : previous.to();
      return new AgentQueryScope(requested.kind(), from, until, requested.offerId());
    }
    var first = today.withDayOfMonth(1);
    var start = first.minusMonths(1);
    if (previous.from() != null && previous.to() != null) {
      var previousMonth = previous.from().withDayOfMonth(1);
      if (!previous.to().isAfter(previousMonth.plusMonths(1))) start = previousMonth;
    }
    return new AgentQueryScope(
        requested.kind(),
        start,
        start.equals(first) ? today.plusDays(1) : start.plusMonths(1),
        requested.offerId());
  }

  private static boolean isConditionFragment(
      String original,
      List<String> countries,
      List<GpuModelDto> models,
      List<ProviderDto> providers) {
    String remainder = original;
    for (String country : countries) {
      remainder = COUNTRIES.get(country).matcher(remainder).replaceAll(" ");
    }
    remainder = remainder.toLowerCase(Locale.ROOT);
    for (var model : models) {
      remainder = removeName(remainder, model.name());
    }
    for (var provider : providers) {
      remainder = removeName(remainder, provider.name());
    }
    for (Pattern pattern : SYSTEMS.values()) {
      remainder = pattern.matcher(remainder).replaceAll(" ");
    }
    for (Pattern pattern : PROVIDER_TYPES.values()) {
      remainder = pattern.matcher(remainder).replaceAll(" ");
    }
    remainder = GPU_COUNT.matcher(remainder).replaceAll(" ");
    remainder = ON_DEMAND.matcher(remainder).replaceAll(" ");
    remainder = SPOT.matcher(remainder).replaceAll(" ");
    remainder = COMMITMENT_TERM.matcher(remainder).replaceAll(" ");
    remainder = COMMITMENT.matcher(remainder).replaceAll(" ");
    remainder = REGION_CODE.matcher(remainder).replaceAll(" ");
    remainder = CURRENT_PRICE.matcher(remainder).replaceAll(" ");
    for (Pattern pattern : PAYMENT_OPTIONS.values()) {
      remainder = pattern.matcher(remainder).replaceAll(" ");
    }
    return FOLLOW_UP_FILLER.matcher(remainder).replaceAll("").isEmpty();
  }

  private static String removeName(String question, String name) {
    return bounded(Pattern.quote(name.toLowerCase(Locale.ROOT))).matcher(question).replaceAll(" ");
  }

  private static Map<String, String> filters(PublicContextDto context, String providerId) {
    var filters = new LinkedHashMap<String, String>();
    filters.put("modelId", context.modelId());
    if (context.contractType() != null) filters.put("contractType", context.contractType());
    if (context.countryCode() != null) {
      filters.put("countryCode", context.countryCode());
    }
    if (context.regionCode() != null) {
      filters.put("regionCode", context.regionCode());
    }
    if (context.commitmentMonths() != null) {
      filters.put("commitmentMonths", context.commitmentMonths().toString());
    }
    if (context.paymentOption() != null) {
      filters.put("paymentOption", context.paymentOption());
    }
    if (context.systemType() != null) {
      filters.put("systemType", context.systemType());
    }
    if (context.gpuCount() != null) {
      filters.put("gpuCount", context.gpuCount().toString());
    }
    if (context.providerType() != null) {
      filters.put("providerType", context.providerType());
    }
    if (providerId != null) {
      filters.put("providerIds", providerId);
    }
    return filters;
  }

  private static Lookup clarify(PublicContextDto context, String question, String field) {
    return clarify(context, question, field, List.of());
  }

  private static Lookup clarify(
      PublicContextDto context, String question, String field, List<String> assumptions) {
    var response =
        new AgentMessageResponseDto(
            null,
            "NEEDS_CLARIFICATION",
            question,
            context,
            List.copyOf(assumptions),
            List.of(),
            List.of(),
            List.of(field),
            List.of(),
            null,
            null,
            null,
            List.of(),
            null,
            "NOT_QUERIED",
            null,
            null,
            DISCLAIMER,
            null,
            null);
    return new Lookup(response, null);
  }

  private static AgentMessageResponseDto followUpResponse(
      String id, AgentMessageResponseDto response, Conditions before, Conditions after) {
    var previous = before.publicContext();
    var current = after.publicContext();
    var retained = new ArrayList<String>();
    var changed = new ArrayList<String>();
    compareCondition("modelId", previous.modelId(), current.modelId(), retained, changed);
    compareCondition(
        "contractType", previous.contractType(), current.contractType(), retained, changed);
    compareCondition(
        "countryCode", previous.countryCode(), current.countryCode(), retained, changed);
    compareCondition("regionCode", previous.regionCode(), current.regionCode(), retained, changed);
    compareCondition(
        "commitmentMonths",
        previous.commitmentMonths(),
        current.commitmentMonths(),
        retained,
        changed);
    compareCondition(
        "paymentOption", previous.paymentOption(), current.paymentOption(), retained, changed);
    compareCondition("systemType", previous.systemType(), current.systemType(), retained, changed);
    compareCondition("gpuCount", previous.gpuCount(), current.gpuCount(), retained, changed);
    compareCondition(
        "providerType", previous.providerType(), current.providerType(), retained, changed);
    compareCondition("providerIds", before.providerId(), after.providerId(), retained, changed);
    compareCondition(
        "observedBefore", before.observedBefore(), after.observedBefore(), retained, changed);
    compareCondition("period", before.scope().from(), after.scope().from(), retained, changed);
    compareCondition(
        "offerId", before.scope().offerId(), after.scope().offerId(), retained, changed);
    return response.withSession(id, List.copyOf(retained), List.copyOf(changed));
  }

  private static void compareCondition(
      String field, Object before, Object after, List<String> retained, List<String> changed) {
    if (!Objects.equals(before, after)) {
      changed.add(field);
    } else if (after != null) {
      retained.add(field);
    }
  }

  private static <T> T firstValue(T explicit, T remembered) {
    return explicit == null ? remembered : explicit;
  }

  private record Lookup(AgentMessageResponseDto response, Conditions conditions) {}

  private AgentMessageResponseDto comparisonReply(
      GpuModelDto model,
      PublicContextDto context,
      List<String> assumptions,
      Map<String, String> filters,
      LocalDate before,
      List<Integer> terms) {
    var comparison =
        terms.size() > 1
            ? offers.summariesForTerms(
                filters, terms, before == null ? null : before.atStartOfDay(KST).toInstant())
            : before == null
                ? offers.summaryLatest(filters)
                : offers.summaryBefore(filters, before.atStartOfDay(KST).toInstant());
    boolean empty = ((List<?>) comparison.get("groups")).isEmpty();
    String answer =
        before == null
            ? model.name()
                + (empty
                    ? "의 동일 조건 비교에 사용할 공개가격을 확보하지 못했습니다."
                    : "의 상품별 마지막 공개가격을 동일 조건 그룹별로 비교했습니다.")
            : historicalHeading(model, before, empty, true);
    return new AgentMessageResponseDto(
        null,
        empty ? "NO_DATA" : "ANSWERED",
        answer,
        context,
        List.copyOf(assumptions),
        List.of(),
        List.of(),
        List.of(),
        List.of(),
        history(before),
        null,
        null,
        List.of(),
        null,
        "GROUPED",
        "PROVIDER_REPRESENTATIVE_LOWEST",
        (Instant) comparison.get("asOf"),
        COMPARISON_DISCLAIMER,
        comparison,
        null);
  }

  private static AgentMessageResponseDto reply(
      GpuModelDto model,
      PublicContextDto conditions,
      PublicPriceLookupDto data,
      List<String> assumptions,
      LocalDate before) {
    var items = data.items();
    var sources = new LinkedHashMap<String, Map<String, Object>>();
    var results =
        items.stream()
            .map(
                item -> {
                  var evidenceIds =
                      item.sources().stream()
                          .map(
                              source -> {
                                String id = source.get("evidenceId").toString();
                                sources.putIfAbsent(id, source);
                                return id;
                              })
                          .toList();
                  return new AgentResultDto(
                      item.offerId(),
                      item.observationId(),
                      item.normalizationId(),
                      item.rawPriceText(),
                      item.rawComponents(),
                      item.providerName(),
                      item.gpuCount(),
                      item.countryCode(),
                      item.regionCode(),
                      item.systemType(),
                      item.contractType(),
                      item.collectedAt(),
                      item.priceUsdGpuHour(),
                      "EFFECTIVE",
                      "OK",
                      evidenceIds);
                })
            .toList();
    var samples =
        new SampleCountsDto(
            items.stream().map(PublicPriceDto::providerId).distinct().count(),
            items.stream().map(PublicPriceDto::productId).distinct().count(),
            items.stream().map(PublicPriceDto::offerId).distinct().count(),
            items.stream().map(PublicPriceDto::observationId).distinct().count());
    String heading =
        before != null
            ? historicalHeading(model, before, items.isEmpty(), false)
            : items.isEmpty()
                ? "조회한 범위에서 출처 있는 온디맨드 공개 확정가를 찾지 못했습니다."
                : model.name() + " 온디맨드 공개가격을 수집 시각이 최근인 순서로 표시했습니다.";
    String answer = describe(heading, items);
    return new AgentMessageResponseDto(
        null,
        items.isEmpty() ? "NO_DATA" : "ANSWERED",
        answer,
        conditions,
        List.copyOf(assumptions),
        List.of(),
        List.of(),
        List.of(),
        results,
        history(before),
        null,
        null,
        List.copyOf(sources.values()),
        samples,
        "QUERIED",
        "OFFER_OBSERVATION",
        data.asOf(),
        DISCLAIMER,
        null,
        null);
  }

  private static AgentMessageResponseDto analysisReply(
      PublicContextDto conditions, Map<String, Object> analysis) {
    return analysisReply(conditions, analysis, null, null);
  }

  private static AgentMessageResponseDto analysisReply(
      PublicContextDto conditions,
      Map<String, Object> analysis,
      LocalDate before,
      String modelName) {
    String kind = analysis.get("kind").toString();
    Object observedOn = analysis.get("observedOn");
    boolean noPublicLookup = "COST_IMPACT".equals(kind) || "INTERNAL_PROPOSAL".equals(kind);
    String detailStatus = String.valueOf(analysis.get("status"));
    boolean noData =
        switch (kind) {
          case "TREND", "TREND_TIMING", "WEEK_OVER_WEEK", "MONTH_OVER_MONTH" ->
              ((Number) analysis.get("comparablePairs")).intValue() == 0;
          case "FORECAST" -> !"OK".equals(detailStatus);
          default -> ((List<?>) analysis.get("items")).isEmpty();
        };
    String status =
        "NEEDS_CLARIFICATION".equals(detailStatus) ? detailStatus : noData ? "NO_DATA" : "ANSWERED";
    String answer =
        switch (kind) {
          case "TREND" ->
              noData
                  ? "가격 변화를 판정할 동일 오퍼의 연속 관측이 부족합니다."
                  : ((List<?>) analysis.get("changes")).isEmpty()
                      ? "조회 범위의 비교 가능한 연속 관측에서 확인된 가격 변동이 없습니다."
                      : "동일 오퍼의 연속 관측에서 확인한 가격 변화입니다.";
          case "TYPICAL_PRICE" ->
              noData
                  ? "이 기간에 중앙값을 계산할 수 있는 공개가격 관측이 없습니다."
                  : "오퍼마다 하루의 마지막 공개 단가를 하나씩 골라 중앙값을 계산했습니다.";
          case "TREND_TIMING" ->
              noData
                  ? "가격이 오르거나 내린 시점을 확인할 연속 공개가격 관측이 부족합니다."
                  : "같은 오퍼의 연속 관측에서 조회 기간 내 처음 확인된 상승·하락 시점을 표시했습니다.";
          case "WEEK_OVER_WEEK" ->
              noData
                  ? "지난주와 이번 주를 모두 관측한 비교 가능한 가격 쌍이 부족해 주간 변동률을 계산할 수 없습니다."
                  : "지난주와 이번 주의 같은 상품별 마지막 공개가격을 비교했습니다.";
          case "MONTH_OVER_MONTH" ->
              noData
                  ? "지난달과 이번 달을 모두 관측한 비교 가능한 가격 쌍이 부족해 월간 변동률을 계산할 수 없습니다."
                  : "지난달과 이번 달의 같은 오퍼별 마지막 공개가격을 비교했습니다.";
          case "FORECAST" ->
              noData
                  ? String.valueOf(analysis.get("reason"))
                  : "같은 오퍼의 실제 월별 공개가격으로 계산한 12개월 예상 범위입니다.";
          case "DISCOUNT" ->
              noData ? "할인율을 계산할 수 있는 공개 약정 가격이 없습니다." : "같은 상품의 마지막 온디맨드와 약정 공개 단가를 기준으로 조회했습니다.";
          case "PRICE_RANGE", "PRICE_MINIMUM" -> {
            var contract =
                switch (conditions.contractType()) {
                  case "ON_DEMAND" -> "온디맨드";
                  case "SPOT" -> "스팟";
                  default -> conditions.commitmentMonths() + "개월 약정";
                };
            if (observedOn != null) {
              yield observedOn
                  + " 당일(KST) "
                  + modelName
                  + " "
                  + contract
                  + (noData
                      ? " 공개가격을 확인할 수집 기록이 없습니다."
                      : "PRICE_MINIMUM".equals(kind)
                          ? " 최저 공개가격과 제공 사업자입니다."
                          : " 공개가격의 최저가와 최고가입니다.");
            }
            yield (before == null ? "" : before + " 00:00 KST 이전 ")
                + modelName
                + " "
                + contract
                + (noData
                    ? " 공개가격 기록을 찾지 못했습니다."
                    : "PRICE_MINIMUM".equals(kind)
                        ? " 상품별 마지막 공개가격 중 최저가와 제공 사업자입니다."
                        : " 상품별 마지막 공개가격의 최저가와 최고가입니다.");
          }
          case "CONTRACT_COMPARISON" -> {
            if (observedOn != null) {
              yield observedOn
                  + " 당일(KST) "
                  + modelName
                  + (noData
                      ? "의 상품별 온디맨드·약정 공개가격 수집 기록이 없습니다."
                      : "의 상품별 온디맨드·약정 공개 단가와 할인율입니다. 당일 수집 기록을 기준으로 비교했습니다.");
            }
            yield (before == null ? "" : before + " 00:00 KST 이전 ")
                + modelName
                + (noData ? "의 조건에 맞는 공개가격 기록을 찾지 못했습니다." : "의 상품별 마지막 온디맨드·약정 공개 단가와 할인율입니다.");
          }
          case "COST_IMPACT" ->
              "현재 연결된 자료로는 전기료·메모리 가격이 바뀐 뒤 GPU 임대료가 몇 주 뒤에 움직였는지 확인할 근거가 부족합니다. "
                  + "같은 지역과 기간의 비용 지표 및 GPU 임대료 시계열이 필요합니다.";
          case "PRICE_REMOVAL" ->
              noData
                  ? "조회 기간에 공개 가격표에서 빠졌다고 확인할 수 있는 상품 근거가 없습니다. "
                      + "수집 실패나 수집 대상에서 가격을 찾지 못한 기록만으로는 개별 상품의 삭제를 판단할 수 없습니다."
                  : "조회 기간에 공개가격에서 빠졌고, 기간 종료 시점까지 공개 전환이 확인되지 않은 요금 항목입니다.";
          case "INTERNAL_PROPOSAL" -> modelName + " 고객 제안가는 내부 원가, 유료 가동률, 목표 마진을 반영해 검토합니다.";
          case "INTERNAL_ANALYSIS" ->
              "제안가는 시간당 원가를 1−목표 매출 마진율로 나눠 산출합니다. 판매가 검토는 입력한 가동률과 목표 IRR, 공개 시장가격으로 계산합니다. 내부 입력과"
                  + " 구체적인 계산 결과는 아래 화면에서 확인할 수 있어요.";
          default ->
              noData
                  ? "선택한 범위의 수집 대상이나 확인 기록을 찾지 못했습니다."
                  : "대상별로 공개 상태, 최근 점검 결과와 마지막 정상 관측을 표시합니다.";
        };
    return new AgentMessageResponseDto(
        null,
        status,
        answer,
        conditions,
        List.of(),
        List.of(),
        List.of(),
        "NEEDS_CLARIFICATION".equals(status) ? List.of("offerId") : List.of(),
        List.of(),
        observedOn == null ? history(before) : Map.of("observedOn", observedOn),
        null,
        null,
        List.of(),
        null,
        noPublicLookup ? "NOT_QUERIED" : "QUERIED",
        null,
        noPublicLookup ? null : MarketForecastService.instant(analysis.get("asOf")),
        analysisDisclaimer(kind, conditions, noPublicLookup),
        null,
        analysis);
  }

  private static String analysisDisclaimer(
      String kind, PublicContextDto conditions, boolean noPublicLookup) {
    if (noPublicLookup || "PRICE_REMOVAL".equals(kind)) return null;
    if (("TYPICAL_PRICE".equals(kind)
            || "TREND_TIMING".equals(kind)
            || "PRICE_RANGE".equals(kind)
            || "PRICE_MINIMUM".equals(kind)
            || "WEEK_OVER_WEEK".equals(kind)
            || "MONTH_OVER_MONTH".equals(kind))
        && !"COMMITMENT".equals(conditions.contractType())) {
      return "공개가격 기준이며 협상가·대형 고객 할인을 반영하지 않습니다.";
    }
    return COMPARISON_DISCLAIMER;
  }

  private static Map<String, Object> history(LocalDate before) {
    return before == null ? null : Map.of("observedBefore", before.toString());
  }

  private static String historicalHeading(
      GpuModelDto model, LocalDate before, boolean empty, boolean comparison) {
    return before
        + " 00:00 KST 이전 "
        + model.name()
        + (empty
            ? "의 조건에 맞는 공개가격 기록을 찾지 못했습니다."
            : comparison ? "의 동일 조건별 과거 공개가격 비교입니다." : "의 과거 공개가격 관측입니다.");
  }

  private static String describe(String heading, List<PublicPriceDto> items) {
    var text = new StringBuilder(heading);
    for (var item : items) {
      text.append("\n- ")
          .append(item.providerName())
          .append(" · ")
          .append(Objects.toString(item.regionCode(), "리전 미확인"))
          .append(" · ")
          .append(Objects.toString(item.gpuCount(), "미확인"))
          .append(" GPU")
          .append(" · ")
          .append(item.priceUsdGpuHour())
          .append(" USD/GPU·시간")
          .append(" · 수집 ")
          .append(OBSERVED_AT.format(item.collectedAt()))
          .append(" · 출처 ")
          .append(item.sources().get(0).get("sourceName"));
    }
    return text.toString();
  }

  private static List<GpuModelDto> mentionedModels(String question, List<GpuModelDto> catalog) {
    if (catalog.isEmpty()) {
      return List.of();
    }
    var names =
        catalog.stream()
            .map(model -> model.name().toLowerCase(Locale.ROOT))
            .distinct()
            .sorted(Comparator.comparingInt(String::length).reversed())
            .map(Pattern::quote)
            .toList();
    var matcher = bounded(String.join("|", names)).matcher(question);
    var matches = new HashSet<String>();
    while (matcher.find()) {
      matches.add(matcher.group());
    }
    return catalog.stream()
        .filter(model -> matches.contains(model.name().toLowerCase(Locale.ROOT)))
        .toList();
  }

  private static Pattern countryPattern(String code) {
    var locale = new Locale.Builder().setRegion(code).build();
    String names =
        Pattern.quote(locale.getDisplayCountry(Locale.ENGLISH))
            + "|"
            + Pattern.quote(locale.getDisplayCountry(Locale.KOREAN));
    var alias = COUNTRY_ALIASES.get(code);
    if (alias != null) {
      names += "|" + alias.pattern();
    }
    return Pattern.compile(
        "(?iu:" + bounded(names).pattern() + ")|(?i:\\b(?:in|for)\\s+(?:the\\s+)?)" + code + "\\b");
  }

  private static boolean mentions(String question, String name) {
    return bounded(Pattern.quote(name.toLowerCase(Locale.ROOT))).matcher(question).find();
  }

  private static Pattern bounded(String alternatives) {
    return Pattern.compile("(?<![a-z0-9])(?:" + alternatives + ")(?![a-z0-9])");
  }

  private static boolean conflict(String present, String requested) {
    return present != null && !present.equalsIgnoreCase(requested);
  }

  private static String upper(String value) {
    return value == null ? null : value.toUpperCase(Locale.ROOT);
  }

  public static class SensitiveInput extends RuntimeException {}
}
