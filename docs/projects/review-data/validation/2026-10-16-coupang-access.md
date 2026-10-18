# 쿠팡 검색·상세·리뷰 접근 검증

검증일: 2026-10-16 KST.

## 목적과 범위

쿠팡 collector를 만들기 전에 검색·상품 상세·리뷰 페이지에 접근할 수 있는지 확인한다.
수집 방식별로 같은 상품과 검색어를 사용해 응답을 비교한다.
각 URL은 한 번씩만 호출한다. 차단 이후 재시도, 다른 호스트·모바일 사이트를 통한
차단 회피, CAPTCHA·봇 확인 우회는 수행하지 않는다.

## 환경

- 로컬 macOS에서 검증했다.
- 브랜치 기준은 origin/develop `d7b5336`이다.
- 브라우저는 Playwright Chromium(headless shell 및 Chromium)을 사용했다.
- 테스트 상품 ID는 `7489524545`, 검색어는 "텀블러"다.

## 방법

다음 네 가지 방식으로 호출했다.

- curl로 직접 요청한다. User-Agent는 collector 기본값(`DEFAULT_HEADERS`)과 같다.
- headless Chromium으로 페이지를 연다.
- headed Chromium으로 페이지를 연다. 오늘의집 collector와 같은 방식이다.
- 쿠팡 파트너스 Open API 검색을 인증 없이 호출한다.

호출 대상 경로는 검색 `/np/search?q=`, 상품 상세 `/vp/products/{id}`,
리뷰 `/vp/product/reviews?productId=..`, `robots.txt`다.

## 결과

| 방식 | 검색 | 상품 상세 | 리뷰 | robots.txt |
|---|---|---|---|---|
| curl(collector 기본 UA) | 403 Access Denied | 403 | 403 | 403 |
| headless Chromium | 403 Access Denied | 403 Access Denied | 미호출 | 미호출 |
| headed Chromium | 403 Access Denied | **200 + Access Denied 본문** | 미호출 | 미호출 |
| 파트너스 Open API 검색(무인증) | 401 | — | — | — |

차단 본문은 Akamai 형식이다.

```text
<TITLE>Access Denied</TITLE>
Reference #18...
errors.edgesuite.net
```

headed Chromium의 상품 상세는 HTTP 200을 반환했지만 본문은 Access Denied였다.
페이지가 8초 동안 스크립트를 실행한 뒤에도 같은 본문이었다.
따라서 상태 코드만으로는 차단 여부를 판별할 수 없다.

파트너스 Open API 무인증 호출의 응답은 다음과 같다.

```text
401 Request is not authorized.
```

브라우저 방식에서 리뷰와 robots.txt는 상세·검색이 이미 차단되어 호출하지 않았다.
상세·리뷰 셀렉터는 실페이지로 검증하지 못했다.

## 판단

차단을 우회하지 않는다.

- 검색은 공식 쿠팡 파트너스 Open API를 사용한다. `COUPANG_PARTNERS_ACCESS_KEY`와
  `COUPANG_PARTNERS_SECRET_KEY`가 필요하다. 결과는 최대 10개이고 평점·리뷰 수가 없다.
  호출 한도도 작다.
- 상세·리뷰는 브라우저로 시도한다. 403·429 또는 200 + 차단 본문이면
  `AccessBlockedError`로 실패시킨다. 차단을 빈 성공 응답으로 바꾸지 않는다.
- 수동 입력 도구 `python -m review_data.collectors.coupang.manual_entry <products|reviews>`를 둔다.
- 쿠팡은 발굴·스케줄 대상이 아니다. 서버는 `SCHEDULE_EXCLUDED_PLATFORMS`에
  `coupang`을 추가하는 것을 권장한다.

검토했으나 쓰지 않은 대안은 다음과 같다.

- 모바일 사이트·다른 호스트: 차단 회피에 해당한다.
- 네이버 쇼핑 검색 API: 쿠팡 상품 ID 대신 네이버 경유 링크를 준다.

## 후속 작업

- 파트너스 API 키를 발급받아 실제 검색 응답을 확인한다.
- 서버 설정의 `SCHEDULE_EXCLUDED_PLATFORMS`에 `coupang`을 추가한다.
- 접근 조건이 정식으로 바뀌면 같은 상품으로 상세·리뷰 셀렉터를 다시 검증한다.
