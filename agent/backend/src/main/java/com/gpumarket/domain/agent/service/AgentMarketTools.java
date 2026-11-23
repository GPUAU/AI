package com.gpumarket.domain.agent.service;

import com.gpumarket.domain.market.dto.MarketAvailabilityRequestDto;
import com.gpumarket.domain.market.service.MarketAvailabilityService;
import com.gpumarket.domain.market.service.MarketForecastService;
import com.gpumarket.domain.market.service.MarketHistoryService;
import com.gpumarket.domain.market.service.MarketOfferService;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.function.Function;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Isolation;
import org.springframework.transaction.annotation.Transactional;

@Service
public class AgentMarketTools {
  private static final ZoneId KST = ZoneId.of("Asia/Seoul");
  private static final int HISTORY_PAGE_SIZE = 25;
  private static final int MAX_HISTORY_PAGES = 4;
  private final MarketHistoryService history;
  private final MarketAvailabilityService availability;
  private final MarketOfferService offers;
  private final MarketForecastService forecasts;

  public AgentMarketTools(
      MarketHistoryService history,
      MarketAvailabilityService availability,
      MarketOfferService offers,
      MarketForecastService forecasts) {
    this.history = history;
    this.availability = availability;
    this.offers = offers;
    this.forecasts = forecasts;
  }

  private Map<String, Object> loadHistory(
      Map<String, String> filters, LocalDate start, LocalDate end, Instant verifiedAsOf) {
    var query = new LinkedHashMap<>(filters);
    query.put("from", start.atStartOfDay(KST).toOffsetDateTime().toString());
    query.put("to", end.atStartOfDay(KST).toOffsetDateTime().toString());
    query.put("limit", Integer.toString(HISTORY_PAGE_SIZE));
    Function<Map<String, String>, Map<String, Object>> read =
        verifiedAsOf == null
            ? history::publicHistory
            : params -> history.publicHistory(params, verifiedAsOf);
    var result = new LinkedHashMap<>(read.apply(Map.copyOf(query)));
    var series = new ArrayList<>(MarketForecastService.records(result.get("series")));
    var sources = new LinkedHashMap<String, Map<String, Object>>();
    addSources(result, sources);
    Object cursor = result.get("nextCursor");
    for (int page = 1; cursor != null && page < MAX_HISTORY_PAGES; page++) {
      query.put("cursor", cursor.toString());
      var next = read.apply(Map.copyOf(query));
      series.addAll(MarketForecastService.records(next.get("series")));
      addSources(next, sources);
      cursor = next.get("nextCursor");
    }
    result.put("series", series);
    result.put("sources", List.copyOf(sources.values()));
    result.put("nextCursor", cursor);
    return result;
  }

  private static void addSources(
      Map<String, Object> result, Map<String, Map<String, Object>> sources) {
    for (var source : MarketForecastService.records(result.get("sources"))) {
      sources.put(source.get("evidenceId").toString(), source);
    }
  }

  @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
  Map<String, Object> trend(Map<String, String> filters, LocalDate start, LocalDate end) {
    return trend(filters, start, end, null);
  }

  private Map<String, Object> trend(
      Map<String, String> filters, LocalDate start, LocalDate end, Instant verifiedAsOf) {
    var result = loadHistory(filters, start, end, verifiedAsOf);
    var series = MarketForecastService.records(result.get("series"));
    var changes = new ArrayList<Map<String, Object>>();
    int comparablePairs = 0;
    for (var offer : series) {
      Map<String, Object> previous = null;
      for (var point : MarketForecastService.records(offer.get("points"))) {
        var amount = point.get("effectiveUsdGpuHour");
        if (amount == null) {
          previous = null;
          continue;
        }
        if (previous != null && comparable(previous, point)) {
          comparablePairs++;
          recordChange(changes, offer, previous, point);
        }
        previous = point;
      }
    }
    result.put("kind", "TREND");
    result.put("changes", changes);
    result.put("comparablePairs", comparablePairs);
    result.put("dateFrom", start.toString());
    result.put("dateTo", end.minusDays(1).toString());
    result.put("contractType", filters.get("contractType"));
    return result;
  }

  @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
  public Map<String, Object> weeklyTrend(
      Map<String, String> filters, LocalDate start, LocalDate end) {
    return comparePeriods(filters, start, start.plusWeeks(1), end, "WEEK_OVER_WEEK");
  }

  @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
  public Map<String, Object> typicalPrices(
      Map<String, String> filters, LocalDate start, LocalDate end) {
    Instant asOf = Instant.now();
    var items = new ArrayList<Map<String, Object>>();
    var sources = new LinkedHashMap<String, Map<String, Object>>();
    boolean limited = false;
    for (String type : contractTypes(filters)) {
      var query = new LinkedHashMap<>(filters);
      query.put("contractType", type);
      var data = loadHistory(query, start, end, asOf);
      addSources(data, sources);
      limited |= data.get("nextCursor") != null;
      for (var offer : MarketForecastService.records(data.get("series"))) {
        var row = typicalPrice(offer, type);
        if (row != null) items.add(row);
      }
    }
    return Map.of(
        "kind",
        "TYPICAL_PRICE",
        "items",
        items,
        "sources",
        List.copyOf(sources.values()),
        "asOf",
        asOf,
        "dateFrom",
        start.toString(),
        "dateTo",
        end.minusDays(1).toString(),
        "periodDays",
        ChronoUnit.DAYS.between(start, end),
        "limited",
        limited);
  }

  private static Map<String, Object> typicalPrice(Map<String, Object> offer, String type) {
    var points = MarketForecastService.records(offer.get("points"));
    var days = new TreeMap<LocalDate, Map<String, Object>>();
    Map<String, Object> latest = null;
    for (int index = points.size() - 1; index >= 0; index--) {
      var point = points.get(index);
      if (point.get("effectiveUsdGpuHour") == null) continue;
      if (latest == null) latest = point;
      if (!comparable(latest, point)) continue;
      var day = MarketForecastService.instant(point.get("collectedAt")).atZone(KST).toLocalDate();
      days.putIfAbsent(day, point);
    }
    if (days.isEmpty()) return null;
    var amounts =
        days.values().stream()
            .map(point -> new BigDecimal(point.get("effectiveUsdGpuHour").toString()))
            .sorted()
            .toList();
    int middle = amounts.size() / 2;
    var median = amounts.get(middle);
    if (amounts.size() % 2 == 0) {
      median = median.add(amounts.get(middle - 1)).divide(BigDecimal.valueOf(2));
    }
    var row = new LinkedHashMap<String, Object>();
    row.put("offerId", offer.get("offerId"));
    row.put("providerName", offer.get("providerName"));
    row.put("regionCode", offer.get("regionCode"));
    row.put("gpuCount", offer.get("gpuCount"));
    row.put("contractType", type);
    row.put("medianUsdGpuHour", median.stripTrailingZeros().toPlainString());
    row.put("observedDays", days.size());
    row.put("daily", List.copyOf(days.values()));
    return row;
  }

  @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
  public Map<String, Object> trendTiming(
      Map<String, String> filters, LocalDate start, LocalDate end) {
    Instant asOf = Instant.now();
    var sections = new ArrayList<Map<String, Object>>();
    int pairs = 0;
    for (String type : contractTypes(filters)) {
      var query = new LinkedHashMap<>(filters);
      query.put("contractType", type);
      var data = trend(query, start, end, asOf);
      var seen = new HashSet<String>();
      var firstChanges = new ArrayList<Map<String, Object>>();
      for (var change : MarketForecastService.records(data.get("changes"))) {
        var amount = new BigDecimal(change.get("difference").toString());
        if (seen.add(change.get("offerId") + "/" + amount.signum())) firstChanges.add(change);
      }
      data.put("firstChanges", firstChanges);
      sections.add(data);
      pairs += ((Number) data.get("comparablePairs")).intValue();
    }
    return Map.of(
        "kind",
        "TREND_TIMING",
        "items",
        sections,
        "comparablePairs",
        pairs,
        "asOf",
        asOf,
        "dateFrom",
        start.toString(),
        "dateTo",
        end.minusDays(1).toString());
  }

  private static List<String> contractTypes(Map<String, String> filters) {
    return filters.containsKey("contractType")
        ? List.of(filters.get("contractType"))
        : List.of("ON_DEMAND", "SPOT");
  }

  @Transactional(readOnly = true, isolation = Isolation.REPEATABLE_READ)
  public Map<String, Object> monthlyTrend(
      Map<String, String> filters, LocalDate start, LocalDate end) {
    return comparePeriods(filters, start, start.plusMonths(1), end, "MONTH_OVER_MONTH");
  }

  private Map<String, Object> comparePeriods(
      Map<String, String> filters, LocalDate start, LocalDate split, LocalDate end, String kind) {
    Instant asOf = Instant.now();
    var sections = new ArrayList<Map<String, Object>>();
    int totalPairs = 0;
    for (String type : contractTypes(filters)) {
      var query = new LinkedHashMap<>(filters);
      query.put("contractType", type);
      var previous = trend(query, start, split, asOf);
      var current = trend(query, split, end, asOf);
      var previousPrices = lastPrices(previous);
      var currentPrices = lastPrices(current);
      var changes = new ArrayList<Map<String, Object>>();
      var comparisons = new ArrayList<Map<String, Object>>();
      for (var offer : MarketForecastService.records(current.get("series"))) {
        var id = offer.get("offerId").toString();
        var before = previousPrices.get(id);
        var after = currentPrices.get(id);
        if (before != null && after != null && comparable(before, after)) {
          var comparison = priceDifference(offer, before, after, true);
          comparisons.add(comparison);
          if (Boolean.TRUE.equals(comparison.get("changed"))) changes.add(comparison);
        }
      }
      var sources = new LinkedHashMap<String, Map<String, Object>>();
      addSources(previous, sources);
      addSources(current, sources);
      sections.add(
          Map.of(
              "contractType",
              type,
              "previousOffers",
              previousPrices.size(),
              "currentOffers",
              currentPrices.size(),
              "comparablePairs",
              comparisons.size(),
              "priceComparisons",
              comparisons,
              "weeklyChanges",
              changes,
              "previousChanges",
              previous.get("changes"),
              "currentChanges",
              current.get("changes"),
              "sources",
              List.copyOf(sources.values()),
              "limited",
              previous.get("nextCursor") != null || current.get("nextCursor") != null));
      totalPairs += comparisons.size();
    }
    return Map.of(
        "kind",
        kind,
        "items",
        sections,
        "comparablePairs",
        totalPairs,
        "previousFrom",
        start.toString(),
        "previousTo",
        split.minusDays(1).toString(),
        "currentFrom",
        split.toString(),
        "currentTo",
        end.minusDays(1).toString(),
        "asOf",
        asOf);
  }

  private static Map<String, Map<String, Object>> lastPrices(Map<String, Object> result) {
    var prices = new LinkedHashMap<String, Map<String, Object>>();
    for (var offer : MarketForecastService.records(result.get("series"))) {
      for (var point : MarketForecastService.records(offer.get("points"))) {
        if (point.get("effectiveUsdGpuHour") != null)
          prices.put(offer.get("offerId").toString(), point);
      }
    }
    return prices;
  }

  private static void recordChange(
      List<Map<String, Object>> changes,
      Map<String, Object> offer,
      Map<String, Object> before,
      Map<String, Object> after) {
    var change = priceDifference(offer, before, after, false);
    if (change != null) changes.add(change);
  }

  private static Map<String, Object> priceDifference(
      Map<String, Object> offer,
      Map<String, Object> before,
      Map<String, Object> after,
      boolean includeUnchanged) {
    var old = new BigDecimal(before.get("effectiveUsdGpuHour").toString());
    var delta = new BigDecimal(after.get("effectiveUsdGpuHour").toString()).subtract(old);
    boolean changed = delta.signum() != 0;
    if (!changed && !includeUnchanged) return null;
    var change = new LinkedHashMap<String, Object>();
    change.put("offerId", offer.get("offerId"));
    change.put("providerName", offer.get("providerName"));
    change.put("regionCode", offer.get("regionCode"));
    change.put("gpuCount", offer.get("gpuCount"));
    change.put("changed", changed);
    change.put("before", before);
    change.put("after", after);
    change.put("difference", delta.toPlainString());
    change.put(
        "changePercent",
        old.signum() == 0
            ? null
            : delta
                .multiply(BigDecimal.valueOf(100))
                .divide(old, 4, RoundingMode.HALF_UP)
                .stripTrailingZeros()
                .toPlainString());
    return change;
  }

  Map<String, Object> forecast(Map<String, String> filters, Instant asOf) {
    var result = new LinkedHashMap<>(forecasts.forecast(filters, asOf));
    result.put("kind", "FORECAST");
    return result;
  }

  Map<String, Object> statuses(Map<String, String> filters) {
    var result =
        availability.list(
            new MarketAvailabilityRequestDto(
                Long.parseLong(filters.get("modelId")),
                filters.containsKey("providerIds")
                    ? List.of(Long.parseLong(filters.get("providerIds")))
                    : List.of(),
                lower(filters.get("countryCode")),
                lower(filters.get("contractType")),
                lower(filters.get("regionCode")),
                50,
                null));
    return Map.of(
        "kind",
        "AVAILABILITY",
        "items",
        result.items(),
        "asOf",
        result.asOf(),
        "limited",
        result.nextCursor() != null);
  }

  Map<String, Object> discounts(Map<String, String> filters, List<Integer> terms) {
    var items = new ArrayList<Map<String, Object>>();
    for (int term : terms) {
      var query = new LinkedHashMap<>(filters);
      query.put("commitmentMonths", Integer.toString(term));
      for (var group : MarketForecastService.records(offers.summaryLatest(query).get("groups"))) {
        for (var row : MarketForecastService.records(group.get("representatives"))) {
          var quote = offers.contractsLatest(Long.parseLong(row.get("productId").toString()));
          var discount =
              quote.discounts().stream()
                  .filter(item -> item.commitmentOfferId().equals(row.get("offerId")))
                  .findFirst()
                  .orElse(null);
          var item = new LinkedHashMap<String, Object>();
          item.put("providerName", row.get("providerName"));
          item.put("months", term);
          item.put("commitment", row);
          var supported = discount != null && "OK".equals(discount.status());
          var base =
              supported
                  ? quote.items().stream()
                      .filter(candidate -> candidate.offerId().equals(discount.baseOfferId()))
                      .findFirst()
                      .orElse(null)
                  : null;
          if (base != null) {
            var detail =
                MarketForecastService.records(
                        List.of(
                            offers.detail(Long.parseLong(base.offerId()), Map.of()).get("offer")))
                    .get(0);
            if (detail.get("lastSuccessfulObservation") instanceof Map<?, ?> observation
                && base.observationId().equals(observation.get("observationId"))) {
              var basePrice = new BigDecimal(base.effective().get("amount").toString());
              var contractPrice = new BigDecimal(row.get("priceUsdGpuHour").toString());
              if (quote.items().stream()
                  .anyMatch(
                      candidate ->
                          candidate.offerId().equals(row.get("offerId"))
                              && Objects.equals(
                                  candidate.observationId(), row.get("observationId")))) {
                item.put("baseline", observation);
                item.put("difference", basePrice.subtract(contractPrice).toPlainString());
                item.put(
                    "percent",
                    new BigDecimal(discount.discountRate())
                        .movePointRight(2)
                        .stripTrailingZeros()
                        .toPlainString());
              }
            }
          }
          if (!item.containsKey("percent"))
            item.put(
                "reason",
                discount == null || discount.reason() == null
                    ? "BASE_PRICE_UNAVAILABLE"
                    : discount.reason());
          items.add(item);
        }
      }
    }
    return Map.of("kind", "DISCOUNT", "items", items, "asOf", Instant.now());
  }

  private static String lower(String value) {
    return value == null ? null : value.toLowerCase(java.util.Locale.ROOT);
  }

  private static boolean comparable(Map<String, Object> before, Map<String, Object> after) {
    return List.of("ruleVersion", "originalCurrency", "originalTimeUnit", "priceAppliesTo").stream()
        .allMatch(field -> Objects.equals(before.get(field), after.get(field)));
  }
}
