# 운영 AI SSE 실연동

AI 저장소 main 2346111의 docs/data-analysis-stream.md와 실제 구현을 기준으로 한다.
POST https://ai.re-view.kr/api/v1/data/analyze/stream, X-Internal-Token 인증이다.
Data job ID는 두 요청 헤더와 모든 이벤트의 request_id(문자열)에 유지한다.
AI 내부 ai_job_id는 meta/done/error 및 응답 X-Analysis-Job-ID에 제공한다.
meta의 상품·입력 건수·contract_version=v0.5·모델·정책 버전을 검증한다.
모든 이벤트의 request_id, done의 ai_job_id와 result_count를 검증한 뒤 정상 EOF를 요구한다.
AI 내부 ID는 Data 작업 완료 기록의 result JSON에 보존한다.

409의 detail.code=IDEMPOTENCY_IN_PROGRESS는 같은 키·본문으로 재시도한다.
IDEMPOTENCY_KEY_REUSED/IDEMPOTENCY_FAILED 및 알 수 없는 409는 재시도하지 않는다.
오류 응답은 크기 상한을 적용하고 토큰·본문·원문 오류를 기록하지 않는다.
AI RUNNING/FAILED 자동 회수는 없으므로 재시도 상한을 넘으면 운영 확인이 필요하다.

서버 AI_INTERNAL_TOKEN에만 인증을 설정하고 기존 Data INTERNAL_TOKEN은 유지한다.
검증은 리뷰 1~20건의 저장 상품 하나만 예약해 일회성 분석 워커로 처리한다.
전체 서비스 AI_ANALYSIS_ENABLED=false를 유지하며 backfill은 실행하지 않는다.
최초 결과 저장·같은 키 replay·다른 본문 409·상태/결과 조회를 확인한다.
실제 이벤트 검증에 실패하면 완료로 게시하지 않고 계약을 보완한다.
