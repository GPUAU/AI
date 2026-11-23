package com.gpumarket.domain.agent.dto;

import com.fasterxml.jackson.annotation.JsonAnySetter;
import com.fasterxml.jackson.databind.JsonNode;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.Pattern;

public record PublicContextDto(
    @Pattern(regexp = "[1-9][0-9]{0,18}") String modelId,
    @Pattern(regexp = "(?i)ON_DEMAND|SPOT|COMMITMENT|UNKNOWN") String contractType,
    @Pattern(regexp = "[A-Za-z]{2}") String countryCode,
    @Pattern(regexp = "(?i)INSTANCE|NODE|RACK_SCALE|UNKNOWN") String systemType,
    @Min(1) @Max(10000) Integer gpuCount,
    @Pattern(regexp = "(?i)GENERAL_CLOUD|GPU_SPECIALIST|UNKNOWN|ALL") String providerType,
    @Pattern(regexp = "(?i)[a-z0-9][a-z0-9-]{0,49}") String regionCode,
    @Min(1) @Max(120) Integer commitmentMonths,
    @Pattern(regexp = "(?i)ALL_UPFRONT|PARTIAL_UPFRONT|NO_UPFRONT") String paymentOption) {
  @JsonAnySetter
  void unsupported(String ignored, JsonNode value) {
    throw new IllegalArgumentException("지원하지 않는 공개 조건입니다.");
  }
}
