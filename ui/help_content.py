"""Structured in-app help content for IPDK_plus."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HelpTopic:
    """One searchable help topic."""

    title: str
    keywords: tuple[str, ...]
    body: str


HELP_TOPICS: tuple[HelpTopic, ...] = (
    HelpTopic(
        title="프로그램 개요",
        keywords=("IPDK_plus", "RabbitMQ", "request_id", "결과 큐", "진행률"),
        body="""IPDK_plus는 이미지 폴더를 등록하고, 이미지 1장당 RabbitMQ 요청 메시지 1건을 전송한 뒤, 전용 결과 큐에서 worker 응답을 받아 상태를 추적하는 데스크톱 GUI 도구입니다.

전체 흐름은 사용자가 폴더를 선택하는 것에서 시작합니다. 앱은 선택된 폴더에서 설정된 확장자의 이미지 파일을 찾고, 각 이미지에 내부 request_id를 부여합니다. 전송을 시작하면 앱은 먼저 이 PC에서 사용할 결과 큐 이름을 결정하고, 그 큐를 RabbitMQ에 준비한 뒤, request queue로 작업 메시지를 발행합니다.

worker는 request queue에서 메시지를 가져가 작업을 수행하고, 메시지 안의 QUEUE_NAME 또는 AMQP reply_to에 적힌 결과 큐로 결과를 돌려보내야 합니다. 앱은 결과 메시지의 request_id를 현재 세션의 작업과 비교하여 SUCCESS, FAIL, TIMEOUT, ERROR 같은 상태로 반영하고, 폴더 단위 진행률과 이미지 단위 상세 상태를 갱신합니다.

이 프로그램은 작업 실행 엔진이 아니라 작업 요청/상태 추적 클라이언트입니다. 실제 이미지 처리와 recipe 실행은 RabbitMQ 뒤쪽의 worker가 담당합니다. 따라서 앱 설정, worker 설정, RabbitMQ queue/exchange 설정, recipe 경로 규칙이 서로 맞아야 정상적으로 끝까지 동작합니다.""",
    ),
    HelpTopic(
        title="기본 사용 흐름",
        keywords=("폴더 추가", "sub_folder", "Recipe", "Priority", "전송 시작", "중지", "초기화"),
        body="""1. 왼쪽 폴더 탐색기에서 작업할 폴더를 선택합니다. 깊은 계층을 선택하거나 경로로 이동하면 선택 노드의 들여쓰기와 이름이 가로 화면 중앙에 오도록 자동 정렬됩니다.

2. 폴더 추가를 누르면 선택한 폴더 자체를 작업 대상으로 등록합니다. 설정의 scan_mode가 direct이면 선택 폴더 바로 아래의 이미지만 대상으로 삼고, recursive이면 하위 폴더까지 탐색합니다.

3. sub_folder 추가를 누르면 선택한 폴더 아래에서 이미지가 들어 있는 하위 폴더들을 작업 단위로 등록합니다. 여러 하위 폴더를 폴더 단위 진행 현황에서 따로 추적하려는 경우에 사용합니다.

4. Recipe는 worker에 전달할 RECIPE_PATH입니다. Recipe 선택 메뉴에서 하나 이상을 체크할 수 있고, 선택 경로 영역에는 Recipe별 alias와 path가 각각 표시됩니다. 폴더 추가 시점의 선택이 이미지별 작업에 저장되며, 이미지 N개와 Recipe M개는 N×M개의 고유 request로 등록됩니다. 로컬에서 recipe 파일이 보이지 않아도 payload에는 설정 문자열이 그대로 전송될 수 있으므로, worker가 접근할 수 있는 경로인지 확인해야 합니다.

5. Priority는 RabbitMQ AMQP BasicProperties.priority로 전달됩니다. JSON payload 안에는 priority 필드가 들어가지 않습니다. 선택 가능한 범위는 request queue의 x-max-priority 설정을 기준으로 만들어집니다.

6. 전송 시작을 누르면 initial_open_folders만큼의 폴더만 백그라운드에서 분할 스캔하고, max_active_open_folders 범위에서 다음 폴더를 순차 개방합니다. 열리지 않은 폴더의 이미지와 메시지는 메모리에 만들지 않습니다. 작업은 SQLite에 저장되고 publish_chunk_size 단위로만 메시지를 생성하며, queue와 처리 중 작업이 자동 상한에 도달하면 발행을 멈췄다가 여유가 생기면 재개합니다. 각 메시지의 RECIPE_PATH는 폴더 추가 시 저장된 Recipe를 사용합니다.

7. 중지는 publish/poll worker를 정지시키는 동작입니다. 이미 broker에 발행된 메시지를 worker나 RabbitMQ에서 회수하는 기능은 아닙니다.

8. 초기화는 앱 내부 작업 상태를 비우고 화면을 초기 상태로 되돌립니다. 이미 발행된 MQ 메시지나 worker가 처리 중인 외부 작업까지 취소한다고 가정하면 안 됩니다.

9. 가운데 표의 진행 중/대기 폴더와 완료된 폴더를 선택하면 오른쪽 상세 상태 탭에서 해당 폴더의 이미지 작업을 확인할 수 있습니다. MQ 버튼은 선택 이미지의 예상 payload, 실제 publish payload, 수신 payload를 비교하는 용도입니다.""",
    ),
    HelpTopic(
        title="폴더 스캔 방식",
        keywords=("scan_mode", "direct", "recursive", "image_extensions", "이미지 확장자", "빈 폴더"),
        body="""폴더 스캔은 publish.image_extensions와 publish.scan_mode 설정을 따릅니다.

image_extensions에는 등록 대상 이미지 확장자가 들어갑니다. 기본값은 .jpg, .jpeg, .png, .bmp, .tif, .tiff입니다. 확장자는 점을 포함한 문자열로 관리하며, 이 목록에 없는 파일은 작업으로 등록되지 않습니다.

scan_mode가 direct이면 선택한 폴더 바로 아래의 이미지 파일만 찾습니다. 하위 폴더 안의 이미지는 자동으로 포함되지 않습니다. 한 폴더를 하나의 작업 묶음으로 다루고 싶을 때 적합합니다.

scan_mode가 recursive이면 선택한 폴더 아래의 하위 폴더까지 탐색합니다. 깊은 폴더 구조를 한 번에 등록할 수 있지만, 예상보다 많은 이미지가 등록될 수 있으므로 대량 처리 전에 대상 폴더를 확인하는 것이 좋습니다.

폴더 단위 진행률은 SQLite에 증감식으로 저장된 상태 카운터를 사용하므로 전체 작업을 다시 읽지 않습니다. 이미지가 없는 폴더는 이미지 없음 상태로 표시됩니다. 같은 세션의 동일한 이미지 경로와 Recipe 경로 조합은 중복 등록되지 않습니다.""",
    ),
    HelpTopic(
        title="RabbitMQ 연동 구조",
        keywords=("request_queue", "result_queue", "request_exchange", "routing_key", "result_queue_base", "IPv4"),
        body="""앱과 worker는 RabbitMQ를 통해 request queue와 result queue를 나누어 사용합니다.

request_queue는 앱이 작업 요청 메시지를 publish하는 대상입니다. 현재 기본 설정에서는 request_exchange가 빈 문자열이므로 RabbitMQ default exchange를 사용하고, 실제 routing key는 request_routing_key가 아니라 request_queue 값입니다.

request_exchange를 비워 두지 않으면 custom exchange 모드가 됩니다. 이때 request_routing_key가 있으면 그 값을 사용하고, 비어 있으면 request_queue를 fallback routing key로 사용합니다. worker와 broker binding 설정이 이 규칙과 맞아야 메시지가 도착합니다.

result_queue_base는 앱이 결과를 받을 queue 이름의 접두어입니다. 실제 result queue는 {result_queue_base}_{local_ipv4} 형식으로 만들어집니다. local_ipv4는 RabbitMQ host/port로 통신할 때 OS가 선택한 대표 IPv4를 우선 사용하고, 실패하면 hostname lookup의 non-loopback IPv4를 사용합니다.

Worker Count는 result queue에 붙은 consumer 수를 의미하고, Queued Messages는 result queue에 대기 중인 메시지 수를 나타냅니다. 이 값이 - 또는 조회 실패 상태라면 RabbitMQ 연결, queue 권한, broker 상태를 확인해야 합니다.

RabbitMQ 연결 실패 시에는 host, port, username, password, virtual_host, 방화벽, broker 실행 여부, queue declare 옵션 충돌을 순서대로 확인하세요. 이미 생성된 RabbitMQ queue와 durable/exclusive/auto_delete/arguments 값이 다르면 broker가 precondition failed 오류를 낼 수 있습니다.""",
    ),
    HelpTopic(
        title="메시지 전송 방식",
        keywords=("request_id", "QUEUE_NAME", "RECIPE_PATH", "IMG_LIST", "BasicProperties", "priority"),
        body="""앱이 발행하는 request JSON body는 request_id, action, QUEUE_NAME, RECIPE_PATH, IMG_LIST 5개 필드를 중심으로 고정됩니다.

request_id는 앱이 이미지 작업마다 생성하는 고유 ID이며, 결과 매칭의 기준입니다. worker는 결과 payload나 AMQP metadata에 같은 request_id를 돌려줘야 합니다.

action은 publish.default_action 또는 화면 runtime 설정에서 결정됩니다. worker가 이해하는 action 값이어야 합니다.

QUEUE_NAME은 worker가 결과를 publish해야 하는 result queue 이름입니다. 앱은 이 값을 AMQP reply_to에도 넣습니다.

RECIPE_PATH는 선택한 recipe alias에 연결된 path입니다. 앱은 이 문자열을 그대로 전송하며, worker 실행 환경에서 접근 가능한지까지 보장하지 않습니다.

IMG_LIST는 배열이지만 현재 앱은 메시지 1건당 이미지 경로 1개만 넣습니다. worker는 배열을 처리하되, 현재 운영에서는 길이 1을 기대해도 됩니다.

AMQP BasicProperties에는 message_id=request_id, correlation_id=request_id, reply_to=QUEUE_NAME, content_type=application/json, delivery_mode=2, timestamp, priority가 들어갑니다. priority는 JSON body가 아니라 AMQP property로 전달됩니다.

request queue의 x-max-priority보다 큰 priority는 설정 검증 단계에서 막아야 합니다. RabbitMQ priority queue는 queue 생성 시 argument가 고정되므로, 운영 중 기존 queue의 priority argument를 바꾸려면 broker 쪽 queue 재생성 정책까지 함께 검토해야 합니다.""",
    ),
    HelpTopic(
        title="결과 수신 및 상태 갱신",
        keywords=("PENDING", "SENT", "RUNNING", "SUCCESS", "FAIL", "TIMEOUT", "ERROR", "CANCELLED", "PASS"),
        body="""이미지 작업 상태는 PENDING, SENT, RUNNING, SUCCESS, FAIL, TIMEOUT, ERROR, CANCELLED로 관리됩니다.

PENDING은 아직 publish되지 않은 대기 상태입니다. SENT는 request message가 broker로 발행된 상태입니다. RUNNING은 앱이 결과 대기 중인 작업을 진행 중으로 반영한 상태입니다.

SUCCESS는 result 배열 안에 대소문자와 무관하게 PASS가 포함된 경우입니다. FAIL은 결과는 왔지만 PASS가 없는 경우입니다. result payload의 status 값이 DONE이어도 PASS가 없으면 성공으로 처리되지 않습니다.

TIMEOUT은 SENT 또는 RUNNING 작업이 publish.timeout_seconds를 초과했을 때 표시됩니다. 기본 설정은 86400초, 즉 24시간입니다.

ERROR는 publish 실패나 처리 중 오류가 앱 내부 상태로 반영된 경우입니다. CANCELLED는 삭제 또는 취소 정책으로 terminal 상태가 된 작업에 사용됩니다.

결과 메시지는 payload의 request_id를 우선 사용하고, 없으면 AMQP correlation_id, 그 다음 message_id로 fallback 매칭합니다. 현재 세션에 없는 request_id이거나 request_id를 찾을 수 없는 결과는 ACK 처리 후 로그만 남기고 무시됩니다. 즉 잘못된 result queue로 들어온 메시지는 재처리되지 않을 수 있습니다.

같은 request_id의 결과가 중복 수신되면 첫 처리 이후 중복 결과는 상태를 다시 바꾸지 않습니다. 문제 분석이 필요하면 이미지 상세의 MQ 미리보기에서 expected, published, received payload와 metadata를 비교하세요.""",
    ),
    HelpTopic(
        title="진행률과 통계",
        keywords=("진행률", "ETA", "Avg Time", "완료", "성공", "실패", "타임아웃"),
        body="""전체 진행률은 전체 이미지 작업 수 대비 terminal 상태 작업 수를 기준으로 계산됩니다. terminal 상태에는 SUCCESS, FAIL, TIMEOUT, ERROR, CANCELLED가 포함됩니다.

폴더 단위 진행률은 해당 폴더에 묶인 이미지 작업 중 완료된 작업 수로 계산합니다. 폴더 안의 모든 작업이 terminal 상태가 되면 완료된 폴더 목록으로 이동합니다.

성공 카운트는 SUCCESS 작업 수, 실패 카운트는 FAIL 작업 수, 타임아웃 카운트는 TIMEOUT 작업 수, 에러 카운트는 ERROR 작업 수입니다. 실패와 에러는 원인이 다르므로 로그와 MQ payload를 같이 확인해야 합니다.

Avg Time/Image는 완료된 작업과 경과 시간을 바탕으로 계산되는 평균 처리 시간입니다. ETA는 남은 작업 수와 평균 처리 시간을 이용한 추정치입니다.

완료된 작업 수가 0이거나 경과 시간이 유효하지 않으면 평균 시간과 ETA가 표시되지 않을 수 있습니다. 작업 초반에는 표본이 적기 때문에 ETA가 크게 흔들릴 수 있고, worker 처리 시간이 이미지마다 다르면 실제 완료 시각과 차이가 날 수 있습니다.

대량 이미지 처리 중 Queued Messages가 계속 증가하고 완료 카운트가 늘지 않으면 worker 수, worker 로그, request queue 소비 여부, result queue publish 여부를 함께 확인하세요.""",
    ),
    HelpTopic(
        title="설정 파일",
        keywords=("app_config.yaml", "recipe_config.yaml", "AppData", "seed", "fingerprint", "mock_mode", "update"),
        body="""앱 설정은 app_config.yaml과 recipe_config.yaml을 중심으로 관리됩니다.

설정 탐색 우선순위는 CLI --config 경로, 호환용 positional config 경로, %APPDATA%\\IPDK_plus\\app_config.yaml, 설치 패키지 seed, 실행파일 옆 config, 개발 repo 기본값 순서입니다. 설치형 실행에서는 기본 편집 대상이 %APPDATA%\\IPDK_plus\\app_config.yaml입니다.

첫 실행 시 AppData 설정이 없으면 번들된 app_config.yaml과 recipe_config.yaml seed를 복사합니다. 새 설치본의 seed fingerprint가 기존 .seed_fingerprint와 다르면 기존 AppData 설정은 .bak.<timestamp>로 백업되고 새 seed가 반영됩니다. 언인스톨은 AppData 설정을 삭제하지 않습니다.

mock_mode가 true이면 실제 RabbitMQ 대신 내부 mock broker를 사용합니다. 실제 worker 연동 검증에서는 false로 두고 rabbitmq 섹션을 맞춰야 합니다.

rabbitmq 섹션은 host, port, username, password, virtual_host, request queue, result queue, queue_declare 옵션을 정의합니다. worker와 같은 vhost, queue, exchange/routing 규칙을 사용해야 합니다.

publish 섹션은 default_action, default_priority, polling_interval_seconds, timeout_seconds, max_messages_per_poll, retry 정책, 폴더 개방 정책, publish_chunk_size, queue 자동 상한, UI 갱신/로그 상한, image_extensions, scan_mode를 정의합니다. 대용량 기본값은 앱에 내장되어 있어 사용자가 배치를 수동 분리할 필요가 없습니다.

recipe_config_path는 별도 recipe 설정 파일 경로입니다. 상대 경로는 app_config.yaml이 있는 폴더 기준으로 해석됩니다. recipe_config.yaml에는 default_alias와 recipes[].alias/path 목록을 둡니다.

ui 섹션은 앱 이름, 기본 창 크기, theme, font_family를 담습니다. 현재 스타일은 ui/styles.qss를 통해 적용됩니다.

update 섹션의 latest_release_url은 앱 메뉴의 업데이트 확인 링크로 사용됩니다. manifest_url은 향후 자동 비교용 manifest 위치이지만, 현재 앱은 자동 다운로드나 자동 설치를 수행하지 않습니다.""",
    ),
    HelpTopic(
        title="업데이트 확인",
        keywords=("업데이트", "latest_release_url", "manifest_url", "GitHub Releases", "자동 설치"),
        body="""현재 IPDK_plus의 업데이트 확인은 링크 기반입니다. 앱 내부의 도움말 > 업데이트 확인 메뉴는 config/app_config.yaml의 update.latest_release_url을 기본 브라우저로 엽니다.

기본 URL은 GitHub Releases의 latest 페이지입니다. 사용자는 그 페이지에서 최신 설치 파일을 내려받아 설치해야 합니다. 앱이 백그라운드에서 설치 파일을 자동 다운로드하거나 자동으로 설치 프로그램을 실행하지 않습니다.

update.enabled가 false이면 앱 내부 업데이트 확인 action은 비활성화됩니다. 설치 프로그램의 시작 메뉴 업데이트 확인 shortcut은 Inno Setup 설정의 AppUpdatesURL/MyUpdateUrl과 연결되므로 앱 내부 메뉴와 별도로 관리됩니다.

update.manifest_url은 latest.json 같은 manifest 파일 위치를 가리키는 설정입니다. 현재 구현에서는 URL 유효성 검증과 향후 자동 비교 준비 용도이며, 실제 자동 업데이트 판단이나 파일 교체에는 사용하지 않습니다.

배포 위치가 바뀌면 config/app_config.yaml의 update.latest_release_url, update.manifest_url, packaging/IPDK_plus.iss의 MyUpdateUrl을 같은 배포 대상으로 맞춰야 사용자가 혼동하지 않습니다.""",
    ),
    HelpTopic(
        title="로그와 문제 해결",
        keywords=("로그", "app.log", "연결 실패", "인증 실패", "결과 미수신", "recipe", "worker"),
        body="""상태 및 로그 패널은 앱 내부 이벤트를 시간순으로 보여줍니다. 같은 내용은 로거에도 기록되며, 설치형 기본 로그 위치는 %APPDATA%\\IPDK_plus\\logs\\app.log입니다. 설치 폴더 아래에는 로그를 쓰지 않습니다.

RabbitMQ 연결 실패가 발생하면 broker 실행 여부, host/port, 방화벽, username/password, virtual_host, queue declare 권한을 확인하세요. 인증 실패는 계정 또는 vhost 권한 문제일 가능성이 큽니다.

큐가 없거나 declare 오류가 나면 request_queue_declare/result_queue_declare 옵션이 이미 존재하는 broker queue의 durable, exclusive, auto_delete, arguments와 충돌하지 않는지 확인해야 합니다.

Recipe 경로 오류는 선택한 recipe 파일이 로컬에서 보이지 않을 때 경고로 표시될 수 있습니다. 다만 앱은 payload의 RECIPE_PATH를 설정 문자열 그대로 전송하므로, worker 쪽에서 접근 가능한 공유 경로를 쓰는 운영 방식이라면 경고 자체가 반드시 실패를 뜻하지는 않습니다.

이미지가 등록되지 않으면 image_extensions와 scan_mode를 확인하세요. direct 모드에서는 하위 폴더의 이미지를 찾지 않습니다.

전송 후 결과가 오지 않으면 worker가 request_queue를 consume하고 있는지, worker가 request body의 QUEUE_NAME 또는 AMQP reply_to로 결과를 publish하는지, result payload의 request_id가 원 request_id와 같은지 확인하세요. mismatch result는 앱이 ACK 후 무시합니다.

Worker Count가 0이거나 Queued Messages가 계속 쌓이면 worker 미실행, vhost/queue 불일치, routing key 불일치, worker 처리 오류를 의심해야 합니다.""",
    ),
    HelpTopic(
        title="주의사항",
        keywords=("주의", "설정 변경", "초기화", "중지", "네트워크", "대량 처리", "result queue"),
        body="""작업 중 설정값을 바꿔도 이미 생성되었거나 이미 publish된 작업에는 소급 적용되지 않을 수 있습니다. Recipe, Priority, timeout, queue 설정은 전송 시작 시점 또는 작업 생성 시점의 값이 내부 상태와 payload에 반영됩니다.

중지는 앱의 publish/poll worker를 멈추는 동작이며, 이미 RabbitMQ에 들어간 메시지나 worker가 처리 중인 외부 작업을 되돌리는 기능이 아닙니다.

초기화는 앱 내부 상태와 화면을 초기화합니다. broker queue에 남아 있거나 worker가 이미 소비한 메시지의 외부 상태까지 삭제하지 않습니다.

RabbitMQ 서버와 worker는 앱이 보내는 메시지 규격을 그대로 이해해야 합니다. request_id, QUEUE_NAME, RECIPE_PATH, IMG_LIST, PASS 판정 규칙이 어긋나면 앱은 결과를 정상 반영하지 못합니다.

result queue 이름은 클라이언트 PC의 IPv4에 따라 달라집니다. 네트워크 어댑터, VPN, IP 변경, hostname lookup 변화가 있으면 result queue suffix가 바뀔 수 있습니다. worker는 반드시 request의 QUEUE_NAME 또는 reply_to를 사용해야 합니다.

대량 이미지 처리 시 앱은 AppData의 SQLite 작업 DB, 활성 폴더 지연 스캔, Chunk 발행과 inflight 상한을 사용합니다. worker가 없거나 느린 경우에도 전체 작업을 RabbitMQ에 한꺼번에 넣지 않고 자동 대기합니다. 화면의 Queued Messages와 폴더 상태에서 스캔 대기/스캔 중/전송 대기/처리 중 상태를 확인할 수 있습니다. 앱이 중단되면 저장된 세션 설정으로 미완료 스캔과 결과 polling을 다음 실행에서 자동 재개합니다.

운영 중 RabbitMQ queue argument를 바꾸면 기존 queue와 충돌할 수 있습니다. 특히 x-max-priority, durable, auto_delete 같은 값은 broker에 이미 만들어진 queue와 맞아야 합니다.""",
    ),
)
