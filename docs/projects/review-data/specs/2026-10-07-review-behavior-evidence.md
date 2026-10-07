# 플랫폼별 리뷰 행동 근거 조사

조사일: 2026-10-07. 범위는 현재 collector 9개, 공통 Review 계약, 공식 자료,
이번에 정상 수신한 네이버 브랜드스토어 응답이다. collector나 공통 계약은 변경하지 않는다.

## 판정 기준

- 구매 인증은 리뷰별 원본 근거와 구매 후 작성 정책을 구분한다. 정책만 보고 모든
  리뷰의 구매 인증 값을 true로 채우지 않는다.
- 회원 ID는 닉네임, 마스킹 문자열, 내부 식별자를 구분한다. 닉네임/마스킹값만으로
  동일 회원을 묶거나 다른 플랫폼의 회원과 연결하지 않는다.
- 작성 수는 **작성자 개인의 리뷰 작성 수**이다. 상품 전체 리뷰 수인
  `Product.review_count`, 수집한 리뷰 수, `helpful_count`와 다르다.
- `freeTrial`과 `repurchase`는 해당 키가 실제로 관측된 경우에만 값의 존재를 인정한다.
  누락은 false가 아니다. 이번 응답의 false도 플랫폼 전체의 부재를 뜻하지 않는다.
- 미수집은 현재 코드가 매핑하지 않는다는 뜻이다. 미확인은 공개 원본 제공 여부를
  이번 조사로 확정할 수 없다는 뜻이다. 접근 차단은 정보가 없는 상태가 아니다.

## 현재 collector 결과

공통 `Review`에는 구매 인증, 안정적인 회원 키, 작성자 작성 수, 체험단, 재구매 필드가
없다. 아래 원본 후보를 확보했더라도 해당 행동 근거는 현재 표준 응답·DB에 매핑되지 않는다.
원본 필드의 개인정보 값은 조사 문서에 기록하지 않았다.

| 플랫폼 | 리뷰별 구매 인증 | 작성자/회원 ID 제공 근거 | 작성자 작성 수 | freeTrial / repurchase | 코드 근거 |
|---|---|---|---|---|---|
| 네이버 | 이번 샘플에서 `purchase`/`purchaseVerified` 미관측; 정책은 구매확정 연계 | 정상 원본에서 `maskedWriterId`, `writerId`, `writerMemberNo` 관측. 표준 `author=None` | `writerReviewCount` 미관측, 현재 미수집. 제공 여부 전체는 미확인 | 두 키 모두 실제 관측, 현재 미수집 | `collectors/naver/collector.py`의 `parse_reviews` |
| 무신사 | 현재 미수집; 원본 구매 인증 필드 미확인 | `userProfileInfo.userNickName`을 `author`로 매핑. 닉네임은 안정적인 회원 ID가 아니다 | 현재 미수집; 원본 제공 여부 미확인 | 현재 미수집; 원본 제공 여부 미확인 | `collectors/musinsa/collector.py`의 `_parse_review` |
| 올리브영 | 현재 미수집; 원본 구매 인증 필드 미확인 | `profileDto.memberNickname`을 `author`로 매핑. 회원 ID 제공 여부 미확인 | 현재 미수집; 원본 제공 여부 미확인 | 현재 미수집; 원본 제공 여부 미확인 | `collectors/oliveyoung/collector.py`의 `_parse_review` |
| 컬리 | 현재 미수집; 원본 구매 인증 필드 미확인 | `ownerName`, 없으면 `author`를 매핑. 이름 문자열만으로 회원 ID를 확정하지 않는다 | 현재 미수집; 원본 제공 여부 미확인 | 현재 미수집; 원본 제공 여부 미확인 | `collectors/kurly/collector.py`의 `_parse_review` |
| 11번가 | 현재 미수집; 원본 구매 인증 필드 미확인 | `.c_product_reviewer` 표시 문자열을 `author`로 매핑. 닉네임/마스킹 여부는 문자열만으로 확정하지 않는다 | 현재 미수집; 원본 제공 여부 미확인 | 현재 미수집; 원본 제공 여부 미확인 | `collectors/elevenst/collector.py`의 `_parse_review` |
| 오늘의집 | 현재 미수집; 문의의 구매/비구매 표시는 리뷰별 구매 인증으로 전용하지 않는다 | 현재 `author=None`; 현재 파서에서 회원 키 제공 근거 없음 | 현재 미수집; 원본 제공 여부 미확인 | 현재 미수집; 원본 제공 여부 미확인 | `collectors/ohouse/collector.py`의 `_parse_review` |
| 에이블리 | 현재 미수집; 이번 상세 보안 차단으로 추가 원본 확인 불가 | 공개 요약 리뷰의 `sno`, `contents`, `images`만 매핑하며 `author=None` | 현재 미수집; 원본 제공 여부 미확인 | 현재 미수집; 원본 제공 여부 미확인 | `collectors/ably/collector.py`의 `_parse_review` |
| 옥션 | 현재 미수집; 기존 상세 HTTP 403으로 실사이트 확인 불가 | `.text__writer` 표시 문자열을 `author`로 매핑하는 코드만 확인. 실제 회원 키 제공은 미검증 | 현재 미수집; 접근 차단으로 실사이트 미확인 | 현재 미수집; 접근 차단으로 실사이트 미확인 | `collectors/auction/collector.py`의 `_parse_review_page` |
| G마켓 | 현재 미수집; 기존 상세 HTTP 403으로 실사이트 확인 불가 | `td.info dl.writer-info dd` 첫 문자열을 `author`로 매핑하는 코드만 확인. 회원 키로 보장되지 않는다 | 현재 미수집; 접근 차단으로 실사이트 미확인 | 현재 미수집; 접근 차단으로 실사이트 미확인 | `collectors/gmarket/collector.py`의 `_parse_review_page` |

코드 기준 경로는 `src/review_data/` 아래이다. 공통 계약은 `core/models.py`의
`Product`와 `Review`를 읽기 검토했다. 도움돼요·좋아요 수를 작성 수로 변환하지 않는다.
G마켓 리뷰 ID를 만드는 해시에 표시 작성자 문자열이 포함돼 있어도 안정적인 회원 키가
새로 확보되는 것은 아니다.

## 네이버 실제 응답 관측

GCP에서 기존 `NaverCollector`로 상품 페이지를 정상 방문하고, 페이지가 스스로 받은
리뷰 응답만 확인했다. 내부 API를 별도로 호출하거나 쿠키/헤더를 복제하지 않았다.
검증은 읽기 전용이며 해당 리뷰 40건을 DB에 저장하지 않았다.

| 상품 | 상품 전체 리뷰 수 | 수신한 비어 있지 않은 본문 | freeTrial 키 존재 / true | repurchase 키 존재 / true | 회원 필드 |
|---|---|---|---|---|---|
| `locknlock:11614753248` | 1,633 | 20 | 20 / 0 | 20 / 0 | 세 키 모두 각 20건에서 존재 |
| `philipshue:8140149248` | 815 | 20 | 20 / 0 | 20 / 3 | 세 키 모두 각 20건에서 존재 |

회원 필드는 `maskedWriterId`, `writerId`, `writerMemberNo`이다. 값의 안정성·회원 간
중복·기간 간 지속성을 검증하지 않았고 값 자체는 출력·보관하지 않았다.
두 표본의 `writerReviewCount`는 각 0건에서 관측됐다. 락앤락 표본에서 최상위
`reviewCount`, `purchase`, `purchaseVerified`도 관측되지 않았다. 다른 이름이나
중첩 위치로 원본에 제공되는지까지 모두 배제한 결과는 아니다.

`freeTrial=false`만으로 금전·포인트·판매자 이벤트 등 모든 보상이 없었다고 판단하지
않는다. `repurchase=true`는 원본 재구매 표식이며 구매 횟수나 주문 내역 자체가 아니다.
네이버는 현재 랭킹순 첫 페이지 최대 20건만 수집하므로 이 표본의 비율을 전체 리뷰나
전체 회원의 행동 분포로 일반화하지 않는다.

## 공식 자료와 한계

| 플랫폼 | 확인한 공식 자료 | 확인 가능한 범위와 제한 |
|---|---|---|
| 네이버 | [리뷰 작성 안내](https://help.pay.naver.com/faq/content.help?faqId=10755) | 구매확정 후 리뷰 작성과 포인트 혜택을 설명한다. 이것만으로 리뷰별 구매 인증 boolean이나 작성자 전체 리뷰 수를 제공한다고 판단하지 않는다 |
| 무신사 | [오프라인 구매 후기 도입 안내](https://www.musinsa.com/content/1339053761465713500) | 오프라인 구매 제품의 후기 기능과 적립금 이벤트를 설명한다. 공지의 특정 이벤트를 모든 온라인 리뷰의 인증·무보상 정책으로 일반화하지 않는다 |
| 컬리 | [공식 기술 블로그: 후기 개선](https://helloworld.kurly.com/blog/review-renewal/) | 작성자의 등급·구매 상품·뷰티프로필과 주문/배송 기반 작성 가능 후기 목록을 설명한다. 2022년 글로, 현재 API의 회원 ID·작성 수 제공 계약이 아니다. 현재 FAQ는 목록 본문을 충분히 읽지 못했다 |
| 올리브영 | [공식 FAQ](https://www.oliveyoung.co.kr/store/counsel/getFaqList.do?faqLrclCd=300) | 리뷰·탑리뷰어체험단 항목은 확인했다. 반환된 FAQ 본문은 오늘드림 항목이라 리뷰별 구매 인증·회원 ID·작성 수 정책을 확정하지 못했다 |
| 오늘의집 | [공식 고객센터](https://ohou.se/contact_us) | 검색 색인에서 리뷰 운영 기준과 보상 미공개 리뷰 금지 안내를 확인했다. 직접 페이지 읽기는 도구 오류로 실패했다. 개별 리뷰의 구매 인증 boolean 제공은 미확인 |
| 에이블리 | [공식 앱 설명](https://apps.apple.com/kr/app/id1084960428) · [공식 팀 소식](https://ably.team/news/ZQju2xIAAB8AyMLF) | 앱 설명은 구매 후 리뷰 작성 기능을, 팀 소식은 리뷰·구매 이력을 추천에 활용함을 설명한다. 내부 활용 데이터가 공개 리뷰 응답에 제공된다는 근거는 아니다 |
| 11번가 | [공식 상품 리뷰 화면](https://www.11st.co.kr/products/1908805182) | 표시 작성자와 리뷰 목록은 확인 가능하다. 상품 화면만으로 작성자 전체 작성 수나 리뷰별 구매 인증 필드의 제공을 확정하지 못했다 |
| G마켓 | [공식 안전거래 상품평 안내](https://help.gmarket.co.kr/SecurityCenter/Partial/_Guide?lkind=04&mkind=08&skind=52) | 구매자가 수령 후 거래를 평가하는 상품평 정책을 설명한다. 현재 collector의 리뷰별 구매 인증 필드 제공과는 별개이다 |
| 옥션 | [공식 안전거래 상품평 안내](https://member.auction.co.kr/SecurityCenter/Sub.aspx?code=D0203) | 검색 색인에서 발송 후 한줄 상품평·구매결정 후 꼼꼼 상품평 안내를 확인했다. 직접 문서 읽기는 도구 오류였다. 상품평 전부를 구매확정 완료로 단정하지 않는다 |

공식 회원 문의 API의 `maskedWriterId` 안내는 상품 문의에 관한 자료이므로 리뷰 API의
회원 연결 계약으로 전용하지 않았다. 비공식 블로그·커뮤니티는 신규 상품 URL 발견에만
사용했고 플랫폼 정책·회원 ID 제공의 근거로 쓰지 않았다.

## 결론과 후속 조건

현재 확실한 신규 행동 근거 후보는 네이버의 원본 `freeTrial`·`repurchase`이다.
실제 재구매 표식 true도 확인했지만 현재 파서는 버린다. 다른 플랫폼은 이번 조사로
제공되지 않는다고 결론내릴 수 없다. 확인하지 못한 값은 미확인으로 남긴다.

정식 저장을 추가하려면 별도 팀 계약 변경과 개인정보 검토가 필요하다. 회원 ID 값을
지금 문서나 표준 `author`에 옮기지 않는다. 원본 의미·관측 경로·결측 사유를 함께
정의한 뒤 증거 필드를 추가해야 한다. 이번 작업은 조사 문서와 검증된 시드 입력만
변경하며 모델·API·DB·collector 변경은 포함하지 않는다.
