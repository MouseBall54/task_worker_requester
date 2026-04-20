# VC++ Redistributable 준비

설치형 배포(`IPDK_plusSetup.exe`)는 오프라인 환경에서도 Qt 런타임 의존성을 맞추기 위해
`vc_redist.x64.exe`를 인스톨러에 포함합니다.

아래 공식 링크에서 파일을 내려받아 이 경로에 배치하세요.

- 다운로드: https://aka.ms/vs/17/release/vc_redist.x64.exe
- 저장 위치: `packaging/prereqs/vc_redist.x64.exe`

`scripts/build_windows.ps1`는 빌드 시작 전에 위 파일 존재 여부를 검사합니다.
