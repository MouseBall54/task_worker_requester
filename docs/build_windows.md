## Windows Build Guide

### Overview

이 프로젝트는 Windows에서 아래 두 단계로 배포합니다.

1. `PyInstaller` 로 `IPDK_plus.exe` onedir 산출물 생성
2. `Inno Setup` 으로 설치형 패키지 생성

기본 아이콘은 사용자 제공 `C:\Users\youngmoon\Pictures\IPDK_plus_02_layer_stack.ico` 를 반영한 [assets/IPDK_plus.ico](.\assets\IPDK_plus.ico) 를 사용합니다.

### Prerequisites

- Python `3.11`
- `uv`
- Inno Setup 6 이상 (`ISCC.exe` 가 PATH 에 잡혀 있으면 가장 편합니다)

### 1. Build Environment

```powershell
uv sync --group build
```

### 2. Create EXE

권장 방식:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

직접 실행:

```powershell
uv run --group build pyinstaller .\packaging\IPDK_plus.spec --clean --noconfirm
```

성공하면 아래 폴더가 생성됩니다.

- `dist\IPDK_plus\IPDK_plus.exe`

제목 표시줄 아이콘까지 정상 표시되려면 아래 runtime asset도 함께 포함되어야 합니다.

- `dist\IPDK_plus\_internal\assets\IPDK_plus.ico`

작업표시줄 아이콘은 exe 내부 아이콘을 사용하고, 메인창 제목 표시줄 아이콘은 위 runtime asset을 읽어 설정합니다.

### 3. Build Installer

```powershell
ISCC .\packaging\IPDK_plus.iss
```

성공하면 `dist\installer\IPDK_plusSetup.exe` 가 생성됩니다.

### Runtime Config Location

설치 후 사용자가 수정할 기본 설정은 설치 폴더가 아니라 아래 위치를 우선 사용합니다.

```text
%APPDATA%\IPDK_plus\
```

주요 파일:

- `%APPDATA%\IPDK_plus\app_config.yaml`
- `%APPDATA%\IPDK_plus\recipe_config.yaml`
- `%APPDATA%\IPDK_plus\logs\app.log`

앱 첫 실행 시 위 파일이 없으면 번들된 seed 템플릿을 자동 복사합니다.
로그도 동일한 AppData 루트 아래에 기록되며, 설치 폴더(`Program Files` 등) 아래에 `logs` 디렉터리를 만들지 않습니다.

### Config Override

기본 AppData 설정 대신 다른 YAML 을 직접 지정하려면:

```powershell
.\IPDK_plus.exe --config "D:\custom\app_config.yaml"
```

### Recipe Notes

- `recipe_config.yaml` 안의 `path` 값은 현재 환경에 맞는 실제 recipe JSON 경로로 수정하는 것을 권장합니다.
- 기본 seed 설정은 예시 경로를 담고 있으며, repo 에 실제 `recipes\*.json` 파일은 포함되어 있지 않습니다.
- 앱은 선택한 recipe 파일이 로컬에 없으면 경고 로그를 남기지만, MQ payload 의 `RECIPE_PATH` 값은 설정 문자열 그대로 유지합니다.
