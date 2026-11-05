# 구조와 보장 범위

```text
Agent A ─ client.py / HTTP ─ session A ─┐
Agent B ─ client.py / HTTP ─ session B ─┼─ shared broker (127.0.0.1)
Agent C ─ client.py / HTTP ─ session C ─┘       │
                                      sessions + cooperative task queue
                                              │
                                      lease + request cache
                                              │
                                      serialized backend calls
                                              │
                                      OS-selected native backend
                                  macOS AX / Windows UIA / Linux AT-SPI
                                              │
                                      one OS desktop
```

## 다중 에이전트

HTTP 연결은 여러 스레드에서 받지만 backend 호출과 세션 상태 변경은 같은 mutex 안에서 실행한다. 같은 데스크톱은 포커스·활성 앱을 공유하므로 앱별 잠금만으로는 안전하지 않다. 세션별 비밀 토큰은 다른 에이전트의 세션/스냅샷을 실수로 재사용하는 것을 막는다. 잠금 소유권은 매 획득 때 새 토큰과 증가하는 generation으로 구분한다. 만료된 소유자가 돌아와도 예전 토큰으로 조작할 수 없다.

서버 CLI는 모든 native 백엔드에서 사용자별 파일 잠금으로 두 번째 broker 실행을 거부한다. POSIX는 flock, Windows는 msvcrt byte-range lock을 사용한다. 모든 에이전트는 같은 상태 디렉터리 설정을 사용한다. 프로세스 worker를 늘리거나 서로 다른 포트에 broker를 복제하지 않는다. 다른 사용자 계정·외부 자동화 도구·사람의 입력은 이 잠금에 참여하지 않는다.

시분할은 OS별 입력 계층 위의 공통 broker에서 수행한다. 에이전트가 enqueue한 의도를 FIFO로 배정하고, 실행 중인 에이전트가 안전한 작업 경계에서 yield하면 다음 차례를 준다. 재대기는 큐 끝으로 이동하므로 협력하는 에이전트는 순환하며 작업한다. quantum은 양보 권고인 soft limit이며 native 호출이나 여러 단계의 UI 작업을 중간에 강제 선점하지 않는다. `renew`도 quantum을 초기화하지 않는다.

작업 큐는 UI 명령이나 node_id를 실행 대기시키지 않는다. 각 차례는 새 lease token/generation을 사용하고 다시 status를 읽어야 한다. 스케줄러 메타데이터만 읽는 turn과 달리 status도 UI 호출과 직렬화되므로, 대기자는 제한된 간격의 turn polling을 사용한다. TTL/큐 만료는 작업 종료 상태로 기록하고 동작을 재생하지 않는다. 미확정 결과를 재관찰하는 책임은 에이전트에 있다.

기존 수동 acquire는 호환 모드다. 수동 lease를 가진 에이전트가 계속 갱신하거나 예약 차례 소유자가 양보하지 않으면 공정한 교대를 보장하지 못한다. 공유 폼처럼 한 에이전트의 중간 결과를 다른 에이전트가 바꿀 수 있는 작업은 함께 완료·검증할 단위를 먼저 정한다. 협력적 시분할은 앱 데이터나 사용자 입력을 격리하지 않는다.

진짜 병렬 UI 실행이 필요하면 별도 VM/OS 로그인 세션마다 broker를 띄워 데스크톱 자체를 격리한다. 앱이나 탭만 다르다는 이유로 이 구현의 전역 잠금을 제거하지 않는다.

## 관찰과 동작

`snapshot_id`는 세션 소유의 짧은 수명 참조이고, `revision`은 상태 변경 및 조작 시도 때 증가한다. 조작 전에 backend 상태를 다시 읽고 전체 반환 상태의 SHA-256과 revision을 비교한다. 각 native worker는 마지막 관찰의 실제 접근성 객체 참조를 보관한다. 경로 기반 node ID가 다른 항목으로 이동했어도 관찰 비교를 통과하기 전에는 실행하지 않는다.

OS UI 읽기와 동작을 하나의 원자적 트랜잭션으로 묶을 수는 없다. 관찰 중의 변경, dispatch 직후 사용자 개입, 접근성 API가 노출하지 않는 상태는 완전히 탐지할 수 없다. 트리가 잘렸을 때 해시가 같다는 사실은 전체 UI의 동일성을 보장하지 않는다. 여러 단계 동작은 각각 관찰하고 검증한다.

## OS 로딩과 worker

`backend_factory.py`가 OS를 확인하고 선택된 백엔드만 import한다. mock은 native 의존성을 import하지 않는다. macOS는 기존 Swift JSONL 프로세스를 사용한다. Windows/Linux는 `process_backend.py` → `python_worker.py`의 단일 스레드 프로세스 안에서 native adapter를 로딩한다. Windows는 UIA COM 객체를 만든 MTA 스레드에서 사용하고 Linux는 관찰 전에 AT-SPI 캐시를 갱신한다.

Windows/Linux pipe I/O는 POSIX select에 의존하지 않는다. 응답 시간 초과 시 worker를 종료하고 객체 참조를 무효화한다. 다음 status로 worker를 새로 시작할 수 있지만 act는 자동으로 worker를 재시작하거나 조작을 재전송하지 않는다. 죽은 worker에 보낸 동작은 결과 미확정으로 처리한다. 초기 의존성 오류는 서버 시작 때 구조화된 오류로 출력한다. macOS Swift 컴파일/권한 검사는 첫 status에서 수행한다.

같은 요청을 재전송하면 세션 메모리에 저장한 결과/오류를 반환한다. 세션당 256개 결과를 보관하며 임의로 지우고 다시 실행하지 않는다. 한도가 차면 새 세션이 필요하다. 이는 **살아 있는 서버의 동일 세션 안에서의 중복 억제**다. 프로세스 crash를 넘는 exactly-once 보장은 없으며 재시작 후 미확정 조작은 상태를 조사해야 한다.

## 배포 범위

Python 표준 HTTP 서버를 사용하는 로컬 에이전트 도구다. loopback 주소만 bind하고, bearer token, 세션 token, Host/Origin 검사, 크기/연결 수 제한을 적용한다. 공유 bearer token을 가진 같은 OS 사용자들은 동일 신뢰 영역이며, 적대적인 tenant 간 보안 경계는 아니다. 인터넷 공개·TLS 원격 제공·영속 작업 큐·MCP transport는 이 구현의 범위가 아니다.

원격 호스트가 필요하면 인증·TLS·권한 분리와 각 호스트의 broker를 갖춘 별도 gateway를 구현한다. MCP가 필요하면 HTTP API를 호출하는 adapter를 추가하되 잠금/상태 검증을 우회하지 않는다.

## 백엔드 추가

backend는 `snapshot() -> dict`, `act(action) -> dict`, `close()`를 구현한다. 상태에는 `platform`, `desktop_id`, `frontmost_app`, `applications`, `roots`, `nodes`, `truncated`, `capabilities`를 넣고 노드에는 id/role/actions/settable을 넣는다. 변경 없는 UI의 상태는 결정적으로 직렬화되어야 한다. 벽시계 timestamp, 매번 달라지는 ID 같은 필드는 상태 해시에 넣지 않는다. 활성 앱의 UI를 조작하는 백엔드는 전역 직렬화를 유지한다.

현재 Windows UI Automation, macOS AX, Linux AT-SPI 어댑터를 제공한다. OS별 role/action 이름을 공통 문자열로 강제 변환하지 않으므로 에이전트는 매번 반환된 capability를 사용한다. 격리 브라우저 DOM 등의 백엔드는 추후 구현 대상이다.

공식 자료: [Apple AXUIElement](https://developer.apple.com/documentation/applicationservices/axuielement), [Python http.server의 배포 범위](https://docs.python.org/3/library/http.server.html).
