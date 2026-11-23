package com.gpumarket.domain.agent.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.gpumarket.domain.market.service.MarketForecastService;
import com.gpumarket.domain.market.service.MarketHistoryService;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class AgentMarketToolsTest {
  @Test
  void typicalPriceUsesOnlyTheLastValidPointPerKoreanCalendarDay() {
    var history = mock(MarketHistoryService.class);
    var tools = new AgentMarketTools(history, null, null, null);
    var points =
        List.of(
            Map.of(
                "collectedAt", Instant.parse("2026-10-16T00:00:00Z"), "effectiveUsdGpuHour", "40"),
            Map.of(
                "collectedAt", Instant.parse("2026-10-16T14:55:00Z"), "effectiveUsdGpuHour", "8"),
            Map.of(
                "collectedAt", Instant.parse("2026-10-16T15:05:00Z"), "effectiveUsdGpuHour", "4"),
            Map.of("collectedAt", Instant.parse("2026-10-17T11:00:00Z")));
    when(history.publicHistory(anyMap(), any(Instant.class)))
        .thenReturn(
            Map.of(
                "series",
                List.of(Map.of("offerId", "40", "providerName", "Nebius", "points", points)),
                "asOf",
                Instant.EPOCH));
    var result =
        tools.typicalPrices(
            Map.of("modelId", "4", "contractType", "SPOT"),
            LocalDate.parse("2026-10-01"),
            LocalDate.parse("2026-11-01"));
    assertThat(result).containsEntry("periodDays", 31L);
    assertThat(MarketForecastService.records(result.get("items")))
        .singleElement()
        .satisfies(
            row ->
                assertThat(row)
                    .containsEntry("observedDays", 2)
                    .containsEntry("medianUsdGpuHour", "6")
                    .containsEntry("daily", List.of(points.get(1), points.get(2))));
  }

  @Test
  void timingKeepsTheFirstRiseAndFallForEachOffer() {
    var history = mock(MarketHistoryService.class);
    var tools = new AgentMarketTools(history, null, null, null);
    var points =
        List.of("8", "6", "9", "5").stream()
            .map(price -> Map.of("effectiveUsdGpuHour", price))
            .toList();
    when(history.publicHistory(anyMap(), any(Instant.class)))
        .thenReturn(
            Map.of(
                "series",
                List.of(Map.of("offerId", "40", "providerName", "Nebius", "points", points)),
                "asOf",
                Instant.EPOCH));
    var output =
        tools.trendTiming(
            Map.of("modelId", "4", "contractType", "SPOT"),
            LocalDate.parse("2026-10-01"),
            LocalDate.parse("2026-11-01"));
    var section = MarketForecastService.records(output.get("items")).get(0);
    assertThat(MarketForecastService.records(section.get("firstChanges")))
        .extracting(item -> item.get("difference"))
        .containsExactly("-2", "3");
  }

  @Test
  void showsLastWeeksSpotChangesFromLaterPagesWhenThisWeekIsUnobserved() {
    var history = mock(MarketHistoryService.class);
    var tools = new AgentMarketTools(history, null, null, null);
    var monday = LocalDate.parse("2026-11-23");
    when(history.publicHistory(anyMap(), any(Instant.class)))
        .thenAnswer(
            call -> {
              Map<String, String> query = call.getArgument(0);
              if (query.get("from").startsWith(monday.toString())
                  || "ON_DEMAND".equals(query.get("contractType"))) {
                return Map.of("series", List.of(), "asOf", Instant.EPOCH);
              }
              if (!query.containsKey("cursor"))
                return Map.of("series", List.of(), "nextCursor", "25", "asOf", Instant.EPOCH);
              var offer =
                  Map.of(
                      "offerId",
                      "40",
                      "providerName",
                      "Nebius",
                      "points",
                      List.of(
                          Map.of("effectiveUsdGpuHour", "8"), Map.of("effectiveUsdGpuHour", "6")));
              return Map.of("series", List.of(offer), "asOf", Instant.EPOCH);
            });
    var result =
        tools.weeklyTrend(Map.of("modelId", "4"), monday.minusWeeks(1), monday.plusDays(1));
    assertThat(result).containsEntry("comparablePairs", 0);
    var spot = MarketForecastService.records(result.get("items")).get(1);
    assertThat(spot)
        .containsEntry("contractType", "SPOT")
        .containsEntry("previousOffers", 1)
        .containsEntry("currentOffers", 0);
    assertThat(MarketForecastService.records(spot.get("previousChanges")))
        .singleElement()
        .satisfies(
            change ->
                assertThat(change)
                    .containsEntry("difference", "-2")
                    .containsEntry("changePercent", "-25"));
  }

  @Test
  void monthlyComparisonUsesTheLatestVerifiedPriceOfTheSameOfferInEachMonth() {
    var history = mock(MarketHistoryService.class);
    var tools = new AgentMarketTools(history, null, null, null);
    when(history.publicHistory(anyMap(), any(Instant.class)))
        .thenAnswer(
            call -> {
              Map<String, String> query = call.getArgument(0);
              boolean october = query.get("from").startsWith("2026-10-01");
              var points =
                  List.of(
                      Map.of("effectiveUsdGpuHour", october ? "10" : "9"),
                      Map.of("effectiveUsdGpuHour", october ? "8" : "6"));
              return Map.of(
                  "series",
                  List.of(
                      Map.of("offerId", "40", "providerName", "Nebius", "points", points),
                      Map.of(
                          "offerId",
                          "41",
                          "providerName",
                          "AWS",
                          "points",
                          List.of(Map.of("effectiveUsdGpuHour", "7")))),
                  "asOf",
                  call.getArgument(1, Instant.class));
            });
    var result =
        tools.monthlyTrend(
            Map.of("modelId", "4", "contractType", "SPOT"),
            LocalDate.parse("2026-10-01"),
            LocalDate.parse("2026-11-24"));
    assertThat(result)
        .containsEntry("kind", "MONTH_OVER_MONTH")
        .containsEntry("comparablePairs", 2)
        .containsEntry("previousTo", "2026-10-31")
        .containsEntry("currentFrom", "2026-11-01");
    var spot = MarketForecastService.records(result.get("items")).get(0);
    assertThat(MarketForecastService.records(spot.get("weeklyChanges")))
        .singleElement()
        .satisfies(
            change ->
                assertThat(change)
                    .containsEntry("difference", "-2")
                    .containsEntry("changePercent", "-25"));
    var comparisons = MarketForecastService.records(spot.get("priceComparisons"));
    assertThat(comparisons).hasSize(2);
    assertThat(comparisons.get(1))
        .containsEntry("offerId", "41")
        .containsEntry("changed", false)
        .containsEntry("difference", "0")
        .containsEntry("changePercent", "0");
  }
}
