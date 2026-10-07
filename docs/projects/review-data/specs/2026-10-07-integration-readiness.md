# Spring·분석 서비스 연동 준비

## 운영 계약

Spring은 `https://data.re-view.kr`의 상품 조회 API에 `X-Internal-Token`을 붙여 요청한다.
`GET /api/v1/{platform}/products/{product_id}?limit=20`의 응답은 다음과 같다.

| HTTP | status | 후속 처리 |
|---|---|---|
| 200 | fresh | 상품·리뷰를 표시한다. |
| 200 | stale | 마지막 정상 데이터를 표시하고 job.id로 갱신 상태를 확인한다. |
| 202 | queued | job.id를 보존하고 job 상태를 조회한다. |
| 401 | 인증 오류 | 서버 간 토큰 설정을 확인한다. |

`GET /api/v1/jobs/{job_id}`에서 succeeded·partial·failed를 구분한다.
partial은 상품과 리뷰의 성공 여부를 따로 확인한다. 실패 또는 polling timeout을
완료로 표시하지 않는다. succeeded 후 상품을 다시 조회한다.
리뷰 다음 페이지는 응답의 reviews.next_cursor를 URL 인코딩하여 전달한다.
검색용 `/{platform}/search`는 직접 수집 경로이며 상품 DB 조회 API와 다르다.

## 분석 연동 — 저장소별 계약 확인

2026-10-07 각 저장소 main을 읽기 검토했다.

| 저장소 | 확인한 계약 |
|---|---|
| review-ai-db (50a7fa5) | 운영 API POST /api/v1/data/analyze JSON. SSE는 ENABLE_EXPERIMENTAL_COLLECTION=1일 때만 /experimental/analysis/collect/stream에 등록된다. |
| review-ai-new (ff3c149) | POST /analysis/collect/stream과 Data 리뷰 SSE 구독 구현이 존재한다. 실제 배포 주소와 활성 계약은 별도 확인한다. |
| review-backend (33f07b2) | AiServerClient가 예전 /api/internal/ai/... 5개 경로를 호출한다. v0.5 JSON 및 실험 SSE 계약과 다르다. |

코드의 SSE 존재만으로 현재 운영 경로라고 판단하지 않는다. 분석 job의 호출 주체,
배포된 서버·엔드포인트·인증 및 결과 저장 주체를 확인한 뒤 구현을 연결한다.
기존 JSON 초안은 보존한다. 합의된 Data POST → AI SSE 방향의 별도 파이프라인을
구현하며 실제 이벤트 예시·인증 토큰·신규 endpoint 검증 전에는 AI_ANALYSIS_ENABLED=false로 운영한다.
review-ai-db는 X-Internal-Token 인증을 사용하고 한 요청 최대 500개 리뷰를 받는다.
점수 미제공은 -1, level은 null로 반환한다. Data DB의 nullable 점수로 변환하는
정규화 규칙을 명시해야 하며 -1을 유효 점수나 평균에 포함하지 않는다.

Spring의 Data 연동은 토큰·fresh/stale/queued·job·cursor를 이미 구현했다.
Data 상품 응답은 analysis 상태·페이지 결과를 제공한다. Spring 상품 응답의 analysis는 현재 null이다. 분석 연동에는 다음 수정이 필요하다.

- 현재 분석 endpoint와 DTO를 선택된 실제 계약에 맞춘다.
- 원본 review_id를 Spring DB의 Long PK로 해석하지 않고 플랫폼·상품·리뷰 키로 연결한다.
- level만 보고 RTI를 85/55/30으로 채우는 코드를 제거하고 실제 nullable 점수를 보존한다.
- 실패한 분석을 완료 알림이나 정상 결과로 처리하지 않는다.
- 완료된 결과를 analysis 응답과 화면에 연결한다.

담당자가 https://ai.re-view.kr과 X-Internal-Token 인증을 확인했다.
신규 POST /api/v1/data/analyze/stream과 Data job ID를 X-Request-ID·Idempotency-Key에
같이 전달하는 계약을 합의했다. AI 구현·실제 이벤트 예시·인증 토큰은 대기 중이다. Data가 저장된 리뷰를 POST하고
응답 SSE를 받는 방향으로 합의하였다. 확정 전에는 자동 호출을 활성화하지 않는다.
구현·운영 방법은 analysis-pipeline 및 analysis-stream-contract 문서를 따른다.

## 공용 모델 변경 제안

| 영역 | 제안 필드 | 확정할 내용 |
|---|---|---|
| 상품 상세 | images, description, options, original_price, sale_price | 필드 타입·누락 표현·플랫폼별 지원 범위 |
| 리뷰 근거 | evidence, observed_at, schema_version | 플랫폼 allowlist와 feature 의미 |
| 작성자 연결 | author_key, source, scope, key_version | HMAC 입력·서버 비밀키·회전 방식 |
| 원본 조각 | raw | 개인정보 제거·크기 상한·보관 기간·삭제 작업 |

공용 core/models.py는 현재 계약을 유지한다. 원본 회원 ID나 닉네임을
안정적인 회원 식별자로 추측하지 않는다. 미확인 행동 근거는 null로 유지한다.
팀 승인 여부와 구체적인 계약을 확인한 뒤 collector별로 적용한다.
