# IPDK_plus MQ Message and Config Specification

이 문서는 IPDK_plus와 worker가 RabbitMQ로 협업할 때 맞춰야 하는 메시지 계약과 설정값을 정리한 문서입니다.

기준 구현:

- `config/app_config.yaml`
- `config/recipe_config.yaml`
- `models/task_models.py`
- `services/broker/rabbitmq_client.py`
- `services/result_parser.py`
- `services/workers/polling_worker.py`

## 1. 전체 흐름

1. 사용자가 GUI에서 이미지 폴더를 등록한다.
2. 앱은 이미지 파일 1개마다 내부 `request_id`를 1개 생성한다.
3. 실행 시 앱은 결과 수신 큐 이름을 먼저 결정한다.
4. 앱은 결과 수신 큐를 RabbitMQ에 `queue_declare` 한다.
5. 앱은 이미지 1개당 request MQ message 1건을 request queue로 publish 한다.
6. worker는 request queue에서 메시지를 consume 한다.
7. worker는 request payload의 `QUEUE_NAME`으로 결과 메시지를 publish 한다.
8. 앱은 result queue를 consume 하면서 `request_id`를 현재 세션의 작업과 매칭한다.
9. 매칭된 결과는 성공/실패로 반영되고 UI 상태가 갱신된다.
10. 매칭되지 않는 결과 메시지는 broker에서는 ACK 처리되고 앱 상태에는 반영되지 않는다.

현재 구조에서 request message는 "이미지 1장당 1건"입니다. `IMG_LIST`는 배열이지만 현재 앱이 만드는 메시지는 항상 이미지 경로 1개만 포함합니다.

## 2. 설정 파일 위치와 로딩 우선순위

앱 실행 시 `app_config.yaml`은 아래 순서로 결정됩니다.

1. CLI에서 `--config <path>`를 넘긴 경우 해당 파일을 사용한다.
2. 기존 호환용 positional config path를 넘긴 경우 해당 파일을 사용한다.
3. 명시 경로가 없으면 `%APPDATA%\IPDK_plus\app_config.yaml`을 사용한다.
4. `%APPDATA%\IPDK_plus\app_config.yaml`이 없으면 설치 패키지에 포함된 `config/app_config.yaml`을 seed로 복사한다.
5. AppData seed가 실패하면 실행 파일 옆 `config\app_config.yaml`을 찾는다.
6. 그 다음 실행 파일 옆 `app_config.yaml`을 찾는다.
7. 개발 실행에서는 repo의 `config/app_config.yaml`을 fallback으로 사용한다.

`recipe_config.yaml`은 `app_config.yaml`의 `recipe_config_path`로 지정합니다. 현재 값은 `"recipe_config.yaml"`이며, 상대 경로이므로 사용 중인 `app_config.yaml`과 같은 폴더 기준으로 해석됩니다.

설치형 실행의 기본 편집 대상:

- `%APPDATA%\IPDK_plus\app_config.yaml`
- `%APPDATA%\IPDK_plus\recipe_config.yaml`

## 3. 현재 app_config.yaml 예시

현재 repo 기준 `config/app_config.yaml` 전체 내용입니다.

```yaml
mock_mode: false
log_level: INFO
recipe_config_path: "recipe_config.yaml"

rabbitmq:
  host: "127.0.0.1"
  port: 5672
  username: "young"
  password: "young"
  virtual_host: "/"
  request_exchange: ""
  request_routing_key: "task.request"
  request_queue: "IPDK_WORKER_INTERFACE" #목표 큐 이름
  result_queue_base: "IPDK_WORKER_INTERFACE_RESULT" #결과 큐 접두어. 실제 consume 큐는 {base}_{실행PC IPv4} 형식으로 결정됨
  request_queue_declare:
    durable: true
    exclusive: false
    auto_delete: false
    arguments:
      x-max-priority: 5
      module_group: "IPDK_WORKER"
  result_queue_declare:
    durable: true
    exclusive: false
    auto_delete: false
    arguments:
      x-max-priority: 5
      module_group: "default"
  heartbeat: 30
  blocked_connection_timeout: 30
  connection_attempts: 3
  retry_delay_seconds: 2.0
publish:
  default_action: "RCP_EXECUTE"
  default_priority: 0
  polling_interval_seconds: 5
  timeout_seconds: 86400
  max_messages_per_poll: 100
  max_publish_retries: 3
  publish_retry_backoff_seconds: 1.5
  initial_open_folders: 2
  max_active_open_folders: 3
  image_extensions: [".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"]
  scan_mode: "direct"

ui:
  app_name: "IPDK_plus"
  window_width: 1480
  window_height: 900
  theme: "dark"
  font_family: "Segoe UI"
```

## 4. 현재 recipe_config.yaml 예시

현재 repo 기준 `config/recipe_config.yaml` 전체 내용입니다.

```yaml
default_alias: "Default Recipe"
recipes:
  - alias: "Default Recipe"
    path: "recipes/default_recipe.json"
  - alias: "Fast Recipe"
    path: "recipes/fast_recipe.json"
  - alias: "Precision Recipe"
    path: "recipes/precision_recipe.json"
```

`path` 값은 request payload의 `RECIPE_PATH`로 그대로 들어갈 수 있습니다. 앱은 선택한 recipe 파일이 로컬에서 보이지 않으면 경고 로그를 남기지만, MQ payload에는 설정 문자열을 그대로 전송합니다.

## 5. 설정값 상세

### 5.1 Top-level

| Key | 현재 값 | 의미 | 변경 시 영향 | 주의사항 |
| --- | --- | --- | --- | --- |
| `mock_mode` | `false` | 실제 RabbitMQ 대신 mock broker를 쓸지 여부 | `true`면 실제 MQ 접속 없이 내부 mock 결과가 생성됨 | 실제 worker 연동 테스트에서는 `false` 필요 |
| `log_level` | `INFO` | 앱 로그 레벨 | `DEBUG`, `INFO` 등으로 로그 상세도가 바뀜 | 너무 낮은 레벨은 로그 파일이 커질 수 있음 |
| `recipe_config_path` | `"recipe_config.yaml"` | 별도 recipe 설정 파일 경로 | recipe 목록과 기본 recipe 선택 위치가 바뀜 | 상대 경로는 `app_config.yaml` 위치 기준 |

### 5.2 rabbitmq

| Key | 현재 값 | 의미 | 변경 시 영향 | 주의사항 |
| --- | --- | --- | --- | --- |
| `rabbitmq.host` | `"127.0.0.1"` | RabbitMQ 서버 host | 접속 대상 서버가 바뀜 | 다른 PC/서버를 쓰면 방화벽과 RabbitMQ listen 주소 확인 필요 |
| `rabbitmq.port` | `5672` | AMQP port | 접속 port가 바뀜 | TLS 미사용 기본 AMQP port 기준 |
| `rabbitmq.username` | `"young"` | RabbitMQ 사용자명 | 인증 계정이 바뀜 | 운영 배포 시 계정/권한을 별도로 관리해야 함 |
| `rabbitmq.password` | `"young"` | RabbitMQ 비밀번호 | 인증 비밀번호가 바뀜 | 문서/공유 시 비밀번호 노출 주의 |
| `rabbitmq.virtual_host` | `"/"` | RabbitMQ vhost | queue/exchange namespace가 바뀜 | worker와 앱이 같은 vhost를 써야 함 |
| `rabbitmq.request_exchange` | `""` | request publish exchange | 빈 문자열이면 RabbitMQ default exchange 사용 | 현재 설정에서는 실제 exchange가 `""` |
| `rabbitmq.request_routing_key` | `"task.request"` | custom exchange 사용 시 routing key | `request_exchange`가 비어 있지 않을 때 publish routing key로 사용됨 | 현재 설정에서는 사용되지 않음 |
| `rabbitmq.request_queue` | `"IPDK_WORKER_INTERFACE"` | request queue 이름 | 앱이 request message를 publish하는 대상 queue가 바뀜 | 현재 설정에서는 실제 routing key도 이 값 |
| `rabbitmq.result_queue_base` | `"IPDK_WORKER_INTERFACE_RESULT"` | result queue prefix | 앱이 consume할 결과 queue prefix가 바뀜 | 실제 queue는 `{base}_{실행PC IPv4}` |
| `rabbitmq.heartbeat` | `30` | AMQP heartbeat seconds | 연결 생존 확인 주기가 바뀜 | 네트워크가 불안정하면 너무 낮은 값은 끊김을 유발할 수 있음 |
| `rabbitmq.blocked_connection_timeout` | `30` | broker blocked 상태 timeout | broker가 blocked일 때 대기 시간이 바뀜 | publish hang 방지용 |
| `rabbitmq.connection_attempts` | `3` | 연결 시도 횟수 | 초기 접속 재시도 횟수가 바뀜 | 너무 높으면 실패 피드백이 늦어짐 |
| `rabbitmq.retry_delay_seconds` | `2.0` | 연결 재시도 간격 | 재시도 간격이 바뀜 | seconds 단위 float |

### 5.3 rabbitmq.request_queue_declare

이 설정은 앱이 실제 RabbitMQ client로 연결할 때 request queue를 선언하는 옵션입니다.

| Key | 현재 값 | 의미 | 변경 시 영향 | 주의사항 |
| --- | --- | --- | --- | --- |
| `rabbitmq.request_queue_declare.durable` | `true` | queue를 broker 재시작 후에도 유지할지 여부 | `false`면 durable queue가 아님 | 기존 queue와 durable 값이 다르면 RabbitMQ가 precondition failed를 낼 수 있음 |
| `rabbitmq.request_queue_declare.exclusive` | `false` | 현재 connection 전용 queue 여부 | `true`면 다른 connection이 쓰기 어려움 | 협업 worker queue에는 보통 `false` 유지 |
| `rabbitmq.request_queue_declare.auto_delete` | `false` | consumer가 없어지면 자동 삭제할지 여부 | `true`면 queue가 자동 삭제될 수 있음 | 운영 queue에는 보통 `false` 유지 |
| `rabbitmq.request_queue_declare.arguments.x-max-priority` | `5` | request queue priority 최대값 | UI priority 선택 가능 범위와 AMQP priority 의미에 영향 | RabbitMQ queue argument는 기존 queue 생성 후 변경 불가 |
| `rabbitmq.request_queue_declare.arguments.module_group` | `"IPDK_WORKER"` | queue에 붙이는 커스텀 argument | broker/운영 정책에서 분류 용도로 쓸 수 있음 | RabbitMQ 표준 동작 필드는 아니며 worker와 합의 필요 |

### 5.4 rabbitmq.result_queue_declare

이 설정은 앱이 결과 수신 queue를 선언하는 옵션입니다.

| Key | 현재 값 | 의미 | 변경 시 영향 | 주의사항 |
| --- | --- | --- | --- | --- |
| `rabbitmq.result_queue_declare.durable` | `true` | result queue를 broker 재시작 후에도 유지할지 여부 | `false`면 durable queue가 아님 | worker가 publish할 queue와 선언 속성이 충돌하면 안 됨 |
| `rabbitmq.result_queue_declare.exclusive` | `false` | 현재 connection 전용 queue 여부 | `true`면 앱 재시작/다중 consumer 운영에 제약 발생 | 현재 구조에서는 `false` 권장 |
| `rabbitmq.result_queue_declare.auto_delete` | `false` | consumer가 없어지면 자동 삭제할지 여부 | `true`면 앱 종료 후 결과 queue가 삭제될 수 있음 | 미수신 결과 보존이 필요하면 `false` 유지 |
| `rabbitmq.result_queue_declare.arguments.x-max-priority` | `5` | result queue priority 최대값 | worker가 result priority를 줄 경우 우선순위에 영향 | 현재 앱은 result priority를 해석하지 않음 |
| `rabbitmq.result_queue_declare.arguments.module_group` | `"default"` | queue에 붙이는 커스텀 argument | 운영 분류 용도로 쓸 수 있음 | RabbitMQ 표준 동작 필드는 아님 |

### 5.5 publish

| Key | 현재 값 | 의미 | 변경 시 영향 | 주의사항 |
| --- | --- | --- | --- | --- |
| `publish.default_action` | `"RCP_EXECUTE"` | request payload의 기본 `action` | worker가 수행할 작업 종류가 바뀜 | worker가 이 action 값을 인식해야 함 |
| `publish.default_priority` | `0` | request AMQP priority 기본값 | message priority가 바뀜 | JSON body에는 포함되지 않고 AMQP property로만 전송 |
| `publish.polling_interval_seconds` | `5` | result polling tick/log interval | UI tick 및 timeout 검사 주기에 영향 | consumer 자체는 active 상태로 계속 이벤트를 pump 함 |
| `publish.timeout_seconds` | `86400` | SENT/RUNNING 작업 timeout 기준 | 이 시간 안에 result가 없으면 timeout 처리 | seconds 단위, 현재 24시간 |
| `publish.max_messages_per_poll` | `100` | result consumer prefetch count | 한 번에 broker가 밀어줄 수 있는 미확인 메시지 수에 영향 | 너무 높으면 한 client가 result를 많이 선점할 수 있음 |
| `publish.max_publish_retries` | `3` | request publish 실패 시 재시도 횟수 | publish 실패 복구 가능성이 바뀜 | 1 이상이어야 함 |
| `publish.publish_retry_backoff_seconds` | `1.5` | publish retry backoff 기본 seconds | 재시도 대기 시간이 바뀜 | 실제 대기는 `backoff * attempt` |
| `publish.initial_open_folders` | `2` | 시작 시 동시에 열어 전송할 폴더 batch 수 | 초기 전송량과 worker 부하가 바뀜 | `max_active_open_folders`보다 클 수 없음 |
| `publish.max_active_open_folders` | `3` | 동시에 active 상태로 둘 폴더 수 상한 | 병렬 진행 폴더 수가 바뀜 | 1 이상이어야 함 |
| `publish.image_extensions` | `[".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"]` | 이미지 스캔 대상 확장자 | 등록되는 이미지 파일 종류가 바뀜 | 확장자는 점 포함 문자열로 관리 |
| `publish.scan_mode` | `"direct"` | 폴더 스캔 방식 | `"direct"`는 선택 폴더 직접 이미지, `"recursive"`는 하위까지 스캔 | 지원값은 `direct`, `recursive` |

### 5.6 ui

| Key | 현재 값 | 의미 | 변경 시 영향 | 주의사항 |
| --- | --- | --- | --- | --- |
| `ui.app_name` | `"IPDK_plus"` | Qt application name | 창/앱 표시 이름에 영향 | 설치 패키지 이름과 다르면 사용자 혼동 가능 |
| `ui.window_width` | `1480` | 기본 창 너비 | 최초 창 크기가 바뀜 | px 단위 |
| `ui.window_height` | `900` | 기본 창 높이 | 최초 창 크기가 바뀜 | px 단위 |
| `ui.theme` | `"dark"` | UI theme 이름 | 스타일 선택에 영향 | 현재 스타일 파일 구현과 맞아야 함 |
| `ui.font_family` | `"Segoe UI"` | UI 기본 폰트 | 화면 표시 폰트가 바뀜 | Windows 기본 폰트 기준 |

### 5.7 recipe_config

| Key | 현재 값 | 의미 | 변경 시 영향 | 주의사항 |
| --- | --- | --- | --- | --- |
| `default_alias` | `"Default Recipe"` | 기본 선택 recipe alias | 앱 실행 시 기본 recipe 선택이 바뀜 | `recipes[].alias` 중 하나와 일치해야 기대대로 동작 |
| `recipes[].alias` | `"Default Recipe"`, `"Fast Recipe"`, `"Precision Recipe"` | UI에 표시할 recipe 이름 | 사용자가 선택할 recipe 목록이 바뀜 | 빈 문자열 불가 |
| `recipes[].path` | `"recipes/default_recipe.json"` 등 | worker에 전달할 recipe path | request payload의 `RECIPE_PATH`가 바뀜 | worker가 접근 가능한 경로인지 별도 합의 필요 |

## 6. Request MQ publish 형식

### 6.1 Publish 대상

현재 설정 기준 publish route:

| 항목 | 값 |
| --- | --- |
| AMQP exchange | `""` |
| AMQP routing key | `IPDK_WORKER_INTERFACE` |
| 의미 | RabbitMQ default exchange로 `IPDK_WORKER_INTERFACE` queue에 직접 publish |

중요: 현재 `request_exchange: ""`이므로 실제 routing key는 `request_routing_key: "task.request"`가 아니라 `request_queue: "IPDK_WORKER_INTERFACE"`입니다. `request_routing_key`는 custom exchange를 설정했을 때만 publish routing key로 사용됩니다.

### 6.2 JSON body

앱이 publish하는 request body는 아래 5개 key로 고정됩니다.

```json
{
  "request_id": "uuid-string",
  "action": "RCP_EXECUTE",
  "QUEUE_NAME": "IPDK_WORKER_INTERFACE_RESULT_192.168.0.10",
  "RECIPE_PATH": "recipes/default_recipe.json",
  "IMG_LIST": ["D:/images/sample_001.jpg"]
}
```

| Field | Type | Required | Source | 설명 |
| --- | --- | --- | --- | --- |
| `request_id` | `string` | yes | 앱이 `uuid4()`로 생성 | request/result 매칭 기준 ID |
| `action` | `string` | yes | UI runtime setting 또는 `publish.default_action` | worker가 수행할 동작 |
| `QUEUE_NAME` | `string` | yes | 앱이 결정한 result queue name | worker가 결과를 publish해야 하는 queue |
| `RECIPE_PATH` | `string` | yes | 선택 recipe 또는 `recipe_config.default_alias`의 path | worker가 사용할 recipe path |
| `IMG_LIST` | `string[]` | yes | 등록된 이미지 경로 | 현재 앱은 메시지 1건당 이미지 1개만 넣음 |

`priority`, `sent_at`, `timeout_seconds` 등은 JSON body에 포함되지 않습니다.

### 6.3 AMQP BasicProperties

앱은 request publish 시 아래 AMQP properties를 같이 설정합니다.

| Property | 값 | 설명 |
| --- | --- | --- |
| `message_id` | `request_id` | broker/UI 추적용 message id |
| `correlation_id` | `request_id` | result fallback 매칭용 correlation id |
| `reply_to` | `QUEUE_NAME` | worker가 결과를 보낼 queue |
| `content_type` | `"application/json"` | request body format |
| `priority` | `TaskMessage.priority` | UI/config에서 선택한 우선순위 |
| `delivery_mode` | `2` | persistent message |
| `timestamp` | publish 시각 epoch seconds | broker message timestamp |

## 7. Result MQ consume 형식

### 7.1 Worker가 보내야 하는 대상 queue

worker는 request body의 `QUEUE_NAME` 또는 AMQP `reply_to`를 결과 publish 대상 queue로 사용해야 합니다.

현재 설정에서 result queue 이름 예시:

```text
IPDK_WORKER_INTERFACE_RESULT_192.168.0.10
```

IPv4 suffix는 앱 실행 PC가 RabbitMQ host/port로 통신할 때 OS가 선택한 대표 local IPv4입니다. 즉 같은 서버를 쓰더라도 앱 실행 PC가 다르면 result queue 이름이 달라질 수 있습니다.

### 7.2 권장 JSON body

```json
{
  "request_id": "same-request-id",
  "result": ["PASS", "optional_detail"],
  "status": "DONE",
  "error": null,
  "completed_at": "2026-05-06T10:30:00+09:00"
}
```

| Field | Type | Required | 앱 처리 규칙 |
| --- | --- | --- | --- |
| `request_id` | `string` | recommended | payload에 없으면 AMQP `correlation_id`, 그 다음 `message_id`로 fallback |
| `result` | `string[]` 또는 `string` | recommended | list면 문자열 list로 변환, 문자열이면 단일 원소 list로 변환 |
| `status` | `string` | optional | UI forensic preview와 내부 result 객체에 보관 |
| `error` | `string` 또는 `null` | optional | `null` 또는 `""`이면 error 없음 |
| `completed_at` | `string` | optional | 있으면 완료 시각으로 파싱 시도 |

성공 판정은 `status`가 아니라 `result` 기준입니다. `result` 배열 안에 대소문자 무관 `PASS`가 하나라도 있으면 성공으로 처리합니다. 그 외에는 실패로 처리합니다.

### 7.3 Result AMQP metadata 권장값

worker가 result payload에 `request_id`를 넣는 것이 가장 명확합니다. 추가로 아래 properties를 맞추면 payload 누락 시에도 앱이 fallback 매칭할 수 있습니다.

| Property | 권장값 | 설명 |
| --- | --- | --- |
| `message_id` | 원 request의 `request_id` | payload `request_id` 누락 시 fallback |
| `correlation_id` | 원 request의 `request_id` | payload `request_id` 누락 시 우선 fallback |
| `content_type` | `"application/json"` | result body format |

## 8. Routing과 queue naming 규칙

### 8.1 Request publish route

앱의 publish route 결정 규칙:

| 조건 | exchange | routing key |
| --- | --- | --- |
| `request_exchange == ""` | `""` | `request_queue` |
| `request_exchange != ""` and `request_routing_key != ""` | `request_exchange` | `request_routing_key` |
| `request_exchange != ""` and `request_routing_key == ""` | `request_exchange` | `request_queue` |

현재 설정:

```text
request_exchange = ""
request_routing_key = "task.request"
request_queue = "IPDK_WORKER_INTERFACE"
```

따라서 현재 실제 publish target은 아래와 같습니다.

```text
exchange = ""
routing_key = "IPDK_WORKER_INTERFACE"
```

### 8.2 Result queue name

앱은 아래 규칙으로 결과 queue 이름을 만듭니다.

```text
{rabbitmq.result_queue_base}_{local_ipv4}
```

현재 설정 예시:

```text
IPDK_WORKER_INTERFACE_RESULT_192.168.0.10
```

`local_ipv4` 결정 우선순위:

1. RabbitMQ `host:port`로 outbound routing할 때 OS가 선택한 local IPv4
2. hostname lookup에서 찾은 첫 번째 non-loopback IPv4

유효한 IPv4를 찾지 못하면 앱은 result queue를 결정하지 못해 전송을 시작하지 않습니다.

## 9. Consume, ACK, 무시, 중복 처리 정책

### 9.1 Consume 대상

앱은 실행 중 결정된 result queue를 `basic_consume(auto_ack=False)`로 consume 합니다. prefetch count는 `publish.max_messages_per_poll` 값을 사용합니다.

현재 설정:

```text
result queue = IPDK_WORKER_INTERFACE_RESULT_{실행PC IPv4}
prefetch_count = 100
```

### 9.2 ACK 정책

| 상황 | Broker 처리 | 앱 상태 반영 |
| --- | --- | --- |
| `request_id`를 찾을 수 없음 | ACK | 무시, 로그 기록 |
| `request_id`가 현재 세션 tracked id가 아님 | ACK | 무시, 로그 기록 |
| tracked `request_id` 결과 수신 | ACK | result parser 후 상태 반영 |
| 같은 `request_id` 결과가 중복 수신 | ACK | 첫 처리 이후 중복은 상태 변경 없음 |

현재 polling worker는 mismatch 결과도 ACK합니다. 따라서 worker가 잘못된 result queue로 결과를 보내면 메시지가 broker에서 사라지고 앱에는 반영되지 않습니다.

### 9.3 상태 반영 규칙

| Result 내용 | 앱 상태 |
| --- | --- |
| `result`에 `PASS` 포함 | `SUCCESS` |
| `result`에 `PASS` 없음 | `FAIL` |
| publish 실패 | `ERROR` |
| `timeout_seconds` 초과 | `TIMEOUT` |

`status: "DONE"`만으로 성공 처리되지 않습니다. 성공/실패는 반드시 `result` 안의 `PASS` 포함 여부로 결정됩니다.

## 10. 협업자 구현 체크리스트

worker 구현 시 반드시 맞출 항목:

- request queue는 현재 설정 기준 `IPDK_WORKER_INTERFACE`를 consume한다.
- request body는 UTF-8 JSON으로 파싱한다.
- `request_id`를 결과에 그대로 돌려준다.
- 결과는 request body의 `QUEUE_NAME` 또는 AMQP `reply_to` queue로 publish한다.
- result body에는 가능한 한 `request_id`, `result`, `status`, `error`, `completed_at`를 포함한다.
- 성공 결과는 `result` 배열 안에 `"PASS"`를 포함한다.
- 실패 결과는 `result` 배열에 `"PASS"`를 넣지 않고, 필요하면 `error`에 상세 원인을 넣는다.
- result AMQP `correlation_id`와 `message_id`는 원 request의 `request_id`로 맞춘다.
- `RECIPE_PATH`는 앱이 보낸 문자열 그대로 해석한다. worker 실행 환경에서 접근 가능한 경로인지 양쪽이 배포 전에 합의해야 한다.
- `IMG_LIST`는 배열이지만 현재는 1개 이미지 경로만 들어온다고 가정하고 처리해도 된다.

주의할 항목:

- 현재 설정에서는 `request_routing_key: "task.request"`가 실제 request routing key가 아니다.
- result queue 이름은 고정 문자열이 아니라 `IPDK_WORKER_INTERFACE_RESULT_{실행PC IPv4}` 형식이다.
- mismatch `request_id` result는 앱이 ACK 후 무시하므로 재처리되지 않는다.
- RabbitMQ queue argument는 이미 생성된 queue와 다르게 선언하면 broker가 오류를 낼 수 있다.
- JSON body에 `priority`를 넣어도 앱은 사용하지 않는다. request priority는 AMQP property로 전달된다.

## 11. 최소 연동 예시

### 11.1 Worker가 받는 request 예시

```json
{
  "request_id": "3dc7831b-7c4b-45f1-b5cb-f00e6952f6d5",
  "action": "RCP_EXECUTE",
  "QUEUE_NAME": "IPDK_WORKER_INTERFACE_RESULT_192.168.0.10",
  "RECIPE_PATH": "recipes/default_recipe.json",
  "IMG_LIST": ["D:/data/folder_a/img001.jpg"]
}
```

AMQP metadata 예시:

```json
{
  "exchange": "",
  "routing_key": "IPDK_WORKER_INTERFACE",
  "message_id": "3dc7831b-7c4b-45f1-b5cb-f00e6952f6d5",
  "correlation_id": "3dc7831b-7c4b-45f1-b5cb-f00e6952f6d5",
  "reply_to": "IPDK_WORKER_INTERFACE_RESULT_192.168.0.10",
  "content_type": "application/json",
  "priority": 0,
  "delivery_mode": 2
}
```

### 11.2 Worker가 보내는 성공 result 예시

```json
{
  "request_id": "3dc7831b-7c4b-45f1-b5cb-f00e6952f6d5",
  "result": ["PASS", "recipe_completed"],
  "status": "DONE",
  "error": null,
  "completed_at": "2026-05-06T10:30:00+09:00"
}
```

### 11.3 Worker가 보내는 실패 result 예시

```json
{
  "request_id": "3dc7831b-7c4b-45f1-b5cb-f00e6952f6d5",
  "result": ["FAIL", "measurement_out_of_range"],
  "status": "FAILED",
  "error": "Measurement exceeded configured threshold",
  "completed_at": "2026-05-06T10:31:12+09:00"
}
```

실패 result에 `"PASS"`를 포함하면 앱은 성공으로 판정합니다. 실패 상세는 `result`의 부가 문자열과 `error`에 넣어야 합니다.

## 12. Worker 구현 예시

이 섹션의 코드는 worker 쪽에서 request queue를 consume하고, 처리 결과를 result queue로 publish하는 최소 예시입니다.

공통 전제:

- consume queue는 현재 설정 기준 `IPDK_WORKER_INTERFACE`입니다.
- result publish target은 AMQP `reply_to`를 우선 사용하고, 없으면 request body의 `QUEUE_NAME`을 사용합니다.
- result publish metadata는 `message_id=request_id`, `correlation_id=request_id`, `content_type=application/json`, `delivery_mode=2`로 맞춥니다.
- 처리 성공 후 request message를 ACK합니다.
- 처리 중 예외가 나면 request message를 NACK하고 requeue합니다.

### 12.1 Python worker 예시: pika

설치:

```powershell
pip install "pika>=1.3.2"
```

예시 코드:

```python
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import pika


RABBITMQ_HOST = "127.0.0.1"
RABBITMQ_PORT = 5672
RABBITMQ_USERNAME = "young"
RABBITMQ_PASSWORD = "young"
RABBITMQ_VHOST = "/"

REQUEST_QUEUE = "IPDK_WORKER_INTERFACE"


def process_image(action: str, recipe_path: str, image_path: str) -> tuple[bool, list[str], str | None]:
    """Replace this function with real IPDK/recipe execution logic."""

    print(f"action={action}, recipe={recipe_path}, image={image_path}")
    return True, ["PASS", "recipe_completed"], None


def build_result_payload(request_id: str, ok: bool, result: list[str], error: str | None) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "result": result if ok else [item for item in result if item.upper() != "PASS"] or ["FAIL"],
        "status": "DONE" if ok else "FAILED",
        "error": None if ok else (error or "Worker processing failed"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def on_request(channel, method, properties, body: bytes) -> None:  # noqa: ANN001
    try:
        request = json.loads(body.decode("utf-8"))

        request_id = str(request["request_id"])
        action = str(request["action"])
        recipe_path = str(request["RECIPE_PATH"])
        image_list = request.get("IMG_LIST") or []
        if not image_list:
            raise ValueError("IMG_LIST is empty")

        image_path = str(image_list[0])
        result_queue = str(getattr(properties, "reply_to", "") or request["QUEUE_NAME"])

        ok, result, error = process_image(action, recipe_path, image_path)
        result_payload = build_result_payload(request_id, ok, result, error)
        result_body = json.dumps(result_payload, ensure_ascii=False).encode("utf-8")

        result_properties = pika.BasicProperties(
            message_id=request_id,
            correlation_id=request_id,
            content_type="application/json",
            delivery_mode=2,
        )

        channel.queue_declare(
            queue=result_queue,
            durable=True,
            exclusive=False,
            auto_delete=False,
            arguments={"x-max-priority": 5, "module_group": "default"},
        )
        channel.basic_publish(
            exchange="",
            routing_key=result_queue,
            body=result_body,
            properties=result_properties,
            mandatory=False,
        )
        channel.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as exc:
        print(f"request failed; requeue delivery_tag={method.delivery_tag}: {exc}")
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)


def main() -> None:
    credentials = pika.PlainCredentials(RABBITMQ_USERNAME, RABBITMQ_PASSWORD)
    parameters = pika.ConnectionParameters(
        host=RABBITMQ_HOST,
        port=RABBITMQ_PORT,
        virtual_host=RABBITMQ_VHOST,
        heartbeat=30,
        blocked_connection_timeout=30,
        connection_attempts=3,
        retry_delay=2.0,
        credentials=credentials,
    )

    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    channel.queue_declare(
        queue=REQUEST_QUEUE,
        durable=True,
        exclusive=False,
        auto_delete=False,
        arguments={"x-max-priority": 5, "module_group": "IPDK_WORKER"},
    )
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue=REQUEST_QUEUE, on_message_callback=on_request, auto_ack=False)

    try:
        print(f"Consuming request queue: {REQUEST_QUEUE}")
        channel.start_consuming()
    finally:
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    main()
```

Python 예시에서 중요한 지점:

- `channel.basic_consume(..., auto_ack=False)`로 request 처리 성공 전에는 ACK하지 않습니다.
- `properties.reply_to`가 있으면 이 값을 결과 queue로 사용합니다.
- result queue는 앱의 `rabbitmq.result_queue_declare`와 같은 옵션으로 선언합니다.
- result publish는 default exchange(`exchange=""`) + result queue name을 routing key로 사용합니다.
- 실패 result에는 `"PASS"`를 넣지 않아야 앱이 실패로 판정합니다.

### 12.2 Java worker 예시: RabbitMQ Java Client

Maven dependency:

```xml
<dependency>
  <groupId>com.rabbitmq</groupId>
  <artifactId>amqp-client</artifactId>
  <version>5.30.0</version>
</dependency>
<dependency>
  <groupId>com.fasterxml.jackson.core</groupId>
  <artifactId>jackson-databind</artifactId>
  <version>2.21.2</version>
</dependency>
```

Gradle dependency:

```gradle
implementation("com.rabbitmq:amqp-client:5.30.0")
implementation("com.fasterxml.jackson.core:jackson-databind:2.21.2")
```

예시 코드:

```java
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.rabbitmq.client.AMQP;
import com.rabbitmq.client.Channel;
import com.rabbitmq.client.Connection;
import com.rabbitmq.client.ConnectionFactory;
import com.rabbitmq.client.DeliverCallback;
import com.rabbitmq.client.Delivery;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class IpdKWorkerExample {
    private static final String RABBITMQ_HOST = "127.0.0.1";
    private static final int RABBITMQ_PORT = 5672;
    private static final String RABBITMQ_USERNAME = "young";
    private static final String RABBITMQ_PASSWORD = "young";
    private static final String RABBITMQ_VHOST = "/";
    private static final String REQUEST_QUEUE = "IPDK_WORKER_INTERFACE";

    private static final ObjectMapper OBJECT_MAPPER = new ObjectMapper();

    public static void main(String[] args) throws Exception {
        ConnectionFactory factory = new ConnectionFactory();
        factory.setHost(RABBITMQ_HOST);
        factory.setPort(RABBITMQ_PORT);
        factory.setUsername(RABBITMQ_USERNAME);
        factory.setPassword(RABBITMQ_PASSWORD);
        factory.setVirtualHost(RABBITMQ_VHOST);
        factory.setRequestedHeartbeat(30);
        factory.setConnectionTimeout(30_000);

        Connection connection = factory.newConnection();
        Channel channel = connection.createChannel();

        Map<String, Object> queueArguments = new HashMap<>();
        queueArguments.put("x-max-priority", 5);
        queueArguments.put("module_group", "IPDK_WORKER");
        channel.queueDeclare(REQUEST_QUEUE, true, false, false, queueArguments);
        channel.basicQos(1);

        DeliverCallback callback = (consumerTag, delivery) -> handleRequest(channel, delivery);
        channel.basicConsume(REQUEST_QUEUE, false, callback, consumerTag -> {
        });

        System.out.println("Consuming request queue: " + REQUEST_QUEUE);
    }

    private static void handleRequest(Channel channel, Delivery delivery) throws IOException {
        long deliveryTag = delivery.getEnvelope().getDeliveryTag();

        try {
            String body = new String(delivery.getBody(), StandardCharsets.UTF_8);
            Map<String, Object> request = OBJECT_MAPPER.readValue(
                    body,
                    new TypeReference<Map<String, Object>>() {
                    }
            );

            String requestId = requiredString(request, "request_id");
            String action = requiredString(request, "action");
            String recipePath = requiredString(request, "RECIPE_PATH");
            String resultQueue = firstNonBlank(
                    delivery.getProperties().getReplyTo(),
                    requiredString(request, "QUEUE_NAME")
            );

            List<?> imageList = (List<?>) request.get("IMG_LIST");
            if (imageList == null || imageList.isEmpty()) {
                throw new IllegalArgumentException("IMG_LIST is empty");
            }
            String imagePath = String.valueOf(imageList.get(0));

            WorkerResult workerResult = processImage(action, recipePath, imagePath);
            Map<String, Object> resultPayload = buildResultPayload(requestId, workerResult);
            byte[] resultBody = OBJECT_MAPPER.writeValueAsBytes(resultPayload);

            AMQP.BasicProperties resultProperties = new AMQP.BasicProperties.Builder()
                    .messageId(requestId)
                    .correlationId(requestId)
                    .contentType("application/json")
                    .deliveryMode(2)
                    .build();

            Map<String, Object> resultQueueArguments = new HashMap<>();
            resultQueueArguments.put("x-max-priority", 5);
            resultQueueArguments.put("module_group", "default");
            channel.queueDeclare(resultQueue, true, false, false, resultQueueArguments);
            channel.basicPublish("", resultQueue, resultProperties, resultBody);
            channel.basicAck(deliveryTag, false);
        } catch (Exception exc) {
            System.err.println("request failed; requeue delivery_tag=" + deliveryTag + ": " + exc.getMessage());
            channel.basicNack(deliveryTag, false, true);
        }
    }

    private static WorkerResult processImage(String action, String recipePath, String imagePath) {
        System.out.printf("action=%s, recipe=%s, image=%s%n", action, recipePath, imagePath);
        return new WorkerResult(true, List.of("PASS", "recipe_completed"), null);
    }

    private static Map<String, Object> buildResultPayload(String requestId, WorkerResult workerResult) {
        List<String> result = workerResult.ok()
                ? workerResult.result()
                : workerResult.result().stream()
                .filter(item -> !"PASS".equalsIgnoreCase(item))
                .toList();

        if (!workerResult.ok() && result.isEmpty()) {
            result = List.of("FAIL");
        }

        Map<String, Object> payload = new HashMap<>();
        payload.put("request_id", requestId);
        payload.put("result", result);
        payload.put("status", workerResult.ok() ? "DONE" : "FAILED");
        payload.put("error", workerResult.ok() ? null : firstNonBlank(workerResult.error(), "Worker processing failed"));
        payload.put("completed_at", OffsetDateTime.now().toString());
        return payload;
    }

    private static String requiredString(Map<String, Object> payload, String key) {
        Object value = payload.get(key);
        if (value == null || String.valueOf(value).isBlank()) {
            throw new IllegalArgumentException(key + " is required");
        }
        return String.valueOf(value);
    }

    private static String firstNonBlank(String first, String fallback) {
        if (first != null && !first.isBlank()) {
            return first;
        }
        return fallback;
    }

    private record WorkerResult(boolean ok, List<String> result, String error) {
    }
}
```

Java 예시에서 중요한 지점:

- `channel.basicConsume(REQUEST_QUEUE, false, ...)`로 manual ACK를 사용합니다.
- `delivery.getProperties().getReplyTo()`가 있으면 이 값을 결과 queue로 사용합니다.
- result queue는 앱의 `rabbitmq.result_queue_declare`와 같은 옵션으로 선언합니다.
- result publish는 default exchange(`""`) + result queue name을 routing key로 사용합니다.
- `AMQP.BasicProperties.Builder`에서 `messageId`, `correlationId`, `contentType`, `deliveryMode`를 명시합니다.
- 예시 코드는 Java 17 이상 기준입니다. 낮은 Java 버전이면 `record`와 `.toList()`를 일반 class와 collector로 바꾸면 됩니다.
