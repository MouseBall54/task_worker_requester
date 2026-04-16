# RabbitMQ Task Worker Requester (PySide6)

RabbitMQ로 이미지 단위 작업 요청을 전송하고(`IMG_LIST` 1건), 전용 결과 큐를 polling 하여 `request_id` 기준으로 상태를 추적하는 데스크톱 GUI 도구입니다.

## 주요 기능

- 폴더/하위 폴더 이미지 수집
- 이미지 1건당 MQ 메시지 1건 전송
- `request_id` + `correlation_id` 기반 결과 매칭
- 폴더 단위 진행률/성공/실패/타임아웃 집계
- 실시간 로그 패널
- Mock Broker 모드 (`mock_mode: true`)
- 별도 recipe 설정 파일의 레시피 별명(`alias`) 선택 지원
- request/result queue별 `queue_declare` 옵션 설정 지원
- 폴더 동시 전송 수 설정 지원
- request queue `x-max-priority` 기반 MQ priority 선택 지원

## 실행

```bash
uv sync
uv run python main.py
```

커스텀 설정 경로:

```bash
uv run python main.py config/app_config.yaml
```

## 기본 설정

`config/app_config.yaml`

- `mock_mode: true` 이면 실제 RabbitMQ 없이 시뮬레이션 결과를 생성합니다.
- 실제 서버 사용 시 `mock_mode: false` 로 변경 후 `rabbitmq` 섹션을 설정하세요.
- `recipe_config_path`는 별도 recipe 설정 YAML 파일 경로입니다.
- 예제 기본 경로는 [config/app_config.yaml](D:\GIT\task_worker_requester\config\app_config.yaml) 기준 상대경로인 `recipe_config.yaml` 입니다.
- 별도 recipe 파일의 `recipes`에 `alias/path`를 등록하면 UI에는 별명이 표시되고 전송에는 실제 path가 사용됩니다.
- `rabbitmq.request_queue_declare`, `rabbitmq.result_queue_declare`로 queue declare 옵션을 각각 설정할 수 있습니다.
- `publish.initial_open_folders`, `publish.max_active_open_folders`로 폴더 개방 정책을 조정할 수 있습니다.
- `publish.default_priority`는 기본 request MQ priority 입니다.
- UI의 `Priority` 드롭다운 범위는 `rabbitmq.request_queue_declare.arguments.x-max-priority` 값을 기준으로 `0..max`로 생성됩니다.

### Recipe 설정 분리

- 메인 설정: [config/app_config.yaml](D:\GIT\task_worker_requester\config\app_config.yaml)
  - `recipe_config_path: "recipe_config.yaml"`
- 별도 recipe 설정: [config/recipe_config.yaml](D:\GIT\task_worker_requester\config\recipe_config.yaml)
  - `default_alias`
  - `recipes`
  - `recipes[].alias`
  - `recipes[].path`

메인 설정 파일 안의 inline `recipe_config` 블록은 더 이상 지원하지 않습니다.

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
  - 결과 수신에 사용하는 고정 queue 이름입니다.
  - 실행 중에도 동일한 queue 이름(`result_queue_base`)으로 polling 합니다.
- `result_queue_declare`
  - result queue 선언 시 사용할 `durable`, `exclusive`, `auto_delete`, `arguments` 설정입니다.

## 요청 메시지 형식

현재 MQ 요청 payload는 아래 5개 키로 고정됩니다.

```json
{
  "request_id": "3dc7831b-7c4b-45f1-b5cb-f00e6952f6d5",
  "action": "RUN_RECIPE",
  "QUEUE_NAME": "task.result.client",
  "RECIPE_PATH": "recipes/default_recipe.json",
  "IMG_LIST": ["D:/data/folder_a/img001.jpg"]
}
```

- 이미지 1건당 메시지 1건으로 전송되며 `IMG_LIST` 길이는 항상 `1`입니다.
- `message_id`, `correlation_id`, `reply_to`는 각각 `request_id`, `request_id`, `QUEUE_NAME`으로 설정됩니다.
- `priority`는 JSON payload에 추가되지 않고, AMQP `BasicProperties.priority` 속성으로만 전송됩니다.
- `sent_at`는 앱 내부 상태 추적용이며 네트워크 payload에는 포함하지 않습니다.

## 테스트

```bash
uv run python -m unittest discover -s tests -v
```

PySide6 미설치 환경에서는 GUI 의존 테스트(`test_controller`)가 자동 skip 됩니다.

## Python / 의존성 관리

- 기본 Python 버전은 `3.11` (`.python-version`) 입니다.
- `uv` 기준 의존성 소스는 `pyproject.toml` 입니다.
- `requirements.txt`는 호환/참고용으로 유지됩니다.

## 폴더 구조

- `app/`: 부트스트랩, 컨트롤러
- `config/`: 설정 모델 및 로더
- `models/`: 도메인 모델/enum
- `services/`: 폴더 스캐너, 메시지 파서, 브로커, 워커
- `state/`: 중앙 상태 저장소
- `ui/`: 메인 윈도우, 테이블 모델, delegate, QSS
- `tests/`: 단위 테스트
- `utils/`: 로깅, Qt 호환 레이어
