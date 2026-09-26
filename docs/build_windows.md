## Windows Build Guide

### Quick Commands

새 버전 배포에 필요한 명령을 순서대로 모았습니다. 자세한 설명은 아래 각 절을 참고하세요.

```powershell
# 0. 버전 올리기: app\version.py 의 APP_VERSION 한 줄만 수정 (예: APP_VERSION = "26.10.1")

# 1. 빌드 도구 설치 (최초 1회 또는 의존성 변경 시)
uv sync --group build

# 2. 테스트
uv run python -m unittest discover -s tests -v

# 3. EXE + 설치 파일 빌드 (ISCC 가 PATH 에 있으면 설치 파일까지 생성)
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1

# 3-1. ISCC 가 PATH 에 없어 설치 파일이 건너뛰어졌다면 직접 실행
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" .\packaging\IPDK_plus.iss

# 4. NAS 배포 (사용자 앱이 시작 시 자동으로 감지)
Copy-Item .\dist\installer\IPDK_plusSetup_*.exe "\\12.56.53.186\ssa_new\sw\ipdk_plus\"
```

### Overview

이 프로젝트는 Windows에서 아래 두 단계로 배포합니다.

1. `PyInstaller` 로 `IPDK_plus.exe` onedir 산출물 생성
2. `Inno Setup` 으로 설치형 패키지 생성

기본 아이콘은 사용자 제공 [a5303f13-1f30-4cdd-9acb-964ee59596a7.png](.\a5303f13-1f30-4cdd-9acb-964ee59596a7.png)를 투명 배경으로 정리한 [assets/IPDK_plus.png](.\assets\IPDK_plus.png)와 Windows용 다중 해상도 [assets/IPDK_plus.ico](.\assets\IPDK_plus.ico)를 사용합니다.

### Prerequisites

- Python `3.11`
- `uv`
- Inno Setup 6 이상 (`ISCC.exe` 가 PATH 에 잡혀 있으면 가장 편합니다)
- `packaging\prereqs\vc_redist.x64.exe` (Microsoft Visual C++ Redistributable x64)

`vc_redist.x64.exe`는 Microsoft 공식 배포 파일을 사용하세요.  
권장 경로: `https://aka.ms/vs/17/release/vc_redist.x64.exe`

### 1. Build Environment

```powershell
uv sync --group build
```

### 2. Create EXE

권장 방식:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

위 스크립트는 아래를 자동 검증합니다.

- `packaging\prereqs\vc_redist.x64.exe` 존재 여부
- PyInstaller 산출물의 Qt/VC 핵심 파일 존재 여부
- `assets\icons`의 폴더 우선순위·보류 SVG 아이콘 존재 여부
- QWidget 앱에서 쓰지 않는 Qt Quick/QML/PDF/VirtualKeyboard 계열 번들 파일 제거

직접 실행:

```powershell
uv run --group build pyinstaller .\packaging\IPDK_plus.spec --clean --noconfirm
```

성공하면 아래 폴더가 생성됩니다.

- `dist\IPDK_plus\IPDK_plus.exe`

제목 표시줄 아이콘까지 정상 표시되려면 아래 runtime asset도 함께 포함되어야 합니다.

- `dist\IPDK_plus\_internal\assets\IPDK_plus.png`
- `dist\IPDK_plus\_internal\assets\IPDK_plus.ico`
- `dist\IPDK_plus\_internal\assets\icons\*.svg`

작업표시줄 아이콘은 exe 내부 ICO를 사용하고, 메인창 제목 표시줄 아이콘은 PNG runtime asset을 우선 읽어 설정합니다.

### 3. Build Installer

```powershell
ISCC .\packaging\IPDK_plus.iss
```

성공하면 `dist\installer\IPDK_plusSetup_26.9.23.exe` 가 생성됩니다.

설치 과정에서 `vc_redist.x64.exe`를 자동으로 `silent` 설치합니다.  
Python 이 전혀 설치되지 않은 PC에서도 Qt DLL 로딩 실패를 방지하기 위한 필수 단계입니다.

### Runtime Config Location

설치 후 사용자가 수정할 기본 설정은 설치 폴더가 아니라 아래 위치를 우선 사용합니다.

```text
%APPDATA%\IPDK_plus\
```

주요 파일:

- `%APPDATA%\IPDK_plus\app_config.yaml`
- `%APPDATA%\IPDK_plus\recipe_config.yaml`
- `%APPDATA%\IPDK_plus\worker_nodes.yaml`
- `%APPDATA%\IPDK_plus\logs\app.log`
- `%APPDATA%\IPDK_plus\runtime\ui_state.ini` (진행중/대기·완료 표의 사용자 조정 열 너비)

앱 첫 실행 시 위 파일이 없으면 번들된 seed 템플릿을 자동 복사합니다.
로그도 동일한 AppData 루트 아래에 기록되며, 설치 폴더(`Program Files` 등) 아래에 `logs` 디렉터리를 만들지 않습니다.

앱은 마지막으로 적용한 seed fingerprint를 `%APPDATA%\IPDK_plus\.seed_fingerprint`에 저장합니다.
fingerprint에는 앱 버전이 포함되므로 새 버전을 설치하면 항상, 같은 버전이라도 `app_config.yaml`, `recipe_config.yaml` 내용이 달라지면 다음 앱 실행 시 기존 파일은 `.bak.<timestamp>`로 백업되고 새 seed 템플릿으로 갱신됩니다.

주의: 언인스톨 시 `%APPDATA%\IPDK_plus`는 삭제하지 않습니다.

### Config Override

기본 AppData 설정 대신 다른 YAML 을 직접 지정하려면:

```powershell
.\IPDK_plus.exe --config "D:\custom\app_config.yaml"
```

### Recipe Notes

- `recipe_config.yaml` 안의 `path` 값은 현재 환경에 맞는 실제 recipe JSON 경로로 수정하는 것을 권장합니다.
- 기본 seed 설정은 예시 경로를 담고 있으며, repo 에 실제 `recipes\*.json` 파일은 포함되어 있지 않습니다.
- 앱은 선택한 recipe 파일이 로컬에 없으면 경고 로그를 남기지만, MQ payload 의 `RECIPE_PATH` 값은 설정 문자열 그대로 유지합니다.

### Update Deployment

- 버전은 `app/version.py`의 `APP_VERSION` 한 곳에서만 관리합니다. `packaging/IPDK_plus.iss`는 컴파일할 때 이 파일을 직접 읽어 설치 파일명과 버전 정보를 만들고, `pyproject.toml`은 `[tool.hatch.version]`으로 같은 값을 읽습니다.

- 빌드된 `dist\installer\IPDK_plusSetup_<버전>.exe`를 `\\12.56.53.186\ssa_new\sw\ipdk_plus`에 복사하면 배포가 끝납니다.
- 앱은 시작 시와 `도움말 > 업데이트 확인` 때 이 폴더에서 가장 높은 버전의 설치 파일을 찾고, 현재 버전보다 높으면 설치 여부를 묻습니다. 확인하면 설치 프로그램을 실행하고 앱을 종료합니다.
- 설치 프로그램의 `AppUpdatesURL`과 시작 메뉴의 `업데이트 확인` shortcut도 같은 NAS 폴더를 가리킵니다.
- NAS 경로가 바뀌면 `config/app_config.yaml`의 `update.share_dir`, `config/models.py`의 기본값, `packaging/IPDK_plus.iss`의 `MyUpdateUrl`을 함께 맞추세요.
