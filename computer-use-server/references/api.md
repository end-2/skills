# HTTP API v1

기본 주소는 `http://127.0.0.1:8765`다. 모든 요청에 `Authorization: Bearer <token-file 내용>`이 필요하다. 세션별 요청에는 `X-Session-Token`도 필요하다. POST는 `Content-Type: application/json`과 `Content-Length`를 사용한다. 브라우저 origin, localhost 별칭, 임의 Host는 허용하지 않는다.

성공은 `{"ok":true,"result":{...}}`, 실패는 `{"ok":false,"error":{"code":"...","message":"...","details":{...}}}`다. API는 UI 텍스트를 로그로 출력하지 않는다. 상태 응답 자체에는 업무 데이터가 포함될 수 있으므로 필요한 부분만 에이전트 간 전달한다.

| 경로 | 메서드 | 본문 | 결과 |
| --- | --- | --- | --- |
| `/v1/health` | GET | 없음 | 프로세스 생존, instance_id, backend 클래스, selected_backend, os. UI 접근 검사는 아님 |
| `/v1/sessions` | POST | `{"agent_id":"worker-a"}` | session_id, session_token, instance_id, idle_timeout_seconds |
| `/v1/sessions/{id}/status` | POST | `{}` | snapshot_id, revision, state, lease, snapshot_ttl_seconds |
| `/v1/sessions/{id}/acquire` | POST | `{"ttl":30}` | lease_token, generation, expires_in |
| `/v1/sessions/{id}/renew` | POST | `{"lease_token":"...","ttl":30}` | 갱신된 lease |
| `/v1/sessions/{id}/release` | POST | `{"lease_token":"..."}` | released |
| `/v1/sessions/{id}/enqueue` | POST | `{"task_id":"task-a","intent":"입력 및 결과 검증","quantum_ms":1000,"ttl":30,"queue_timeout":60}` | 작업 상태 객체; 차례가 오면 state=running |
| `/v1/sessions/{id}/turn` | POST | `{"task_id":"task-a"}` | 작업 상태 객체; 자기 running 차례의 lease_token과 yield_due |
| `/v1/sessions/{id}/yield` | POST | `{"task_id":"task-a","lease_token":"...","requeue":true}` | `{"yielded":true,"task":<작업 상태>}`; false이면 completed |
| `/v1/sessions/{id}/cancel` | POST | `{"task_id":"task-a"}` | 작업 상태 객체; queued를 cancelled로 변경, running은 yield 필요 |
| `/v1/sessions/{id}/act` | POST | 아래 참조 | accepted, result, revision, request_id, verification_required |
| `/v1/sessions/{id}/close` | POST | `{}` | closed; 해당 세션 잠금/작업 큐/기록도 정리 |

```json
{
  "request_id": "unique-request-uuid",
  "snapshot_id": "copied-from-status",
  "lease_token": "copied-from-running-turn-or-acquire",
  "action": {"kind": "perform", "node_id": "copied-node-id", "action": "AXPress"}
}
```

`act`는 정확히 위 네 필드를 받는다. request_id는 세션 안에서 유일해야 하며 최대 128자다. 조작 전체 payload를 같은 ID로 재전송할 때만 캐시 응답을 돌려준다. snapshot/lease를 바꾸는 것도 다른 payload다. 검증 실패도 캐시되므로 재관찰 후 새로 판단한 동작에는 새 ID를 쓴다.

## 협력적 시분할

스케줄러는 OS와 무관한 broker 계층에 있다. 큐에는 작업 의도만 저장한다. UI 명령을 서버에 예약하거나 미리 관찰한 node_id를 차례가 올 때 재생하는 API가 아니다. `running`을 확인한 에이전트가 **새 status → 대상 선택 → act → 결과 확인**을 실행한다. 큐 권한 할당은 자동이며, 양보 후에는 새 generation의 차례를 받고 이전 snapshot과 lease를 재사용하지 않는다.

| enqueue 옵션 | 기본값 | 범위와 의미 |
| --- | --- | --- |
| task_id | 필수 | 세션 안에서 작업을 식별하는 문자열, 1–128자 |
| intent | 필수 | 작업 목적을 설명하는 문자열, 1–512자. UI 명령/selector 저장 용도가 아님 |
| quantum_ms | 1000 | 100–10,000 ms. 차례가 시작된 뒤 양보를 권고할 시간 |
| ttl | 30 | 1–120초. 해당 차례의 lease 수명 |
| queue_timeout | 60 | 1–300초. 큐에서 기다릴 수 있는 시간 |

세션당 queued/running 작업은 하나다. 같은 task_id와 같은 옵션으로 enqueue를 재전송하면 현재 작업 상태를 반환한다. 옵션이 다르면 `request_conflict`이며, 완료된 ID를 다시 등록해도 실행을 재시작하지 않는다. 새 작업은 새로운 ID를 사용한다.

작업 상태 객체의 `state`는 `queued`, `running`, `completed`, `cancelled`, `expired` 중 하나다. 공통 필드는 task_id, intent, quantum_ms, turns, waiting_count, queue_position이다. queued에는 queue_expires_in이 추가되고, running에는 **최상위 필드** lease_token, generation, expires_in, slice_elapsed_ms, quantum_exhausted, yield_due가 추가된다. completed/cancelled/expired는 종료 상태다. `status`의 state는 이 문자열과 달리 UI 상태 객체이며, 자기 예약 차례의 양보 정보는 status 응답의 lease에도 들어 있다.

`turn`은 UI를 조회하지 않는다. queued이면 0.1–0.5초 간격으로 전체 대기 시간을 제한해 확인하거나 독립 작업을 진행한다. CLI의 `turn --wait-seconds`는 클라이언트 측 polling이며 기본 0초, 최대 30초다. HTTP `turn` 본문에는 task_id만 보낸다. 차례 대기 중 status polling은 다른 에이전트의 UI 작업과 같은 mutex를 사용하므로 피한다.

quantum을 넘기면 quantum_exhausted=true, 다른 대기 작업도 있으면 yield_due=true가 되지만 동작 도중 권한을 강제 회수하지 않는다. 안전한 작업묶음이 끝나면 `yield(requeue=true)`로 FIFO 끝에 다시 들어간다. 대기자가 없으면 곧바로 자기 작업이 새 차례를 얻을 수 있다. `renew`는 lease TTL만 연장하고 quantum 시작 시각은 바꾸지 않는다. `yield(requeue=false)`는 작업을 completed로 만든다. 공유 폼을 작성 중이라면 입력·제출·결과 확인 사이를 양보 경계로 잡지 않는다.

응답 유실 후 같은 task_id·이전 lease_token·동일 requeue로 yield를 다시 보내면 캐시된 응답을 반환하며 두 번 양보하지 않는다. CLI는 `retry-yield`로 저장한 원래 payload를 재전송한다. 같은 차례 토큰으로 requeue 값을 바꾸면 `request_conflict`다. 재전송 응답은 과거 yield의 결과일 수 있으므로 다음 차례는 turn으로 확인한다. 완료·취소·만료된 작업은 자동 재실행하지 않는다. 큐 대기 시간은 lease TTL과 별개로 계산하며, requeue 시 새 큐 대기 시간이 시작된다. 차례당 lease TTL 또는 큐 대기 시간이 만료되면 작업은 expired다. 이미 보낸 동작이 실패했다고 추정하지 말고 상태를 재관찰하여 이어갈지 판단한다.

cancel은 queued 작업에만 사용한다. running을 끝내려면 yield를 사용한다. 예약된 차례의 수동 release는 `scheduled_turn` 오류다. 기존 acquire/renew/release 방식도 유지되지만, 큐는 이미 획득한 수동 lease의 종료/만료를 기다린다. 자기 수동 lease를 보유한 채 enqueue하면 lease_busy이므로 먼저 release한다. 수동 소유자가 계속 갱신하면 교대가 보장되지 않으므로 다중 에이전트는 모두 enqueue를 사용하는 것이 좋다.

## State

```json
{
  "platform": "macos",
  "desktop_id": "local",
  "frontmost_app": {"pid": 123, "name": "Example", "bundle_id": "com.example.app"},
  "applications": [{"pid": 123, "name": "Example", "bundle_id": "com.example.app"}],
  "roots": ["123/0"],
  "nodes": [{
    "id": "123/0.0", "pid": 123, "role": "AXButton", "title": "Apply",
    "description": null, "value": null, "enabled": true, "focused": false,
    "actions": ["AXPress"], "settable": [], "children": [], "secure": false
  }],
  "truncated": false,
  "capabilities": {"scope": "frontmost_application", "screenshots": false}
}
```

위 예시는 macOS 스키마이며 값은 실제 노드 참조가 아니다. `roots`부터 `children` 관계를 따라 탐색한다. 노드 ID는 특정 관찰의 참조이며 영구 selector가 아니다. macOS는 활성 앱, Windows는 전경 창, Linux는 식별된 활성 창의 트리를 담는다. 다른 앱은 관찰한 pid와 활성화 가능 여부를 확인한 뒤 활성화하고 다시 status를 읽는다. `null`인 속성은 알 수 없거나 앱이 노출하지 않는 값이다.

Windows는 `platform: windows`, `capabilities.scope: foreground_window`를 반환하고 노드에 runtime_id, toggle/selection/expand 상태를 포함한다. Linux는 `platform: linux`, `scope: active_window`, `foreground_status`와 앱별 `can_activate`를 반환한다. ACTIVE 창이 모호하거나 없으면 frontmost_app=null이고 조작 기능이 비활성화된다. Linux apps 목록은 AT-SPI가 노출하는 앱이며, Windows는 보이는 최상위 창을 가진 앱 목록이다.

문자열 title/description/value는 4,096자까지만 표시한다. 잘린 문자열이 있으면 노드의 `text_truncated`와 전체 `truncated`가 true다. `title_hash`, `description_hash`, `value_hash`는 전체 원문 문자열의 SHA-256을 제공하므로 잘린 부분만 변경되어도 상태 검증이 실패한다. 해시는 생략된 원문을 읽는 수단은 아니며, 내용을 모르는 긴 필드를 전체 대체하지 않는다. 보안 필드는 값과 value_hash 모두 null이다.

## 지원 동작

| kind | 필드 | 조건 |
| --- | --- | --- |
| `perform` | node_id, action | 노드의 actions에 있는 이름. OS별 예: AXPress, Invoke, click |
| `set_value` | node_id, value | settable에 value; 문자열 전체 대체, 최대 16,384자 |
| `focus` | node_id | settable에 focused |
| `activate_app` | pid | 관찰된 applications 목록의 양수 pid. OS별 활성화 제약 적용 |

각 action 객체에는 kind와 표에 명시한 필드만 보낸다. disabled 노드는 실행하지 않는다. 보안 입력값은 읽거나 수정하지 않는다. AX가 값을 변경해도 앱의 업무 로직이 처리되었는지는 후속 상태로 확인한다.

## 오류 처리

| code | 다음 행동 |
| --- | --- |
| `lease_busy` | 소유 세션과 retry_after_seconds 확인; backoff 또는 독립 작업 |
| `lease_required` | 예약 작업은 turn으로 만료 확인; 재관찰 후 이어갈지 판단. 수동 모드는 acquire → status |
| `scheduled_turn` | 예약된 차례는 release 대신 yield 사용 |
| `task_busy` | 현재 queued/running 작업을 마치거나 대기 작업을 cancel한 뒤 새 작업 등록 |
| `turn_active` | running 작업의 cancel 대신 안전한 경계에서 yield 사용 |
| `task_not_found` | 같은 세션에 등록된 task_id인지 확인; 서버 재시작 후 재생하지 않음 |
| `stale_snapshot` | status → 재판단; 새로운 request_id |
| `request_conflict` | 같은 act request_id, enqueue task_id 또는 yield 차례 토큰에 다른 내용을 보냈는지 확인 |
| `action_outcome_unknown` | 재실행하지 말고 status에서 결과 조사. retry는 캐시 오류 반환 |
| `permission_denied` | macOS Accessibility 권한을 수동 설정한 뒤 서버 재시작 |
| `unsupported` / `element_unavailable` | status 재조회; 대상/기능을 재검토 |
| `invalid_session` | 세션 만료/서버 재시작 여부 확인. 미확정 동작을 조사한 뒤 새 세션 |
| `capacity` | 세션/요청/작업 한도 확인. 작업의 256차례 한도이면 현재 lease가 유지되므로 requeue=false로 마무리 가능 |
| `backend_unavailable` | 컴파일/OS/프로세스 오류 확인. 조작 응답 유실 시 retry 의미에 주의 |

인증 실패는 HTTP 401, 브라우저/Host 차단은 403, 요청 형식 오류는 400, 없는 작업/경로는 404, 충돌은 409, 용량 한도는 429, backend 문제는 502/503이다. 일부 native 오류가 조작 dispatch 뒤에 발생하면 보수적으로 `action_outcome_unknown`으로 감싸고 `details.backend_code`에 원래 코드를 넣는다.

캐시된 act 오류는 `details.request_recorded: true`와 request_id를 포함한다. 클라이언트는 이 표식으로 preflight 중의 일시적 backend 오류도 최종 결과로 수신했음을 확인한다. `action_outcome_unknown`은 캐시에 저장되었더라도 실제 UI 결과는 별도 확인해야 한다.

## 수명과 제한

- 잠금 TTL: 기본 30초, 허용 1–120초. 자동 갱신 없음.
- 예약 작업: 세션당 활성 작업 1개, 종료 상태를 포함해 작업 기록 최대 256개, 작업당 최대 256차례. quantum 기본 1,000ms, 큐 대기 기본 60초. 256번째 차례에서 추가 requeue는 capacity 오류지만 현재 lease는 유지되어 requeue=false로 마칠 수 있다.
- 스냅샷 TTL: 30초, 세션별 최근 16개.
- 세션: 마지막 요청 후 1시간, 서버당 최대 128개.
- 조작 결과: 세션별 256개, 세션 종료/만료/서버 종료 시 폐기.
- HTTP body: 64 KiB, 동시 연결 처리 최대 32개. 큰 한글 입력은 문자 한도보다 body 바이트 한도에 먼저 도달할 수 있다.
- 접근성 트리: 기본 1,200노드/깊이12. 오래 걸리는 요청은 backend timeout 처리. 부분 트리는 truncated로 표시한다.
- 상태 검증과 조작은 서버 mutex 안에서 순차 실행한다. 개별 동작이 진행 중이면 잠금 만료 후에도 다른 동작은 완료/타임아웃까지 기다린다.

서버 재시작은 instance_id를 변경하고 세션·잠금·작업 큐·캐시를 모두 초기화한다. 재시작 전에 보낸 미확정 동작을 새 request_id나 task_id로 자동 재생하지 않는다.
