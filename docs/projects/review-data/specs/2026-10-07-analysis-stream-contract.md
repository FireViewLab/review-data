# 분석 SSE 합의 계약과 HTTP 클라이언트

## 상태와 범위

담당자와 POST 저장 리뷰 → 응답 SSE 방향 및 요청 식별자 계약을 합의했다.
운영 base URL은 https://ai.re-view.kr이며 신규 POST /api/v1/data/analyze/stream을
AI 서버에서 구현한다. 기존 POST /api/v1/data/analyze JSON API는 유지한다.
AI main 2346111의 docs/data-analysis-stream.md와 구현에서 실제 이벤트 형식을 확인했다.
인증은 서버 AI_INTERNAL_TOKEN으로 설정하고 저장 상품 하나의 소규모 연동을 검증한다.
운영 분석 워커는 기본 비활성으로 유지한다. 활성화·저장·배포 절차는
분석 파이프라인 문서를 따른다.

`core/analysis_stream.py`는 HTTP 스트림 경계 검증을 담당한다.
기존 JSON 초안을 재사용하거나 기존 Product/Review 계약을 변경하지 않는다.

## 인터페이스와 요청

```python
client = AnalysisStreamClient(
    url, token=None, timeout=300, model_version=None, policy_version=None
)
result = await client.analyze(platform, product_id, reviews, job_id, input_hash)
# result.results는 리뷰별 결과 사전의 목록이다.
# result.model_version과 result.policy_version은 문자열 또는 None이다.
```

url은 전체 endpoint이며 경로를 추가하지 않는다. POST JSON body는
`{platform, product_id, reviews: [{review_id, content, rating, written_at}]}`이다.
본문의 공백과 원본 식별자를 변형하지 않는다. datetime은 ISO 8601 문자열로 보낸다.
리뷰 목록·식별자·본문이 비어 있거나 요청 ID가 중복되면 전송 전에 거부한다.

헤더:

- `Accept: text/event-stream`
- token이 있을 때만 `X-Internal-Token: <token>`
- `X-Request-ID: <Data analysis_job_id>`
- `Idempotency-Key: <Data analysis_job_id>`

두 식별자 헤더는 같은 Data job ID의 십진수 문자열이다. 같은 job 재시도는 같은 key를
사용한다. AI는 중복 분석을 막고 이미 완료된 결과는 SSE로 재전송한다. 입력·설정 버전이
바뀌면 Data가 새 job ID를 발급한다. 입력 스냅샷·hash는 Data DB에 보존한다.
AI 내부 작업용 X-Analysis-Job-ID는 Data에서 전송하지 않고 외부 연동 ID와 구분한다.
모든 이벤트 request_id는 Data ID의 문자열이다. meta/done/error의 ai_job_id는
AI 내부 ID이며 Data ID와 구분한다. AI 내부 ID는 완료 기록 result JSON에 보존한다.
job_id는 양의 정수이며
boolean은 거부한다. 버전 옵션은 body에 추가하지 않고 meta 검증에 사용한다.
redirect는 따르지 않는다. HTTP 클라이언트·응답 스트림은 각 호출의 async context에서
닫는다. timeout은 httpx의 연결·쓰기·읽기·풀 단계 timeout이며 총 실행시간 제한과 다르다.
자동 재시도는 하지 않는다.

## SSE 프레임과 이벤트

UTF-8, LF/CRLF, 빈 줄 구분, 주석, 여러 data 줄을 처리한다. 여러 data 줄은 LF로
합쳐 JSON을 읽는다. 주석과 id/retry만 있는 프레임은 이벤트로 취급하지 않는다.
첫 프레임의 UTF-8 BOM을 허용한다. 프레임 최대 크기는 구분 줄을 포함해 1MiB이다.
초과 프레임·잘못된 UTF-8/JSON·중복 JSON 키·미완성 EOF는 실패한다.

실제 첫 이벤트는 반드시 meta이다. 모든 이벤트 data는 JSON object이다.

| 이벤트 | data 계약 |
|---|---|
| meta | `request_id` 문자열·`ai_job_id`·platform/product_id/review_count·contract_version=v0.5·model_version/policy_version 필수 |
| result | `request_id`, `review_id`, `rti`, `level`, `text_score`, `behavior_score`, `network_score`, `reasons` 필수 |
| heartbeat / progress | request_id 필수. progress는 stage/processed/total, heartbeat는 약15초 간격이며 짧은 분석에는 없을 수 있다 |
| done | request_id·ai_job_id·result_count 필수. Data ID·meta의 AI ID·전체 입력 건수와 일치해야 한다 |
| error | request_id·ai_job_id를 검사하고 실패한다. code/message는 AI 제공, retryable=true일 때만 재시도한다 |

설정된 model_version/policy_version은 meta에 존재하고 정확히 같아야 한다.
설정이 없으면 meta에서 받은 버전을 보존한다. meta는 한 번만 허용한다.
알 수 없는 이벤트나 meta 전의 업무 이벤트는 거부한다.

점수는 JSON 숫자나 null만 허용한다. -1/null은 None으로 정규화하며 0은 보존한다.
RTI와 component 점수는 유한한 0~100 숫자여야 한다. boolean·숫자 문자열·NaN·Infinity와
그 밖의 음수는 거부한다. 숫자는 Decimal로 반환해 DB Numeric에 저장할 수 있게 한다.
level은 safe/warn/danger/null, reasons는 문자열 배열이며 빈 배열을 허용한다.

result review_id는 입력에 있어야 한다. 정규화한 최종 값이 같은 중복은 무시하고,
다른 값의 중복은 실패한다. 결과 누락이나 외부 ID는 실패한다. done의 result_count는
중복 이벤트 수가 아닌 전체 입력 리뷰 수이다. 반환 results는 입력 리뷰 순서이다.

done과 EOF를 모두 확인한 뒤에만 결과를 반환한다. done 이후의 비어 있지 않은 프레임은
주석 프레임을 포함해 거부한다. 완료 프레임 뒤 빈 구분 줄은 허용한다. 완료된 done 뒤에도
연결 오류나 timeout이 나면 성공으로 반환하지 않는다. 부분 결과를 DB에 저장하지 않는다.

## 오류 분류

`AnalysisStreamError.retryable`로 호출자가 재시도 여부를 판단한다.

- HTTP 429/5xx, timeout과 전송 실패, done 없는 EOF/미완성 프레임 EOF: true.
- HTTP409는 크기 제한된 detail.code=IDEMPOTENCY_IN_PROGRESS만 같은 키로 재시도한다.
  IDEMPOTENCY_KEY_REUSED/IDEMPOTENCY_FAILED 및 알 수 없는 코드·손상 응답은 재시도하지 않는다.
- HTTP 401/422 및 나머지 비정상 HTTP, redirect, 잘못된 URL·요청·응답 계약: false.
- error 이벤트는 위의 명시적 retryable 값에 따른다. 잘못된 타입은 계약 오류이다.

취소는 상위 task에 그대로 전달하면서 자원을 닫는다. 토큰·원본 본문·서버 오류 본문을
로그로 남기지 않는다. 응답의 부분 결과는 실패 시 반환하지 않는다.

## 독립 검증

httpx MockTransport/AsyncByteStream으로 임의 청크 경계, CRLF·주석·여러 data 줄,
프레임 크기, EOF, 정상 결과와 null/-1/0, 버전·job 상관 검증, 중복·누락·외부 ID,
잘못된 JSON·알 수 없는 이벤트·done 뒤 프레임, HTTP 오류·timeout·자원 정리를 확인한다.
실제 서버·DB는 호출하지 않는다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_analysis_stream.py
.venv/bin/ruff check src/review_data/core/analysis_stream.py tests/test_analysis_stream.py
```

독립 SSE 테스트 185개, PostgreSQL 포함 전체 526개 통과.
토큰·본문·오류 원문을 출력하지 않는다.

## 운영 계약 출처

[AI 운영 연동 문서](https://github.com/FireViewLab/review-ai-db/blob/2346111/docs/data-analysis-stream.md)를 따른다.
계산 모델은 ptext-koelectra-v1-2epoch-20260929, 정책은 rti-v0다. AI가 가중치와 등급을
계산하고 Data는 최종 점수·레벨·이유를 저장한다. -1은 null, 0은 0으로 보존한다.
AI는 RUNNING/FAILED 자동 회수를 제공하지 않는다. 재시도 상한에 도달하면 운영 확인이 필요하다.
