# 즐겨찾기 Root 및 폴더 검색 가이드

## 1. 기능 개요

왼쪽 `즐겨찾기 Root`에 자주 사용하는 기준 폴더를 등록하면 해당 Root 아래의 폴더명과 경로를 별도 SQLite 인덱스에서 빠르게 검색할 수 있다.

- `즐겨찾기 관리`: Root 경로·색인 범위·상태·색인/대기/오류 수·마지막 완료 시각 확인
- 색인 범위: 신규 Root 기본 `하위 5계층`, 선택적 `하위 3·4·5·6계층`, `전체 계층`, `색인 제외`
- 관리 동작: 전체 재색인, 증분 갱신, 색인 일시정지/재개, 오류 보기
- `새로고침`: 선택 Root 또는 전체 Root를 완전 재색인
- 즐겨찾기 목록 아래 통합 입력창: 실제 폴더 경로는 트리로 이동하고 일반 검색어는 즐겨찾기 Root에서 검색
- 검색 결과는 SQLite 캐시에서만 즉시 조회하며 200개 단위 `검색 결과 더 보기` 지원
- `색인 일시정지`: 백그라운드 색인을 중단 위치에 보존하고 현재 캐시 결과 유지
- 색인 상태의 확인·완료 시각은 서울 표준시와 0.1초 단위로 기록·표시

즐겨찾기 항목 또는 검색 결과를 누르면 기존 `QFileSystemModel` 경로 이동 기능으로 연결된다.

## 2. 저장 위치와 분리 원칙

```text
%APPDATA%\IPDK_plus\runtime\folder_index.sqlite3
```

이 DB는 RabbitMQ 작업 상태를 저장하는 `task_state.sqlite3`와 분리된다. 검색 색인 갱신이 작업 진행률, 세션 복구, MQ 발행 순서에 영향을 주지 않도록 하기 위한 구조다.

저장 정보는 폴더 경로 검색에 필요한 최소 메타데이터로 제한한다.

- 즐겨찾기 Root 경로와 표시 순서
- Root 온라인 여부와 마지막 확인 시각
- 실제 폴더 경로, 폴더명, 상위 경로
- 디렉터리 수정 시각, 마지막 색인 시각, 마지막 확인 기준 존재 여부(`exists_flag`)
- Root별 색인 상태와 현재 세대
- 재실행 후 이어갈 영속 탐색 대기열
- 접근 권한·긴 경로·네트워크·I/O 오류와 재시도 횟수

파일 내용이나 이미지 데이터는 저장하지 않는다.

## 3. 갱신 방식

최초 추가와 수동 새로고침은 Root 전체를 저우선순위 백그라운드에서 색인한다. 탐색 대상은 `folder_scan_queue`에 저장하고 500개 단위 트랜잭션으로 처리하므로, 프로그램이 종료되거나 비정상 중단돼도 다음 실행에서 남은 대기열부터 이어간다. 검색어 변경은 진행 중인 색인을 취소하지 않는다.

색인 상태는 다음과 같다.

- `EMPTY`: 아직 색인하지 않음
- `INDEXING`: 현재 색인 중
- `PAUSED`: 사용자가 일시정지
- `INCOMPLETE`: 중단 또는 해결되지 않은 오류로 미완료
- `READY`: 대기 0, 오류 0, 세대 정리까지 완료
- `OFFLINE`: Root 연결 해제
- `EXCLUDED`: 사용자가 색인 제외

전체 재색인은 새 `scan_generation`을 발급한다. 기존 완성 캐시는 새 탐색이 끝날 때까지 검색에 남으며, 대기 폴더와 오류가 모두 0인 경우에만 이전 세대에서 사라진 폴더를 정리하고 `READY`로 전환한다. 중단·오프라인·오류 시 기존 캐시를 삭제하지 않는다.

Root 자체의 파일 시스템 변경 알림은 갱신을 시작하는 힌트로만 사용한다. 깊은 하위 폴더나 네트워크 경로에서 알림이 누락될 수 있으므로 5분 주기의 제한된 재조정 작업을 함께 수행한다. 일반 검색은 실제 파일 시스템을 다시 순회하지 않아 RabbitMQ 전송 작업에 불필요한 부하를 주지 않는다.

Windows에서는 탐색할 때만 `\\?\` 확장 경로를 사용하고 UI와 SQLite에는 일반 표시 경로를 저장한다. 심볼릭 링크와 정션은 순환 탐색을 막기 위해 하위 탐색 대상에서 제외한다. 접근 실패는 숨기지 않고 오류 테이블에 기록하며 해결되지 않은 오류가 있으면 `READY`가 될 수 없다.

Root가 오프라인이면 기존 인덱스를 보존한다. 다시 연결된 뒤 검색 또는 새로고침을 수행하면 온라인 상태와 변경분이 갱신된다.

## 4. CMD에서 상태 조회

```cmd
set "DB=%APPDATA%\IPDK_plus\runtime\folder_index.sqlite3"
```

즐겨찾기 Root와 색인 상태:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT position,scope_mode,index_status,indexed_count,pending_count,error_count,last_completed,path FROM favorite_roots ORDER BY position'; print('POS | SCOPE | STATUS | INDEXED | PENDING | ERRORS | LAST_COMPLETED | ROOT'); [print(*r,sep=' | ') for r in c.execute(q)]"
```

Root별 색인 폴더 수:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT root_path,COUNT(*) FROM folder_index GROUP BY root_path ORDER BY root_path'; print('ROOT | FOLDER_COUNT'); [print(*r,sep=' | ') for r in c.execute(q)]"
```

미처리 탐색 대기와 오류:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); print('PENDING=',c.execute(\"SELECT COUNT(*) FROM folder_scan_queue WHERE state IN ('PENDING','PROCESSING')\").fetchone()[0]); print('ERRORS=',c.execute('SELECT COUNT(*) FROM folder_index_errors WHERE blocking=1').fetchone()[0])"
```

이름 또는 경로 검색 예시:

```cmd
set "QUERY=sample"
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT name,path,root_path FROM folder_index WHERE name_norm LIKE ? OR path_norm LIKE ? ORDER BY name_norm LIMIT 100'; v='%'+os.environ['QUERY'].casefold()+'%'; print('NAME | PATH | ROOT'); [print(*r,sep=' | ') for r in c.execute(q,(v,v))]"
```
