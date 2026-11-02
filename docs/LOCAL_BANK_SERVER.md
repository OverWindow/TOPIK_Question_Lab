# 로컬 문항은행 서버 운영

Hub는 `.env`의 `DATABASE_URL` 하나로 `127.0.0.1:5434`의 `topik_bank` DB에 접속합니다.
접속·관리·데이터 소유 계정은 `bank_admin` 하나입니다.
Web 프로젝트의 연결 설정은 별도로 유지합니다.

## 서버 시작·중지·상태

프로젝트 루트의 PowerShell에서 실행합니다.

```powershell
.\manage_bank_server.ps1 start
.\manage_bank_server.ps1 status
.\manage_bank_server.ps1 stop
```

`python -m topik_question_lab.maintenance.bank_server <명령>`으로도 실행할 수 있습니다.
운영 도구는 기존 서버만 제어하며 데이터베이스·계정을 생성하거나 데이터를 복원하지 않습니다.
PC를 다시 시작한 뒤에는 `start`를 실행합니다.

## 파일과 접속

| 항목 | 위치 |
| --- | --- |
| 연결 설정 | `.env`의 `DATABASE_URL` |
| PostgreSQL 데이터 | `.local/postgres-bank/data` |
| 운영 도구 설정 | `.local/postgres-bank/server.json` |
| 서버 로그 | `.local/postgres-bank/server.log` |

서버는 루프백 주소에만 수신하고 SCRAM-SHA-256 인증을 사용합니다.
별도 PostgreSQL 프로세스·데이터 폴더를 사용하지만 같은 PC의 OS·디스크는 공유합니다.

## 데이터 관리

기존 DB 함수·트리거와 문항·세트·미디어 주소는 DB 안에 보관됩니다.
과거 이전·준비 기록과 백업은 `backups`에 보존하며 현재 실행 시 참조하지 않습니다.
PostgreSQL 데이터와 `.env`는 별도로 백업하세요. 기존 SQLite ZIP 백업에는 PostgreSQL 데이터가 포함되지 않습니다.

Hub의 새 은행과 기존 Web 은행 사이의 자동 동기화는 구성하지 않습니다.
