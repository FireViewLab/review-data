# 옥션·G마켓 상세 및 리뷰 접근 검증

검증일: 2026-10-06 15:09~15:15 KST.

## 목적과 범위

검색 차단과 상품 상세·리뷰 차단을 분리하여 URL 기반 수집의 가능성을 확인한다.
같은 상품 식별자로 로컬과 GCP의 저장 없는 직접 수집 경로를 각각 호출한다.
CAPTCHA·Cloudflare 우회, 차단 이후 재시도, 다른 호스트나 리뷰 엔드포인트를 통한
차단 회피는 수행하지 않는다. 상세와 리뷰는 비교를 위해 각각 한 번씩 호출한다.

Obsidian 프로젝트 Handover.md의 수집 원칙과 작업 컨벤션을 확인했다.
프로젝트 및 상위 디렉터리에서 적용할 AGENTS.md는 발견되지 않았다.
서버 설정·서비스 재시작·배포·merge·DB 변경은 수행하지 않았다.

## 환경과 상품

- GCP API·worker·scheduler 이미지 태그:
  `48ee8e66152762843783c82bf5caf7d8dc948575`.
  배포 폴더에는 `.git`이 없으므로 실행 중인 Compose 이미지 태그로 확인했다.
- 로컬 HEAD: `8632ec369be210840aba483765a94814c1840c34`.
- 로컬은 macOS, Playwright 1.62.0, headless Chromium을 사용했다.
  첫 실행은 해당 버전의 Chromium 부재로 사이트 요청 전에 실패했다.
  `python -m playwright install chromium`으로 로컬 브라우저를 설치한 뒤 검증했다.
  이 환경 준비 실패는 사이트 차단 결과에 포함하지 않는다.
- 로컬 `.env`를 읽지 않고 `Settings(_env_file=None, headless=True, internal_token=None)`을
  검증 프로세스에 주입했다. 인증 정보가 필요 없는 두 collector만 호출한다.

| 플랫폼 | 상품 식별자 | 두 환경에서 collector가 사용한 URL | 선정 근거 |
|---|---|---|---|
| 옥션 | D301414505 | http://itempage3.auction.co.kr/DetailView.aspx?itemno=D301414505 | [공개 삼다수 상품 링크](https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no=629471) |
| G마켓 | 2315162136 | https://item.gmarket.co.kr/Item?goodsCode=2315162136 | [공개 상품 페이지](https://item.gmarket.co.kr/Item?goodsCode=2315162136) |

공개된 실제 상품 링크를 사용했다. 이번 환경에서는 상세가 차단되어 현재 판매 상태나
리뷰 총수를 직접 검증하지 못했다. 검색 색인에 노출된 리뷰 수는 수집 성공의 근거로 쓰지 않는다.

## 방법

호출 경로는 다음과 같다. `/api/v1`의 DB 조회·job 생성 경로는 사용하지 않는다.

- `GET /auction/products/D301414505`
- `GET /auction/products/D301414505/reviews?limit=25`
- `GET /gmarket/products/2315162136`
- `GET /gmarket/products/2315162136/reviews?limit=25`

로컬은 ASGITransport로 실제 앱의 직접 수집 endpoint를 호출했다. 앱 lifespan은
실행하지 않아 DB 엔진에 연결하지 않았다. 검증 프로세스에서만 `Page.goto`의
응답 상태와 리뷰 fetch 호출 경로를 기록했다. HTML·쿠키·토큰은 출력하지 않았다.

GCP는 허용된 SSH와 `docker compose exec -T api python -`를 사용했다.
컨테이너 내부에서 기존 API `http://127.0.0.1:8000`에 HTTP 요청을 보냈다.
인증 헤더는 서비스 설정에서 메모리로만 읽어 사용하고 출력하거나 파일로 저장하지 않았다.
프로덕션 코드를 바꾸거나 별도 브라우저 설정을 적용하지 않았다.

## 결과

| 환경 | 플랫폼 | 요청 | 사이트 상태 | 우리 API | 소요 시간 | 수집한 리뷰 본문 |
|---|---|---|---|---|---|---|
| 로컬 | 옥션 | 상세 | 403 | 400 / BAD_REQUEST | 10.01초 | 해당 없음 |
| 로컬 | 옥션 | 리뷰 | 상세 진입 403 | 400 / BAD_REQUEST | 5.35초 | 0건 |
| GCP | 옥션 | 상세 | 403 (API 오류 메시지) | 400 / BAD_REQUEST | 3.03초 | 해당 없음 |
| GCP | 옥션 | 리뷰 | 상세 진입 403 (API 오류 메시지) | 400 / BAD_REQUEST | 1.22초 | 0건 |
| 로컬 | G마켓 | 상세 | 403 | 400 / BAD_REQUEST | 6.21초 | 해당 없음 |
| 로컬 | G마켓 | 리뷰 | 상세 진입 403 | 400 / BAD_REQUEST | 2.42초 | 0건 |
| GCP | G마켓 | 상세 | 403 (API 오류 메시지) | 400 / BAD_REQUEST | 1.06초 | 해당 없음 |
| GCP | G마켓 | 리뷰 | 상세 진입 403 (API 오류 메시지) | 400 / BAD_REQUEST | 1.06초 | 0건 |

옥션 API 오류:

```text
auction: 페이지 접근이 차단되었습니다 (HTTP 403).
```

G마켓 API 오류:

```text
gmarket: G마켓이 자동화 브라우저 요청을 차단했습니다 (Cloudflare 봇 확인, HTTP 403).
```

로컬 리뷰 검증에서 기록된 navigation은 각 1회, 리뷰 fetch 호출은 각 0회였다.
GCP는 API 응답의 오류 메시지와 현재 collector의 호출 순서로 상세 진입 단계의
실패임을 판단했다. GCP 브라우저의 개별 네트워크 요청을 별도로 추적한 것은 아니다.
HTTP 403은 확인했지만 이번 검증에서는 차단 HTML을 별도로 저장·분석하지 않았다.

따라서 옥션 `ReviewService.asmx/GetReviewList`, G마켓 `/Review`·`/Review/Text`의
정상 응답, 실제 리뷰 본문, 두 번째 페이지는 검증하지 못했다. **수집 0건은 리뷰가
없는 상품이라는 뜻이 아니다.** 기존 GCP 검색도 두 플랫폼 모두 사이트 403/API 400으로
실패한 상태라는 인수인계 결과와 별개로, 상세·리뷰 진입도 차단됨을 확인했다.
이번에는 검색을 다시 호출하지 않았다.

## 구현과 검증

정상 리뷰 수집이 가능하다는 조건을 충족하지 못해 URL 입력 경로·collector·테스트를
변경하지 않는다. 실패를 빈 성공 응답으로 바꾸거나 차단된 상세를 건너뛰지 않는다.
이번 저장소 변경은 이 검증 기록 1개 파일이다. A1 미커밋 초안은 그대로 보존한다.

기존 테스트 확인:

```text
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_auction.py tests/test_gmarket.py tests/test_collector_lifecycle.py
94 passed in 1.59s

.venv/bin/ruff check src/review_data/collectors/auction/collector.py src/review_data/collectors/gmarket/collector.py tests/test_auction.py tests/test_gmarket.py
All checks passed!
```

이는 기존 파싱·오류 처리의 회귀 검증이다. 실사이트 리뷰 수집이나 페이지네이션의
성공을 의미하지 않는다. DB 통합 테스트는 실행하지 않았다.

## 가능한 다음 단계

- 두 플랫폼은 수집 불가 상태로 유지한다. URL 입력이나 검색 색인 연동만으로 리뷰
  차단까지 해결된다고 안내하지 않는다.
- 사이트가 허용하는 리뷰 본문 제공 계약·공식 API·데이터 제공 경로를 확인한다.
  [ESM 상품 목록 조회 API](https://etapi.gmarket.com/160)는 등록 상품 관리용이며,
  해당 문서의 응답 필드에서 리뷰 본문 조회를 확인하지 못했다. 이를 리뷰 수집 대체안으로
  구현하지 않는다.
- 접근 조건이 정식으로 변경된 후 동일 상품으로 상세·리뷰 첫 페이지와 두 번째 페이지를
  다시 검증한다. 정상 본문과 서로 다른 리뷰 ID를 확인한 뒤 URL 입력 수집 경로의
  최소 보완 여부를 결정한다.
