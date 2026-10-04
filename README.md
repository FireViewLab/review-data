# review-data

커머스 플랫폼의 상품·리뷰를 수집해 PostgreSQL 에 저장하고, 조회 API 로 제공하는 Data 서버입니다.

전체 구조에서의 위치: **Flutter → Spring(BFF) → review-data → AI 분석**. 이 저장소는 상품·리뷰 원본 데이터의 소유자이며, Spring 은 화면 조합만, AI 는 분석만 담당합니다.

## 수집 방식

요청이 크롤링을 기다리지 않습니다.

1. DB 에 데이터가 있고 TTL 이내면 **즉시 반환**합니다 (`fresh`).
2. TTL 이 지났으면 수집 job 을 만들고, **마지막 정상 데이터를 함께 반환**합니다 (`stale`).
3. 데이터가 아예 없으면 job 만 만들고 `202` 를 반환합니다 (`queued`).
4. 워커가 job 을 가져가 크롤링하고 DB 를 갱신합니다.

신선도는 상품과 리뷰를 따로 판단합니다(기본 상품 24h, 리뷰 6h). 상품만 성공하고 리뷰 수집이 실패했다면 다음 조회에서 다시 수집 대상이 됩니다.

## 담당

| 담당자 | 플랫폼 | 폴더 | 상태 |
|---|---|---|---|
| 김동환 | 에이블리 | `collectors/ably` | 구현됨 |
| 김동환 | 오늘의집 | `collectors/ohouse` | 구현됨 |
| 김하연 | 마켓컬리 | `collectors/kurly` | 구현됨 |
| 김하연 | G마켓 | `collectors/gmarket` | **미구현** |
| 남정현 | 옥션 | `collectors/auction` | 구현됨 |
| 남정현 | 무신사 | `collectors/musinsa` | 구현됨 |
| 정빈 | 11번가 | `collectors/elevenst` | 구현됨 |
| 정빈 | 올리브영 | `collectors/oliveyoung` | 구현됨 |

구현 여부는 `crawler list` 로 확인할 수 있습니다. `collector.py` 가 비어 있으면 '아직 작업 전' 으로 보고 조용히 건너뜁니다.

## 시작하기

```bash
git clone https://github.com/FireViewLab/review-data.git
cd review-data

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -e ".[dev]"
playwright install chromium     # 최초 1회, 몇 분 걸립니다
cp .env.example .env
```

PostgreSQL 을 띄우고 스키마를 적용합니다. 접속 정보는 `.env` 의 `DATABASE_URL` 로 바꿀 수 있습니다.

```bash
docker run -d --name review-data-pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=review_data \
  -p 5432:5432 postgres:16-alpine

alembic upgrade head            # 테이블 생성
crawler list                    # 등록된 collector 확인
```

## 명령어

```bash
crawler list                              # 등록된 collector 목록
crawler collect                           # 대화형으로 플랫폼 선택 후 상품 수집
crawler collect -k 텀블러 -n 30            # 키워드/개수 직접 지정
crawler reviews elevenst 123456 -n 100    # 특정 상품의 리뷰 수집
crawler worker                            # 수집 job 을 처리하는 워커 (Ctrl+C 로 종료)
crawler worker --once                     # job 하나만 처리하고 종료
crawler scheduler                         # 낡은 상품을 주기적으로 다시 예약 (워커와 함께 실행)
crawler scheduler --once                  # 한 번만 예약하고 종료
crawler serve                             # API 서버 (localhost:8000/docs)
```

스케줄러는 DB 에 있는 상품 중 낡은 것(상품 24시간, 리뷰 6시간)을 찾아 수집 job 을 만듭니다. 조회가 없어도 데이터가 최신으로 유지됩니다. 대기 중인 job 이 20개를 넘으면 더 만들지 않고, 수집이 실패한 상품은 6시간 뒤에 다시 시도합니다. 플랫폼 전체가 차단돼 있으면 `SCHEDULE_EXCLUDED_PLATFORMS` 로 스케줄 대상에서 뺍니다. 기본 설정(5분마다 최대 20건)으로는 시간당 약 240건까지 예약하므로, 상품이 많아지면 주기와 상한을 조정해야 합니다.

`collect` 와 `reviews` 는 직접 수집해 바로 저장합니다. API 가 만든 job 을 처리하려면 `crawler worker` 를 띄워야 합니다 — 워커가 없으면 job 이 `pending` 으로 쌓이기만 합니다.

## API

```
GET /api/v1/{platform}/products/{product_id}    상품 + 리뷰 조회 (fresh / stale / queued)
GET /api/v1/jobs/{job_id}                       수집 job 상태 조회
```

`INTERNAL_TOKEN`을 설정하면 모든 API 호출에 같은 값의 `X-Internal-Token` 헤더가
필요합니다. 값을 비우면 로컬 개발을 위해 인증을 검사하지 않으며, `/health`와 API 문서
경로는 항상 인증 없이 접근할 수 있습니다. Swagger의 Authorize 버튼에도 토큰을 넣을 수 있습니다.

리뷰 목록은 cursor 페이지네이션을 씁니다 (`?cursor=...&limit=20`).

응답은 `status` 로 구분합니다.

```json
{ "status": "stale",
  "product": { "platform": "elevenst", "product_id": "123456", "name": "..." },
  "reviews": { "items": [], "next_cursor": null },
  "job":     { "id": 7, "status": "pending", "product_status": "pending", "review_status": "pending" } }
```

오류는 엔드포인트와 무관하게 같은 형식입니다. `detail` 에는 원인이 들어가고, 없으면 `null` 입니다.

```json
{ "error": { "code": "INVALID_CURSOR",
             "message": "cursor 값이 올바르지 않습니다.",
             "detail": "Expecting value: line 1 column 1 (char 0)" } }
```

`code` 는 `NOT_FOUND` · `INVALID_CURSOR` · `VALIDATION_ERROR` · `BAD_REQUEST` · `UNAUTHORIZED` · `FORBIDDEN` · `NOT_SUPPORTED` · `INTERNAL_ERROR` 중 하나입니다.

## Docker 로 실행

Python·Chromium·Postgres 를 따로 설치하지 않고 한 번에 띄웁니다. API 는 `localhost:8000/docs`.

```bash
cp .env.example .env
docker compose up --build       # Postgres → 마이그레이션 → API + 워커
docker compose logs -f worker   # 수집 진행 상황
docker compose down             # 종료 (데이터는 볼륨에 남음, 지우려면 -v)
```

## 배포

`main` 에 머지되면 GitHub Actions 가 이미지를 GHCR 에 올리고 GCP VM 에서 새 이미지로 교체합니다. PR 에서는 테스트와 이미지 빌드만 확인합니다.

서버 최초 1회 준비:

1. VM 에 Docker 설치, `~/review-data/.env` 작성 (`POSTGRES_PASSWORD`, `INTERNAL_TOKEN` 은 반드시 바꿀 것)
2. HTTPS 를 쓰려면 `.env` 에 `COMPOSE_PROFILES=proxy`, `DATA_DOMAIN=<도메인>` 추가, 도메인 A 레코드를 VM IP 로, 방화벽은 80·443 허용
3. 저장소 Secrets 에 `GCP_VM_HOST`, `GCP_VM_USER`, `GCP_VM_SSH_KEY` 등록

API 는 서버 안(127.0.0.1:8000)에만 열리고, 외부 요청은 Caddy 가 HTTPS 로 받아 넘깁니다. 인증서 발급·갱신은 자동입니다.

문제가 생기면 서버에서 이전 커밋 이미지로 되돌립니다.

```bash
cd ~/review-data
IMAGE=ghcr.io/fireviewlab/review-data:<이전 커밋 해시> docker compose up -d --no-build
```

## 테스트

```bash
pytest                          # DB 가 없으면 통합 테스트는 자동으로 skip 됩니다
```

DB 가 필요한 테스트까지 돌리려면 위 Postgres 컨테이너를 띄운 상태여야 합니다.

## 구조

```
api/          엔드포인트, 앱 조립, 공통 오류 형식
core/service  TTL 판단, job 생성, 응답 조립
core/db       ORM 모델, repository(upsert · job claim/lease), 마이그레이션
worker/       job claim → 수집 → 저장 → 완료 기록
collectors/   플랫폼별 수집기
```

## collector 만들기

자기 폴더의 `collector.py` 를 채우면 됩니다. **등록 절차는 없습니다** — `collectors/` 아래 폴더를 자동으로 스캔하므로, 공용 파일을 건드릴 일이 없고 머지 충돌도 나지 않습니다.

지켜야 할 것은 세 가지뿐입니다.

1. `BaseCollector`(또는 `BrowserCollector`) 상속
2. `platform` 값 = **폴더명과 동일하게** (다르면 실행 시 에러로 알려줍니다)
3. 메서드 3개 시그니처 유지

이 외에는 자유입니다. 파일을 몇 개로 나누든, 파서를 분리하든, 상수를 어디에 두든 상관없습니다.

### 최소 예시

```python
from review_data.core.base import BaseCollector
from review_data.core.exceptions import NotSupportedError
from review_data.core.models import Product, Review


class KurlyCollector(BaseCollector):
    platform = "kurly"      # 폴더명과 동일해야 합니다
    label = "마켓컬리"        # CLI 목록에 표시될 이름

    # 인증 정보가 필요한 경우에만 (Settings 의 속성명)
    # required_settings = ("kurly_api_key",)

    async def search_products(self, keyword: str, limit: int = 20) -> list[Product]:
        response = await self.client.get(
            "https://api.example.com/search",
            params={"q": keyword, "limit": limit},
        )
        response.raise_for_status()

        return [
            Product(
                platform=self.platform,
                product_id=str(item["no"]),
                name=item["name"],
                url=item["link"],
                price=item.get("price"),
            )
            for item in response.json()["items"]
        ]

    async def get_product(self, product_id: str) -> Product:
        raise NotSupportedError(f"{self.platform}: 미구현")

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        raise NotSupportedError(f"{self.platform}: 미구현")
```

### JS 렌더링이 필요한 경우

`BrowserCollector` 를 상속하면 Playwright 를 쓸 수 있습니다. `self.client`(httpx)도 그대로 사용 가능하므로, 상품은 API·리뷰는 렌더링 같은 혼합 방식도 됩니다.

```python
from review_data.core.browser import BrowserCollector


class MusinsaCollector(BrowserCollector):
    platform = "musinsa"
    label = "무신사"

    async def get_reviews(self, product_id: str, limit: int = 50) -> list[Review]:
        async with self.page() as page:
            await page.goto(f"https://www.musinsa.com/products/{product_id}")
            await page.wait_for_selector(".review-list")
            html = await page.content()
        ...
```

브라우저는 collector 당 1개만 띄우고 재사용합니다. 페이지가 필요할 때마다 `self.page()` 컨텍스트를 쓰면 블록을 벗어날 때 자동으로 닫힙니다.

브라우저 기반 collector 는 수집마다 Chromium 을 띄우므로, 워커에서 동시 실행 수를 따로 제한합니다(기본 1건). 설정값은 `core/settings.py` 의 `max_concurrent_browser_jobs_per_platform` 입니다.
