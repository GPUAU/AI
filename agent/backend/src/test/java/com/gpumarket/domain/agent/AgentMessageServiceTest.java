package com.gpumarket.domain.agent;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoMoreInteractions;
import static org.mockito.Mockito.when;

import com.gpumarket.domain.agent.dto.AgentMessageRequestDto;
import com.gpumarket.domain.agent.dto.AgentMessageResponseDto;
import com.gpumarket.domain.agent.dto.PublicContextDto;
import com.gpumarket.domain.agent.service.AgentMarketTools;
import com.gpumarket.domain.agent.service.AgentMessageService;
import com.gpumarket.domain.agent.service.AgentMessageService.SensitiveInput;
import com.gpumarket.domain.agent.service.AgentSessionStore;
import com.gpumarket.domain.catalog.dto.CatalogResponseDto;
import com.gpumarket.domain.catalog.dto.CatalogResponseDto.GpuModelDto;
import com.gpumarket.domain.catalog.dto.CatalogResponseDto.ProviderDto;
import com.gpumarket.domain.catalog.service.CatalogService;
import com.gpumarket.domain.market.dto.PublicPriceLookupDto;
import com.gpumarket.domain.market.service.MarketCostImpactService;
import com.gpumarket.domain.market.service.MarketOfferService;
import java.time.DayOfWeek;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.temporal.TemporalAdjusters;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class AgentMessageServiceTest {
  private AgentMessageService service;
  private AgentMarketTools tools;
  private MarketOfferService offers;

  @BeforeEach
  void initialize() {
    var catalog = mock(CatalogService.class);
    offers = mock(MarketOfferService.class);
    var models =
        List.of(
            new GpuModelDto("1", "B300", null, null, null),
            new GpuModelDto("2", "GB300", null, null, null),
            new GpuModelDto("3", "GB300 NVL72", null, null, null),
            new GpuModelDto("4", "B200", null, null, null));
    when(catalog.getCatalog())
        .thenReturn(
            new CatalogResponseDto(
                List.of(new ProviderDto("7", "AWS", "GENERAL_CLOUD")),
                models,
                List.of(),
                List.of(),
                List.of(),
                List.of(),
                List.of(),
                List.of(),
                List.of()));
    var empty = new PublicPriceLookupDto(List.of(), Instant.EPOCH);
    when(offers.publicPrices(anyMap())).thenReturn(empty);
    tools = mock(AgentMarketTools.class);
    service = new AgentMessageService(catalog, offers, new AgentSessionStore(), tools);
  }

  @Test
  void looksUpStoredPricesBeforeTheKoreanCalendarDateAndRemembersTheCutoff() {
    var kst = ZoneId.of("Asia/Seoul");
    var date = LocalDate.now(kst);
    var boundary = date.atStartOfDay(kst).toInstant();
    var data = new PublicPriceLookupDto(List.of(), boundary.minusNanos(1000));
    when(offers.publicPricesBefore(anyMap(), any())).thenReturn(data);
    String dateText = date.getMonthValue() + "/" + date.getDayOfMonth() + "일 전";

    var first = answer(dateText + " B300 온디맨드 가격과 출처 알려줘");
    assertThat(first.status()).isEqualTo("NO_DATA");
    assertThat(first.history()).isEqualTo(Map.of("observedBefore", date.toString()));
    assertThat(first.answer()).contains(date + " 00:00 KST 이전");
    verify(offers)
        .publicPricesBefore(Map.of("modelId", "1", "contractType", "ON_DEMAND"), boundary);
    verify(offers, never()).publicPrices(anyMap());

    var followUp = service.answer(new AgentMessageRequestDto(first.sessionId(), "독일로", null));
    assertThat(followUp.retainedConditions()).contains("observedBefore");
    verify(offers)
        .publicPricesBefore(
            Map.of("modelId", "1", "contractType", "ON_DEMAND", "countryCode", "DE"), boundary);

    var current = service.answer(new AgentMessageRequestDto(first.sessionId(), "현재 가격 알려줘", null));
    assertThat(current.history()).isNull();
    assertThat(current.changedConditions()).contains("observedBefore");
    verify(offers)
        .publicPrices(Map.of("modelId", "1", "contractType", "ON_DEMAND", "countryCode", "DE"));
  }

  @Test
  void usesTheHistoricalSummaryAndClarifiesInvalidOrMultipleDates() {
    when(offers.summaryBefore(anyMap(), any()))
        .thenReturn(Map.of("groups", List.of(), "asOf", Instant.EPOCH));
    var result = answer("2024-11-19 이전 B300 온디맨드 최저가와 중앙값 비교");
    assertThat(result.history()).isEqualTo(Map.of("observedBefore", "2024-11-19"));
    verify(offers)
        .summaryBefore(
            Map.of("modelId", "1", "contractType", "ON_DEMAND"),
            Instant.parse("2024-11-18T15:00:00Z"));
    assertThat(answer("2024년 2월 30일 전 B300 가격").clarificationFields())
        .containsExactly("observedBefore");
    assertThat(answer("2024-11-19부터 2024-11-01 이전 B300 가격").clarificationFields())
        .containsExactly("scope");
    verify(offers, never()).publicPrices(anyMap());
    verify(offers, never()).summaryLatest(anyMap());
  }

  @Test
  void usesTheDashboardSummaryForCommitmentsAndKeepsConditionsOnAFollowUp() {
    var summary =
        Map.<String, Object>of("groups", List.of(Map.of("minimum", "3.20")), "asOf", Instant.EPOCH);
    when(offers.summaryLatest(anyMap())).thenReturn(summary);

    var first = answer("B300 us-east-1 8 GPU 1년 무선납 약정 최저가");
    assertThat(first.comparison()).isSameAs(summary);
    verify(offers)
        .summaryLatest(
            Map.of(
                "modelId",
                "1",
                "contractType",
                "COMMITMENT",
                "regionCode",
                "us-east-1",
                "gpuCount",
                "8",
                "commitmentMonths",
                "12",
                "paymentOption",
                "NO_UPFRONT"));
    var followUp = service.answer(new AgentMessageRequestDto(first.sessionId(), "3년으로", null));
    assertThat(followUp.changedConditions()).containsExactly("commitmentMonths");
    assertThat(followUp.retainedConditions()).contains("modelId", "regionCode", "paymentOption");
    verify(offers)
        .summaryLatest(
            Map.of(
                "modelId",
                "1",
                "contractType",
                "COMMITMENT",
                "regionCode",
                "us-east-1",
                "gpuCount",
                "8",
                "commitmentMonths",
                "36",
                "paymentOption",
                "NO_UPFRONT"));
    verify(offers, never()).publicPrices(anyMap());
  }

  @Test
  void acceptsTheKoreanDateParticleInTheOriginalQuestionAndKeepsCurrentPriceDistinct() {
    var kst = ZoneId.of("Asia/Seoul");
    var date = LocalDate.now(kst);
    var cutoff = date.atStartOfDay(kst).toInstant();
    var current =
        Map.<String, Object>of(
            "kind", "CONTRACT_COMPARISON", "items", List.of("aProduct"), "asOf", Instant.now());
    var past =
        Map.<String, Object>of(
            "kind", "CONTRACT_COMPARISON", "items", List.of("aProduct"), "asOf", cutoff);
    var filters = Map.of("modelId", "1", "contractType", "COMMITMENT", "commitmentMonths", "12");
    when(offers.productPricesForTerms(filters, List.of(12, 36), null)).thenReturn(current);
    when(offers.productPricesForTerms(filters, List.of(12, 36), cutoff)).thenReturn(past);

    String question = "B300의 온디맨드 가격을 1/3년 장기계약으로 환산하면 얼마야?";
    var first = answer(question);
    assertThat(first.status()).isEqualTo("ANSWERED");
    assertThat(first.answer()).contains("마지막 온디맨드");
    assertThat(first.history()).isNull();
    assertThat(first.analysis()).isSameAs(current);

    String dateText = date.getMonthValue() + "월 " + date.getDayOfMonth() + "일 전에 ";
    var second =
        service.answer(new AgentMessageRequestDto(first.sessionId(), dateText + question, null));
    assertThat(second.status()).isEqualTo("ANSWERED");
    assertThat(second.history()).containsEntry("observedBefore", date.toString());
    assertThat(second.analysis()).isSameAs(past);
  }

  @Test
  void appliesAnExactDateToTheContractComparisonWithTheRememberedModel() {
    var kst = ZoneId.of("Asia/Seoul");
    var date = LocalDate.now(kst).minusDays(1);
    var start = date.atStartOfDay(kst).toInstant();
    var end = date.plusDays(1).atStartOfDay(kst).toInstant();
    String inputDate = date.getMonthValue() + "/" + date.getDayOfMonth() + "일";
    when(offers.publicPricesBefore(anyMap(), any()))
        .thenReturn(new PublicPriceLookupDto(List.of(), start));
    var previous = answer(inputDate + " 이전 B200 온디맨드 가격 알려줘");
    var filters = Map.of("modelId", "4", "contractType", "COMMITMENT", "commitmentMonths", "12");
    when(offers.productPricesForTerms(filters, List.of(12, 36), start, end))
        .thenReturn(
            Map.of("kind", "CONTRACT_COMPARISON", "items", List.of("aProduct"), "asOf", end));
    var result =
        service.answer(
            new AgentMessageRequestDto(
                previous.sessionId(), inputDate + " 온디맨드 가격을 1/3년 장기계약으로 환산하면 얼마인가?", null));
    assertThat(result.status()).isEqualTo("ANSWERED");
    assertThat(result.history()).isEqualTo(Map.of("observedOn", date.toString()));
    assertThat(result.answer()).contains(date + " 당일(KST) B200").doesNotContain("48시간");
    assertThat(result.appliedConditions().modelId()).isEqualTo("4");
    verify(offers).productPricesForTerms(filters, List.of(12, 36), start, end);
  }

  @Test
  void remembersTheCutoffAndBothTermsWhileWaitingForTheModel() {
    var date = LocalDate.now(ZoneId.of("Asia/Seoul"));
    var boundary = date.atStartOfDay(ZoneId.of("Asia/Seoul")).toInstant();
    String cutoff = date.getMonthValue() + "/" + date.getDayOfMonth() + "일 이전으로";
    var previous = answer("온디맨드 가격을 1/3년 장기계약으로 환산하면 얼마인가? " + cutoff + " 알려줘");
    assertThat(previous.clarificationFields()).containsExactly("modelId");
    var summary =
        Map.<String, Object>of(
            "kind", "CONTRACT_COMPARISON", "items", List.of("aProduct"), "asOf", boundary);
    when(offers.productPricesForTerms(anyMap(), any(), any())).thenReturn(summary);

    var result = service.answer(new AgentMessageRequestDto(previous.sessionId(), "B300", null));
    assertThat(result.analysis()).isSameAs(summary);
    assertThat(result.clarificationFields()).isEmpty();
    assertThat(result.answer()).contains("상품별 마지막 온디맨드·약정");
    assertThat(result.history()).isEqualTo(Map.of("observedBefore", date.toString()));
    verify(offers)
        .productPricesForTerms(
            Map.of("modelId", "1", "contractType", "COMMITMENT", "commitmentMonths", "12"),
            List.of(12, 36),
            boundary);
  }

  @Test
  void waitsForAContractTypeAndDoesNotRouteFuturePricesToCurrentQuotes() {
    var summary = Map.<String, Object>of("groups", List.of(), "asOf", Instant.EPOCH);
    when(offers.summaryLatest(anyMap())).thenReturn(summary);
    var first = answer("B300 최저가와 중앙값 비교");
    assertThat(first.clarificationFields()).containsExactly("contractType");
    verify(offers, never()).summaryLatest(anyMap());

    var next = service.answer(new AgentMessageRequestDto(first.sessionId(), "온디맨드", null));
    assertThat(next.comparison()).isSameAs(summary);
    verify(offers).summaryLatest(Map.of("modelId", "1", "contractType", "ON_DEMAND"));
    assertThat(answer("1년 후 B300 가격").clarificationFields()).containsExactly("contractType");
    assertThat(answer("B300 1년 가격").clarificationFields()).containsExactly("intent");
    verify(offers, never()).publicPrices(anyMap());
  }

  @Test
  void readsFullModelNameAndRecognizesSeparatelyMentionedModels() {
    assertThat(answer("GB300 NVL72 온디맨드 가격").appliedConditions().modelId()).isEqualTo("3");
    assertThat(answer("GB300 NVL72와 GB300 온디맨드 가격").clarificationFields())
        .containsExactly("modelId");
  }

  @Test
  void recognizesCountryNamesWithoutTreatingPronounsAsCountries() {
    assertThat(
            answer("Can you tell us the B300 on-demand price?").appliedConditions().countryCode())
        .isNull();
    assertThat(answer("B300 on-demand price in Germany").appliedConditions().countryCode())
        .isEqualTo("DE");
    var us = new PublicContextDto(null, null, "US", null, null, null, null, null, null);
    var conflicting = service.answer(new AgentMessageRequestDto(null, "B300 독일 온디맨드 가격", us));
    assertThat(conflicting.clarificationFields()).containsExactly("countryCode");
  }

  @ParameterizedTest
  @ValueSource(
      strings = {
        "What will the B300 on-demand price be tomorrow?",
        "B300 가격을 유로로 알려줘",
        "2025년 11월 B300 온디맨드 가격"
      })
  void clarifiesUnsupportedTimeOrCurrency(String message) {
    assertThat(answer(message).status()).isEqualTo("NEEDS_CLARIFICATION");
  }

  @Test
  void treatsPerOneGpuAsAUnitInsteadOfAnOfferConfiguration() {
    assertThat(answer("B300 on-demand price per 1 GPU").appliedConditions().gpuCount()).isNull();
  }

  @Test
  void followUpChangesOnlyTheCountryAndKeepsTheModelAndContract() {
    var first = answer("B300 미국 온디맨드 가격");

    var next = service.answer(new AgentMessageRequestDto(first.sessionId(), "그럼 독일은요?", null));

    assertThat(next.appliedConditions().countryCode()).isEqualTo("DE");
    assertThat(next.retainedConditions()).containsExactly("modelId", "contractType");
    assertThat(next.changedConditions()).containsExactly("countryCode");
  }

  @Test
  void modelOnlyReplyFinishesClarificationWithTheCountryFromTheFirstTurn() {
    var first = answer("미국 가격 알려줘");
    assertThat(first.clarificationFields()).containsExactly("modelId");
    assertThat(first.assumptions()).anyMatch(value -> value.contains("온디맨드"));

    var next = service.answer(new AgentMessageRequestDto(first.sessionId(), "B300", null));

    assertThat(next.sampleStatus()).isEqualTo("QUERIED");
    assertThat(next.assumptions()).anyMatch(value -> value.contains("온디맨드"));
    assertThat(next.retainedConditions()).contains("countryCode");
    verify(offers)
        .publicPrices(Map.of("modelId", "1", "contractType", "ON_DEMAND", "countryCode", "US"));
  }

  @ParameterizedTest
  @ValueSource(strings = {"그중 GPU 특화 사업자만", "GPU 특화 사업자 가격은?"})
  void followUpChangesProviderTypeAndDropsAnIncompatibleNamedProvider(String message) {
    var first = answer("B300 AWS 미국 온디맨드 가격");

    var next = service.answer(new AgentMessageRequestDto(first.sessionId(), message, null));

    assertThat(next.changedConditions()).contains("providerType", "providerIds");
    verify(offers)
        .publicPrices(
            Map.of(
                "modelId",
                "1",
                "contractType",
                "ON_DEMAND",
                "countryCode",
                "US",
                "providerType",
                "GPU_SPECIALIST"));
  }

  @Test
  void explicitCompatibleProviderTypeDoesNotDropTheNamedProvider() {
    var first = answer("B300 AWS 미국 온디맨드 가격");
    var context =
        new PublicContextDto(null, null, null, null, null, "GENERAL_CLOUD", null, null, null);

    var next = service.answer(new AgentMessageRequestDto(first.sessionId(), "이 조건으로요", context));

    assertThat(next.retainedConditions()).contains("providerIds");
    verify(offers)
        .publicPrices(
            Map.of(
                "modelId",
                "1",
                "contractType",
                "ON_DEMAND",
                "countryCode",
                "US",
                "providerType",
                "GENERAL_CLOUD",
                "providerIds",
                "7"));
  }

  @Test
  void naturalTypicalPriceAndTimingQuestionKeepTheSameModelAndMonth() {
    var today = LocalDate.now(ZoneId.of("Asia/Seoul"));
    var end = today.withDayOfMonth(1);
    var start = end.minusMonths(1);
    var filters = Map.of("modelId", "4");
    when(tools.typicalPrices(filters, start, end))
        .thenReturn(
            Map.of(
                "kind", "TYPICAL_PRICE", "items", List.of("aDailyMedian"), "asOf", Instant.now()));
    when(tools.trendTiming(filters, start, end))
        .thenReturn(
            Map.of(
                "kind",
                "TREND_TIMING",
                "items",
                List.of(),
                "comparablePairs",
                1,
                "asOf",
                Instant.now()));
    var first = answer("저번달에 B200은 보통 얼마였어?");
    assertThat(first.status()).isEqualTo("ANSWERED");
    assertThat(first.clarificationFields()).isEmpty();
    assertThat(first.analysis()).containsEntry("kind", "TYPICAL_PRICE");

    var next =
        service.answer(new AgentMessageRequestDto(first.sessionId(), "언제부터 오르거나 내렸어?", null));
    assertThat(next.status()).isEqualTo("ANSWERED");
    assertThat(next.analysis()).containsEntry("kind", "TREND_TIMING");
    assertThat(next.retainedConditions()).contains("modelId", "period");
    verify(tools).trendTiming(filters, start, end);
  }

  @Test
  void monthlyComparisonPreservesModelAndCalendarDatesForAContractOnlyFollowUp() {
    var today = LocalDate.now(ZoneId.of("Asia/Seoul"));
    var start = today.withDayOfMonth(1).minusMonths(1);
    var end = today.plusDays(1);
    var result =
        Map.<String, Object>of(
            "kind",
            "MONTH_OVER_MONTH",
            "items",
            List.of(),
            "comparablePairs",
            1,
            "asOf",
            Instant.now());
    when(tools.monthlyTrend(anyMap(), any(), any())).thenReturn(result);

    var first = answer("지난달 대비 B200의 가격 변동은?");
    assertThat(first.status()).isEqualTo("ANSWERED");
    assertThat(first.clarificationFields()).isEmpty();
    verify(tools).monthlyTrend(Map.of("modelId", "4"), start, end);

    var followup = service.answer(new AgentMessageRequestDto(first.sessionId(), "온디맨드", null));
    assertThat(followup.clarificationFields()).isEmpty();
    assertThat(followup.appliedConditions().modelId()).isEqualTo("4");
    assertThat(followup.retainedConditions()).contains("modelId", "period");
    verify(tools).monthlyTrend(Map.of("modelId", "4", "contractType", "ON_DEMAND"), start, end);
  }

  @Test
  void contractOnlyFollowUpContinuesAConversationWaitingForTheContract() {
    var first = answer("B200 가격 추이");
    assertThat(first.clarificationFields()).containsExactly("contractType");
    var followup = service.answer(new AgentMessageRequestDto(first.sessionId(), "온디맨드", null));
    assertThat(followup.clarificationFields()).containsExactly("period");
    assertThat(followup.appliedConditions().modelId()).isEqualTo("4");
  }

  @Test
  void weekOverWeekDoesNotInheritAnImplicitOnDemandDefaultFromTheCheapestProviderQuestion() {
    var kst = ZoneId.of("Asia/Seoul");
    var today = LocalDate.now(kst);
    var monday = today.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY));
    when(offers.publicMinimumPrice(Map.of("modelId", "4", "contractType", "ON_DEMAND"), null))
        .thenReturn(Map.of("kind", "PRICE_MINIMUM", "items", List.of(), "asOf", Instant.now()));
    var previous = answer("B200을 가장 저렴하게 제공하는 사업자는?");
    var weekly =
        Map.<String, Object>of(
            "kind",
            "WEEK_OVER_WEEK",
            "items",
            List.of(),
            "comparablePairs",
            0,
            "asOf",
            Instant.now());
    when(tools.weeklyTrend(Map.of("modelId", "4"), monday.minusWeeks(1), today.plusDays(1)))
        .thenReturn(weekly);
    var result =
        service.answer(
            new AgentMessageRequestDto(previous.sessionId(), "지난주 대비 B200의 가격변동은?", null));
    assertThat(result.status()).isEqualTo("NO_DATA");
    assertThat(result.answer()).contains("주간 변동률을 계산할 수 없습니다.");
    assertThat(result.analysis()).isSameAs(weekly);
    assertThat(result.appliedConditions().contractType()).isNull();
    verify(tools).weeklyTrend(Map.of("modelId", "4"), monday.minusWeeks(1), today.plusDays(1));
  }

  @Test
  void asksTheSpecificKoreanDayWithoutReusingThePreviousCutoffOrFreshnessWindow() {
    var kst = ZoneId.of("Asia/Seoul");
    var date = LocalDate.now(kst).minusDays(1);
    var start = date.atStartOfDay(kst).toInstant();
    var end = date.plusDays(1).atStartOfDay(kst).toInstant();
    String dateText = date.getMonthValue() + "/" + date.getDayOfMonth() + "일";
    var filters = Map.of("modelId", "4", "contractType", "ON_DEMAND");
    when(offers.publicMinimumPrice(filters, start))
        .thenReturn(
            Map.of("kind", "PRICE_MINIMUM", "items", List.of("historical minimum"), "asOf", start));
    when(offers.publicMinimumPrice(filters, start, end))
        .thenReturn(Map.of("kind", "PRICE_MINIMUM", "items", List.of(), "asOf", end));
    String question = " B200을 가장 저렴하게 제공하는 사업자는?";
    var before = answer(dateText + " 이전" + question);
    var on =
        service.answer(
            new AgentMessageRequestDto(before.sessionId(), dateText + "에" + question, null));
    assertThat(on.status()).isEqualTo("NO_DATA");
    assertThat(on.answer()).contains(date + " 당일(KST)").doesNotContain("48시간");
    assertThat(on.history()).isEqualTo(Map.of("observedOn", date.toString()));
    assertThat(on.analysis()).doesNotContainKey("historicalReference");
    verify(offers).publicMinimumPrice(filters, start);
    verify(offers).publicMinimumPrice(filters, start, end);
    verifyNoMoreInteractions(offers);
  }

  @ParameterizedTest
  @ValueSource(strings = {"B200을 가장 저렴하게 제공하는 사업자는?", "B200 최저가 알려줘"})
  void recognizesTheCheapestProviderWithoutReaskingForB200(String question) {
    var filters = Map.of("modelId", "4", "contractType", "ON_DEMAND");
    when(offers.publicMinimumPrice(filters, null))
        .thenReturn(
            Map.of("kind", "PRICE_MINIMUM", "items", List.of("aMinimum"), "asOf", Instant.now()));
    var result = answer(question);
    assertThat(result.status()).isEqualTo("ANSWERED");
    assertThat(result.clarificationFields()).isEmpty();
    assertThat(result.answer()).contains("마지막 공개가격 중 최저가");
    assertThat(result.disclaimer()).doesNotContain("3~5년 약정");
    verify(offers).publicMinimumPrice(filters, null);
    verifyNoMoreInteractions(offers);
  }

  @Test
  void routesNaturalLanguageRangeQuestionsToAllEligibleOffersInsteadOfTheRecentList() {
    var date = LocalDate.now(ZoneId.of("Asia/Seoul"));
    var cutoff = date.atStartOfDay(ZoneId.of("Asia/Seoul")).toInstant();
    var filters = Map.of("modelId", "1", "contractType", "ON_DEMAND");
    when(offers.publicPriceRange(filters, cutoff))
        .thenReturn(
            Map.of("kind", "PRICE_RANGE", "items", List.of("minimum", "maximum"), "asOf", cutoff));
    String dateText = date.getMonthValue() + "/" + date.getDayOfMonth() + " 이전 ";

    var result = answer(dateText + "B300의 가격 분포는 얼마에서 얼마까지인가?");
    assertThat(result.status()).isEqualTo("ANSWERED");
    assertThat(result.answer()).contains("온디맨드", "최저가와 최고가");
    assertThat(result.history()).containsEntry("observedBefore", date.toString());
    assertThat(result.analysis()).containsEntry("kind", "PRICE_RANGE");
    assertThat(result.results()).isEmpty();
    verify(offers).publicPriceRange(filters, cutoff);
    verifyNoMoreInteractions(offers);
  }

  @Test
  void mentoringQuestionsUseLastCalendarWeekAcrossModelsAndRecognizeAProposal() {
    var kst = ZoneId.of("Asia/Seoul");
    var monday = LocalDate.now(kst).with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY));
    var from = monday.minusWeeks(1).atStartOfDay(kst).toInstant();
    var until = monday.atStartOfDay(kst).toInstant();
    when(offers.confirmedPriceRemovals(Map.of(), from, until)).thenReturn(List.of());
    var lastWeek = answer("지난주 가격표에서 사라진 상품이 있나?");
    assertThat(lastWeek.status()).isEqualTo("NO_DATA");
    assertThat(lastWeek.analysis()).containsEntry("kind", "PRICE_REMOVAL");
    assertThat(lastWeek.analysis()).containsEntry("dateFrom", monday.minusWeeks(1).toString());
    assertThat(lastWeek.clarificationFields()).isEmpty();
    verify(offers).confirmedPriceRemovals(Map.of(), from, until);

    var proposal =
        service.answer(
            new AgentMessageRequestDto(
                lastWeek.sessionId(), "B200의 우리의 적절한 고객 제안가를 이유와 함께 알려줘", null));
    assertThat(proposal.analysis())
        .containsEntry("kind", "INTERNAL_PROPOSAL")
        .containsEntry("modelId", "4")
        .containsEntry("modelName", "B200");
    assertThat(proposal.sampleStatus()).isEqualTo("NOT_QUERIED");
    assertThat(proposal.history()).isNull();
    assertThat(proposal.answer()).contains("목표 마진");
    verifyNoMoreInteractions(offers, tools);
  }

  @Test
  void costLagReplacesHistoricalContractContextAndKeepsModelFollowUpOnTopic() {
    var date = LocalDate.now(ZoneId.of("Asia/Seoul"));
    var cutoff = date.atStartOfDay(ZoneId.of("Asia/Seoul")).toInstant();
    var filters = Map.of("modelId", "1", "contractType", "COMMITMENT", "commitmentMonths", "12");
    when(offers.productPricesForTerms(filters, List.of(12, 36), cutoff))
        .thenReturn(
            Map.of("kind", "CONTRACT_COMPARISON", "items", List.of("aProduct"), "asOf", cutoff));
    String dateText = date.getMonthValue() + "월 " + date.getDayOfMonth() + "일 전에 ";
    var price = answer(dateText + "B300의 온디맨드 가격을 1/3년 장기계약으로 환산하면 얼마야?");
    assertThat(price.status()).isEqualTo("ANSWERED");
    assertThat(price.history()).containsEntry("observedBefore", date.toString());

    when(tools.costImpact(any()))
        .thenReturn(
            Map.of(
                "kind",
                "COST_IMPACT",
                "status",
                "INSUFFICIENT_EVIDENCE",
                "items",
                List.of(),
                "asOf",
                cutoff));
    var lag =
        service.answer(
            new AgentMessageRequestDto(
                price.sessionId(), "전기료/메모리 가격이 오른 뒤 GPU 임대가가 몇 주 뒤에 움직였나?", null));
    var model = service.answer(new AgentMessageRequestDto(price.sessionId(), "B200", null));
    for (var result : List.of(lag, model)) {
      assertThat(result.status()).isEqualTo("NO_DATA");
      assertThat(result.analysis()).containsEntry("status", "INSUFFICIENT_EVIDENCE");
      assertThat(result.answer()).contains("근거가 부족");
      assertThat(result.clarificationFields()).isEmpty();
      assertThat(result.history()).isNull();
      assertThat(result.comparison()).isNull();
      assertThat(result.sampleStatus()).isEqualTo("QUERIED");
    }
    verify(offers).productPricesForTerms(filters, List.of(12, 36), cutoff);
    var queries = org.mockito.ArgumentCaptor.forClass(MarketCostImpactService.Query.class);
    verify(tools, org.mockito.Mockito.times(2)).costImpact(queries.capture());
    assertThat(queries.getAllValues().get(0).factors())
        .containsExactly(MarketCostImpactService.Factor.POWER, MarketCostImpactService.Factor.DRAM);
    assertThat(queries.getAllValues().get(1).models())
        .extracting(GpuModelDto::name)
        .containsExactly("B200");
    assertThat(queries.getAllValues().get(1).contract()).isNull();
    verifyNoMoreInteractions(offers, tools);

    var nextPrice =
        service.answer(new AgentMessageRequestDto(price.sessionId(), "B200 온디맨드 가격 알려줘", null));
    assertThat(nextPrice.appliedConditions().modelId()).isEqualTo("4");
    assertThat(nextPrice.history()).isNull();
    verify(offers).publicPrices(Map.of("modelId", "4", "contractType", "ON_DEMAND"));
  }

  @Test
  void unrelatedQuestionWithACountryDoesNotQueryOrOverwriteSessionConditions() {
    var first = answer("B300 독일 온디맨드 가격");

    var unrelated =
        service.answer(new AgentMessageRequestDto(first.sessionId(), "미국 수도는 어디야?", null));

    assertThat(unrelated.sampleStatus()).isEqualTo("NOT_QUERIED");
    verify(offers)
        .publicPrices(Map.of("modelId", "1", "contractType", "ON_DEMAND", "countryCode", "DE"));
    verifyNoMoreInteractions(offers);
    var next = service.answer(new AgentMessageRequestDto(first.sessionId(), "가격 다시 알려줘", null));
    assertThat(next.appliedConditions().countryCode()).isEqualTo("DE");
  }

  @Test
  void blocksInternalPurchasePriceQuestion() {
    assertThatThrownBy(() -> answer("B300 internal purchase price"))
        .isInstanceOf(SensitiveInput.class);
  }

  private AgentMessageResponseDto answer(String message) {
    return service.answer(new AgentMessageRequestDto(null, message, null));
  }
}
