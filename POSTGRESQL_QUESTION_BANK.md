# TOPIK PostgreSQL 문항 은행

이 문서는 `013_remove_deployment_history`까지 적용한 코드와 스키마를 설명합니다.
로컬 생성기의 원본은 `DATABASE_URL`의 PostgreSQL입니다. 별도의 서버 등록 화면에서 지정한 대상 DB로 단방향 복사할 수 있습니다.
기존 DB의 구조 전환은 [이관 안내](docs/UNVERSIONED_DATABASE_MIGRATION.md)를 따릅니다.

## 저장 원칙

- 문제 하나는 고정된 `item_id`와 현재 내용 한 행을 갖습니다. 문항 버전 번호는 없습니다.
- 명시적인 문항 동기화는 같은 행을 갱신합니다. 과거 수정 내용은 보존하지 않습니다.
- `PostgreSQL 문제 관리`에서도 로컬 원본 없이 현재 행을 직접 수정할 수 있습니다. 세트·유형·번호·생성 출처는 유지합니다.
- 직접 편집 시 `content_json`과 검색용 컬럼·내용 해시를 함께 갱신하고 `source_provenance.bank_edited_at`에 최근 편집 시각을 기록합니다.
- 수정 문항과 세트는 검수 대기로 전환합니다. 관련 미디어가 그대로이면 재연결하고, 대본·그림 자료 변경 시에는 기존 연결을 비활성화합니다.
- 사용 중지는 기존 `retired`, 복원은 `pending_review`를 사용합니다. 영구 삭제는 사용 기록 참조를 검사하고 해당 미디어 연결만 제거합니다.
- 한 문제는 최대 한 세트에 속하며, 미배정 문제의 `set_id`, `position`은 모두 `NULL`입니다.
- 세트는 `items.set_id`, `items.position`으로 구성합니다. 문제 갱신은 해당 세트의 조회 결과에도 반영됩니다.
- 핵심 테이블은 `items`, `question_sets`입니다. 생성·검수·프롬프트·소재·등록 작업 보조 저장소는 유지합니다.
- 프롬프트 저장 이력과 모델 정보는 문항 수정 버전과 별개입니다. `prompt_version`은 프롬프트 해시이고, 기존 이름 `generator_version`은 모델 ID입니다.

## 기존 생성 흐름

유형·모델 선택 → 문제 생성 → 검수·수정 후, `문제 등록·세트 만들기`에서 대상 선택 → 내용 확인 → 등록 완료 순서로 처리합니다. 일괄 생성의 미디어 자동 등록은 기존 파이프라인을 사용하며, 등록 완료와 검수 승인을 별도로 표시합니다.
읽기·듣기 생성 화면, SQLite 생성·검수 저장, 50문항 일괄 생성, 소재 은행, 파일 내보내기와 백업은 유지합니다.
PostgreSQL 연결이 없어도 기존 생성 기능을 사용할 수 있습니다.

## 테이블과 뷰

### `topik_bank.items`

기존 `items`에 문항 내용과 세트 소속을 통합합니다.

| 필드 | 의미 |
| --- | --- |
| `item_id` | 문제 UUID, 기본 키 |
| `source_key` | 로컬 문제 식별자, 유일 값 |
| `section`, `type_slot`, `item_type` | 영역, TOPIK 번호 유형, 문제 유형 |
| `primary_skill`, `target_level` | 평가 요소와 급수 |
| `stem`, `choices`, `correct_answer`, `explanation` | 지문·질문, 보기, 정답, 해설 |
| `stem_length`, `choice_count` | 지문 길이와 보기 수 |
| `content_json` | 공통 지문·대본·그림 설명을 포함한 전체 문항 |
| `generator_provider`, `generator_model`, `generator_version` | 기존 생성 백엔드·공급자 키·모델 ID |
| `prompt_version` | 생성에 사용한 프롬프트 해시 |
| `review_status` | pending_review / reviewed / pilot / active / retired |
| `source_provenance` | 원본 DB, 문제·실행 ID, 생성 옵션, `publication_metadata.predicted_difficulty` |
| `content_hash` | 현재 내용·메타데이터의 동일 여부를 판단하는 해시 |
| `set_id`, `position` | 소속 세트 FK와 세트 내 1~50번 순서, 미배정 시 모두 NULL |
| `created_at`, `updated_at` | 최초 저장 시각과 현재 내용 갱신 시각 |

`UNIQUE(set_id, position)`으로 세트 내 번호 중복을 막습니다.
`set_id`와 `position`은 함께 설정되거나 함께 비어 있어야 합니다.
문제 ID가 기본 키이므로 한 문제를 여러 세트에 연결할 수 없습니다.

### `topik_bank.question_sets`

세트 UUID, 영역, 생성 모델 정보, `set_sequence`, 검수 상태,
기본 급수·난이도, 생성·발행 시각을 저장합니다.
같은 영역·모델의 세트 번호는 중복될 수 없습니다.
문항·세트 버전과 버전 기반 `set_fingerprint`는 저장하지 않습니다.
검증과 재시도에는 실제 내용·소속·순서와 작업 스냅샷 해시를 사용합니다.

### 보조 저장소

`schema_migrations`는 적용한 구조 변경 이력을 보존합니다.
과거 배포 기록 테이블 `deployment_runs`, `deployment_run_sets`는 마이그레이션 013에서 제거합니다.
로컬 SQLite의 `examples`, `settings`, `prompt_versions`, `runs`, `generated_questions`,
`reviews`, 생성 완료 기록, 소재·사용 이력, 일괄 작업 및 원격 등록 작업도 유지합니다.

### 조회 뷰

`current_items`와 `current_set_contents` 이름은 유지하지만 `item_version` 컬럼은 제거합니다.

```sql
SELECT * FROM topik_bank.current_items
WHERE review_status IN ('reviewed', 'pilot', 'active');

SELECT * FROM topik_bank.current_set_contents
WHERE set_id = '세트 UUID'
ORDER BY position;
```

두 뷰는 현재 내용만 반환합니다. 과거 응시 문제는 Web의 `session_items.question_snapshot`으로 재현합니다.

## 로컬 등록과 검증

`문제 등록·세트 만들기`는 `DATABASE_URL`을 사용합니다. 문제·세트 등록과 생성 묶음 승인을 한 화면에서 처리하며, 단순 조회는 DB 구조나 내용을 변경하지 않습니다.
미디어는 로컬 파일 또는 S3·CloudFront 설정으로 검증합니다.
저장 위치와 검증 조건은 [로컬 미디어 검증 안내](docs/LOCAL_MEDIA_DEPLOYMENT.md)를 참고하세요.

### 생성 묶음 등록

50문항의 승인, 자동 검사, 번호 분포와 공통 지문을 확인한 다음 한 트랜잭션으로 등록합니다.
작업 내용과 대상 식별자는 `data/publication_jobs.db`에 저장합니다.
동일 내용의 재시도는 기존 세트를 재사용하고, 다른 내용·출처·순서는 충돌로 차단합니다.
등록 후 별도 연결에서 실제 조회 뷰를 검증하며, 결과 재확인과 재시도를 지원합니다.
등록 단계는 DDL이나 마이그레이션을 실행하지 않습니다.

이전 DB 대상의 작업도 이력에서 볼 수 있지만 조회 전용이며 다시 전송하지 않습니다.

### 고급 관리

문항 동기화는 같은 `item_id`의 현재 내용을 갱신합니다.
동일한 문항·순서의 세트는 기존 세트를 갱신하고, 미사용 문항 세트는 새 번호로 발행합니다.
`문제 등록·세트 만들기 → 최근 등록 결과`는 `data/publication_jobs.db`의 등록 작업을 조회합니다.
고급 설정의 과거 배포 이력 조회와 CSV 다운로드는 제거되었습니다.

미디어 메타데이터와 연결은 기존 4개 테이블에 유지합니다.
`로컬 검증`은 선택 세트, 필수 음원·이미지, 파일 크기·SHA-256을 읽기 전용으로 검사합니다.
로컬 파일 모드는 기존 주소를 보존합니다. S3·CloudFront 모드는 이관한 파일의 실제 바이트도 검사합니다.
[로컬 미디어 검증 안내](docs/LOCAL_MEDIA_DEPLOYMENT.md)를 참고하세요.

## Django 서버에 문제 등록

사이드바의 **서버에 문제 등록**에서 로컬 서버 또는 운영 서버를 선택합니다. 기본 선택은 로컬이며,
선택하지 않은 대상에는 접속하지 않습니다. 원본 DB를 대상으로 지정하면 쓰기 전에 차단합니다.

Hub의 `.env`에 아래 연결값을 설정합니다. 비밀번호를 포함한 값은 화면이나 로그에 출력하지 않습니다.

```dotenv
DATABASE_URL=기존_생성기_원본_DB
QUESTION_BANK_LOCAL_DATABASE_URL=로컬_Django가_사용하는_DB
QUESTION_BANK_PRODUCTION_DATABASE_URL=운영_Django가_사용하는_DB
```

1. **최초 대상 DB 구조 준비 → 선택 서버 구조 초기화**로 빈 DB에 `topik_bank`의 문제 테이블 5개와
   `current_items`, `current_set_contents` 뷰를 만듭니다. 기존에 호환되는 구조가 있으면 재사용하고,
   구조가 다르거나 일부만 존재하면 변경하지 않습니다. 초기화 실패는 전체 롤백됩니다.
2. 최초에는 **전체 문제은행**을 선택하여 미배정 문항·과거/삭제 표시 미디어 기록까지 복사합니다.
   이후에는 **원본 세트 불러오기 → 복사할 세트**에서 하나 또는 여러 세트를 선택할 수 있습니다.
3. **등록 내용 확인**에서 대상별 행 수, 미배정으로 보존할 문항, 고유키 충돌, 공유 미디어 접근 상태를 확인합니다.
4. **선택 서버에 원본으로 덮어쓰기**를 실행합니다. 대상마다 한 트랜잭션으로 처리하며 커밋 후 재조회하여 비교합니다.

복사 대상은 `question_sets`, `items`, `tts_audio_assets`, `item_audio_bindings`,
`item_visual_assets`입니다. 음원 연결은 세트 유무와 관계없이 문항 ID를 기준으로 하며,
연결 UUID와 과거 연결 이력도 함께 복사합니다. Hub의 `schema_migrations`와
로컬 SQLite 생성·등록 기록은 전송하지 않습니다.
대상의 Django 인증·설문·마이그레이션 테이블도 변경하지 않습니다.

서버 등록 결과는 현재 화면 세션에만 보관합니다. 세션 종료 후 조회할 수 있는 영구 배포 이력은 없습니다.

UUID, 생성/수정 시각, 검수 상태, JSON, 해시, 세트 번호·순서, 미디어 주소는 원본 값으로 덮어씁니다.
검수 승인 여부로 자동 필터링하지 않습니다. 세트에서 빠진 대상 문항은 미배정으로 보존하며
불필요해진 세트 음원 연결은 제거합니다. 대상에만 있는 별도 세트와 관련 없는 미디어는 보존합니다.
선택 세트의 문항에 연결된 과거 미디어도 함께 복사합니다. 참조되지 않은 음원 기록은 전체 등록에 포함됩니다.

고유키가 같고 UUID가 다른 문항·세트·음원은 원본 ID로 교체합니다. 외부 응시 기록이 참조하는 ID를
교체해야 하거나, 선택하지 않은 세트의 소속·공유 음원을 변경해야 하면 영향 범위를 표시하고 중단합니다.
관련 세트를 함께 선택하거나 외부 참조를 별도로 정리한 후 다시 확인합니다. 외부 테이블은 강제로 삭제하지 않습니다.
미리보기 이후 원본/대상이 변경되면 다시 확인해야 합니다. 네트워크 장애로 커밋 여부가 불명확할 때도
다시 확인하면 실제 대상과 비교하며, 이미 일치하면 중복 쓰기를 하지 않습니다. 두 서버의 결과는 독립적입니다.

S3·CloudFront 파일은 업로드·변경하지 않습니다. 현재 참조되는 S3 파일은 설정된 `CLOUDFRONT_BASE_URL`에만
HEAD 요청을 보내 응답·크기·MIME을 확인합니다. 이 검사는 전체 파일 SHA-256 검사가 아닙니다.
접근 실패나 이전 저장소 주소가 있어도 DB 기록은 그대로 복사하며 화면에 별도로 알립니다.
기존 Supabase 미디어 메타데이터를 보존하기 위해 초기 스키마는 `local`, `supabase`, `s3` 값을 허용합니다.

Django `question_bank` 앱은 동일 DB의 `topik_bank`를 명시한 `managed=False` 모델을 사용합니다.
문제 테이블 DDL은 Hub가 담당하며 Django의 상태 마이그레이션은 문제 테이블을 생성하지 않습니다.
`managed=False` 자체가 DB 쓰기 권한을 제한하는 것은 아닙니다. 이번 연동은 조회 명령만 제공하며
수신 HTTP API, 역방향 동기화, 관리자 편집 화면, 백그라운드 작업은 추가하지 않습니다.

```powershell
# unigate-main/backend의 로컬 개발 환경
$env:DJANGO_ENV = 'development'
.\.venv\Scripts\python.exe manage.py inspect_question_bank
.\.venv\Scripts\python.exe manage.py inspect_question_bank --set-id 세트_UUID
```

스키마 변경은 등록 중에 자동 적용하지 않습니다. 구조 차이를 확인하고 별도의 변경 SQL을 검토·적용해야 합니다.
복사 통합 테스트는 `tests/test_bank_transfer.py`이며 `TEST_PG_ADMIN_URL`의 격리된 로컬 임시 DB만 사용합니다.

## 과거 배포 이력 제거

`013_remove_deployment_history.sql`은 `deployment_run_sets`와 `deployment_runs`를 순서대로 삭제합니다.
전체 PostgreSQL DB를 먼저 백업한 뒤, 고급 설정의 `DB 구조 준비` 등 명시적인 마이그레이션 경로로 적용하세요.
013이 미적용 상태이면 앱 시작 검사가 중단하므로, 실행 중인 앱을 통한 적용이 불가능하면
백업 후 `PostgresQuestionBank(url).ensure_schema()`로 적용하고 앱을 다시 시작합니다.
이 메서드는 다른 미적용 마이그레이션도 실행하므로 적용 목록을 먼저 확인해야 합니다.
앱 시작 시 013을 자동 적용하지 않습니다.

삭제와 `schema_migrations` 기록은 같은 트랜잭션에서 처리합니다. 예상 밖의 참조가 있으면
전체 롤백하며 `CASCADE`로 다른 객체를 삭제하지 않습니다. 기존 004 파일과 적용 이력은 유지하고,
새 설치에서도 전체 마이그레이션 완료 후 두 테이블이 남지 않습니다.
과거 배포 기록 복구에는 삭제 전 백업이 필요합니다. 문항·세트·미디어와 SQLite 등록 이력은 보존합니다.

## 기존 DB 이관

이관 파일은 `topik_question_lab/maintenance/migrations/006_unversioned_items.sql`입니다.
전체 DB를 백업하고 별도 임시 DB에 복원하여 구조 전환을 검증한 후 적용하세요.
문항·세트 ID, 순서, 기존 세트 내용과 보조 이력이 보존되었는지 확인합니다.
외부 응시·미디어 테이블이 구 버전 구조를 참조하면 Hub의 이관은 차단됩니다.
이때 외부 참조를 삭제하거나 Hub만으로 강제 이관하지 마세요.

세트 소속 문항은 **세트가 참조하던 버전의 내용**을 남깁니다.
그 문항에 더 최신의 미발행 내용이 있어도 기존 세트 내용을 바꾸지 않습니다.
미배정 문항은 최신 내용만 남깁니다. 나머지 문항 버전은 제거됩니다.
원래 `item_id`, `source_key`, 생성 시각과 세트 ID는 유지합니다.

한 문항의 다중 세트 소속, 내용이 없는 문항, 외부 테이블의 문항 버전·세트 연결 참조가 있으면 이관을 중단합니다.
뷰·기타 참조가 남아 있으면 PostgreSQL의 의존성 검사로 중단합니다. `CASCADE`로 삭제하지 않습니다.
실패한 이관의 DDL과 데이터 변경은 전체 롤백됩니다.
과거 버전 복구는 이관 전 DB 백업으로만 가능합니다.

## 검증

PostgreSQL 통합 테스트는 `TEST_PG_ADMIN_URL`로 명시한 루프백 서버에서
무작위 이름의 임시 DB를 만들고 종료 시 제거합니다.
`.env`의 DB를 테스트 대상으로 사용하지 않습니다.
환경 변수가 없으면 통합 테스트를 건너뜁니다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_publication.py tests/test_postgres_inspection.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_deployment_history_removal.py tests/test_bank_transfer.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_unversioned_questions.py tests/test_postgres_integration.py tests/test_media_validation_integration.py tests/test_direct_publication.py
```

합성 데이터로 기존 세트 내용 보존, 미배정 최신 내용 선택, 다중 소속·외부 참조 차단,
동일 ID 갱신, 중복·동시 등록, 전체 롤백, 네트워크 장애 후 복구를 확인합니다.
생성 API는 호출하지 않습니다. 새 HTTP API나 영구 테이블은 추가하지 않습니다.
