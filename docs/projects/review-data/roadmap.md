# review-data 로드맵

기준일: 2026-10-07.

## 운영 중인 기능

- GCP에서 HTTPS API·수집 워커·스케줄러·PostgreSQL·Caddy를 운영한다.
- Spring 조회에 X-Internal-Token 인증, TTL 기반 fresh/stale/queued,
  수집 job 조회 및 cursor 페이지네이션을 제공한다.
- Docker 이미지와 GitHub Actions로 main 머지 시 자동 배포한다.
- 네이버 브랜드스토어 collector와 시드, 자동 갱신 스케줄러를 운영한다.
- 직접 수집 API의 Xvfb·브라우저 리소스 정리와 오류 처리를 보완했다(#62~#64).

## 플랫폼 상태

| 플랫폼 | 검색 | 상품·리뷰 | 제한 |
|---|---|---|---|
| oliveyoung, kurly, elevenst, musinsa | 지원 | 지원 | 실제 응답과 수집 오류를 확인한다. |
| ohouse | 지원 | 지원 | 브라우저 기반으로 수집 시간이 길다. |
| naver | 미지원 | 브랜드스토어 지원 | 리뷰 첫 페이지 20건, 스마트스토어 차단 |
| ably | 외부 색인 기반 | 공개 요약 리뷰 구현, 검증 상품 상세 차단 | 검색 성공과 리뷰 수집 성공을 구분한다. |
| auction, gmarket | 차단 | 차단 | 로컬·GCP 상세 접근 HTTP 403, 정상 리뷰 본문 0건 |

차단된 플랫폼은 우회하지 않고 실패로 기록한다. 옥션·G마켓은 공식 리뷰 데이터
제공 경로가 확인돼야 수집을 재개한다. ESM 상품 관리 API를 리뷰 API로 취급하지 않는다.

## 이번 작업

| 항목 | 상태·산출물 |
|---|---|
| 리뷰별 분석 결과 저장 | 이슈 #65, PR #66. PostgreSQL 16 전체 테스트 320개 통과. |
| Spring 계약 검증 | 공개 HTTPS 인증·조회·cursor·오류 확인, validation 문서 작성 |
| CI 실행 환경 | Ubuntu 24.04 고정 및 Node.js 24 액션으로 갱신, actionlint 통과 |
| 수집 확대 | 검증된 상품 URL만 시드에 추가하고 서버 수집 결과를 확인한다. |
| 행동 근거 조사 | 현재 collector·fixture와 공식 자료에서 실제 제공 여부를 구분한다. |

## 계약 확정 후 구현

1. 분석 서버 URL·인증·SSE 계약을 확정하고 Data 분석 워커를 연동한다.
2. 분석 실행의 lease·재시도·완료 판정과 결과 멱등 저장을 구현한다.
3. 완료된 분석 결과를 Spring 상품 응답에 포함한다.
4. 상품 상세 필드와 리뷰 raw/evidence/author_key의 공용 모델 계약을 확정한다.
5. collector별 필드를 적용하고 원본 보관·정제 및 학습 데이터 export를 구현한다.

구체적인 계약 변경 제안은 specs/2026-10-07-integration-readiness.md에 정리한다.
현재 분석 서버 자동 호출과 공용 core/models.py 변경은 적용하지 않는다.

## 운영 규모에 따라 진행

조회 job 우선순위, 차단 자동 감지, 워커 처리량 조정, Redis 캐시를 검토한다.
Spring IP 확정 후 접근 제한을 검토한다. 네이버 리뷰 추가 페이지와 검색 API
이관은 플랫폼 지원 및 공식 계약을 확인한 뒤 수행한다.
