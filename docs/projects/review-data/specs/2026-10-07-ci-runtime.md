# CI 실행 환경 갱신

## 변경

CI와 배포의 실행 환경을 ubuntu-24.04로 고정한다. ubuntu-latest의 OS 전환으로
브라우저 패키지 및 Python 설치 환경이 함께 바뀌지 않도록 한다.

Node.js 24 기반 공식 액션으로 변경한다.

| 액션 | 버전 |
|---|---|
| actions/checkout | v7.0.1 |
| actions/setup-python | v7.0.0 |
| docker/setup-buildx-action | v4.4.1 |
| docker/build-push-action | v7.4.0 |
| docker/login-action | v4.6.0 |

공식 저장소의 최신 release 태그와 action.yml의 node24 실행 설정을 확인한다.
Docker 기반 scp-action과 ssh-action은 변경하지 않는다.

## 검증

actionlint와 ShellCheck를 통과한다. PostgreSQL 통합 테스트에서 skip 발생 시
실패하는 기존 정책을 유지한다. feature PR과 release PR에서 이미지 빌드·테스트를
확인한 뒤 main 배포의 이미지 업로드와 서버 교체를 확인한다.

참고: https://github.com/actions/checkout, https://github.com/actions/setup-python,
https://github.com/docker/setup-buildx-action, https://github.com/docker/build-push-action,
https://github.com/docker/login-action.
