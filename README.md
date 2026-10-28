# DistriBuild — 언리얼 엔진용 분산 빌드 가속기

Incredibuild처럼 LAN의 여러 PC에 빌드 작업을 나눠 실행하는 언리얼 플러그인과 도구 세트입니다.

| 대상 | 방식 | 구성 요소 |
|---|---|---|
| **셰이더 컴파일** | 엔진의 `IDistributedBuildController` 훅(엔진의 XGE 컨트롤러가 쓰는 것과 같은 인터페이스)을 구현해 ShaderCompileWorker 작업을 에이전트로 보냄 | `Plugin/DistriBuild` (에디터 플러그인) |
| **C++ 컴파일** | UBT의 XGE 실행기가 호출하는 `xgConsole.exe`를 대체. 로컬에서 전처리하고, 원격에서 같은 컴파일러로 컴파일한 뒤 `.obj`를 돌려받음 | `Tools/DistriBuild/xgconsole.py` |
| **헬퍼 PC** | 작업을 받아 실행하는 에이전트, UDP로 자동 검색됨 | `Tools/DistriBuild/agent.py` |

```
 개발자 PC                                             헬퍼 PC (N대)
┌──────────────────────────┐   TCP 31890 (zlib)    ┌──────────────────────────┐
│ UnrealEditor             │ ────────────────────▶ │ DistriBuild Agent        │
│  └ DistriBuild plugin    │   셰이더 작업          │  ├ ShaderCompileWorker   │
│     (셰이더 컨트롤러)      │                       │  │  (같은 엔진 버전)       │
│ UBT ─▶ xgConsole.exe     │ ────────────────────▶ │  └ cl.exe / clang        │
│       (DistriBuild)      │   전처리된 .i → .obj   │     (같은 컴파일러 버전)    │
└──────────────────────────┘ ◀─ UDP 31891 검색 ──▶  └──────────────────────────┘
```

## 동작 방식

- **슬롯 스케줄링**: 에이전트마다 슬롯 수(기본값: CPU 코어 수)가 정해져 있습니다. 클라이언트는 부하가 가장 낮은 에이전트에 먼저 `reserve`를 보내고, 슬롯이 꽉 찼으면(다른 개발자가 쓰는 중이면) 다음 에이전트를 시도합니다. 중앙 코디네이터는 필요 없습니다.
- **호환성 보장**: 셰이더 작업은 엔진 버전(`5.4.4` 같은 키)이 같은 에이전트에만 보냅니다. C++ 작업은 컴파일러 배너(`msvc-19.38.33130-x64` 같은 형식)가 정확히 같은 에이전트에만 보냅니다.
- **로컬 폴백**: 원격 작업이 실패하거나(네트워크, 경고 차이 등) 받을 에이전트가 없으면 로컬에서 다시 실행합니다. 그래서 빌드 결과는 항상 로컬 빌드와 같습니다.
- **로컬 우선**: C++ 빌드는 로컬 코어를 먼저 채우고, 남는 작업만 원격으로 보냅니다(`local_first`).
- **소스 동기화가 필요 없음**: C++는 로컬에서 전처리한 `.i` 하나만 보냅니다. 에이전트 PC에는 같은 버전의 컴파일러만 있으면 되고, 엔진 소스·SDK 헤더·프로젝트 소스는 필요 없습니다.

---

## 1. 에이전트 설치 (헬퍼 PC마다)

Python 3.9 이상이 필요합니다. 배포용 exe를 만들려면 `build_xgconsole.bat`을 실행하세요(PyInstaller 사용).

```bash
python agent.py --print-detected
```
감지된 엔진과 컴파일러를 확인합니다. Epic 런처로 설치한 엔진(`C:\Program Files\Epic Games\UE_*`)과 vswhere로 찾은 MSVC는 자동으로 감지됩니다.

```bash
python agent.py --config agent.json
```
에이전트를 실행합니다. 설정 형식은 `agent.example.json`을 참고하세요. 소스 빌드 엔진을 쓴다면 `engines`에 루트 경로를 추가합니다.

방화벽에서 **TCP 31890**과 **UDP 31891**을 열어야 합니다.

```bash
python monitor.py --watch
```
네트워크에 있는 에이전트 목록과 부하를 보여 줍니다.

## 2. 셰이더 분산 (UE 플러그인)

1. `Plugin/DistriBuild`를 프로젝트의 `Plugins/` 폴더(또는 `Engine/Plugins/`)에 복사하고 플러그인을 활성화합니다.
2. **Project Settings → Plugins → DistriBuild**에서 에이전트를 추가합니다(`host` 또는 `host:port`). 같은 서브넷이라면 자동 검색만으로 충분합니다. 설정은 `DefaultEngine.ini`에 저장되어 팀 전체가 공유합니다.
3. 에디터를 재시작합니다. 시작할 때 호환되는 에이전트가 1대 이상 있으면 셰이더 분산이 켜집니다. 출력 로그에 `DistriBuild: N remote shader slots ...`가 표시됩니다.
4. **Window → Developer Tools → DistriBuild Monitor**(또는 툴바의 DistriBuild 버튼)에서 에이전트 상태, 원격·로컬 처리 수, 전송량을 볼 수 있습니다.

명령줄 옵션: `-DistriBuildAgents=10.0.0.5,10.0.0.6:31890`, `-DistriBuildToken=...`, `-NoDistriBuild`

> 엔진이 분산 셰이더 컴파일을 쓰려면 엔진 쪽 설정도 켜져 있어야 합니다. UE5에서는 `r.XGEShaderCompile=1`(기본값)이고, 버전에 따라 `r.ShaderCompiler.AllowDistributedCompilation` 같은 CVar로 이름이 바뀌었을 수 있습니다. 쿠킹 커맨드릿(`UnrealEditor-Cmd -run=cook`)에서도 같은 방식으로 동작합니다.

## 3. C++ 분산 (UBT)

1. `build_xgconsole.bat`으로 `xgConsole.exe`를 만들고 PATH에 있는 폴더에 넣습니다. 실제 Incredibuild가 설치되어 있다면 PATH 순서나 레지스트리 때문에 충돌할 수 있습니다.
2. `%USERPROFILE%\.distribuild\client.json`을 만듭니다. 형식은 `distribuild_client.example.json`을 참고하세요.
3. UBT가 XGE 실행기를 쓰도록 설정합니다. `%APPDATA%\Unreal Engine\UnrealBuildTool\BuildConfiguration.xml` 예시:
   ```xml
   <?xml version="1.0" encoding="utf-8" ?>
   <Configuration xmlns="https://www.unrealengine.com/BuildConfiguration">
     <BuildConfiguration>
       <bAllowXGE>true</bAllowXGE>
       <bAllowUBAExecutor>false</bAllowUBAExecutor>  <!-- UE 5.4 이상 -->
       <bAllowFASTBuild>false</bAllowFASTBuild>
     </BuildConfiguration>
   </Configuration>
   ```
4. 평소처럼 빌드합니다. 로그에 `DistriBuild: N actions, X local slots, Y remote slots`가 나오고, 원격에서 처리된 항목에는 `(에이전트명)`이 붙습니다.

원격 처리 규칙:
- 원격으로 보내는 것은 컴파일(`/c`)만입니다. 링크, PCH 생성(`/Yc`), `/analyze`, C++20 모듈, `cl-filter.exe` 래퍼는 로컬에서 실행합니다.
- `/Yu`(PCH 사용)는 빼고 강제 include 헤더를 전처리 결과에 펼쳐 넣습니다. 전송량은 늘지만 zlib 압축으로 대부분 상쇄됩니다.
- `/Zi`는 `/Z7`로 바꿉니다. 디버그 정보가 .obj에 들어가므로 PDB 서버가 필요 없습니다.
- `/sourceDependencies` JSON은 로컬 전처리 단계의 `/showIncludes` 결과로 다시 만들어, UBT의 증분 빌드 추적이 그대로 동작합니다.

## 보안

에이전트는 설정된 ShaderCompileWorker와 컴파일러만 실행합니다. 작업 폴더 밖을 가리키는 절대 경로, `..` 경로, 응답 파일(`@`)이 들어 있는 인자는 거부합니다. 그래도 **신뢰할 수 있는 사내 LAN에서만** 운영하고 `token`을 설정하세요. 통신은 암호화되지 않습니다.

## 검증 상태와 한계

- **검증 완료** (Linux, gcc/clang): 에이전트 검색, 슬롯 예약과 busy 처리, 토큰 인증, xgConsole 대체 도구의 의존성 그래프 실행, 원격 컴파일 → 링크, 의존성(.d) 파일, 컴파일 오류 시 로컬 재실행과 후속 작업 스킵, 스텁 SCW를 사용한 셰이더 작업 왕복. MSVC 인자 분리 로직은 UE 스타일 rsp로 단위 확인만 했습니다.
- **미검증**: UE 플러그인 C++ 코드는 이 환경에 엔진이 없어 **컴파일해 보지 못했습니다**. 특히 `IDistributedBuildController`와 `FTaskCommandData`는 엔진 버전마다 시그니처가 조금씩 다릅니다(`GetName()` 반환형, `ExtraCommandArgs`·`DispatcherPID` 필드 등). 첫 빌드에서 에러가 나면 `Engine/Source/Developer/DistributedBuildInterface/Public/DistributedBuildControllerInterface.h`에 맞춰 `DistriBuildController.h/.cpp`를 수정하세요. 대상 버전은 UE 5.1 이상입니다(`FAppStyle` 사용).
- Windows 실환경(cl.exe + UBT XGE 실행기)에서 처음 실행할 때는 `"verbose": true`로 원격 실패 사유를 확인하세요. 원격 컴파일이 실패해도 로컬에서 다시 실행되므로 빌드는 깨지지 않지만, 그만큼 가속 효과는 줄어듭니다.
- Incredibuild처럼 파일 시스템을 가상화하지 않습니다. 그래서 링크, 쿠킹, 커스텀 툴은 분산되지 않습니다.
