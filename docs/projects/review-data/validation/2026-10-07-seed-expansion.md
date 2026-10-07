# 네이버 시드 후보와 에이블리 운영 검증

검증일: 2026-10-07 KST. 이후 추가 원격 수집은 중단한다.
기존 옥션·G마켓 접근 검증 문서는 유지한다.

## 실행 전 상태

- GCP 메모리: 총 3,908MB, 사용 1,189MB, 가용 2,719MB, swap 없음.
- API 약 135.5MiB/1GiB, worker 약 165.7MiB/2GiB.
- 전체 pending/running 수집 job 0건.
- 에이블리 상품 0건, 리뷰 0건, 활성 job 0건.
- 네이버 DB 상품 21건, 리뷰 수집 완료 상품 21건, 리뷰 438건.
  기존 DB 값이며 아래 읽기 검증의 신규 리뷰 40건을 포함하지 않는다.

실행한 상태 확인 명령:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 -i ~/.ssh/review-data-deploy deploy@34.50.27.128 \
  'cd /home/deploy/review-data && free -m && docker stats --no-stream --format "{{.Name}} {{.MemUsage}} {{.CPUPerc}}" && docker compose exec -T db psql -U postgres -d review_data -c "SELECT platform, status, count(*) FROM collection_jobs GROUP BY platform,status ORDER BY platform,status;"'
```

## 네이버 후보별 결과

SSH의 `docker compose exec -T api python -`에 Python을 표준입력으로 전달했다.
기존 `NaverCollector(settings=get_settings())`에서 같은 collector의 `get_product`와
`get_reviews(limit=20)`를 차례로 호출했다. 상품/리뷰 DB 저장이나 확장 시드는 실행하지 않았다.

| 후보 | 상세 | 리뷰 | 실제 본문 수신 | seeds.toml |
|---|---|---|---|---|
| `locknlock:11614753248` | 성공: 락앤락 임직원 패밀리 세일 up to 55% | 성공, 상품 전체 표시 1,633건 | 20건 | 추가 |
| `philipshue:8140149248` | 성공: 필립스 휴 그라디언트 사인 플로어 무드등 거실등 | 성공, 상품 전체 표시 815건 | 20건 | 추가 |
| `philips:10730284372` | `get_product` 성공 후 다음 단계 진행 | `ParseError: [naver] 리뷰 목록을 받지 못했습니다: philips:10730284372` | 성공 반환 없음 | 제외 |

필립스 면도기의 상세 결과명·전체 리뷰 수는 리뷰 실패 후 출력하지 않아 기록하지 못했다.
상품 상세 실패나 CAPTCHA로 판정한 사례가 아니라 **리뷰 응답 수신 실패**이다.
그 후보를 다시 방문하거나 다른 요청으로 보완하지 않았다.

실제 정상 검증한 주소:

- https://brand.naver.com/locknlock/products/11614753248
- https://brand.naver.com/philipshue/products/8140149248

락앤락과 필립스 휴는 상품별로 비어 있지 않은 본문 20건, 합계 40건을 확인했다.
이는 전체 리뷰 수집이나 두 번째 페이지 성공을 뜻하지 않는다. 기존 collector는 첫
페이지 최대 20건만 지원한다. 네이버 신규 DB 저장 상품/리뷰는 0건이다.

변경은 `[products] naver`의 기존 `zinus:6000252751`에 위 두 식별자를 추가한 것이다.
`per_keyword=20`, 다른 플랫폼 키워드, `[expand] naver=20`과 입력 형식은 유지했다.
이번에는 서버 seeds.toml을 교체하거나 새로운 네이버 확장 job을 예약하지 않았다.
파일 통합 후 처음 확장을 실행하면 관련 상품마다 성공 여부를 따로 확인해야 한다.

## 에이블리 실행과 중단

전체 플랫폼 시드는 실행하지 않았다. 기존 서버 시드 파일에서 에이블리 5개 키워드만
선택한 첫 실행은 아직 첫 키워드의 상세 검증 중일 때 범위를 축소하라는 지시에 따라
SIGINT로 취소했다. 취소한 프로세스의 PID와 시작 시각 식별자를 확인했으며 서비스
프로세스나 다른 검증 프로세스는 종료하지 않았다. 첫 실행은 검색 결과 저장 전 중단됐다.

이후 단일 키워드 `원피스`, 최대 상품 3개인 작은 계획으로 한 번 확인했다. 기존
collector의 후보 건너뛰기 때문에 차단 뒤 다음 후보로 계속 진행하지 않도록 검증
프로세스 내부의 subclass에서 첫 상세 예외를 즉시 전체 중단으로 전달했다.
저장소·서버 설치 코드·서비스 설정은 바꾸지 않았다.

실제 실행한 SSH 명령 형태:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 -i ~/.ssh/review-data-deploy deploy@34.50.27.128 \
  'cd /home/deploy/review-data && docker compose exec -T api python -' <<'PY'
# 아래의 단일 키워드 계획을 기존 SeedingService에 전달한 검증 Python
PY
```

실행 Python의 핵심 동작은 다음과 같다. 전체 스크립트는 시작/종료 DB 건수 확인,
비밀 문자열 제거와 엔진 정리도 포함했다.

```python
class StopProbe(BaseException):
    pass

class LimitedAbly(AblyCollector):
    async def get_product(self, product_id):
        try:
            return await super().get_product(product_id)
        except Exception as exc:
            raise StopProbe(f"상품 {product_id}: {type(exc).__name__}: {exc}") from exc

plan = SeedPlan(3, {"ably": ("원피스",)}, {})
await asyncio.wait_for(
    SeedingService({"ably": LimitedAbly}, factory, settings).run(plan), 120
)
```

에이블리 결과:

```text
실행 전: products=0, reviews=0, active_jobs=0
첫 상세 중단: 상품 21096052: ParseError: [ably] 에이블리 보안 확인 페이지가 표시됐습니다.
실행 후: products=0, reviews=0, active_jobs=0
```

**운영 에이블리 상품 0건/리뷰 0건을 유지했다.** 원인은 작은 검증의 첫 상품 상세
보안 확인 화면이다. 리뷰 엔드포인트까지 진행하지 못했으며 실제 리뷰 본문은 수신하지
못했다. HTTP 상태나 보안 화면 종류를 별도 네트워크 계측으로 확정하지는 않았다.
현재 collector의 보안 판정과 예외를 기록한 결과이다. 차단 후 다른 후보·키워드·호스트로
추가 접근하지 않는다. 에이블리 네이버 검색 색인이 존재해도 상세·리뷰 성공을 보장하지 않는다.

## 통합 검증

기본 시드 검증의 기대값을 지누스·락앤락·필립스 휴 세 곳으로 갱신한다.
운영 DB와 분리한 PostgreSQL 16에서 전체 테스트 320개가 통과했다(skip 없음).
CI와 배포 후 시드 실행 결과는 별도로 확인한다.

## 변경 범위

- `seeds.toml`
- `docs/projects/review-data/specs/2026-10-07-review-behavior-evidence.md`
- 이 검증 문서

모델·마이그레이션·API·CI·README·roadmap·Handover는 편집하지 않았다.
공유 브랜치 변경, commit, push, 이슈 등록, 배포, merge, 서버 설정 변경은 수행하지 않았다.
