# IPDK_plus (PySide6)

RabbitMQ로 이미지 단위 작업 요청을 전송하고(`IMG_LIST` 1건), 전용 결과 큐를 polling 하여 `request_id` 기준으로 상태를 추적하는 데스크톱 GUI 도구입니다.

## 주요 기능

- 폴더/하위 폴더 이미지 수집
- 즐겨찾기 Root 영속화와 SQLite 캐시 기반 빠른 폴더 검색
- 깊은 폴더 계층 선택 시 해당 노드가 가로 뷰포트 중앙에 오도록 자동 정렬
- 이미지 1건당 MQ 메시지 1건 전송
- `request_id` + `correlation_id` 기반 결과 매칭
- 폴더 단위 진행률/성공/실패/타임아웃 집계
- 실시간 로그 패널
- Mock Broker 모드 (`mock_mode: true`)
- 별도 recipe 설정 파일의 레시피 별명(`alias`) 선택 지원
- 단일 또는 다중 Recipe를 폴더 추가 시점에 지정하고 이미지×Recipe별 독립 메시지 발행
- request/result queue별 `queue_declare` 옵션 설정 지원
- 폴더 동시 전송 수 설정 지원
- 전체 모수 산정 후 RabbitMQ/Recipe/경로/작업량 사전 점검
- 대기 폴더 순서 이동 및 보류/해제
- 사용자 일시정지 상태 영속화와 확인 기반 재개
- 실행 이력 조회 및 작업 상세 CSV 내보내기
- request queue `x-max-priority` 기반 MQ priority 선택 지원
- 중복 실행 차단(같은 PC에서 1개 인스턴스만 허용)

## 실행

```bash
uv sync
uv run python main.py
```

커스텀 설정 경로:

```bash
uv run python main.py --config config/app_config.yaml
```

기존 positional 인자(`uv run python main.py config/app_config.yaml`)도 호환용으로 계속 허용됩니다.

## Config 탐색 우선순위

앱은 설정 파일을 아래 순서로 찾습니다.

1. `--config <path>` 로 직접 지정한 파일
2. `%APPDATA%\IPDK_plus\app_config.yaml`
3. 실행파일 옆 `config\app_config.yaml` 또는 실행파일 옆 `app_config.yaml`
4. 개발 실행 시 repo 기본값 [config/app_config.yaml](.\config\app_config.yaml)

인자를 주지 않고 실행하면 `%APPDATA%\IPDK_plus\` 를 우선 사용합니다. 첫 실행 시 기존 `%APPDATA%\TaskWorkerRequester\` 가 있고 새 위치가 비어 있으면 자동 마이그레이션한 뒤, 새 AppData 위치를 계속 사용합니다. `app_config.yaml`, `recipe_config.yaml` 이 모두 없으면 번들된 기본 템플릿을 자동으로 복사합니다.

## 중복 실행 정책

- `IPDK_plus` 는 같은 Windows 사용자 세션에서 한 번만 실행할 수 있습니다.
- 이미 실행 중인 상태에서 다시 실행하면 경고창을 띄우고 새로 실행한 인스턴스는 즉시 종료합니다.
- 이 정책은 결과 queue 충돌과 `request_id` 매칭 혼선을 방지하기 위한 것입니다.

## 기본 설정

`config/app_config.yaml`

- `mock_mode: true` 이면 실제 RabbitMQ 없이 시뮬레이션 결과를 생성합니다.
- 실제 서버 사용 시 `mock_mode: false` 로 변경 후 `rabbitmq` 섹션을 설정하세요.
- `recipe_config_path`는 별도 recipe 설정 YAML 파일 경로입니다.
- 예제 기본 경로는 [config/app_config.yaml](.\config\app_config.yaml) 기준 상대경로인 `recipe_config.yaml` 입니다.
- 별도 recipe 파일의 `recipes`에 `alias/path`를 등록하면 UI에는 별명이 표시되고 전송에는 실제 path가 사용됩니다.
- Recipe 선택 메뉴에서 하나 이상을 체크할 수 있습니다. 폴더를 추가하면 `폴더 경로 + Recipe 경로` 조합마다 독립된 대기열 행과 우선순위가 생성되며, 이미지 N개와 Recipe M개는 N×M개의 고유 request로 등록됩니다.
- 완전히 같은 폴더 경로와 Recipe 경로 조합만 중복으로 차단합니다. 이미 등록된 폴더라도 다른 Recipe를 선택해 다시 추가하면 별도 대기열 행으로 등록됩니다. 동일한 물리 폴더의 이미지 목록은 먼저 완료된 Recipe 대기열의 SQLite 집계 결과를 재사용하여 반복 스캔을 줄입니다.
- `rabbitmq.request_queue_declare`, `rabbitmq.result_queue_declare`로 queue declare 옵션을 각각 설정할 수 있습니다.
- `publish.max_active_open_folders`는 전체 모수 집계 시 동시에 스캔할 폴더 수와 전송 중 활성 폴더 수를 제한하고, `publish.initial_open_folders`는 집계 완료 후 처음 활성화할 폴더 수를 결정합니다. 이미지 작업은 전체 목록을 메모리에 유지하지 않고 SQLite에 청크 저장합니다.
- 작업 상태는 `%APPDATA%\IPDK_plus\runtime\task_state.sqlite3`에 저장되며, 앱은 `publish_chunk_size` 단위로만 메시지를 만들고 queue/inflight 상한에 따라 자동으로 발행을 멈췄다가 재개합니다. CMD 조회 방법은 [SQLite 작업 상태 조회 가이드](.\docs\sqlite_state_query_guide.md)를 참고하세요.
- 즐겨찾기 Root와 폴더명 검색 인덱스는 작업 상태와 분리된 `%APPDATA%\IPDK_plus\runtime\folder_index.sqlite3`에 저장됩니다. 신규 Root는 기본 하위 5계층이며, Root별로 하위 3·4·5·6계층, 전체 계층 또는 색인 제외 정책을 지정할 수 있습니다. SQLite 영속 대기열은 중단 후 재개되며 검색은 파일 시스템 재탐색 없이 캐시만 페이지 단위로 조회합니다. 자세한 상태·오류·운영 방법은 [즐겨찾기 Root 및 폴더 검색 가이드](.\docs\folder_search_index.md)를 참고하세요.
- 프로그램이 생성하고 표시하는 일시는 서울 표준시(`UTC+09:00`)와 0.1초 정밀도로 통일합니다. 과거 UTC 데이터도 화면과 이력 CSV에서는 서울 시간으로 변환됩니다.
- 상단 `설정` 메뉴에서 현재 실행에 사용 중인 `app_config.yaml`의 주요 RabbitMQ/publish 값과 `recipe_config.yaml`의 Recipe 목록을 검증 후 직접 저장할 수 있습니다. 설치형 실행에서는 `%APPDATA%\IPDK_plus`의 실제 런타임 파일이 편집되며 재시작 후 적용됩니다.
- 사용자가 `전송 시작`을 누른 작업이 앱 종료로 중단되면 미발행 CLAIMED 작업은 다시 대기로 돌리고, 저장된 Action·Priority·결과 큐를 사용해 미완료 스캔과 결과 polling을 다음 실행에서 자동 재개합니다. 시작하지 않고 쌓아둔 폴더는 목록만 복원되며 자동 전송되지 않습니다.
- `일시정지`를 누른 작업은 `PAUSED_BY_USER`로 별도 저장되어 앱을 다시 실행해도 자동 전송하지 않습니다. 시작할 때 재개 확인창이 나타나며 `전송 재개`를 눌러 수동으로 이어갈 수도 있습니다.
- 전체 폴더 스캔이 끝나면 최초 publish 전에 폴더·고유 이미지·Recipe·최종 메시지 수, 접근 불가 경로, RabbitMQ request queue 상태, Priority와 활성 폴더 정책을 사전 점검합니다. `preflight_warning_task_threshold` 이상이면 대량 작업 경고가 함께 표시됩니다.
- 폴더를 추가하면 SQLite `folders.position`에 0부터 시작하는 폴더+Recipe 대기열별 전송 우선순위가 등록 순서대로 저장되고, 진행중/대기 표에는 이를 1부터 시작하는 `우선순위`로 즉시 정렬해 표시합니다. 아직 스캔·전송·처리가 시작되지 않은 대기열은 `맨 위/위/아래/맨 아래`로 이동하거나 보류할 수 있으며, 변경 즉시 SQLite 우선순위와 실제 publish 순서에 반영됩니다. 보류 대기열은 전체 모수에는 포함되지만 보류 해제 전까지 MQ 발행 대상에서는 제외됩니다.
- `작업 > 실행 이력`에서 완료·초기화·현재 세션의 집계를 확인하고 선택 세션의 이미지×Recipe 작업 상세를 UTF-8 BOM CSV로 내보낼 수 있습니다. `history_max_sessions`를 넘은 오래된 완료/초기화 이력은 자동 정리됩니다.
- 선택한 폴더의 상세 작업은 최초 500행만 읽고, 스크롤 끝에서 다음 500행을 추가로 조회합니다. 화면 로그는 설정된 최대 줄 수만 유지하고 파일 로그는 회전 보관합니다.
- `publish.default_priority`는 기본 request MQ priority 입니다.
- UI의 `Priority` 드롭다운 범위는 `rabbitmq.request_queue_declare.arguments.x-max-priority` 값을 기준으로 `0..max`로 생성됩니다.
- `update.latest_release_url`은 앱 메뉴와 설치 프로그램의 업데이트 확인 링크에서 사용됩니다.
- 설치형 실행에서는 기본 편집 대상 설정 파일이 `%APPDATA%\IPDK_plus\app_config.yaml` 입니다.
- 로그 파일은 `%APPDATA%\IPDK_plus\logs\app.log` 에 기록되며, 설치 폴더 아래에는 로그를 만들지 않습니다.
- 새 설치본의 번들 설정 fingerprint가 바뀌면 다음 앱 실행 때 새 기본 설정이 `%APPDATA%\IPDK_plus`에 반영됩니다. 기존 `app_config.yaml`, `recipe_config.yaml`은 같은 폴더에 `.bak.<timestamp>` 백업으로 남깁니다.
- 설치 제거(Uninstall)는 사용자 AppData 설정을 삭제하지 않습니다.

### Recipe 설정 분리

- 메인 설정: [config/app_config.yaml](.\config\app_config.yaml)
  - `recipe_config_path: "recipe_config.yaml"`
- 별도 recipe 설정: [config/recipe_config.yaml](.\config\recipe_config.yaml)
  - `default_alias`
  - `recipes`
  - `recipes[].alias`
  - `recipes[].path`

메인 설정 파일 안의 inline `recipe_config` 블록은 더 이상 지원하지 않습니다.

### Recipe JSON 파일 주의사항

- 기본 seed 설정에는 `recipes/default_recipe.json` 같은 예시 경로가 들어 있지만, 현재 repo에는 실제 `recipes/*.json` 파일이 포함되어 있지 않습니다.
- 따라서 설치 후에는 사용 환경에 맞는 실제 recipe JSON 경로로 `recipe_config.yaml` 을 수정하는 것을 권장합니다.
- 앱은 시작 시 선택한 recipe 경로가 로컬에서 보이지 않으면 경고 로그를 남기지만, MQ payload 에는 설정된 `RECIPE_PATH` 문자열을 그대로 사용합니다.

### RabbitMQ 라우팅 설정 의미

- `request_queue`
  - 기본 exchange(`request_exchange: ""`) 사용 시 실제 publish 대상 queue 이름입니다.
  - 이 경우 routing key도 항상 `request_queue`로 강제됩니다.
- `request_queue_declare`
  - request queue 선언 시 사용할 `durable`, `exclusive`, `auto_delete`, `arguments` 설정입니다.
  - `arguments.x-max-priority`가 있으면 request publish 시 사용할 수 있는 priority 범위를 결정합니다.
- `request_routing_key`
  - custom exchange(`request_exchange != ""`) 사용 시 우선 routing key로 사용됩니다.
  - 비어 있으면 `request_queue`를 fallback routing key로 사용합니다.
- `result_queue_base`
  - 결과 수신 queue 이름의 접두어(prefix)입니다.
  - 실제 결과 queue 이름은 실행 PC의 대표 IPv4를 붙인 `result_queue_base_ipv4` 형식으로 결정됩니다.
  - 예: `IPDK_WORKER_INTERFACE_RESULT_192.168.0.10`
- `result_queue_declare`
  - result queue 선언 시 사용할 `durable`, `exclusive`, `auto_delete`, `arguments` 설정입니다.

## 요청 메시지 형식

현재 MQ 요청 payload는 아래 5개 키로 고정됩니다.

```json
{
  "request_id": "3dc7831b-7c4b-45f1-b5cb-f00e6952f6d5",
  "action": "RUN_RECIPE",
  "QUEUE_NAME": "task.result.client_192.168.0.10",
  "RECIPE_PATH": "recipes/default_recipe.json",
  "IMG_LIST": ["D:/data/folder_a/img001.jpg"]
}
```

- 이미지 1건당 메시지 1건으로 전송되며 `IMG_LIST` 길이는 항상 `1`입니다.
- 여러 Recipe가 지정된 이미지는 Recipe마다 고유한 `request_id`와 `RECIPE_PATH`를 가진 메시지를 기존 request queue에 각각 발행합니다. 별도 queue를 생성하지는 않습니다.
- `message_id`, `correlation_id`, `reply_to`는 각각 `request_id`, `request_id`, `QUEUE_NAME`으로 설정됩니다.
- `priority`는 JSON payload에 추가되지 않고, AMQP `BasicProperties.priority` 속성으로만 전송됩니다.
- `sent_at`는 앱 내부 상태 추적용이며 네트워크 payload에는 포함하지 않습니다.
- 결과 consumer는 `QUEUE_NAME`과 동일한 resolved queue를 consume 하고, `request_id`가 현재 세션에 등록된 요청과 매칭될 때만 상태 반영을 수행합니다.
- 매칭되지 않는 결과 메시지는 소비(ack)한 뒤 경고 로그만 남기고 무시합니다.

## 테스트

```bash
uv run python -m unittest discover -s tests -v
```

PySide6 미설치 환경에서는 GUI 의존 테스트(`test_controller`)가 자동 skip 됩니다.

## Python / 의존성 관리

- 기본 Python 버전은 `3.11` (`.python-version`) 입니다.
- `uv` 기준 의존성 소스는 `pyproject.toml` 입니다.
- Windows exe 빌드 도구는 `pyproject.toml` 의 `build` dependency group(`pyinstaller`)로 관리합니다.
- `requirements.txt`는 호환/참고용으로 유지됩니다.

## Windows 배포

- PyInstaller onedir GUI exe + Inno Setup 설치형 패키지 기준으로 구성했습니다.
- 설치형 패키지는 `packaging\prereqs\vc_redist.x64.exe`를 포함해 VC++ 런타임을 자동 설치합니다.
- 빌드 스크립트: [scripts/build_windows.ps1](.\scripts\build_windows.ps1)
- PyInstaller spec: [packaging/IPDK_plus.spec](.\packaging\IPDK_plus.spec)
- Inno Setup 스크립트: [packaging/IPDK_plus.iss](.\packaging\IPDK_plus.iss)
- 세부 절차 문서: [docs/build_windows.md](.\docs\build_windows.md)
- 설치 프로그램 파일명은 버전을 포함한 `IPDK_plusSetup_26.8.13.exe` 형식입니다.
- 시작 메뉴의 `업데이트 확인`과 앱의 `도움말 > 업데이트 확인`은 GitHub 최신 릴리스 링크로 연결됩니다.

기본 아이콘은 사용자 제공 [a5303f13-1f30-4cdd-9acb-964ee59596a7.png](.\a5303f13-1f30-4cdd-9acb-964ee59596a7.png)를 투명 배경으로 정리한 [assets/IPDK_plus.png](.\assets\IPDK_plus.png)와 Windows용 다중 해상도 [assets/IPDK_plus.ico](.\assets\IPDK_plus.ico)를 사용합니다.

- 작업표시줄 아이콘은 exe 내부에 박힌 아이콘을 사용합니다.
- 메인창 제목 표시줄 아이콘은 번들된 runtime asset `assets/IPDK_plus.png`를 우선 사용합니다.
- 따라서 설치형 산출물에는 `_internal\assets\IPDK_plus.png`, `_internal\assets\IPDK_plus.ico`가 포함되는 것이 정상입니다.

## 폴더 구조

- `app/`: 부트스트랩, 컨트롤러
- `config/`: 설정 모델 및 로더
- `models/`: 도메인 모델/enum
- `services/`: 폴더 스캐너, 메시지 파서, 브로커, 워커
- `state/`: 중앙 상태 저장소
- `ui/`: 메인 윈도우, 테이블 모델, delegate, QSS
- `tests/`: 단위 테스트
- `utils/`: 로깅, Qt 호환 레이어
