# 실제 적용 전 체크리스트 (UE 4.26 / UE 5.8)

체크 순서: **① 플러그인 컴파일 → ② 엔진 버전별 설정 → ③ 분산되지 않은 작업 확인**

---

## ① 플러그인 컴파일 에러 확인

플러그인 C++ 코드는 언리얼 엔진이 없는 환경에서 작성되었습니다. 그래서 **각 엔진으로 직접 한 번 빌드해 봐야 합니다.**

### 1-1. 엔진별로 빌드해 보기

리포지토리 루트(`C:\dev\git\DistriBuildSystem`)에서 실행합니다. 엔진 설치 경로는 PC에 맞게 바꾸세요.

```bat
Tools\BuildPlugin.bat "C:\Program Files\Epic Games\UE_4.26"
```

```bat
Tools\BuildPlugin.bat "C:\Program Files\Epic Games\UE_5.8"
```

- 성공하면 `SUCCESS`가 나오고, 패키징된 플러그인이 `Build\UE_4.26\`, `Build\UE_5.8\`에 생깁니다.
- 실패하면 에러 줄만 골라서 보여 줍니다. 전체 로그는 `Build\<엔진폴더명>.log`에 있습니다. **에러 메시지를 그대로 붙여 주시면 수정하겠습니다.**

### 1-2. 엔진 간 차이로 이미 처리해 둔 것

| 차이 | 4.26 | 5.x | 처리 방법 |
|---|---|---|---|
| `IsSupported()` / `GetName()` 반환형 | `const bool` / `const FString`일 수 있음 | `bool` / `FString`일 수 있음 | 엔진 헤더에서 반환형을 그대로 읽어 씀 (`DistriBuildCompat.h`) |
| 셰이더 작업 인자 (`FTaskCommandData`) | `CommandArgs` 하나 | `WorkingDirectory`, `ExtraCommandArgs`, `DispatcherPID` 각각 | 엔진에 있는 필드를 컴파일 시점에 감지해서 사용 |
| `CreateUniqueFilePath()` | 엔진이 호출할 수 있고, 기본 구현은 크래시(`check(false)`) | 동일 | 직접 구현함 |
| 에디터 스타일 | `FEditorStyle` (EditorStyle 모듈) | `FAppStyle` | 버전 매크로로 분기 (`DistriBuildEditorCompat.h`) |
| 툴바 메뉴 이름 | `LevelEditor.LevelEditorToolBar` | `LevelEditor.LevelEditorToolBar.User` | 버전 매크로로 분기 |
| ini 저장 | `UpdateDefaultConfigFile()` | `TryUpdateDefaultConfigFile()` | 버전 매크로로 분기 |
| `.uplugin`의 플랫폼 필드 | `WhitelistPlatforms` | `PlatformAllowList` | 필드를 제거 (에디터 모듈이라 불필요) |
| `TAtomic`, `CompressMemoryBound` | 사용 가능 | 최신 버전에서 deprecated 또는 제거 | `std::atomic`과 자체 계산으로 대체 |

위의 반환형·필드 감지 코드는 두 가지 형태의 가짜 엔진 헤더로 C++14 컴파일과 동작까지 확인했습니다. 실제 엔진 헤더로는 확인하지 못했습니다.

### 1-3. 그래도 에러가 난다면 (예상 원인)

| 에러 메시지 | 원인 | 해결 |
|---|---|---|
| `Cannot open include file: 'DistributedBuildControllerInterface.h'` | 그 엔진에 셰이더 분산 인터페이스 모듈이 없음 | 아래 1-4로 확인합니다. 없으면 그 엔진에서는 셰이더 분산을 쓸 수 없고, C++ 분산만 쓸 수 있습니다 |
| `cannot instantiate abstract class 'FDistriBuildController'` | 엔진에 새 순수 가상 함수(`= 0`)가 추가됨 | 인터페이스 헤더에서 `= 0`인 함수를 찾아 에러 메시지와 함께 알려 주세요 |
| `Could not find definition for module 'EditorStyle'` / `'DeveloperSettings'` | 모듈 이름이 엔진 버전마다 다름 | `DistriBuildEditor.Build.cs`의 버전 조건을 조정합니다 |
| `C4996 ... deprecated` | 경고일 뿐 | 빌드 실패가 아니면 무시해도 됩니다 |

### 1-4. 엔진에 셰이더 분산 인터페이스가 있는지 미리 확인

```bat
dir /s /b "C:\Program Files\Epic Games\UE_4.26\Engine\Source\Developer\DistributedBuildInterface\*.h"
```

```bat
dir /s /b "C:\Program Files\Epic Games\UE_5.8\Engine\Source\Developer\DistributedBuildInterface\*.h"
```

`DistributedBuildControllerInterface.h`가 나오면 그 엔진은 셰이더 분산을 지원합니다. 파일 내용(특히 `FTaskCommandData`와 `IDistributedBuildController` 부분)을 보내 주시면 코드를 그 버전에 정확히 맞춰 드릴 수 있습니다.

---

## ② 엔진 버전별 설정 (4.26 / 5.8)

| 기능 | UE 4.26 | UE 5.8 |
|---|---|---|
| **셰이더 분산** | 엔진에 인터페이스가 있으면 동작 (1-4로 확인) | 인터페이스는 있을 가능성이 높음. Epic의 UBA 셰이더 컨트롤러와 경쟁 (아래 참고) |
| **C++ 분산** | UBT의 XGE 실행기 + `xgConsole.exe` 대체. **4.26의 `cl-filter.exe` 방식도 지원하도록 이번에 추가** | UBA가 기본 실행기. XGE 실행기가 아직 남아 있어야 동작 (아래로 확인) |
| UBT 설정 파일 | `bAllowXGE` = true | `bAllowXGE` = true, `bAllowUBAExecutor` = false |

### 2-1. UE 4.26

1. `xgConsole.exe`를 PATH에 넣습니다 (`Tools\DistriBuild\build_xgconsole.bat`로 빌드).
   - 진짜 Incredibuild가 설치되어 있으면 UBT가 레지스트리로 그쪽을 먼저 찾을 수 있습니다. 그럴 때는 Incredibuild를 제거하거나 비활성화하세요.
2. `%APPDATA%\Unreal Engine\UnrealBuildTool\BuildConfiguration.xml` 예시:
   ```xml
   <?xml version="1.0" encoding="utf-8" ?>
   <Configuration xmlns="https://www.unrealengine.com/BuildConfiguration">
     <BuildConfiguration>
       <bAllowXGE>true</bAllowXGE>
     </BuildConfiguration>
   </Configuration>
   ```
3. 빌드 출력에 `DistriBuild: N actions, ...` 줄이 나오면 연결된 것입니다.
4. 셰이더: 에디터를 실행하고 출력 로그에서 `LogDistriBuild`를 확인합니다. `DistriBuild controller initialized`가 보이면 엔진이 이 플러그인을 셰이더 컨트롤러로 쓰고 있는 것입니다.

### 2-2. UE 5.8

1. `BuildConfiguration.xml`:
   ```xml
   <?xml version="1.0" encoding="utf-8" ?>
   <Configuration xmlns="https://www.unrealengine.com/BuildConfiguration">
     <BuildConfiguration>
       <bAllowXGE>true</bAllowXGE>
       <bAllowUBAExecutor>false</bAllowUBAExecutor>
       <bAllowFASTBuild>false</bAllowFASTBuild>
     </BuildConfiguration>
   </Configuration>
   ```
2. 빌드 출력에 `DistriBuild: N actions, ...`가 **나오지 않으면**, 5.8 UBT가 XGE 실행기를 더 이상 쓰지 않는 것입니다. 원인은 UBT 로그(`%LOCALAPPDATA%\UnrealBuildTool\Log.txt`)에서 `XGE`로 검색해 확인하세요.
   - 이 경우 5.8의 C++ 분산은 Epic 내장 **UBA + Horde** 사용을 권장합니다. 무료이고 엔진에 포함되어 있으며, C++와 셰이더를 모두 분산합니다.
3. 셰이더: 5.x에는 Epic의 UBA 셰이더 컨트롤러도 같은 인터페이스로 등록됩니다. 엔진은 **먼저 등록되고 사용 가능한 컨트롤러 하나만** 씁니다. 에디터 콘솔(`~`)에서 `DistriBuild.Stats`를 실행해 `controller ACTIVE`가 나오는지 확인하세요. `NOT ACTIVE`이고 에이전트도 연결되어 있다면, UBA 셰이더 컴파일이 켜져 있는지 확인해야 합니다.

### 2-3. 두 버전 공통: 에이전트 쪽 준비

- **셰이더:** 에이전트에 **같은 엔진 버전**이 설치되어 있어야 합니다 (4.26.2 ↔ 4.26.2, 5.8.x ↔ 5.8.x). 하나의 에이전트에 두 엔진이 모두 설치되어 있으면 둘 다 받습니다.
- **C++:** 에이전트에 **같은 MSVC 버전**이 있어야 합니다. 4.26과 5.8은 보통 서로 다른 VS/MSVC를 쓰므로, 에이전트에도 두 버전을 모두 설치하세요.
- 에이전트가 무엇을 감지했는지는 이 명령으로 봅니다.
  ```bat
  python agent.py --print-detected
  ```

---

## ③ 분산되지 않은 작업 확인 방법

### 3-1. C++ (빌드할 때마다 자동으로 기록됨)

**빌드 출력 끝의 요약** — 로컬에서 실행된 작업을 이유별로 묶어 보여 줍니다.
```
DistriBuild: finished in 312.4s
  remote: 1840   local: 410   skipped/not run: 0
  cannot be distributed:
      120  not marked remote-safe by UBT (AllowRemote=False: link, PCH, codegen, ...)
       35  creates a precompiled header (/Yc)
  could be distributed but ran locally:
      230  local core was free (local_first)
       25  all agents with this compiler were busy
```

**작업별 보고서 (CSV, 엑셀로 열림)**: `%USERPROFILE%\.distribuild\last_build.csv`
열 구성: 작업 / 실행 위치 / 원격 가능 여부 / 로컬 실행 이유 / 소요 시간 / 종료 코드

**빌드 없이 미리 분석하기**: 마지막 빌드의 작업 파일이 자동 보관되므로, 이것으로 분석할 수 있습니다.
```bat
xgConsole --analyze "%USERPROFILE%\.distribuild\last_tasks.xml"
```
결과는 `%USERPROFILE%\.distribuild\analysis.csv`에도 저장됩니다.

**실시간으로 이유 보기**: 클라이언트 설정에 `"verbose": true`를 넣으면, 원격으로 보낼 수 있었던 작업이 로컬에서 실행될 때마다 이유를 출력합니다.

### 3-2. 셰이더

- **에디터 콘솔(`~`)에서 `DistriBuild.Stats` 실행** — 출력 로그에 아래처럼 나옵니다.
  ```
  ran locally 120 = no free compatible agent 100 + after remote failure 20 + disabled 0
  agent BUILD-PC-07  10.0.0.21:31890  online  slots 16 ... engines [5.8.0]
  agent BUILD-PC-09  10.0.0.23:31890  online  ... engines [5.4.4]  <- engine version does not match
  ```
- **DistriBuild Monitor 탭** (툴바 버튼 또는 Window → Developer Tools): 로컬 실행 수가 "no free agent"와 "after remote failure"로 나뉘어 표시됩니다.
- **원격 실패 상세**: 출력 로그에서 `LogDistriBuild` 필터 → `Shader job failed on ...` 메시지에 에이전트의 출력이 함께 나옵니다.

### 3-3. 이유별 대처법

| 표시되는 이유 | 의미 | 해결 |
|---|---|---|
| `local core was free (local_first)` | 정상. 내 PC 코어가 비어 있어서 먼저 쓴 것 | 필요 없음. 원격을 더 쓰려면 `"local_first": false` |
| `all agents with this compiler were busy` | 에이전트 슬롯이 부족함 | 에이전트나 슬롯을 늘립니다 |
| `no agent has this exact compiler (...)` | 같은 MSVC 버전을 가진 에이전트가 없음 | 에이전트에 그 MSVC 버전을 설치합니다 (괄호 안에 정확한 버전이 표시됨) |
| `remote run on X failed` | 원격에서 실패해 로컬에서 다시 실행함 | `"verbose": true`로 상세 이유를 보고, 에이전트 콘솔 로그도 확인합니다 |
| `network/agent error` | 연결이 끊김 | 방화벽(TCP 31890)과 에이전트 상태를 확인합니다 |
| `local preprocessing failed` | 대부분 실제 코드 오류 | 로컬 컴파일 결과의 에러를 고칩니다 |
| `creates a precompiled header (/Yc)` | PCH 생성 작업 | 구조상 로컬 전용 |
| `static analysis (/analyze)`, `C++20 modules ...` | 지원하지 않는 컴파일 형태 | 구조상 로컬 전용 |
| `not marked remote-safe by UBT` | 링크, 리소스 컴파일 등 | 구조상 로컬 전용 |
| (셰이더) `no free compatible agent` | 같은 엔진 버전의 에이전트가 없거나 모두 바쁨 | `DistriBuild.Stats`에서 `engine version does not match` 표시를 확인합니다 |

### 3-4. 이 도구가 아예 다루지 않는 작업

링크, UHT(헤더 툴), 쿠킹 자체, DDC 생성, **라이트 빌드(Lightmass/Swarm)**, 커스텀 빌드 단계, 엔진이 처음부터 로컬 워커에 맡긴 셰이더 작업은 분산되지 않습니다. Incredibuild처럼 파일 시스템을 가상화하지 않기 때문입니다.
