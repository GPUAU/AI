package com.gpumarket.domain.agent.service;

import java.time.DateTimeException;
import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.temporal.ChronoUnit;
import java.time.temporal.TemporalAdjusters;
import java.util.regex.MatchResult;
import java.util.regex.Pattern;

record AgentQueryScope(Kind kind, LocalDate from, LocalDate to, String offerId) {
  enum Kind {
    PRICE,
    PRICE_RANGE,
    PRICE_MINIMUM,
    CONTRACT_COMPARISON,
    COST_IMPACT,
    PRICE_REMOVAL,
    PROPOSAL,
    TREND,
    TREND_TIMING,
    TYPICAL_PRICE,
    WEEK_OVER_WEEK,
    MONTH_OVER_MONTH,
    DISCOUNT,
    AVAILABILITY,
    FORECAST;

    boolean isPriceExtrema() {
      return this == PRICE_RANGE || this == PRICE_MINIMUM;
    }

    boolean supportsExactPriceDate() {
      return isPriceExtrema() || this == CONTRACT_COMPARISON || this == DISCOUNT;
    }

    boolean isPeriodComparison() {
      return this == WEEK_OVER_WEEK || this == MONTH_OVER_MONTH;
    }

    boolean defaultsToOnDemandAndSpot() {
      return isPeriodComparison() || this == TYPICAL_PRICE || this == TREND_TIMING;
    }
  }

  private static final String DATE =
      "(?:([0-9]{4})\\s*(?:[./-]|년)\\s*)?([0-9]{1,2})\\s*(?:[./-]|월)\\s*([0-9]{1,2})\\s*(?:일)?";
  private static final Pattern RANGE =
      Pattern.compile("(?<![0-9./-])" + DATE + "\\s*(?:부터|[~–〜])\\s*" + DATE + "\\s*(이전|전|까지)?");
  private static final Pattern RECENT = Pattern.compile("최근\\s*([0-9]{1,3})\\s*일(?:간)?");
  private static final Pattern RELATIVE = Pattern.compile("(?:지난|저번)\\s*(?:주|달|개월)|최근\\s*한\\s*달");
  private static final Pattern TREND =
      Pattern.compile("이력|추이|변동|변화|떨어|내렸|올랐|그래프|timeline|history|trend");
  private static final Pattern WEEK_OVER_WEEK = Pattern.compile("지난\\s*주\\s*(?:대비|에\\s*비해)");
  private static final Pattern MONTH_OVER_MONTH =
      Pattern.compile("(?:지난\\s*달|전월|저번\\s*달)\\s*(?:대비|에\\s*비해)");
  private static final Pattern TYPICAL_PRICE =
      Pattern.compile("보통.{0,24}(?:얼마|가격|단가|시세)|대표\\s*(?:가격|단가)|월간\\s*중앙값");
  private static final Pattern MONTH_NAME =
      Pattern.compile(
          "(?:지난|저번|이번|그)\\s*달|전월|(?<![0-9])(?:(20[0-9]{2})\\s*년\\s*)?([0-9]{1,2})\\s*월(?!\\s*[0-9]{1,2}\\s*일)");
  private static final Pattern CHANGE_TIMING = Pattern.compile("언제\\s*부터|(?:상승|하락|변동)\\s*시점");
  private static final Pattern PRICE_MOVEMENT =
      Pattern.compile("오르|올랐|올라|내리|내렸|내려|떨어|상승|하락|변동|변화|바뀌|바뀐");
  private static final Pattern DISCOUNT = Pattern.compile("할인|discount");
  private static final Pattern STATUS =
      Pattern.compile("미공개|미확보|공개\\s*(?:여부|안|했|됐|되었|상태)|수집\\s*(?:상태|실패)|가격\\s*(?:이|가)?\\s*없");
  private static final Pattern FORECAST = Pattern.compile("예측|전망|미래|내년|forecast|predict");
  private static final Pattern HORIZON =
      Pattern.compile("(?<![0-9])([0-9]{1,3})\\s*(년|개월)\\s*(?:후|뒤)");
  private static final Pattern OFFER =
      Pattern.compile("(?:오퍼|\\boffer)\\s*(?:id\\s*)?#?([1-9][0-9]{0,18})(?![0-9])");
  private static final Pattern DISCOUNT_BASELINE =
      Pattern.compile("(?:온\\s*디[맨멘]드|on[ _-]*demand)\\s*(?:대비|보다|기준)");
  private static final Pattern COST_FACTOR =
      Pattern.compile(
          "전기\\s*(?:료|요금|단가|가격)|전력\\s*(?:비|요금|단가|가격)"
              + "|(?:메모리|hbm|dram)\\s*(?:가격|비용|단가|값)"
              + "|\\b(?:electricity|power|memory|dram|hbm)\\s+(?:prices?|costs?|rates?)\\b");
  private static final Pattern GPU_MARKET =
      Pattern.compile("\\bgpu\\b|임대\\s*(?:가|료)|온\\s*디[맨멘]드|\\brental\\s+(?:prices?|rates?)\\b");
  private static final Pattern COST_RELATIONSHIP =
      Pattern.compile(
          "시차|영향|상관|연관|관계|반영|원인|때문|몇\\s*(?:일|주|달|개월)"
              + "|(?:오른|내린|변한|바뀐|변동한|급등한|급락한)\\s*(?:이후|뒤|후)"
              + "|\\b(?:lag|correlation|impact|affect|how long)\\b");
  private static final Pattern PRICE_REMOVAL =
      Pattern.compile(
          "(?=.*(?:가격표|가격\\s*목록|공개\\s*가격|공개가|요금표))" + "(?=.*(?:사라졌|사라진|빠진|빠졌|철회|삭제|제외|빠져|없어진|없어졌))",
          Pattern.DOTALL);
  private static final Pattern PROPOSAL = Pattern.compile("제안\\s*(?:가격|가)(?![액])");
  private static final Pattern PRICE_RANGE_QUESTION =
      Pattern.compile("얼마\\s*(?:에서|부터)\\s*얼마\\s*까지");
  private static final Pattern PRICE_RANGE_WORDING =
      Pattern.compile(
          "(?:가격|단가|시세)\\s*(?:분포|범위)|"
              + PRICE_RANGE_QUESTION.pattern()
              + "|(?:최저.{0,20}최고|최고.{0,20}최저)");
  private static final Pattern OTHER_RANGE_STATISTICS = Pattern.compile("중앙값|평균|사업자별|조건별");
  private static final Pattern MINIMUM_WORDING =
      Pattern.compile(
          "(?:가장|제일)\\s*(?:저렴|싸|싼|낮은\\s*(?:가격|단가))|\\bcheapest\\b"
              + "|최저(?:가|\\s*단가).{0,24}(?:사업자|업체|공급사|제공사|어디)"
              + "|(?:사업자|업체|공급사|제공사).{0,24}최저(?:가|\\s*단가)");
  private static final Pattern MINIMUM_PRICE = Pattern.compile("최저\\s*(?:가|단가)");
  private static final Pattern CONTRACT_PRICE =
      Pattern.compile("약정|장기\\s*계약|\\b(?:commitment|reserved)\\b");

  static AgentQueryScope price() {
    return new AgentQueryScope(Kind.PRICE, null, null, null);
  }

  static AgentQueryScope parse(String question, LocalDate today) {
    if (COST_FACTOR.matcher(question).find()
        && GPU_MARKET.matcher(question).find()
        && COST_RELATIONSHIP.matcher(question).find()) {
      return new AgentQueryScope(Kind.COST_IMPACT, null, null, null);
    }
    if (PROPOSAL.matcher(question).find()) {
      return new AgentQueryScope(Kind.PROPOSAL, null, null, null);
    }
    boolean removal = PRICE_REMOVAL.matcher(question).find();
    var offer = OFFER.matcher(question).results().toList();
    if (offer.size() > 1) throw new IllegalArgumentException("오퍼 ID 한 가지를 알려 주세요.");
    String offerId = offer.isEmpty() ? null : offer.get(0).group(1);
    var month = MONTH_NAME.matcher(question);
    boolean namesMonth = month.find();
    boolean typical =
        TYPICAL_PRICE.matcher(question).find() || (namesMonth && question.contains("중앙값"));
    boolean timing =
        CHANGE_TIMING.matcher(question).find() && PRICE_MOVEMENT.matcher(question).find();
    var range = RANGE.matcher(question).results().toList();
    if (range.size() > 1) throw new IllegalArgumentException("조회할 기간 한 가지를 알려 주세요.");
    if (!range.isEmpty()) {
      var match = range.get(0);
      LocalDate start = date(match, 1, today.getYear());
      LocalDate end = date(match, 4, start.getYear());
      if (!"이전".equals(match.group(7)) && !"전".equals(match.group(7))) end = end.plusDays(1);
      var period = trend(start, end, today, offerId);
      if (removal) return new AgentQueryScope(Kind.PRICE_REMOVAL, start, end, null);
      if (typical) return new AgentQueryScope(Kind.TYPICAL_PRICE, start, end, offerId);
      return timing ? new AgentQueryScope(Kind.TREND_TIMING, start, end, offerId) : period;
    }
    if (removal) {
      var recent = RECENT.matcher(question);
      if (recent.find()) {
        var from = today.plusDays(1L - Integer.parseInt(recent.group(1)));
        var until = today.plusDays(1);
        trend(from, until, today, null);
        return new AgentQueryScope(Kind.PRICE_REMOVAL, from, until, null);
      }
      var week = today.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY));
      if (question.contains("오늘"))
        return new AgentQueryScope(Kind.PRICE_REMOVAL, today, today.plusDays(1), null);
      if (question.contains("어제"))
        return new AgentQueryScope(Kind.PRICE_REMOVAL, today.minusDays(1), today, null);
      if (question.contains("이번 주") || question.contains("이번주")) {
        return new AgentQueryScope(Kind.PRICE_REMOVAL, week, today.plusDays(1), null);
      }
      return new AgentQueryScope(Kind.PRICE_REMOVAL, week.minusWeeks(1), week, null);
    }
    var horizon = HORIZON.matcher(question);
    if (horizon.find()) {
      int months = Integer.parseInt(horizon.group(1)) * ("년".equals(horizon.group(2)) ? 12 : 1);
      if (months != 12) throw new IllegalArgumentException("미래가격은 12개월 후 전망을 조회할 수 있어요.");
      return new AgentQueryScope(Kind.FORECAST, null, null, offerId);
    }
    if (FORECAST.matcher(question).find())
      return new AgentQueryScope(Kind.FORECAST, null, null, offerId);
    if (DISCOUNT.matcher(question).find())
      return new AgentQueryScope(Kind.DISCOUNT, null, null, offerId);
    if (STATUS.matcher(question).find())
      return new AgentQueryScope(Kind.AVAILABILITY, null, null, offerId);
    if (WEEK_OVER_WEEK.matcher(question).find()) {
      var monday = today.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY));
      return new AgentQueryScope(
          Kind.WEEK_OVER_WEEK, monday.minusWeeks(1), today.plusDays(1), offerId);
    }
    if (MONTH_OVER_MONTH.matcher(question).find()) {
      return new AgentQueryScope(
          Kind.MONTH_OVER_MONTH,
          today.withDayOfMonth(1).minusMonths(1),
          today.plusDays(1),
          offerId);
    }
    if ((typical || timing) && namesMonth) {
      var start = monthStart(month.toMatchResult(), today);
      if (start == null)
        return new AgentQueryScope(
            typical ? Kind.TYPICAL_PRICE : Kind.TREND_TIMING, null, null, offerId);
      if (start.isAfter(today)) throw new IllegalArgumentException("관측을 확인할 과거 월을 알려 주세요.");
      var end =
          start.getMonth() == today.getMonth() && start.getYear() == today.getYear()
              ? today.plusDays(1)
              : start.plusMonths(1);
      return new AgentQueryScope(
          typical ? Kind.TYPICAL_PRICE : Kind.TREND_TIMING, start, end, offerId);
    }
    if (typical) {
      var start = today.withDayOfMonth(1).minusMonths(1);
      return new AgentQueryScope(Kind.TYPICAL_PRICE, start, start.plusMonths(1), offerId);
    }
    var recent = RECENT.matcher(question);
    var relative = RELATIVE.matcher(question);
    if (recent.find()) {
      int days = Integer.parseInt(recent.group(1));
      if (timing && days > 0)
        return new AgentQueryScope(
            Kind.TREND_TIMING, today.plusDays(1L - days), today.plusDays(1), offerId);
      return trend(today.plusDays(1L - days), today.plusDays(1), today, offerId);
    }
    if (relative.find()) {
      var previousMonth = today.withDayOfMonth(1).minusMonths(1);
      if (timing) {
        var period =
            relative.group().contains("주")
                ? today.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY)).minusWeeks(1)
                : previousMonth;
        return new AgentQueryScope(
            Kind.TREND_TIMING,
            period,
            relative.group().contains("주") ? period.plusWeeks(1) : period.plusMonths(1),
            offerId);
      }
      return relative.group().contains("주")
          ? trend(
              today.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY)).minusWeeks(1),
              today.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY)),
              today,
              offerId)
          : trend(previousMonth, previousMonth.plusMonths(1), today, offerId);
    }
    if (timing) return new AgentQueryScope(Kind.TREND_TIMING, null, null, offerId);
    if (TREND.matcher(question).find()) return new AgentQueryScope(Kind.TREND, null, null, offerId);
    if (!OTHER_RANGE_STATISTICS.matcher(question).find()) {
      if (PRICE_RANGE_WORDING.matcher(question).find()) {
        return new AgentQueryScope(Kind.PRICE_RANGE, null, null, offerId);
      }
      if (MINIMUM_WORDING.matcher(question).find()
          || (MINIMUM_PRICE.matcher(question).find() && !CONTRACT_PRICE.matcher(question).find())) {
        return new AgentQueryScope(Kind.PRICE_MINIMUM, null, null, offerId);
      }
    }
    return new AgentQueryScope(Kind.PRICE, null, null, offerId);
  }

  private static AgentQueryScope trend(
      LocalDate from, LocalDate to, LocalDate today, String offerId) {
    if (!from.isBefore(to)
        || to.isAfter(today.plusDays(1))
        || ChronoUnit.DAYS.between(from, to) > 730) {
      throw new IllegalArgumentException("기간은 시작일보다 종료일이 뒤여야 하며 최대 730일입니다.");
    }
    return new AgentQueryScope(Kind.TREND, from, to, offerId);
  }

  private static LocalDate date(MatchResult match, int first, int year) {
    try {
      return LocalDate.of(
          match.group(first) == null ? year : Integer.parseInt(match.group(first)),
          Integer.parseInt(match.group(first + 1)),
          Integer.parseInt(match.group(first + 2)));
    } catch (DateTimeException e) {
      throw new IllegalArgumentException("실제 달력 날짜를 알려 주세요.");
    }
  }

  private static LocalDate monthStart(MatchResult match, LocalDate today) {
    if (match.group(2) != null) {
      try {
        var year = match.group(1) == null ? today.getYear() : Integer.parseInt(match.group(1));
        return LocalDate.of(year, Integer.parseInt(match.group(2)), 1);
      } catch (DateTimeException e) {
        throw new IllegalArgumentException("실제 달력 월을 알려 주세요.");
      }
    }
    if (match.group().startsWith("그")) return null;
    return today.withDayOfMonth(1).minusMonths(match.group().startsWith("이번") ? 0 : 1);
  }

  String withoutIntent(String text) {
    String remaining = OFFER.matcher(text).replaceAll(" ");
    return switch (kind) {
      case PRICE, PRICE_MINIMUM, AVAILABILITY, COST_IMPACT, PROPOSAL, PRICE_REMOVAL -> remaining;
      case PRICE_RANGE -> PRICE_RANGE_QUESTION.matcher(remaining).replaceAll(" ");
      case WEEK_OVER_WEEK ->
          TREND.matcher(WEEK_OVER_WEEK.matcher(remaining).replaceAll(" ")).replaceAll(" ");
      case MONTH_OVER_MONTH ->
          TREND.matcher(MONTH_OVER_MONTH.matcher(remaining).replaceAll(" ")).replaceAll(" ");
      case TYPICAL_PRICE ->
          MONTH_NAME.matcher(RANGE.matcher(remaining).replaceAll(" ")).replaceAll(" ");
      case TREND_TIMING ->
          PRICE_MOVEMENT
              .matcher(
                  CHANGE_TIMING
                      .matcher(
                          RELATIVE
                              .matcher(
                                  RECENT
                                      .matcher(
                                          MONTH_NAME
                                              .matcher(RANGE.matcher(remaining).replaceAll(" "))
                                              .replaceAll(" "))
                                      .replaceAll(" "))
                              .replaceAll(" "))
                      .replaceAll(" "))
              .replaceAll(" ");
      case TREND ->
          RANGE
              .matcher(
                  RELATIVE
                      .matcher(
                          RECENT.matcher(TREND.matcher(remaining).replaceAll(" ")).replaceAll(" "))
                      .replaceAll(" "))
              .replaceAll(" ");
      case DISCOUNT, CONTRACT_COMPARISON ->
          DISCOUNT_BASELINE.matcher(DISCOUNT.matcher(remaining).replaceAll(" ")).replaceAll(" ");
      case FORECAST -> HORIZON.matcher(FORECAST.matcher(remaining).replaceAll(" ")).replaceAll(" ");
    };
  }
}
