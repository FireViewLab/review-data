# 운영 상품 조회 연동 검증

검증일: 2026-10-07. 운영 서버 컨테이너에서 공개 HTTPS 주소로 호출한다.
비밀값과 리뷰 본문은 기록하지 않는다.

## 확인한 요청 흐름

| 확인 | 결과 |
|---|---|
| 내부 토큰 없는 상품 조회 | HTTP 401 |
| 토큰을 붙인 컬리 1001319595 조회(limit=1) | HTTP 200, fresh, 리뷰 1건 |
| next_cursor로 다음 페이지 조회 | HTTP 200, 리뷰 1건, 이전 페이지와 ID 중복 없음 |
| 존재하지 않는 job 조회 | HTTP 404 |
| 잘못된 cursor | HTTP 400 |

운영 로그와 DB에서 다음 비동기 흐름도 확인한다.

| job | 플랫폼 | 최초 요청 시각(KST) | 결과 |
|---|---|---|---|
| 3378 | oliveyoung | 2026-10-07 07:43:51 | 202 → job 조회 200 → 상품 재조회 200, 수집 succeeded |
| 3419 | kurly | 2026-10-07 07:52:08 | 202 → job polling 200 → 상품 재조회 200, 수집 succeeded |
| 3468 | musinsa | 2026-10-07 08:43:51 | 202 → job 조회 200 → 상품 재조회 200, 수집 succeeded |

세 job 모두 product_status와 review_status가 succeeded다.
API access log에는 프록시의 내부 IP만 보여 요청 주체가 Spring인지 확정할 수 없다.
실제 Spring 요청 시각·경로 및 Spring 로그와 대조해야 실연동 확인이 완료된다.

## 서버 로그 확인

```bash
cd /home/deploy/review-data
docker compose logs -f --since=10m --timestamps api
```

토큰 자체를 로그나 공유 문서에 출력하지 않는다. 응답의 job.id를 기준으로
`GET /api/v1/jobs/{id}` 또는 DB의 collection_jobs를 조회한다.
