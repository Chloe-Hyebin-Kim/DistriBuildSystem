# DistriBuild — 언리얼 엔진용 분산 빌드 가속기

Incredibuild처럼 LAN의 여러 PC에 빌드 작업을 나눠 실행합니다. **UE 4.26과 UE 5.8**에서 동작합니다. 엔진 수정은 필요 없습니다.

| 대상 | 방식 | 구성 요소 |
|---|---|---|
| **C++ 컴파일** | UBT의 XGE 실행기가 호출하는 `xgConsole.exe`를 대체합니다. 로컬에서 전처리하고, PCH를 재사용해 원격에서 같은 컴파일러로 컴파일한 뒤 `.obj`를 돌려받습니다. | `Tools/DistriBuild/xgconsole.py`, `install_client.bat` |
| **셰이더 컴파일** | 엔진의 `IDistributedBuildController` 훅(엔진의 XGE 컨트롤러가 쓰는 것과 같은 인터페이스)을 구현해 ShaderCompileWorker 작업을 에이전트로 보냅니다. | `Plugin/DistriBuild` (에디터 플러그인) |
| **헬퍼 PC** | 작업을 받아 실행하는 에이전트입니다. UDP로 자동 검색됩니다. | `Tools/DistriBuild/agent.py` |

```
 개발자 PC                                             헬퍼 PC (N대)
┌──────────────────────────┐   TCP 31890 (zlib)    ┌──────────────────────────┐
│ UnrealEditor             │ ────────────────────▶ │ DistriBuild Agent        │
│  └ DistriBuild plugin    │   셰이더 작업          │  ├ ShaderCompileWorker   │
│     (셰이더 컨트롤러)      │                       │  │  (같은 엔진 버전)       │
│ UBT ─▶ xgConsole.exe     │ ────────────────────▶ │  └ cl.exe                │
│       (DistriBuild)      │ PCH 캐시 + 텍스트→.obj │     (같은 컴파일러 버전)    │
└──────────────────────────┘ ◀─ UDP 31891 검색 ──▶  └──────────────────────────┘
```

## 동작 방식

- **슬롯 스케줄링**: 에이전트마다 슬롯 수(기본값: CPU 코어 수)가 정해져 있습니다. 클라이언트는 부하가 가장 낮은 에이전트에 `reserve`를 보내고, 슬롯이 꽉 찼으면 다음 에이전트를 시도합니다. 중앙 코디네이터는 필요 없습니다.
- **호환성 보장**: 셰이더 작업은 엔진 버전(`5.8.4`)이 같은 에이전트에만, C++ 작업은 컴파일러 빌드(`msvc-19.44.35229-x64`)가 정확히 같은 에이전트에만 보냅니다.
- **로컬 폴백**: 원격 작업이 실패하거나 받을 에이전트가 없으면 로컬에서 다시 실행합니다. 코드 오류는 로컬 컴파일러가 다시 보고하므로 빌드 결과는 로컬 빌드와 같습니다.
- **소스 동기화가 필요 없음**: C++는 전처리된 텍스트와 PCH만 보냅니다. 에이전트 PC에는 같은 버전의 컴파일러만 있으면 됩니다. 셰이더는 5.8에서는 작업 파일에 소스가 들어 있고, 4.26에서는 셰이더 폴더를 번들로 올려 에이전트가 해시별로 캐시합니다.

## GUI로 설정하기 (Qt)

`Tools\DistriBuild\setup_gui.py`는 아래 1, 2장의 설정을 창에서 합니다. 같은 `agent.json`과 `client.json`을 편집하므로 명령줄 도구와 함께 쓸 수 있습니다.

```bash
pip install PySide6-Essentials
```
```bash
python Tools\DistriBuild\setup_gui.py
```

Python이 없는 PC에서 쓰려면 `pip install pyinstaller`까지 한 뒤 `build_setup_gui.bat`으로 `dist\DistriBuildSetup.exe`를 만듭니다.

| 탭 | 할 수 있는 일 |
|---|---|
| 헬퍼 PC | 에이전트 설정과 시작/중지, PC를 켜면 자동 시작(UAC), 방화벽 규칙(UAC), 제공 중인 컴파일러와 엔진 확인 |
| 개발자 PC | xgConsole 빌드, 서비스 설치/제거(UAC), `--doctor`, 헬퍼 PC 검색과 컴파일러 일치 확인, 빌드 옵션, 헬퍼용 패키지 만들기 |

**헬퍼 PC용 패키지**는 개발자 PC의 MSVC 복사본과 실행 파일을 한 폴더에 모읍니다. 그 폴더의 내용을 헬퍼 PC의 `C:\DistriBuild`에 복사하고 `DistriBuildSetup.exe`를 실행하면 컴파일러가 자동으로 등록됩니다.

창에서 시작한 에이전트는 창을 닫으면 함께 중지됩니다. 헬퍼 PC를 계속 헬퍼로 쓰려면 **자동 시작 켜기**를 누르세요.

- 작업 스케줄러에 `DistriBuild Agent` 작업을 등록합니다. PC가 켜지면 로그인하지 않아도 SYSTEM 계정으로 에이전트가 시작되고, 창을 닫아도 계속 실행됩니다.
- 로그는 설정 파일 옆의 `agent-autostart.log`에 남습니다(5MB가 넘으면 `.old`로 바꿉니다).
- 설정을 바꾸면 **설정 적용해 다시 시작**을 누릅니다. 패키지 폴더를 옮기면 **이 위치로 다시 등록**이 나타납니다.
- SYSTEM 계정은 사용자 환경 변수(`UE_ROOT`, 사용자 PATH)를 보지 못하므로 그렇게만 찾은 엔진과 컴파일러는 직접 추가해 두세요.
- 작업은 보통보다 낮은 우선순위로 실행되어 헬퍼 PC를 쓰는 사람에게 방해가 덜 됩니다.

## 1. 에이전트 설치 (헬퍼 PC마다)

Python 3.9 이상이 필요합니다. Python이 없는 PC에는 `build_xgconsole.bat`으로 만든 `dist\DistriBuildAgent.exe`를 복사하면 됩니다.

```bash
python agent.py --print-detected
```
감지된 엔진과 컴파일러를 확인합니다. Epic 런처로 설치한 엔진과 vswhere로 찾은 MSVC는 자동으로 감지됩니다. 소스 빌드 엔진은 `--engine <엔진 루트>`나 설정 파일의 `engines`에 추가합니다.

```bash
python agent.py --config agent.json
```
설정 형식은 `agent.example.json`을 참고하세요. 방화벽에서 **TCP 31890**과 **UDP 31891**을 열어야 합니다. `--log <파일>`을 주면 출력을 파일에도 남깁니다. 같은 포트에 에이전트가 이미 있으면 두 번째 에이전트는 시작하지 않고 종료 코드 2로 끝납니다.

**에이전트에는 개발자 PC의 UBT가 고른 것과 같은 MSVC가 있어야 합니다.** 빌드 번호까지 같아야 하므로 팀 전체가 같은 버전을 쓰세요. UBT는 설치된 것 중 가장 새 버전을 고를 수 있습니다(`xgConsole --doctor`로 확인).

## 2. C++ 분산 (개발자 PC)

**UBT는 `Incredibuild Agent`라는 Windows 서비스가 실행 중일 때만 XGE 실행기를 씁니다**(`XGE.IsAvailable`). 그래서 설치 단계에 관리자 권한이 한 번 필요합니다.

1. `Tools\DistriBuild\build_xgconsole.bat`을 실행합니다(`pip install pyinstaller` 필요).
2. **관리자 권한 명령 프롬프트**에서 `Tools\DistriBuild\install_client.bat`을 실행합니다.
   - 아무 일도 하지 않는 `Incredibuild Agent` 서비스(`DistriBuildXgeShim.exe`)를 등록합니다.
   - `xgConsole.exe`를 `C:\DistriBuild\bin`에 복사하고 PATH에 추가합니다.
   - 실제 Incredibuild가 있으면 아무것도 바꾸지 않고 중단합니다. 되돌리려면 `uninstall_client.bat`을 실행합니다.
3. 에이전트 목록이 필요하면 `%USERPROFILE%\.distribuild\client.json`을 만듭니다(`distribuild_client.example.json` 참고). 같은 서브넷이면 자동 검색으로 충분합니다.
4. **새 터미널**에서 확인합니다.
   ```bash
   xgConsole --doctor
   ```
5. 평소처럼 빌드합니다. 로그에 `DistriBuild: N actions, X local slots, Y remote slots`가 나오면 연결된 것입니다. `BuildConfiguration.xml`은 바꿀 필요가 없습니다. 두 엔진 모두 기본값에서 XGE를 먼저 고릅니다.

원격 처리 규칙:
- 원격으로 보내는 것은 컴파일(`/c`)만입니다. 링크, PCH 생성(`/Yc`), `/analyze`, C++20 모듈은 로컬에서 실행합니다.
- **PCH는 에이전트로 복사해 재사용합니다.** MSVC PCH는 같은 컴파일러 빌드면 다른 위치에서도 열립니다. 한 번 올린 뒤로는 작업마다 PCH 뒤쪽 텍스트(UE 소스 파일 기준 수 KB)만 보내므로, 원격 컴파일 속도가 로컬과 비슷합니다.
- UE 기본값인 `/Z7`에서는 원격 `.obj`가 로컬 PCH `.obj`와 그대로 링크됩니다. `/Zi`로 만든 PCH는 `.pdb`가 있어야 열리므로 공유하지 않고, 헤더 전체를 텍스트로 보내며 `/Z7`로 컴파일합니다. 이 경우는 파일마다 훨씬 느립니다.
- `/external:W0`는 전처리 파일 안의 경고 영역으로 재현합니다. SDK·서드파티 헤더 경고는 숨기고, 프로젝트 코드의 경고와 오류는 그대로 UBT에 전달합니다.
- 의존성 파일은 로컬 전처리의 `/showIncludes` 결과로 다시 만듭니다. 4.26의 `cl-filter` `.txt`와 5.8의 `/sourceDependencies` `.json`을 모두 지원합니다. 한국어 VS처럼 영어 언어 팩이 없는 설치에서도 동작합니다.

## 3. 셰이더 분산 (UE 플러그인)

1. `Plugin/DistriBuild`를 프로젝트의 `Plugins/` 폴더(또는 `Engine/Plugins/`)에 복사하고 플러그인을 활성화합니다.
2. **Project Settings → Plugins → DistriBuild**에서 에이전트를 추가합니다(`host` 또는 `host:port`). 같은 서브넷이면 자동 검색으로 충분합니다. 설정은 `DefaultEngine.ini`에 저장되어 팀 전체가 공유합니다.
3. 에디터를 재시작합니다. 출력 로그에 `Using DistriBuild for shader compilation`이 나오면 사용 중입니다.
4. **Window → Developer Tools → DistriBuild Monitor**, 또는 콘솔 명령 `DistriBuild.Stats`로 상태를 봅니다.

명령줄 옵션: `-DistriBuildAgents=10.0.0.5,10.0.0.6:31890`, `-DistriBuildToken=...`, `-NoDistriBuild`

쿠킹 커맨드릿(`-run=Cook`)에서도 같은 방식으로 동작합니다.

## 보안

에이전트는 설정된 ShaderCompileWorker와 컴파일러만 실행합니다. 작업 폴더 밖을 가리키는 절대 경로, `..` 경로, 응답 파일(`@`)이 들어 있는 인자는 거부합니다. 그래도 **신뢰할 수 있는 사내 LAN에서만** 운영하고 `token`을 설정하세요. 통신은 암호화되지 않습니다.

## 적용 체크리스트와 테스트

버전별 설정과 분산되지 않은 작업을 확인하는 방법은 **[CHECKLIST.md](CHECKLIST.md)** 에 있습니다. 검증 도구는 `Tools/tests/`에 있습니다.

| 도구 | 확인 내용 | 필요한 것 |
|---|---|---|
| `check_plugin_headers.py` | 엔진 소스 헤더로 플러그인 컴파일 (UHT 없이 수 초) | 압축 푼 `Engine/Source` |
| `e2e_windows.py` | 실제 cl/link/cl-filter로 두 UBT 형식의 전체 경로, 오류 처리, 4.26 셰이더 경로 재작성 | MSVC |
| `check_setup_gui.py` | Qt 설정 창을 임시 홈 폴더와 별도 포트에서 실제 에이전트로 구동 | PySide6, MSVC |
| `check_autostart.py` | 자동 시작 작업을 실제 작업 스케줄러에 등록해 실행/중지 (관리자 권한 없이, 현재 사용자로). `--exe`는 빌드한 exe로 | PySide6 |
| `real_engine_check.py` | 실제 UBT 작업 파일 빌드, UBT의 up-to-date 판정, 실제 에디터 쿠킹의 셰이더 작업 | 설치된 엔진 |
| `Tools\BuildPlugin.bat` | RunUAT로 플러그인 패키징 | 설치된 엔진 |

## 검증 상태

설치된 UE 4.26.2(UBT가 MSVC 14.51 선택)와 5.8.4(MSVC 14.44), 한국어 Windows에서 실제로 돌린 결과입니다. **에이전트는 모두 같은 PC에서 실행했습니다.**

| 항목 | UE 4.26.2 | UE 5.8.4 |
|---|---|---|
| RunUAT로 플러그인 패키징 | 성공 | 성공 |
| 실제 UBT 작업 파일(`-XGEExport`)을 xgConsole로 빌드, 원격 `.obj` 포함 링크 | 성공 (30 액션) | 성공 (32 액션) |
| 빌드 후 UBT의 up-to-date 판정, 파일 하나 수정 시 그 파일만 재빌드 | 통과 | 통과 |
| PCH 캐시 후 원격 컴파일 시간(중앙값) | 2.3초 | 8.6초 (PCH 공유 전 128초) |
| 실제 에디터 쿠킹에서 원격 ShaderCompileWorker 배치 | 171개, 실패 0 | 1,232개, 실패 0 |
| 쿠킹 결과 | 완료, 오류 0 | 12분 제한에서 중단, 그때까지 오류 0 |
| 실제 엔진 소스 3개 파일: 원격 컴파일과 직접 컴파일의 외부 심볼 비교 | 일치 | 일치 |

**아직 확인하지 못한 것**
- **두 대 이상의 PC.** LAN 지연, 방화벽, 다른 PC에서 PCH를 여는 경우는 시험하지 못했습니다. MSVC PCH는 같은 컴파일러 빌드면 다른 PC에서도 쓸 수 있도록 설계되어 있고(UBA도 같은 전제로 동작합니다), 실패하면 로컬로 재실행합니다.
- **서비스를 설치한 상태의 UBT 직접 실행.** 위 C++ 결과는 UBT가 내보낸 작업 파일을 실행한 것입니다. UBT가 서비스를 보고 XGE를 골라 `xgConsole.exe`를 띄우는 부분은 관리자 권한이 필요해 아직 돌리지 않았습니다. `XGE.cs`는 코드로 대조했습니다.
- 소스 빌드 엔진. 설치본으로만 시험했습니다.
- 플러그인의 Monitor 탭 등 에디터 UI.

## 한계

- **전처리는 항상 개발자 PC에서 돕니다.** UE 소스 파일 하나에 2~4초가 걸립니다. PC 수를 아주 많이 늘리면 여기서 막히므로 필요하면 `max_preprocess`를 올리세요. Incredibuild는 파일 시스템을 가상화해서 이 단계가 없습니다.
- **첫 빌드의 PCH 전송 비용.** UE 5.8 UnrealEd PCH는 2.4GB입니다. 에이전트마다 PCH 하나당 한 번씩 보냅니다.
- 링크, UHT, 쿠킹 자체, 라이트 빌드, 커스텀 빌드 단계는 분산되지 않습니다.
- UE 4.26 C++는 해당 PC의 UBT가 고른 컴파일러 그대로 동작합니다. UBT가 더 새 MSVC를 고르면 에이전트에도 그 버전이 있어야 합니다.
