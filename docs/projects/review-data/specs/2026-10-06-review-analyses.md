# 리뷰별 분석 결과 저장

인계 문서 A1과 Data ↔ AI 결과 계약서 v0.5를 기준으로 한다.
이 문서는 볼트 `docs/projects/review-data/specs/`에 반영할 스펙이다.

## 범위

`review_analyses` 테이블과 ORM, Alembic 마이그레이션, DB 통합 테스트를 추가한다.
팀 공용 `core/models.py`, 수집 워커, API 응답은 변경하지 않는다.
분석 job 생성·AI 호출·저장 서비스·최신 결과 조회는 B2에서 합의 후 구현한다.
기존 `analysis_jobs.result` 데이터는 옮기거나 삭제하지 않는다.

## 저장 구조

- 기본키는 `(analysis_job_id, review_id)`로 한다. 실행별 이력을 보존하고 같은 실행의
  중복 결과를 막는다. 재시도 upsert는 후속 저장 서비스에서 구현한다.
- `platform`, `product_id`, `review_id`는 원본 `reviews`에 복합 외래키로 연결한다.
- `(analysis_job_id, platform, product_id)`는 `analysis_jobs`에 복합 외래키로 연결한다.
  이를 위해 job에 `(id, platform, product_id)` 유일 제약을 추가한다.
  job과 리뷰가 서로 다른 상품을 가리키는 결과는 저장할 수 없다.
- job 또는 원본 리뷰를 삭제하면 해당 결과도 삭제한다. 리뷰를 갱신하는 것만으로
  이전 실행의 결과를 지우지는 않는다.
- `rti`: nullable Numeric, 계약에 명시된 0~100 범위만 허용한다.
- `level`: nullable Text, `safe`, `warn`, `danger`만 허용한다.
  정책별 임계값과 점수 결합식을 Data에 하드코딩하지 않는다.
- `text_score`, `behavior_score`, `network_score`: nullable Numeric.
  계약에서 별도 수치 범위가 명시되지 않았으므로 범위를 강제하지 않는다.
  Numeric에 precision/scale을 지정하지 않아 임의 반올림하지 않는다.
- `reasons`: NOT NULL Text 배열, 기본값 `[]`. NULL 원소를 허용하지 않는다.
  reason code 목록은 제약으로 고정하지 않는다.
- `model_version`, `input_hash`: nullable Text. 형식·생성 주체는 AI팀 합의 전까지
  강제하지 않는다. 값이 없으면 NULL로 유지한다.
- `created_at`: timezone-aware 저장 시각, 서버 기본값 `now()`.
- 리뷰별 실행 이력 조회용 `(platform, product_id, review_id, analysis_job_id)` 인덱스를 둔다.

## 마이그레이션과 검증

upgrade는 기존 job에 유일 제약을 추가하고 새 빈 테이블과 인덱스를 만든다.
downgrade는 결과 테이블을 삭제한 뒤 job의 추가 제약을 제거한다.
롤백하면 새 분석 결과는 삭제되므로 배포 시 별도 검토한다.
운영 배포는 전체 테스트와 CI 통과 후 릴리스 PR로 수행한다.

테스트는 nullable 결과·빈 reasons·정밀도 보존·실행별 이력·중복 결과 거부·
외래키 매칭·삭제 연쇄·RTI/level/reasons 제약·upgrade/downgrade와 기존 데이터 보존을
PostgreSQL에서 확인한다. DB가 없으면 통합 테스트 skip 여부를 명시한다.

2026-10-07 검증: 운영 DB와 분리한 PostgreSQL 16에서 전체 테스트 320개가
통과했다(skip 없음). 마이그레이션 round trip과 ORM drift 검증을 포함한다.
