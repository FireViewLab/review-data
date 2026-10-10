# 모델 버전별 재분석

## 목표와 활성화

AI 배포의 버전은 OpenAPI의 서비스 버전1.0.0과 다르다. 운영 SSE `meta`의 정확한 `model_version`과 `policy_version`을 목표로 사용한다. 같은 이름으로 가중치나 RTI 정책을 바꾸면 Data가 변경을 판별할 수 없으므로 AI가 버전을 갱신해야 한다.

`ANALYSIS_REFRESH_ENABLED`는 기본false다. 확정된 목표 버전이 없으면 예약하지 않는다. 이번 기능 배포만으로 미확인 새 모델을 추측해 분석하지 않는다.

서버 `/home/deploy/review-data/.env`의 기존 토큰·URL을 보존하고 다음 값만 맞춘다.

```dotenv
AI_MODEL_VERSION=<운영 SSE meta의 model_version>
AI_POLICY_VERSION=<운영 SSE meta의 policy_version>
ANALYSIS_REFRESH_ENABLED=true
ANALYSIS_REFRESH_MAX_PENDING=100
ANALYSIS_REFRESH_BATCH_SIZE=20
ANALYSIS_REFRESH_PAUSE_COLLECTION=true
```

설정 변경 후 네 앱 서비스가 같은 설정을 읽도록 재생성한다.

```bash
cd /home/deploy/review-data
docker compose up -d --no-build --no-deps --force-recreate api worker analysis-worker scheduler
docker compose exec -T api crawler analysis-refresh
```

실제 목표 값이 없으면 위 placeholder를 넣지 않는다. 상품 하나의 연동과 버전·결과 저장을 검증한 후 운영 목표를 활성화한다.

## 대상 선정과 재개

목표 모델·AI 정책·Data 선정 정책·표본 상한으로 캠페인을 식별한다. 새 목표를 처음 실행할 때 리뷰가 저장된 기존 상품을 DB에 기록한다. 목표 버전·현재 대표 집합 해시·선정 조건에 맞는 성공 결과가 있는 상품은 제외한다. 리뷰0건 상품도 제외한다.

캠페인과 상품별 작업 ID를 DB에 보존한다. 서버·스케줄러·워커 재시작 후 같은 목표를 다시 실행해도 동일 캠페인을 이어서 처리한다. 같은 목표에 완료된 캠페인은 매 주기 전체 재분석하지 않는다. 이후 새 상품과 변경 리뷰는 기존 수집 후 분석 흐름으로 처리한다.

새 버전이 들어오면 진행 중인 이전 캠페인은 superseded로 남긴다. 이전 캠페인의 결과·작업 이력을 삭제하지 않는다. 오래된 버전으로 대기 중인 작업을 워커가 획득하면 AI에 보내지 않고 새 버전 작업으로 교체한다.

## 큐와 수집 순서

- 캠페인 예약 시 전체 분석 queued/running 수를 확인한다. 기본100건의 남는 자리 안에서 회차당 최대20상품을 예약한다.
- 스케줄러와 분석 워커가 동일 DB 잠금으로 경합 예약을 막는다. 분석 워커는 작업 사이에도 예약을 보충한다.
- 이 상한은 캠페인의 추가 예약을 제어한다. 별도의 긴급 조회·수집 완료로 추가되는 분석 작업을 전역으로 차단하는 것은 아니다.
- `ANALYSIS_REFRESH_PAUSE_COLLECTION=true`이면 running 캠페인 동안 신규 발굴·정기 재수집 예약을 대기한다. 이미 예약되거나 실행 중인 수집은 마무리한다. 조회 API의 긴급 수집은 유지한다.
- 대상이 완료·실패·제외로 모두 정리되면 캠페인을 완료 처리하고 정기 수집·신규 발굴 예약을 재개한다. 실패가 있으면 completed_with_errors로 명확히 표시한다.
- 활성 플래그를false로 바꾸면 새 예약을 중지한다. 현재 작업은 계속 처리되고 캠페인은 남아 있으므로true로 복구하면 이어서 처리한다. 대기 중 수집까지 재개하려면 pause_collection도false로 설정한다.

## 결과 게시와 실패 처리

완료 결과는 enqueue 시 stale로 바꾸지 않는다. 새 작업의 결과가 검증·저장될 때까지 기존 결과를 조회한다. 이전 배포가 stale로 바꿨던 완료 이력도 완료 시각·결과 메타데이터가 있는 경우 보존 결과로 조회한다. 미완료 stale 작업은 결과로 취급하지 않는다.

API·카탈로그의 `status=done`은 현재 표시 가능한 성공 결과가 있다는 뜻이다. `is_current`가 현재 입력·목표 조건 일치 여부이며, `refresh_job`은 더 최신 작업 ID·상태다. 표시 결과의 실제 모델·정책·표본 범위는 해당 성공 결과를 따른다. 목표 버전의 성공 이력이 있으면 이를 우선 선택하며, 없을 때 다른 버전의 이전 성공 결과를 유지한다.

새 응답의 버전·리뷰 ID·건수가 요청과 일치해야 하며 결과 저장과done 처리는 한 트랜잭션이다. 실패하면 이전 결과가 유지된다. 일시 오류는 기존 재시도 상한·대기와 같은 ID를 유지한다. 종료된 실패는 캠페인 실패로 표시하고 무한 반복하지 않는다.

실패 건을 명시적으로 다시 예약하려면 다음 명령을 사용한다. 새 작업 ID로 재요청하여 실패한 AI Idempotency-Key를 재사용하지 않는다.

```bash
docker compose exec -T api crawler analysis-refresh --retry-failed
```

## 대시보드

`/status`에 최근5개 캠페인의 목표 버전·대상·예약 전 대기·큐 대기·실행·완료·실패·제외·시작/완료 시각을 표시한다. 진행률은 `(완료 + 실패 + 제외) / 대상`이며 실패 건수와 완료 상태를 따로 표시한다. 대상0건은100% 완료다. 일반 작업 상세에서 전송 표본과 저장 결과를 확인한다.

Spring·프론트 코드는 수정하지 않는다. 기존 점수 제공 필드는 유지하고 신선도·재분석 상태 필드를 추가한다. [리뷰 선정 기준](2026-10-09-review-analysis-selection.md)을 함께 참고한다.
