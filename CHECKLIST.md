# 적용 체크리스트 (UE 4.26 / UE 5.8)

순서: **① 헬퍼 PC → ② 개발자 PC(C++) → ③ 플러그인(셰이더) → ④ 분산되지 않은 작업 확인**

①과 ②는 Qt 설정 창(`Tools\DistriBuild\setup_gui.py`, README의 "GUI로 설정하기")에서도 할 수 있습니다.

---

## ① 헬퍼 PC

- [ ] 에이전트 실행: `python agent.py --config agent.json` (또는 `DistriBuild.exe agent --config agent.json`)
- [ ] 방화벽: **TCP 31890**, **UDP 31891** 허용
- [ ] `python agent.py --print-detected`에 필요한 항목이 보이는지 확인

| 작업 | 에이전트에 필요한 것 |
|---|---|
| C++ | 개발자 PC의 UBT가 고른 것과 **빌드 번호까지 같은** MSVC (예: `msvc-19.44.35229-x64`). `xgConsole --doctor`에 PC에 있는 컴파일러별 원격 슬롯 수가 나옵니다 |
| 셰이더 | 같은 버전의 엔진 바이너리 (`ShaderCompileWorker.exe`와 셰이더 포맷 DLL). 소스 빌드 엔진은 `--engine <루트>` |

엔진 소스와 프로젝트 소스, Windows SDK는 필요 없습니다. 디스크는 PCH 캐시용으로 40GB쯤 남겨 두세요(`pch_cache_gb`로 조절).

## ② 개발자 PC: C++

- [ ] `Tools\DistriBuild\build.bat`
- [ ] **관리자 권한으로** `Tools\DistriBuild\install_client.bat`
- [ ] 새 터미널에서 `xgConsole --doctor` 실행. 모든 항목이 `[ok]`인지 확인
- [ ] 빌드 로그에 `DistriBuild: N actions, X local slots, Y remote slots`가 나오는지 확인

### 왜 서비스를 설치하나요

UBT의 `XGE.IsAvailable()`은 Windows에서 **`Incredibuild Agent` 서비스가 실행 중이 아니면 XGE 실행기를 쓰지 않습니다.** PATH에 `xgConsole.exe`를 두는 것만으로는 부족합니다. 설치 스크립트는 아무 일도 하지 않는 같은 이름의 서비스를 등록합니다(권한: LocalService). 엔진은 수정하지 않습니다.

두 엔진 모두 기본 설정에서 XGE를 먼저 고르므로 `BuildConfiguration.xml`은 고칠 필요가 없습니다. 예외가 있습니다.

| 상황 | 동작 |
|---|---|
| `<bAllowXGE>false</bAllowXGE>` | 분산 안 됨. `--doctor`가 알려 줌 |
| UE 5.8에서 액션 수가 코어 수보다 적음 | UBT가 XGE 대신 로컬 UBA 실행기를 씀(`minActionsForRemote`). 작은 빌드라 영향 없음 |
| 실제 Incredibuild 설치됨 | UBT가 레지스트리 경로를 먼저 찾으므로 그쪽이 실행됨. 설치 스크립트가 중단함 |

### PCH 공유

처음 빌드할 때 PCH를 에이전트로 올립니다(UE 5.8 UnrealEd PCH 2.4GB, 로컬 호스트에서 약 30초). 올리는 동안 같은 PCH를 쓰는 작업은 로컬에서 실행하고, 이후로는 PCH 뒤쪽 텍스트만 보냅니다. PCH가 다시 만들어지면 자동으로 다시 올립니다.

`"share_pch": false`로 끄면 헤더 전체를 텍스트로 보냅니다. 파일 하나에 수 분까지 걸릴 수 있어 권장하지 않습니다.

## ③ 개발자 PC: 셰이더 (플러그인)

- [ ] `Tools\BuildPlugin.bat "<엔진 경로>"`로 빌드 확인
- [ ] `Plugin\DistriBuild`를 프로젝트 `Plugins\`에 복사하고 활성화
- [ ] 출력 로그에 `Using DistriBuild for shader compilation` 표시 확인

| 차이 | UE 4.26 | UE 5.8 |
|---|---|---|
| 선택 우선순위 | 먼저 등록된 지원 컨트롤러 | UBA > XGE > 그 외. `-UBA`나 Horde 설정이 켜져 있으면 UBA가 우선 |
| 셰이더 소스 | 작업 파일에 **이 PC의 절대 경로**가 들어감. 플러그인이 셰이더 폴더를 번들로 올리고 에이전트가 경로를 재작성 | 작업 파일에 소스 포함. 번들 불필요 |
| 프로젝트·플러그인 셰이더 | 번들에 포함되어 동작 | 작업 파일에 포함되어 동작 |
| 셰이더 수정 | 5초 내에 감지해 새 번들을 올림 | 그대로 반영 |

실제 Incredibuild가 설치된 PC에서는 엔진의 XGE 컨트롤러가 먼저 선택됩니다. `xgConsole.exe`만 PATH에 있는 경우는 해당하지 않습니다(엔진은 PATH에서 `Xoreax\IncrediBuild` 경로만 찾습니다).

## ④ 분산되지 않은 작업 확인

### C++

**빌드 출력 끝의 요약**
```
DistriBuild: finished in 312.4s
  remote: 1840   local: 410   skipped/not run: 0
  cannot be distributed:
      120  not marked remote-safe by UBT (AllowRemote=False: link, PCH, codegen, ...)
  could be distributed but ran locally:
      230  local core was free (local_first)
       25  all agents with this compiler were busy
```

작업별 보고서: `%USERPROFILE%\.distribuild\last_build.csv`  
빌드 없이 분석: `xgConsole --analyze "%USERPROFILE%\.distribuild\last_tasks.xml"`  
실시간 이유 출력: 클라이언트 설정에 `"verbose": true`

### 셰이더

에디터 콘솔에서 `DistriBuild.Stats` 실행, 또는 **DistriBuild Monitor** 탭을 봅니다. 에디터 종료 시 로그에도 합계가 남습니다.

### 이유별 대처

| 표시되는 이유 | 의미 | 해결 |
|---|---|---|
| `local core was free (local_first)` | 정상. 내 PC 코어가 비어 있어서 먼저 씀 | 원격을 더 쓰려면 `"local_first": false` |
| `agents were still receiving this precompiled header` | 정상. PCH 업로드 중 | 기다리면 해소됨 |
| `all agents with this compiler were busy` | 슬롯 부족 | 에이전트나 슬롯을 늘림 |
| `no agent has this exact compiler (...)` | 같은 MSVC 빌드가 없음 | 괄호 안 버전을 에이전트에 설치 |
| `remote run on X failed` | 원격 실패 후 로컬 재실행 | 코드 오류면 정상. 반복되면 `"verbose": true`와 에이전트 로그 확인 |
| `network/agent error` | 연결 끊김 | 방화벽(TCP 31890), 에이전트 상태 확인 |
| `local preprocessing failed` | 대부분 실제 코드 오류 | 로컬 컴파일 결과의 오류를 고침 |
| `creates a precompiled header (/Yc)`, `not marked remote-safe by UBT` | 링크, PCH 생성 등 | 구조상 로컬 전용 |
| `tool is not a supported compiler (rc.exe)` | 리소스 컴파일 등 | 구조상 로컬 전용 |
| (셰이더) `no free compatible agent` | 같은 엔진 버전 없음 또는 전부 사용 중 | `DistriBuild.Stats`에서 `engine version does not match` 확인 |

### 이 도구가 다루지 않는 작업

링크, UHT, 쿠킹 자체, DDC 생성, 라이트 빌드(Lightmass/Swarm), 커스텀 빌드 단계는 분산되지 않습니다. Incredibuild처럼 파일 시스템을 가상화하지 않기 때문입니다.
