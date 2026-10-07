# 저장된 리뷰의 SSE 분석 파이프라인

## 범위

수집 워커는 리뷰 저장·분석 예약을 한 트랜잭션으로 처리한다. 별도 분석 워커가
저장된 스냅샷을 POST하고 SSE를 수신하여 완료된 결과만 원자적으로 게시한다.
수집 공용 모델은 유지한다. AI 운영 URL·신규 endpoint·이벤트 종류와 Data job ID 계약은 합의했다.
AI main 2346111에서 이벤트 request_id·ai_job_id·진행 중409 계약을 확인했다.
서버 토큰 설정 후 상품 하나로 실연동을 검증한다.
기본 AI_ANALYSIS_ENABLED=false로 배포하며 외부 자동 호출은 비활성 상태를 유지한다.

## 입력·작업·결과

analysis_jobs에 input_payload/input_hash/model_version/policy_version/available_at을 추가한다.
products.analysis_input_hash를 저장 시 갱신하고 조회는 전체 리뷰 본문 재로딩 없이 비교한다.
리뷰·분석 페이지 조회는 같은 상품 공유 잠금 트랜잭션으로 일관성을 유지한다.
스냅샷은 리뷰 ID 순서의 content/rating/written_at으로 구성하며 표시 작성자는 전송하지 않는다.
동일 입력과 설정 버전은 중복 분석하지 않는다. 모델/정책 버전 설정 변경은 재분석한다.
재시도는 동일 job ID와 스냅샷을 사용한다. 결과는 review_analyses에 실행별로 저장한다.
입력이 바뀌면 진행 중·완료된 이전 실행은 stale로 전환하고 새 실행을 등록한다.
상품 행 잠금 뒤 job 행 잠금을 사용해 수집 저장과 결과 게시를 직렬화한다.
최대 500개를 넘으면 명시적으로 실패시키며 네트워크 분석의 문맥을 임의 분할하지 않는다.
0개 리뷰는 AI에 보내지 않는다. -1과 null은 DB NULL로 정규화하고 0은 보존한다.

## 워커와 장애

crawler analysis-worker와 compose analysis-worker는 수집 워커와 별도 프로세스다.
claim은 FOR UPDATE SKIP LOCKED, heartbeat는 lease 소유권을 유지한다.
소유권을 잃거나 입력이 갱신되면 요청을 취소하고 결과 게시를 거부한다.
진행 중409/429/5xx/timeout/스트림 끊김은 최대 3회, 지수 간격으로 재시도한다.
인증·입력·응답 계약 오류는 즉시 실패한다. 오류 응답·토큰·리뷰 본문은 로그에 남기지 않는다.
모든 이벤트 request_id와 meta/done의 ai_job_id, 상품·건수·계약 버전을 검증하며 done 및 정상 EOF 전에는 결과를 최신으로 게시하지 않는다.
결과 저장과 job done은 같은 트랜잭션이다. 전송 중에는 DB 트랜잭션을 열어두지 않는다.

## 조회·운영

기존 상품 API에 analysis 상태와 해당 리뷰 페이지의 결과를 추가한다.
GET /api/v1/{platform}/products/{product_id}/analysis는 분석 상태·결과 페이지를 제공한다.
GET /api/v1/analysis-jobs/{job_id}는 원본 입력·토큰을 제외한 상태를 제공한다.
상태는 disabled/not_analyzed/queued/running/done/failed/stale이다.
전체 count와 현재 페이지 results를 구분한다. 실패한 run과 입력이 바뀐 run은 게시하지 않는다.
crawler analysis-backfill은 활성화 이후 기존 DB 상품을 예약한다. 자동 호출 활성화는
https://ai.re-view.kr/api/v1/data/analyze/stream의 구현·토큰·실제 SSE 예시를
확인하고 상품 하나로 검증한 뒤 진행한다. X-Request-ID와 Idempotency-Key는
같은 Data job ID이며 AI 내부 작업 ID와 구분한다.

## 검증

PostgreSQL 통합 테스트로 원자성·중복 예약·동시 claim·lease 회수·입력 변경·
결과 저장 실패·재시도·조회 상태를 확인한다. SSE는 모의 서버로 프레이밍·연결 끊김·
누락·중복·잘못된 ID·done 건수·점수 정규화를 검증한다.
