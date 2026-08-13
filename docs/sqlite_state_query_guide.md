# SQLite 작업 상태 조회 가이드 (CMD)

이 문서는 IPDK_plus가 저장한 폴더 및 이미지 작업 상태를 Windows 명령 프롬프트(CMD)에서 읽기 전용으로 조회하는 방법을 정리한다.

## 1. 데이터베이스 위치

기본 SQLite 파일은 다음 위치에 있다.

```text
%APPDATA%\IPDK_plus\runtime\task_state.sqlite3
```

CMD를 열고 현재 창에서 사용할 환경 변수를 먼저 설정한다.

```cmd
set "DB=%APPDATA%\IPDK_plus\runtime\task_state.sqlite3"
```

파일 존재 여부와 수정 시각을 확인한다.

```cmd
dir "%DB%"
```

이 문서의 조회 명령은 Python 표준 라이브러리의 `sqlite3`를 사용한다. 별도의 `sqlite3.exe` 설치는 필요하지 않다.

```cmd
python --version
```

## 2. 안전한 조회 원칙

- 모든 명령은 SQLite URI의 `mode=ro` 옵션으로 데이터베이스를 읽기 전용으로 연다.
- `UPDATE`, `DELETE`, `DROP`, `VACUUM` 같은 변경 명령은 사용하지 않는다.
- 프로그램 실행 중에도 조회할 수 있지만, 조회 순간에 커밋된 데이터까지만 보인다.
- 실행 중인 DB를 백업하려면 `task_state.sqlite3` 파일만 임의로 복사하지 말고 프로그램을 종료한 뒤 복사하는 것이 안전하다. 실행 중에는 WAL 파일에 최신 변경이 남아 있을 수 있다.

## 3. 기본 조회 명령

### 3.1 테이블별 데이터 개수

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); print('sessions:',c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]); print('folders:',c.execute('SELECT COUNT(*) FROM folders').fetchone()[0]); print('tasks:',c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0])"
```

### 3.2 작업 상태별 개수

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); sid=c.execute('SELECT session_id FROM sessions ORDER BY CASE WHEN state IN (?,?) THEN 0 ELSE 1 END,updated_at DESC LIMIT 1',('ACTIVE','PAUSED_BY_USER')).fetchone()[0]; print('SESSION:',sid); print('STATUS | COUNT'); [print(status,count,sep=' | ') for status,count in c.execute('SELECT status,COUNT(*) FROM tasks WHERE session_id=? GROUP BY status ORDER BY status',(sid,))]"
```

주요 상태는 다음과 같다.

| 상태 | 의미 |
| --- | --- |
| `PENDING` | MQ 전송 대기 |
| `CLAIMED` | 전송 청크로 선택됨 |
| `SENT` | MQ 발행 완료 |
| `RUNNING` | 결과 대기 또는 처리 중 |
| `SUCCESS` | 성공 완료 |
| `FAIL` | Worker 실패 결과 수신 |
| `TIMEOUT` | 제한 시간 초과 |
| `ERROR` | 전송 또는 내부 처리 오류 |
| `CANCELLED` | 취소됨 |

### 3.3 폴더+Recipe 대기열별 진행 상황

각 행은 `source_path + recipe_path` 조합으로 구분되며 프로그램의 진행 중/대기 표와 같은 전송 우선순위인 `position` 순으로 출력한다. 내부 `folder_path`는 대기열 식별자이고 실제 폴더 경로는 `source_path`다. SQLite에는 우선순위가 0부터 저장되고 화면에는 `position + 1` 값이 표시된다.

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); sid=c.execute('SELECT session_id FROM sessions ORDER BY CASE WHEN state IN (?,?) THEN 0 ELSE 1 END,updated_at DESC LIMIT 1',('ACTIVE','PAUSED_BY_USER')).fetchone()[0]; q='SELECT position,held,scan_state,total_count,pending_count,claimed_count,sent_count,running_count,success_count,fail_count,timeout_count,error_count,cancelled_count,inaccessible_count,recipe_alias,recipe_path,source_path FROM folders WHERE session_id=? ORDER BY position'; print('POS | HELD | SCAN | TOTAL | PENDING | CLAIMED | SENT | RUNNING | SUCCESS | FAIL | TIMEOUT | ERROR | CANCELLED | INACCESSIBLE | RECIPE | RECIPE_PATH | FOLDER'); [print(*r,sep=' | ') for r in c.execute(q,(sid,))]"
```

`held=1`은 사용자가 보류한 폴더+Recipe 대기열이다. 전체 모수에는 포함되지만 보류 해제 전까지 publish 대상에서는 제외된다. `inaccessible_count`는 스캔 중 읽기 권한이 없어 제외한 이미지 수다.

`scan_state`의 주요 값은 다음과 같다.

| 상태 | 의미 |
| --- | --- |
| `WAITING` | 전체 모수 집계 대기 |
| `SCANNING` | 이미지 작업 집계 중 |
| `SCANNED` | 해당 폴더 집계 완료 |
| `EMPTY` | 대상 이미지 없음 |
| `ERROR` | 폴더 스캔 오류 |

전체 폴더의 `scan_state`가 `WAITING` 또는 `SCANNING`이 아니면 화면에서 사용할 전체 모수가 확정된 상태다.

### 3.4 최근 작업 20건

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); sid=c.execute('SELECT session_id FROM sessions ORDER BY CASE WHEN state IN (?,?) THEN 0 ELSE 1 END,updated_at DESC LIMIT 1',('ACTIVE','PAUSED_BY_USER')).fetchone()[0]; q='SELECT status,recipe_alias,image_path,sent_at,completed_at FROM tasks WHERE session_id=? ORDER BY rowid DESC LIMIT 20'; print('STATUS | RECIPE | IMAGE | SENT_AT | COMPLETED_AT'); [print(*r,sep=' | ') for r in c.execute(q,(sid,))]"
```

### 3.5 특정 상태 작업 조회

조회할 상태를 환경 변수로 지정한다.

```cmd
set "STATUS=PENDING"
```

최대 100건을 폴더 등록 순서와 작업 생성 순서대로 조회한다.

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); sid=c.execute('SELECT session_id FROM sessions ORDER BY CASE WHEN state IN (?,?) THEN 0 ELSE 1 END,updated_at DESC LIMIT 1',('ACTIVE','PAUSED_BY_USER')).fetchone()[0]; q='SELECT t.request_id,t.status,t.recipe_alias,t.image_path FROM tasks t JOIN folders f ON f.session_id=t.session_id AND f.folder_path=t.folder_path WHERE t.session_id=? AND t.status=? ORDER BY f.position,t.rowid LIMIT 100'; print('REQUEST_ID | STATUS | RECIPE | IMAGE'); [print(*r,sep=' | ') for r in c.execute(q,(sid,os.environ['STATUS']))]"
```

예를 들어 `SENT`, `RUNNING`, `FAIL`, `ERROR`를 확인할 때는 `STATUS` 값만 변경하면 된다.

### 3.6 오류가 있는 최근 작업 조회

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); sid=c.execute('SELECT session_id FROM sessions ORDER BY CASE WHEN state IN (?,?) THEN 0 ELSE 1 END,updated_at DESC LIMIT 1',('ACTIVE','PAUSED_BY_USER')).fetchone()[0]; q='SELECT status,request_id,image_path,error_message,completed_at FROM tasks WHERE session_id=? AND status IN (?,?,?) ORDER BY rowid DESC LIMIT 100'; print('STATUS | REQUEST_ID | IMAGE | ERROR | COMPLETED_AT'); [print(*r,sep=' | ') for r in c.execute(q,(sid,'FAIL','TIMEOUT','ERROR'))]"
```

## 4. 전체 진행률 직접 계산

전체 모수가 확정되었는지와 완료율을 함께 출력한다. 완료에는 `SUCCESS`, `FAIL`, `TIMEOUT`, `ERROR`, `CANCELLED`가 포함된다.

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); sid=c.execute('SELECT session_id FROM sessions ORDER BY CASE WHEN state IN (?,?) THEN 0 ELSE 1 END,updated_at DESC LIMIT 1',('ACTIVE','PAUSED_BY_USER')).fetchone()[0]; total,done=c.execute('SELECT COALESCE(SUM(total_count),0),COALESCE(SUM(success_count+fail_count+timeout_count+error_count+cancelled_count),0) FROM folders WHERE session_id=?',(sid,)).fetchone(); pending_scan=c.execute('SELECT EXISTS(SELECT 1 FROM folders WHERE session_id=? AND scan_state IN (?,?))',(sid,'WAITING','SCANNING')).fetchone()[0]; print('전체 모수 확정:',not bool(pending_scan)); print('완료/전체:',str(done)+'/'+str(total)); print('진행률:',f'{done/total*100:.1f}%' if total else '0.0%')"
```

`전체 모수 확정: False`인 동안 출력되는 진행률은 아직 최종 비율이 아니므로 운영 판단에 사용하지 않는다.

## 5. CSV 파일로 내보내기

현재 CMD 경로에 `task_status.csv`를 생성한다.

```cmd
python -c "import os,sqlite3,csv; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); sid=c.execute('SELECT session_id FROM sessions ORDER BY CASE WHEN state IN (?,?) THEN 0 ELSE 1 END,updated_at DESC LIMIT 1',('ACTIVE','PAUSED_BY_USER')).fetchone()[0]; q='SELECT request_id,status,folder_path,image_path,recipe_alias,recipe_path,sent_at,completed_at,error_message FROM tasks WHERE session_id=? ORDER BY rowid'; rows=c.execute(q,(sid,)); f=open('task_status.csv','w',newline='',encoding='utf-8-sig'); w=csv.writer(f); w.writerow([d[0] for d in rows.description]); w.writerows(rows); f.close(); print('생성 완료:',Path('task_status.csv').resolve())"
```

Excel에서 한글이 깨지지 않도록 `UTF-8 BOM` 형식인 `utf-8-sig`로 저장한다.

## 6. 실행 이력 조회

앱의 `작업 > 실행 이력`과 같은 세션 목록을 조회한다. `PAUSED_BY_USER`는 사용자가 명시적으로 일시정지한 세션, `COMPLETED`는 완료 이력, `RESET`은 초기화로 종료된 이력이다.

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT s.session_id,s.state,COALESCE(s.started_at,s.created_at),s.ended_at,COUNT(DISTINCT f.folder_path),COALESCE(SUM(f.total_count),0),COALESCE(SUM(f.success_count),0),COALESCE(SUM(f.fail_count+f.timeout_count+f.error_count+f.cancelled_count),0) FROM sessions s LEFT JOIN folders f ON f.session_id=s.session_id GROUP BY s.session_id ORDER BY COALESCE(s.started_at,s.created_at) DESC'; print('SESSION | STATE | START | END | FOLDERS | TOTAL | SUCCESS | FAILED'); [print(*r,sep=' | ') for r in c.execute(q)]"
```

특정 이력을 조회하려면 위 결과의 session ID를 지정한다.

```cmd
set "SESSION_ID=조회할-session-id"
```

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); q='SELECT status,COUNT(*) FROM tasks WHERE session_id=? GROUP BY status ORDER BY status'; print('STATUS | COUNT'); [print(*r,sep=' | ') for r in c.execute(q,(os.environ['SESSION_ID'],))]"
```

## 7. 테이블 및 컬럼 구조 확인

테이블 목록:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); [print(r[0]) for r in c.execute('SELECT name FROM sqlite_master WHERE type=? ORDER BY name',('table',))]"
```

`folders` 테이블 컬럼:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); [print(r[1],r[2]) for r in c.execute('PRAGMA table_info(folders)')]"
```

`tasks` 테이블 컬럼:

```cmd
python -c "import os,sqlite3; from pathlib import Path; c=sqlite3.connect(Path(os.environ['DB']).as_uri()+'?mode=ro',uri=True); [print(r[1],r[2]) for r in c.execute('PRAGMA table_info(tasks)')]"
```

## 8. 문제 해결

### `python`을 찾을 수 없는 경우

프로젝트의 가상환경 Python이 있으면 저장소 루트에서 다음과 같이 실행한다.

```cmd
.venv\Scripts\python.exe --version
```

각 조회 명령의 맨 앞 `python`을 `.venv\Scripts\python.exe`로 바꾸면 된다.

### `KeyError: 'DB'`가 발생하는 경우

현재 CMD 창에서 DB 환경 변수가 설정되지 않은 상태다. 다음 명령을 다시 실행한다.

```cmd
set "DB=%APPDATA%\IPDK_plus\runtime\task_state.sqlite3"
```

### `unable to open database file`이 발생하는 경우

다음 항목을 확인한다.

1. `dir "%DB%"`로 파일이 실제로 존재하는지 확인한다.
2. 프로그램을 한 번 실행하여 런타임 DB가 생성되었는지 확인한다.
3. 현재 Windows 계정이 해당 AppData 폴더를 읽을 수 있는지 확인한다.

### 조회 결과가 0건인 경우

오류가 아니라 현재 세션에 등록된 폴더나 작업이 없는 상태일 수 있다. 먼저 테이블별 데이터 개수와 `folders` 내용을 확인한다.
