# 검색 상품·분석 카탈로그 연결

검색 결과의 신규 상품을 Data DB에 등록하고 pending 상한20건 안에서 수집을 예약한다.
상한 밖의 상품은 등록만 하고5분 스케줄러가 예약한다. 기존 상세·수집 시각을 덮지 않고
플랫폼 제외·실패6시간 대기·같은 입력 분석 재사용을 유지한다.

GET /api/v1/catalog?limit=100&cursor=... 은 platform/product_id 순의 keyset 목록이다.
내부 토큰이 필요하다. items에는 product와 analysis를 담고 next_cursor를 제공한다.
analysis에는 status/job_id/avg_rti/scored_review_count/review_count/source_review_count/
sampled/model_version/policy_version을 제공한다. avg_rti는 최신 done의 제공된 RTI만
평균한다. NULL은 제외하고0은 포함한다. 입력·모델·정책 변경 또는 미완료 상태에는
이전 점수를 내리지 않는다. 분석 건수는 실제 선택된 표본, 원본 건수는 중복 통합 후 대표 리뷰 전체 건수다. 원본 DB 행은 보존한다.
단일 SQL snapshot으로 상품·작업·집계 상태를 일관되게 읽고 원본 리뷰 본문은 보내지 않는다.

Spring은 기동 시와5분마다 페이지를 읽어 표시 캐시를 동기화한다. 외부 호출은 DB
트랜잭션 밖에서 하고 페이지 저장만 묶는다. 실패 시 기존 캐시를 유지하고 다음 주기에
다시 읽는다. 진행·실패·stale는 이전 점수를 지우며0/NULL과 표본 여부를 보존한다.
표시 정보와 분석 요약은 서로의 컬럼을 덮지 않는다. 홈은 완료 상품을 우선한다.

기존 Spring HomeCatalogRefresher의6시간 분야별 검색을 사용한다. 기본26개 키워드,
4개 HTTP 기반 플랫폼, 몰당3건을 검색하고 Data 등록·수집·분석으로 이어진다.
검색 실패는 플랫폼·키워드별로 격리하고 화면 전체를 실패시키지 않는다. 별도 발굴
워커를 중복 생성하지 않는다. 네이버 브랜드스토어는 기존 지정 상품·관련 상품 시드를
유지하며 옥션·G마켓·차단 상품은 우회하지 않는다.

Flutter 홈·검색의 분석 상태·표본 RTI 배지 변경은 담당팀 후속 제안이며 아직 미적용이다.
Data·Spring 카탈로그 동기화는 PR84·Spring PR212의 운영 배포로 적용했다.
리뷰0건 상품은 Data 검색 응답과 카탈로그에서 제외하고 등록·수집 예약은 유지한다.
