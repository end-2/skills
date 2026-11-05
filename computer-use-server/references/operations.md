# 실행과 검증

## OS 자동 선택

Python 3.10 이상이 필요하다. `SKILL_DIR`에 이 스킬의 절대 경로를 넣는다. 다른 에이전트가 이미 실행한 서버가 있으면 재사용한다. 아래는 macOS/Linux 쉘 예시다.

```bash
SKILL_DIR=/absolute/path/to/computer-use-server
python3 "$SKILL_DIR/scripts/client.py" health
python3 "$SKILL_DIR/scripts/server.py"
```

health가 정상이라면 두 번째 명령은 실행하지 않는다. server는 foreground로 계속 실행되므로 오케스트레이터의 장기 실행 프로세스 또는 별도 터미널에서 유지한다. 테스트/작업 후에는 소유한 서버 프로세스만 종료한다. 로그인 시 자동 실행이나 서비스 등록은 하지 않는다.

| 실행 OS | 자동 선택 | 필요한 구성 | 상태 조회 범위 |
| --- | --- | --- | --- |
| Windows | windows | `uiautomation==2.0.29`, 로그인된 사용자 데스크톱 | 보이는 최상위 창의 앱 목록과 전경 창 UIA 트리 |
| macOS | macos | Swift 컴파일러, Accessibility 권한 | 일반 GUI 앱 목록과 활성 앱 AX 트리 |
| Linux | linux | pyatspi2, AT-SPI, 사용자 D-Bus 및 그래픽 세션 | 접근성 앱 목록과 유일하게 식별된 활성 창 |

기본 `--backend auto`는 `platform.system()`의 Windows/Darwin/Linux에 대응한다. `--backend windows`, `macos`, `linux`로 명시할 수도 있지만 실행 OS와 다르면 `backend_os_mismatch`로 종료한다. 다른 OS는 `unsupported_platform`으로 종료한다. `--backend mock`은 모든 지원 OS에서 사용할 수 있으며 native 의존성을 로딩하지 않는다. 시작 로그와 `health`의 `os`, `selected_backend`로 선택 결과를 확인한다. health는 프로세스 생존 확인이며 실제 UI 읽기는 status로 검증한다.

기본 token-file 위치:

- Windows: `%LOCALAPPDATA%\computer-use-server\token` (미설정 시 사용자 `AppData\Local`).
- macOS: `~/.local/state/computer-use-server/token` (기존 경로 유지).
- Linux: `$XDG_STATE_HOME/computer-use-server/token`, 미설정 시 `~/.local/state/computer-use-server/token`.

## Windows

PowerShell에서 같은 Python 환경에 의존성을 설치한다.

```powershell
$SkillDir = 'C:\path\to\computer-use-server'
python -m pip install -r "$SkillDir\scripts\requirements-windows.txt"
python "$SkillDir\scripts\server.py"
```

다른 PowerShell에서 에이전트별 고유 세션을 연다.

```powershell
$SkillDir = 'C:\path\to\computer-use-server'
$SessionFile = Join-Path $env:TEMP ('cu-' + [guid]::NewGuid().ToString() + '.json')
python "$SkillDir\scripts\client.py" --session-file $SessionFile open --agent-id agent-a
python "$SkillDir\scripts\client.py" --session-file $SessionFile enqueue --task-id agent-a-task-1 --intent 'Inspect and validate the target form'
python "$SkillDir\scripts\client.py" --session-file $SessionFile turn --wait-seconds 10
```

turn 결과가 running이면 status를 읽고 대상과 동작을 정한다. queued라면 제한된 대기나 독립 작업을 이어간다.

UIA/COM 객체는 전용 worker의 동일 MTA 스레드에서만 사용한다. 서비스의 Session 0, 잠긴/보안 데스크톱, 다른 사용자 세션, 권한 수준이 다른 앱은 접근되지 않을 수 있다. 관리자 권한 상승이나 키 입력으로 제한을 우회하지 않는다. Windows가 전경 활성화를 거부하면 `unsupported`로 보고한다. UIA ValuePattern을 제공하는 컨트롤에 문자열 전체 대체를 지원한다.

인증/세션 파일은 현재 사용자만 접근하는 보호된 DACL로 생성한다. Windows의 POSIX chmod 비트로 권한을 판단하지 않는다. 기존의 일반 파일을 token-file로 재사용하면 상속 ACL 때문에 거부될 수 있으므로, 사용자 디렉터리의 새 경로에서 서버가 파일을 만들게 한다. ACL을 지원하는 파일시스템과 신뢰하는 상위 디렉터리가 필요하다.

## macOS

`swiftc`가 필요하다. 없으면 Xcode Command Line Tools를 설치한다. macOS 백엔드는 추가 pip 패키지를 요구하지 않는다.

처음 status를 읽으면 Swift helper를 `~/Library/Caches/computer-use-server/`에 컴파일한다. 소스 hash가 실행파일 이름에 포함된다. 첫 컴파일은 일반 상태 조회보다 오래 걸릴 수 있으므로 첫 status가 클라이언트 타임아웃으로 끝나면 서버 로그/health를 확인한 후 상태 조회만 재시도한다.

실제 UI에 접근하려면 macOS의 Accessibility 권한이 필요하다. `permission_denied`가 나오면 먼저 실제 권한 심사 대상을 확인한다. 오류의 `helper_binary`는 AX 검사를 호출한 실행 파일 경로이며, 시스템 설정에서 허용해야 할 대상임을 보장하지 않는다. macOS는 실행 방식에 따라 실행을 책임지는 호스트 프로세스에 요청을 귀속할 수 있다. 부모 PID나 앱 이름만으로 CodexCLI, Terminal 또는 helper를 권한 대상으로 단정하지 않는다.

기본 제공 `Codex Computer Use`를 허용했더라도 이 스킬의 독립 Swift helper가 허용되었다는 뜻은 아니다. 이 서버는 해당 앱의 서비스나 권한을 공유하지 않는다. 권한 문제를 진단할 때는 읽기 전용 status 요청 직후 macOS TCC 로그의 `kTCCServiceAccessibility`, 요청 PID, `responsible_path`, `Sub`, 허용/거부 결과를 함께 확인한다. 예를 들어 `log show --last 1m --info --style compact --predicate 'subsystem == "com.apple.TCC" AND eventMessage CONTAINS[c] "ax-bridge"'`로 관련 기록을 좁힐 수 있다. 로그가 없거나 대상이 가려져 있으면 특정 권한 대상을 추측하지 않는다.

확인한 대상에 사용자가 권한을 부여한 뒤 서버를 재시작하고 status로 검증한다. 서버는 권한 프롬프트를 자동으로 열지 않는다. 소스 수정으로 helper 경로가 바뀌면 재승인이 필요할 수 있다. 화면 캡처 권한은 사용하지 않는다.

이전 버전의 `desktop.lock` 파일 권한 때문에 시작이 거부되면 아래 명령으로 같은 파일의 권한만 수정한다. 실행 중인 서버가 있을 수 있으므로 파일을 삭제하거나 교체하지 않는다.

```bash
chmod 600 ~/.local/state/computer-use-server/desktop.lock
```

## Linux

Debian/Ubuntu에서는 배포판 Python과 pyatspi 패키지를 사용한다.

```bash
sudo apt install python3-pyatspi
/usr/bin/python3 "$SKILL_DIR/scripts/server.py"
```

다른 배포판에서는 해당 배포판의 pyatspi2/AT-SPI Python 바인딩을 설치한다. 임의의 venv나 pyenv Python에서는 시스템 바인딩이 보이지 않을 수 있다. 서버가 실행한 Python으로 worker도 실행하므로 `import pyatspi`가 되는 동일 인터프리터를 선택한다.

로그인된 사용자의 그래픽 터미널에서 실행한다. DISPLAY 또는 WAYLAND_DISPLAY와 사용자 D-Bus 연결이 필요하다. headless 서버나 일반 SSH 세션에서 화면 상태를 만들어 내지 않는다. 앱이 AT-SPI를 노출하지 않거나 ACTIVE 창이 없거나 여러 개라면 `foreground_status`로 이유를 표시하고 조작을 거부한다. 앱 활성화는 유일하게 선택 가능한 focusable 창의 Component.grabFocus를 사용하며, Wayland compositor나 툴킷이 거절할 수 있다.

## 에이전트별 세션과 차례

서버 endpoint와 token-file 경로만 공유하고, session-file은 각각 따로 만든다. 아래 예시는 같은 쉘에서 두 에이전트 흐름을 보여준다.

```bash
RUN_DIR=$(mktemp -d)
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" open --agent-id agent-a
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" open --agent-id agent-b
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" enqueue \
  --task-id a-task-1 --intent 'A의 입력과 결과 검증' --quantum-ms 1000 --ttl 30 --queue-timeout 60
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" enqueue \
  --task-id b-task-1 --intent 'B의 입력과 결과 검증' --quantum-ms 1000 --ttl 30 --queue-timeout 60
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" turn --wait-seconds 10
```

A가 running이면 새 status에서 대상을 찾고 안전한 작업묶음을 수행·검증한다. 예를 들어 같은 폼에 값을 입력하고 제출한 결과를 확인할 때까지는 양보하지 않는다. 여러 단계를 한 번에 전송하지 않으며 매 조작 후 새 상태를 확인한다. B는 status를 반복 조회하지 않고 turn으로 기다린다. `turn --wait-seconds`는 최대 30초까지만 기다리며 제한 시간 뒤 queued가 반환될 수 있으므로 running 여부를 확인한다.

A에게 할 일이 더 남았고 현재 작업묶음을 마쳤다면 양보한다. 서버는 B에게 차례를 주고 A를 큐 끝으로 보낸다.

```bash
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" yield --requeue
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" turn --wait-seconds 10
```

B도 running을 확인한 다음 **새 status → 조작 → 검증**을 수행한다. 아래 yield는 B가 작업을 마친 경우다.

```bash
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" yield
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" turn --wait-seconds 10
```

A는 새 차례의 상태를 다시 읽어 남은 일을 판단한다. A의 작업까지 마친 뒤 정리한다.

```bash
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" yield
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/a.json" close
python3 "$SKILL_DIR/scripts/client.py" --session-file "$RUN_DIR/b.json" close
```

클라이언트는 현재 task_id를 세션 파일에 보관하므로 turn/yield/cancel에는 task_id를 다시 쓰지 않는다. 대기 작업만 `cancel`할 수 있고 running 작업은 `yield`로 마친다. `yield --requeue`는 안전한 경계에서 나머지 작업을 계속할 때만 쓴다. quantum 기본 1초는 강제 중단 제한이 아니며 `yield_due`를 보고 현재 묶음이 끝나는 대로 양보한다. `renew --ttl 30`은 TTL만 연장하고 quantum을 다시 시작하지 않는다.

yield 응답을 잃었으면 `retry-yield`로 이전 차례의 원래 요청을 재전송한 뒤 turn으로 현재 상태를 확인한다. 새 차례 토큰을 넣은 yield나 requeue 값을 바꾼 요청으로 대신하지 않는다. 작업/잠금이 만료되거나 동작 결과가 미확정이면 입력·제출 등을 자동 반복하지 않는다. status로 실제 결과를 조사한 뒤 새 작업으로 이어갈지 판단한다. 같은 task_id를 다시 enqueue해도 종료된 작업이 재실행되지는 않는다. 작업당 차례는 최대 256회이며 추가 requeue가 capacity로 거부되면 현재 차례에서 `yield`로 마칠 수 있다. HTTP 필드와 멱등성 규칙은 [API 명세](api.md)를 참고한다.

단독 작업이나 기존 연동에는 `acquire --ttl 30` → 새 status → 조작·검증 → `release`를 계속 쓸 수 있다. 수동 lease가 끝나기 전에는 큐가 기다리므로 공정한 교대가 필요하면 모든 에이전트를 enqueue 흐름으로 맞춘다. 예약된 차례에서 release를 호출하면 scheduled_turn 오류가 발생한다.

기본 설정을 바꾸려면 모든 에이전트에 같은 `--endpoint`, `--token-file` 옵션을 전달한다. 클라이언트는 환경 변수 `CU_ENDPOINT`, `CU_TOKEN_FILE`, `CU_SESSION_FILE`도 지원한다. 세션 파일에는 인증정보와 마지막 조작 payload(입력한 텍스트 포함)가 들어간다. POSIX는 0600, Windows는 보호된 현재 사용자 전용 DACL로 만든다. close는 파일 내용을 비운다. 외부에 공유하거나 저장소에 커밋하지 않는다.

클라이언트는 마지막 snapshot/lease와 task_id를 보관한다. `act --action '<JSON>'`은 UUID를 자동 생성하고 요청을 저장한 후 전송한다. `retry`는 마지막 조작 요청을 그대로 다시 보낸다. transport 오류 후 다른 `act`를 차단한다. 만료된 세션은 새로운 파일로 open할 수 있지만 미확정 동작은 먼저 조사한다. 새 세션을 만들었다는 사실은 과거 작업 실패를 의미하지 않는다.

## UI를 건드리지 않는 mock 실습

실제 서버와 구별되는 포트와 token-file을 사용한다.

```bash
python3 "$SKILL_DIR/scripts/server.py" --backend mock --port 8766 \
  --token-file /tmp/cu-demo-token
```

다른 터미널에서:

아래는 호환되는 수동 lease 흐름을 사용한다. 여러 에이전트의 교대는 위의 enqueue/turn/yield 예시를 사용한다.

```bash
RUN_DIR=$(mktemp -d)
export CU_ENDPOINT=http://127.0.0.1:8766
export CU_TOKEN_FILE=/tmp/cu-demo-token
export CU_SESSION_FILE="$RUN_DIR/demo.json"
python3 "$SKILL_DIR/scripts/client.py" open --agent-id demo
python3 "$SKILL_DIR/scripts/client.py" acquire
python3 "$SKILL_DIR/scripts/client.py" status
python3 "$SKILL_DIR/scripts/client.py" act --action '{"kind":"set_value","node_id":"input","value":"hello"}'
python3 "$SKILL_DIR/scripts/client.py" status
python3 "$SKILL_DIR/scripts/client.py" act --action '{"kind":"perform","node_id":"submit","action":"AXPress"}'
python3 "$SKILL_DIR/scripts/client.py" retry
python3 "$SKILL_DIR/scripts/client.py" status
python3 "$SKILL_DIR/scripts/client.py" release
python3 "$SKILL_DIR/scripts/client.py" close
unset CU_ENDPOINT CU_TOKEN_FILE CU_SESSION_FILE
```

`input.value`는 hello, `result.value`는 1이 된다. retry 후에도 버튼 실행 횟수는 늘어나지 않는다. mock의 고정 ID를 실제 앱에서 사용하지 않는다.

## 개발 검증

```bash
python3 -m unittest discover -s "$SKILL_DIR/tests" -v
python3 -m compileall -q "$SKILL_DIR/scripts"
```

테스트는 fake/mock UI만 사용한다. 잠금 경쟁·순환 차례·만료·상태 변경·중복 요청·미확정 결과·HTTP 인증을 검증한다. 실제 native UI 조작 검증은 권한이 허용된 환경에서 별도로 수행해야 한다.

공식 근거: [Apple Accessibility](https://developer.apple.com/documentation/applicationservices/1462085-axuielementcopyattributevalue), [Windows UIA 스레딩](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading), [uiautomation 2.0.29](https://pypi.org/project/uiautomation/2.0.29/), [GNOME pyatspi2](https://github.com/GNOME/pyatspi2), [AT-SPI grab_focus](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.Component.grab_focus.html).
