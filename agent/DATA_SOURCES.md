# Agent 데이터 원천과 적재

## 미국 전력가격

[EIA 도매 전력시장 데이터](https://www.eia.gov/electricity/wholesale/index.php)의 ICE 엑셀을 사용했다. SHA-256은 실제 적재한 파일의 값이다. 2026-11-24 KST에 공식 다운로드 세 건의 값과 모두 일치함을 확인했다. 2026년 링크는 연중 갱신된다.

| 파일 및 공식 다운로드 | SHA-256 | 적재 행 | 거래일 |
| --- | --- | ---: | --- |
| [ice_electric-2024final.xlsx](https://www.eia.gov/electricity/wholesale/xls/archive/ice_electric-2024final.xlsx) | `24c7106b9711f339e898141ce0f75f59fccd023ded50c7217fe1c60a37f806d5` | 1,226 | 2023-12-27 ~ 2024-12-24 |
| [ice_electric-2025final.xlsx](https://www.eia.gov/electricity/wholesale/xls/archive/ice_electric-2025final.xlsx) | `5890d90e227ddff14e2beadf231a7a5792c6328f697466f4e2383982a600780b` | 1,269 | 2024-12-26 ~ 2025-12-23 |
| [ice_electric-2026.xlsx](https://www.eia.gov/electricity/wholesale/xls/ice_electric-2026.xlsx) | `66a36ba591a01d8cf3f84e6983cf3353b62dae5dd1c8c39a5d193e9dc503dbd7` | 1,110 | 2025-12-26 ~ 2026-11-10 |

총 7개 허브, 3,605행이다. `public.us_wholesale_power_price`는 각 행의 URL, 파일명, SHA-256과 시트 행 번호를 저장한다. 적재기로 전달할 파일 경로를 준비하고 다음을 전력 적재기가 설치된 환경에서 실행한다.

```bash
shasum -a 256 "$POWER_DIR"/ice_electric-{2024final,2025final,2026}.xlsx
python -m gpu_collector.erd.power --check "$POWER_DIR"/ice_electric-{2024final,2025final,2026}.xlsx
python -m gpu_collector.erd.power "$POWER_DIR"/ice_electric-{2024final,2025final,2026}.xlsx
```

## 한국은행 DRAM 수출물가지수

[ECOS Open API](https://ecos.bok.or.kr/api/)의 통계표 `402Y016`, 품목 `30911201AA`, 달러 기준 `D`, 월별 `M`, 기준연도 `2020=100`을 사용한다. 조회 기간은 **2024-01 ~ 2026-10**으로 고정했다. `public.dram_export_price_index`에 34개월을 적재했다. 공개 샘플을 사용하면 기존 수집기가 최대 10개월씩 나누어 요청한다. [예시 원본 응답](https://ecos.bok.or.kr/api/StatisticSearch/sample/json/kr/1/10/402Y016/M/202601/202610/30911201AA/D/)

```sh
python -m gpu_collector.erd.dram --since 2024-01 --until 2026-10 --check
python -m gpu_collector.erd.dram --since 2024-01 --until 2026-10
```

두 적재기의 `DATABASE_URL`은 같은 PostgreSQL DB를 가리키는 `postgresql://` 형식이다. 전력·DRAM 수집기 설치는 각각의 `collector/README.md`를 참고한다. 발급받은 ECOS 키를 사용하는 환경은 `ECOS_API_KEY`로 설정한다. PostgreSQL에서 다음 쿼리로 적재 결과를 확인할 수 있다.

```sql
SELECT source_file, source_sha256, count(*), min(trade_date), max(trade_date)
FROM public.us_wholesale_power_price
GROUP BY source_file, source_sha256
ORDER BY source_file;

SELECT count(*), min(observation_month), max(observation_month)
FROM public.dram_export_price_index;
```
