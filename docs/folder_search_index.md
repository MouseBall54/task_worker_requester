# 즐겨찾기 Root 및 폴더 검색 가이드

## 1. 기능 개요

왼쪽 `즐겨찾기 Root`에 자주 사용하는 기준 폴더를 등록하면 해당 Root 아래의 폴더명과 경로를 별도 SQLite 인덱스에서 빠르게 검색할 수 있다.

- `즐겨찾기 관리`: 별도 테이블에서 순서·Root 경로·상태·마지막 확인 시각을 확인하고 `폴더 추가`·삭제와 표시 순서 변경
- `새로고침`: 선택 Root 또는 전체 Root를 완전 재색인
- 즐겨찾기 목록 아래 통합 입력창: 실제 폴더 경로는 트리로 이동하고 일반 검색어는 즐겨찾기 Root에서 검색
- 검색 결과는 캐시에서 즉시 표시한 뒤 실제 파일 시스템 변경분 확인
- `취소`: 백그라운드 확인을 중지하고 현재 캐시 결과 유지

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

파일 내용이나 이미지 데이터는 저장하지 않는다.

## 3. 갱신 방식

최초 추가와 수동 새로고침은 Root 전체를 백그라운드에서 색인한다. 이후 검색과 5분 주기 갱신은 기존 폴더의 수정 시각을 확인하고 변경된 디렉터리만 다시 열어 신규·삭제 하위 폴더를 반영한다.

Root 자체의 파일 시스템 변경 알림은 빠른 갱신을 시작하는 힌트로만 사용한다. 깊은 하위 폴더나 네트워크 경로에서 알림이 누락될 수 있으므로 주기적 증분 확인을 함께 수행한다.

Root가 오프라인이면 기존 인덱스를 보존한다. 다시 연결된 뒤 검색 또는 새로고침을 수행하면 온라인 상태와 변경분이 갱신된다.

## 4. CMD에서 상태 조회

```cmd
set "DB=%APPDATA%\IPDK_plus\runtime\folder_index.sqlite3"
```

즐겨찾기 Root와 상태:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT position,online,last_checked,path FROM favorite_roots ORDER BY position'; print('POS | ONLINE | LAST_CHECKED | ROOT'); [print(*r,sep=' | ') for r in c.execute(q)]"
```

Root별 색인 폴더 수:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT root_path,COUNT(*) FROM folder_index GROUP BY root_path ORDER BY root_path'; print('ROOT | FOLDER_COUNT'); [print(*r,sep=' | ') for r in c.execute(q)]"
```

이름 또는 경로 검색 예시:

```cmd
set "QUERY=sample"
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT name,path,root_path FROM folder_index WHERE name_norm LIKE ? OR path_norm LIKE ? ORDER BY name_norm LIMIT 100'; v='%'+os.environ['QUERY'].casefold()+'%'; print('NAME | PATH | ROOT'); [print(*r,sep=' | ') for r in c.execute(q,(v,v))]"
```
