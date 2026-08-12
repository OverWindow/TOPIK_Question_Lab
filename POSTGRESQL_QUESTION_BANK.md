# TOPIK PostgreSQL 문항 은행 — 구조·운영·인수인계 문서

> 최종 확인일: 2026-08-11 (미국 동부 시간) / 2026-08-12 (한국 시간)
>
> 이 문서는 다음 작업 세션에서 PostgreSQL 구조와 현재 이관 상태를 빠르게 복원하기 위한 기준 문서다. 실제 접속 주소와 비밀번호는 보안상 적지 않는다. 로컬 원본은 `.env`의 `DATABASE_URL`, 운영 Supabase는 `PRODUCTION_DATABASE_URL`을 사용한다.

## 1. 가장 먼저 알아야 할 결론

- 문항은 이제 단순한 `.sql` 파일에 들어 있는 것이 아니라, **실제 PostgreSQL 데이터베이스의 `topik_bank` 스키마에 행(row)으로 저장**된다.
- SQL은 데이터를 조회·변경하는 언어이고 PostgreSQL은 데이터베이스다. 따라서 “문항이 SQL에 저장되었다”보다 “문항이 PostgreSQL 테이블에 저장되었다”가 정확하다.
- 로컬 SQLite는 문제 생성·수정·검수용 원본(authoring source)으로 계속 사용한다.
- PostgreSQL은 검수가 끝난 문항과 발행 세트를 서비스에서 읽기 위한 문항 은행(production/read model)이다.
- 현재 흐름은 **SQLite → 로컬 PostgreSQL → 운영 Supabase PostgreSQL 단방향 수동 동기화**다. 반대 방향 자동 역동기화는 없다.
- 문항 자체와 문항 버전, 모델 정보, 원본 추적 정보, 세트 및 세트 버전이 모두 PostgreSQL에 보존된다.
- 동일 문항을 수정한 뒤 다시 동기화하면 기존 문항을 덮어쓰지 않고 `item_version`이 증가한다.
- 세트는 포함된 정확한 `(item_id, item_version)` 조합을 저장하므로, 문항이 나중에 수정되어도 과거 세트 내용은 재현할 수 있다.
- 프로덕션 조회의 기본 진입점은 다음 두 뷰다.

  - 최신 문항: `topik_bank.current_items`
  - 모델·영역별 최신 세트 구성: `topik_bank.current_set_contents`

## 2. 현재 실제 데이터 스냅샷

이 수치는 마지막 검증 시 실제 `DATABASE_URL` 대상 PostgreSQL에서 확인한 결과다. 이후 동기화/발행을 수행하면 달라질 수 있다.

| 객체 | 현재 행 수 | 의미 |
| --- | ---: | --- |
| `topik_bank.items` | 386 | 논리 문항 ID 수 |
| `topik_bank.item_versions` | 386 | 저장된 전체 문항 버전 수 |
| `topik_bank.question_sets` | 6 | 모델·영역·세트 번호로 만든 논리 세트 수 |
| `topik_bank.question_set_versions` | 6 | 저장된 전체 세트 버전 수 |
| `topik_bank.question_set_items` | 300 | 세트에 고정된 문항 버전 연결 수(6세트 × 50문항) |
| `topik_bank.current_items` | 386 | 논리 문항별 최신 버전 수 |
| `topik_bank.current_set_contents` | 300 | 논리 세트별 최신 버전의 문항 수 |

적용된 마이그레이션:

| 마이그레이션 | 실제 적용 시각(KST) | 역할 |
| --- | --- | --- |
| `001_question_bank` | `2026-08-11 03:31:53.851261+09:00` | 문항·세트·버전 테이블과 기본 인덱스 생성 |
| `002_complete_item_bank` | `2026-08-11 04:21:08.703536+09:00` | `type_slot`, 추가 인덱스, 최신 문항/세트 뷰 추가 |
| `003_multi_question_sets` | `2026-08-12 06:33:11.951543+09:00` | 모델·영역별 독립 세트 번호와 다중 세트 제약 추가 |

`004_postgres_deployments`는 코드에 추가되어 있으나 마지막 데이터 확인 시 로컬 DB에는 아직
적용하지 않았다. 앱의 `원본·운영 배포 스키마 준비`를 명시적으로 눌렀을 때 양쪽 DB에 적용된다.

현재 최신 문항의 모델·영역별 분포:

| 영역 | 정확한 모델 버전(`generator_version`) | 문항 수 |
| --- | --- | ---: |
| 듣기 | `deepseek-v4-pro` | 50 |
| 듣기 | `gpt-5.6-luna` | 50 |
| 읽기 | `claude-haiku-4-5-20251001` | 26 |
| 읽기 | `deepseek-v4-flash` | 26 |
| 읽기 | `deepseek-v4-pro` | 104 |
| 읽기 | `gemini-3.5-flash` | 26 |
| 읽기 | `gpt-5.6-luna` | 104 |

현재 완성된 최신 50문항 세트:

| 영역 | 모델 | 세트 번호 | 문항 수 |
| --- | --- | ---: | ---: |
| 듣기 | DeepSeek Pro (`deepseek-v4-pro`) | 1 | 50 |
| 듣기 | GPT Luna (`gpt-5.6-luna`) | 1 | 50 |
| 읽기 | DeepSeek Pro (`deepseek-v4-pro`) | 1 | 50 |
| 읽기 | DeepSeek Pro (`deepseek-v4-pro`) | 2 | 50 |
| 읽기 | GPT Luna (`gpt-5.6-luna`) | 1 | 50 |
| 읽기 | GPT Luna (`gpt-5.6-luna`) | 2 | 50 |

마지막 이관 대조 결과:

- 최신: 386개
- 발행 제외: 9개
- 과거·현재 세트에 포함: 300개
- 승인되었지만 미동기화된 문항: 0개

“발행 제외”는 PostgreSQL 저장 실패가 아니다. 로컬 문항이 승인 조건 또는 유효성 조건을 충족하지 않아 발행 후보에서 제외되었고 PostgreSQL에도 아직 없는 상태다.

## 3. 전체 아키텍처

```mermaid
flowchart LR
    A["문제 생성 및 수정"] --> B["로컬 SQLite"]
    B --> C["검수 및 유효성 검사"]
    C --> D["문항 동기화"]
    C --> E["1~50번 세트 선택"]
    D --> F["로컬 PostgreSQL topik_bank"]
    E --> G["50문항 세트 발행"]
    G --> F
    F --> K["운영 Supabase 원자적 배포"]
    K --> L["운영 topik_bank"]
    L --> H["current_items"]
    L --> I["current_set_contents"]
    H --> J["프로덕션 서비스"]
    I --> J
```

### 저장소별 책임

| 저장소 | 책임 | 프로덕션 서비스가 직접 읽는가? |
| --- | --- | --- |
| 로컬 SQLite (`data/.../*.db`) | 생성 실행, 원본/수정본, 검수 결과, 프롬프트, 유효성 검사 기록 | 아니요 |
| PostgreSQL `topik_bank` | 승인 문항, 불변 문항 버전, 모델 메타데이터, 불변 세트 버전, 운영 조회 | 예 |
| SQL 마이그레이션 파일 | PostgreSQL 구조를 재현·변경하는 스키마 정의 | 데이터 자체가 아님 |

### 핵심 관계

```mermaid
erDiagram
    ITEMS ||--o{ ITEM_VERSIONS : "has versions"
    QUESTION_SETS ||--o{ QUESTION_SET_VERSIONS : "has versions"
    QUESTION_SET_VERSIONS ||--o{ QUESTION_SET_ITEMS : "contains positions"
    ITEM_VERSIONS ||--o{ QUESTION_SET_ITEMS : "pinned by"

    ITEMS {
        uuid item_id PK
        text source_key UK
    }
    ITEM_VERSIONS {
        uuid item_id PK_FK
        int item_version PK
        text section
        smallint type_slot
        text generator_version
        jsonb content_json
        char content_hash UK
    }
    QUESTION_SETS {
        uuid set_id PK
        text section
        text generator_provider
        text generator_model
        text generator_version
    }
    QUESTION_SET_VERSIONS {
        uuid set_id PK_FK
        int set_version PK
        char set_fingerprint UK
    }
    QUESTION_SET_ITEMS {
        uuid set_id PK_FK
        int set_version PK_FK
        smallint position PK
        uuid item_id FK
        int item_version FK
    }
```

## 4. PostgreSQL 객체 목록

모든 문항 은행 객체는 기본 `public`이 아니라 별도 스키마 `topik_bank` 아래에 있다.

테이블:

1. `topik_bank.schema_migrations`
2. `topik_bank.items`
3. `topik_bank.item_versions`
4. `topik_bank.question_sets`
5. `topik_bank.question_set_versions`
6. `topik_bank.question_set_items`
7. `topik_bank.deployment_runs`
8. `topik_bank.deployment_run_sets`

뷰:

1. `topik_bank.current_items`
2. `topik_bank.current_set_contents`

## 5. 테이블 상세

### 5.1 `schema_migrations`

이미 적용된 SQL 마이그레이션을 기록한다. 앱의 `ensure_schema()`가 파일명 순서대로 아직 적용되지 않은 마이그레이션만 실행한다.

| 컬럼 | 형식 | NULL | 제약/기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `version` | `TEXT` | 불가 | 기본 키 | 마이그레이션 파일의 stem. 예: `001_question_bank` |
| `applied_at` | `TIMESTAMPTZ` | 불가 | `CURRENT_TIMESTAMP` | 적용 시각 |

중요: 운영 DB에 이미 적용된 마이그레이션 파일은 수정하지 않는다. 구조를 변경하려면 `003_...sql`처럼 다음 번호 파일을 추가한다.

### 5.2 `items`

문항의 변하지 않는 논리적 정체성만 저장한다. 실제 문제 내용은 `item_versions`에 저장된다.

| 컬럼 | 형식 | NULL | 제약/기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `item_id` | `UUID` | 불가 | 기본 키 | 문항의 영구 ID |
| `source_key` | `TEXT` | 불가 | 유일 | 로컬 SQLite 문항과 연결하는 안정적인 키 |
| `created_at` | `TIMESTAMPTZ` | 불가 | `CURRENT_TIMESTAMP` | 논리 문항 최초 생성 시각 |

`source_key` 형식:

```text
{section}:{question_type}:{generated_question_id}
```

예:

```text
reading:content_match:1
listening:dialogue_response:42
```

`item_id`는 코드의 고정 네임스페이스와 `item:{source_key}`를 사용한 UUIDv5로 결정적으로 생성된다. 같은 `source_key`는 다음 세션이나 다른 실행에서도 같은 `item_id`를 만든다.

주의: 현재 `source_key`에는 모델명이 포함되지 않는다. 로컬 `generated_questions.id`가 해당 문항 유형 DB 안에서 문항을 안정적으로 유일하게 식별한다는 전제다. 데이터 저장 구조를 바꿀 때 이 전제를 반드시 확인해야 한다.

### 5.3 `item_versions`

문항의 실제 내용과 세부 메타데이터를 불변 버전 단위로 저장하는 핵심 테이블이다. 기본 키는 `(item_id, item_version)`이다.

| 컬럼 | 형식 | NULL | 제약/기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `item_id` | `UUID` | 불가 | `items(item_id)` FK, 복합 PK | 논리 문항 ID |
| `item_version` | `INTEGER` | 불가 | 1 이상, 복합 PK | 문항 버전 |
| `section` | `TEXT` | 불가 | `reading`, `listening`, `writing` | 영역 |
| `type_slot` | `SMALLINT` | 불가 | 1~50 | TOPIK 세트 내 문제 번호/슬롯 |
| `item_type` | `TEXT` | 불가 |  | 빈칸, 내용 일치, 문장 배열 등에 대응하는 내부 유형 키 |
| `primary_skill` | `TEXT` | 불가 | 공백 문자열 금지 | 대표 문법·어휘·추론/듣기 skill |
| `target_level` | `SMALLINT` | 불가 | 1~6 | 예상 TOPIK 급수 |
| `predicted_difficulty` | `DOUBLE PRECISION` | 불가 | -3.0~3.0 | 생성/검수 단계의 초기 예상 난이도 |
| `irt_difficulty` | `DOUBLE PRECISION` | 가능 |  | 응답 데이터로 추정할 IRT 난이도. 현재 `NULL` |
| `irt_discrimination` | `DOUBLE PRECISION` | 가능 | `NULL` 또는 0 초과 | 문항 변별도. 현재 `NULL` |
| `stem_length` | `INTEGER` | 불가 | 0 이상 | 정규화한 표시용 지문/문제 문자열의 문자 길이 |
| `choice_count` | `INTEGER` | 불가 | 0 이상 | 인식된 선택지 개수 |
| `generator_provider` | `TEXT` | 불가 |  | 실제 호출 백엔드. 예: `chatkhu`, `deepseek` |
| `generator_model` | `TEXT` | 불가 |  | 앱 내부 provider/model 선택 키. 예: `gpt_5_6_luna` |
| `generator_version` | `TEXT` | 불가 |  | 정확한 API 모델 ID. 예: `gpt-5.6-luna` |
| `prompt_version` | `CHAR(64)` | 불가 |  | 시스템+사용자 프롬프트 JSON의 SHA-256 |
| `review_status` | `TEXT` | 불가 | 기본 `reviewed`; `reviewed`, `pilot`, `active`, `retired` | 문항 생명주기 상태 |
| `stem` | `TEXT` | 불가 |  | 프로덕션에서 바로 표시할 수 있도록 조합한 지문/질문 |
| `choices` | `JSONB` | 불가 |  | 표시용 선택지 문자열 배열 |
| `correct_answer` | `SMALLINT` | 가능 |  | 정답 번호. 문항 종류에 따라 없을 수 있음 |
| `explanation` | `TEXT` | 불가 | 기본 빈 문자열 | 해설 |
| `content_json` | `JSONB` | 불가 |  | 로컬에서 확정된 원래 문제 구조 전체 |
| `source_provenance` | `JSONB` | 불가 |  | 원본 SQLite와 생성 실행 추적 정보 |
| `content_hash` | `CHAR(64)` | 불가 | `(item_id, content_hash)` 유일 | 내용·메타데이터 동일성 판정 SHA-256 |
| `created_at` | `TIMESTAMPTZ` | 불가 | `CURRENT_TIMESTAMP` | 해당 문항 버전이 PostgreSQL에 만들어진 시각 |

#### 모델 관련 세 컬럼

| 컬럼 | 질문에 답하는 내용 |
| --- | --- |
| `generator_provider` | 어느 호출 백엔드를 통했는가? |
| `generator_model` | 앱에서 어떤 provider/model 키로 선택했는가? |
| `generator_version` | 실제 생성에 사용된 정확한 모델 버전 문자열은 무엇인가? |

모델별 운영 조회 및 세트 구분에는 세 컬럼을 함께 보는 것이 가장 안전하다. 사용자 표시에는 일반적으로 `generator_version`이 가장 이해하기 쉽다.

#### `stem`과 `content_json`

- `stem`: 프로덕션 목록/화면에서 바로 쓰기 좋은 평면 문자열이다.
- `content_json`: 문항 유형별 구조를 잃지 않은 전체 JSON이다.
- 읽기 `stem`은 `auxiliary_text`, `passage`, `question_prompt`를 순서대로 조합한다.
- 듣기 `stem`은 대화 턴과 질문 문구를 조합한다.
- 구조적 렌더링은 `content_json`, 검색/미리보기는 `stem`을 우선한다.

#### `choices`와 보기 인식

- 일반 문항은 `content_json.choices`에서 표시 가능한 문자열을 가져온다.
- 시각 선택지 문항은 일반 `choices`가 없을 때 `visual_options[].description`을 보기로 사용한다.
- `choice_count`는 최종 인식된 표시용 보기의 수다.

#### `source_provenance` 예시

```json
{
  "source_key": "reading:content_match:1",
  "source_db": "data/types/content_match.db",
  "generated_question_id": 1,
  "run_id": 12,
  "created_at": "로컬 생성 시각",
  "generation_preset": "precise_generation",
  "request_parameters": {
    "api_parameters_applied": true,
    "reasoning_effort": "high",
    "max_completion_tokens": 32768,
    "response_format": {"type": "json_object"}
  }
}
```

이 값으로 PostgreSQL 문항이 어느 SQLite 파일의 어느 생성 행에서 왔는지, 어떤 생성 프리셋과 실제 비밀값 없는 요청 파라미터를 사용했는지 추적한다. 과거 실행은 두 새 필드가 `null` 또는 빈 객체일 수 있다. 웹 수동 생성은 `request_parameters.api_parameters_applied=false`다. API 키는 provenance에 포함하지 않는다.

#### `content_hash`에 포함되는 의미 정보

- 영역, 유형
- 대표 skill, 목표 급수, 예상 난이도
- 모델 backend/key/version
- 프롬프트 버전
- 검수 상태
- 표시용 stem/choices
- 정답과 해설
- 전체 문제 JSON

문제 본문뿐 아니라 운영에 영향을 주는 메타데이터가 바뀌어도 새 문항 버전이 생성된다. 같은 payload를 다시 동기화하면 기존 버전을 재사용한다.

#### 인덱스

| 인덱스 | 컬럼 | 목적 |
| --- | --- | --- |
| 기본 키 인덱스 | `(item_id, item_version)` | 정확한 문항 버전 조회 |
| unique 인덱스 | `(item_id, content_hash)` | 동일 내용 중복 버전 방지 |
| `item_versions_lookup_idx` | `(section, item_type, target_level, review_status)` | 서비스 조건 검색 |
| `item_versions_generator_idx` | `(generator_provider, generator_version)` | 모델별 검색 |
| `item_versions_slot_idx` | `(section, generator_version, type_slot)` | 영역·모델·번호별 검색 |

### 5.4 `question_sets`

“한 모델이 만든 한 영역의 문제 묶음”이라는 논리 세트 정체성을 저장한다.

| 컬럼 | 형식 | NULL | 제약/기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `set_id` | `UUID` | 불가 | 기본 키 | 논리 세트 ID |
| `section` | `TEXT` | 불가 | `reading`, `listening`, `writing` | 세트 영역 |
| `generator_provider` | `TEXT` | 불가 |  | 실제 백엔드 |
| `generator_model` | `TEXT` | 불가 |  | 앱 내부 모델 키 |
| `generator_version` | `TEXT` | 불가 |  | 정확한 모델 버전 |
| `set_sequence` | `INTEGER` | 불가 | 1 이상 | 같은 모델·영역 안의 독립 세트 번호 |
| `created_at` | `TIMESTAMPTZ` | 불가 | `CURRENT_TIMESTAMP` | 논리 세트 최초 생성 시각 |

유일 제약은 `(section, generator_provider, generator_model, generator_version, set_sequence)`이다.

`set_id`도 고정 네임스페이스에 다음 문자열을 넣은 UUIDv5로 생성한다.

```text
set:{section}:{backend}:{provider_key}:{model_id}
```

위 형식은 기존 호환성을 위해 세트 #1에 그대로 사용한다. 세트 #2 이상은 다음 형식이다.

```text
set:{section}:{backend}:{provider_key}:{model_id}:sequence:{set_sequence}
```

미사용 1~50번이 다시 완성되면 같은 모델·영역 안에서 `set_sequence`를 증가시키고 별도 `set_id`를 만든다. 기존 세트를 덮어쓰지 않는다.

### 5.5 `question_set_versions`

세트의 발행 스냅샷을 버전별로 저장한다.

| 컬럼 | 형식 | NULL | 제약/기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `set_id` | `UUID` | 불가 | `question_sets(set_id)` FK, 복합 PK | 논리 세트 ID |
| `set_version` | `INTEGER` | 불가 | 1 이상, 복합 PK | 세트 버전 |
| `review_status` | `TEXT` | 불가 | 기본 `reviewed`; 허용 상태 4종 | 세트 생명주기 상태 |
| `default_target_level` | `SMALLINT` | 불가 | 1~6 | 세트 생성 시 사용한 기본 목표 급수 |
| `default_predicted_difficulty` | `DOUBLE PRECISION` | 불가 | -3.0~3.0 | 세트 생성 시 사용한 기본 예상 난이도 |
| `set_fingerprint` | `CHAR(64)` | 불가 | `(set_id, set_fingerprint)` 유일 | 정확한 세트 내용의 SHA-256 |
| `published_at` | `TIMESTAMPTZ` | 불가 | `CURRENT_TIMESTAMP` | 세트 버전 발행 시각 |

`set_fingerprint`에는 다음 값이 들어간다.

- 1번부터 50번까지 순서가 보존된 `(item_id, item_version)` 목록
- 세트 기본 목표 급수
- 세트 기본 예상 난이도
- 세트 검수 상태(`reviewed`)

현재 새 세트 발행 UI는 독립 세트를 `set_version=1`로 만든다. 정확히 같은 50문항·순서의 네트워크 재시도는 기존 세트 버전을 반환한다. 버전 테이블은 기존 데이터 호환성과 향후 세트 수정 이력을 위해 유지한다.

### 5.6 `question_set_items`

특정 세트 버전의 1~50번 위치에 정확한 문항 버전을 고정한다.

| 컬럼 | 형식 | NULL | 제약/기본값 | 설명 |
| --- | --- | --- | --- | --- |
| `set_id` | `UUID` | 불가 | 복합 FK/PK | 논리 세트 ID |
| `set_version` | `INTEGER` | 불가 | 복합 FK/PK | 세트 버전 |
| `position` | `SMALLINT` | 불가 | 1~50, 복합 PK | 시험지 내 위치 |
| `item_id` | `UUID` | 불가 | `item_versions` 복합 FK | 문항 ID |
| `item_version` | `INTEGER` | 불가 | `item_versions` 복합 FK | 세트가 사용하는 정확한 문항 버전 |

기본 키는 `(set_id, set_version, position)`, 추가 유일 제약은 `(set_id, set_version, item_id)`이다. 한 위치에는 한 문항만 올 수 있고 동일 논리 문항이 같은 세트에 두 번 들어갈 수도 없다.

DB의 `position BETWEEN 1 AND 50`만으로는 “정확히 50행”을 강제하지 못한다. 정확히 1~50번이 하나씩 존재하는지는 앱의 `validate_complete_selection()`이 트랜잭션 전에 검사한다.

### 5.7 운영 배포 감사 테이블

`deployment_runs`는 로컬 PostgreSQL에서 운영 Supabase로 실행한 배포 한 번을 기록한다.
`deployment_run_sets`는 그 실행에 포함된 논리 세트와 원본 스냅샷 해시를 기록한다. 구조
일관성을 위해 두 DB 모두 테이블을 갖지만 앱은 로컬 원본 DB에만 이력을 쓴다.

| 테이블 | 필드 | 설명 |
| --- | --- | --- |
| `deployment_runs` | `run_id` | 배포 실행 UUID |
|  | `target_fingerprint` | 비밀번호를 제외한 대상 연결 식별 해시 |
|  | `target_label` | 비밀번호 없는 호스트/DB 표시 |
|  | `source_snapshot_hash` | 선택한 모든 세트의 원본 의미상 해시 |
|  | `status` | `running`, `succeeded`, `rolled_back`, `outcome_unknown` |
|  | `requested_set_count` | 사용자가 선택한 세트 수 |
|  | `transferred_set_count` / `reused_set_count` | 신규 반영/이미 동일한 세트 수 |
|  | `created_row_count` / `reused_row_count` | 전체 의존성 행 처리 수 |
|  | `error_message` | 접속 비밀을 제거한 오류 또는 재확인 메시지 |
| `deployment_run_sets` | `set_id`, `set_sequence` | 실행에 포함된 논리 세트 |
|  | `source_snapshot_hash` | 해당 세트만의 의미상 해시 |
|  | `status`, `detail` | 세트 단위 감사 상태와 설명 |

성공한 운영 데이터를 삭제하는 수동 롤백은 제공하지 않는다. 실패 시 대상 트랜잭션을
자동 롤백하고, 네트워크 단절로 커밋 결과가 불분명한 경우 재조회 결과에 따라 상태를
결정한다.

## 6. 뷰 상세

### 6.1 `current_items`

각 `item_id`에서 가장 큰 `item_version` 하나만 반환한다. 구현은 `DISTINCT ON (item_id)`와 `ORDER BY item_id, item_version DESC`를 사용한다. `source_key`, 논리 문항 생성 시각, 최신 `item_versions`의 모든 컬럼을 제공한다.

주요 용도:

- 프로덕션 최신 문항 목록
- 모델·영역·유형·상태별 필터링
- 로컬 SQLite와 PostgreSQL 이관 상태 대조
- 최신 JSON과 메타데이터 확인

과거 이력은 이 뷰가 아니라 `item_versions`를 직접 조회한다.

### 6.2 `current_set_contents`

각 `set_id`에서 가장 큰 `set_version`을 고른 뒤 그 세트에 고정된 정확한 문항 버전을 위치 순서와 함께 반환한다.

주요 컬럼:

- 세트: `set_id`, `set_sequence`, `set_version`, `set_section`
- 모델: `set_generator_provider`, `set_generator_model`, `set_generator_version`
- 상태/기본값: `set_review_status`, `default_target_level`, `default_predicted_difficulty`, `published_at`
- 연결: `position`, `source_key`, `item_id`, `item_version`
- 문항: `type_slot`, `item_type`, skill, 난이도/IRT/상태, `stem`, `choices`, 정답, 해설, 전체 JSON, 원본 추적 정보

중요: 이 뷰는 각 문항의 현재 최신 버전을 임의로 붙이지 않는다. 최신 세트 버전이 발행될 때 고정한 `item_version`을 정확히 반환한다. 문항 v2가 나중에 생겨도 v1을 포함한 과거 세트는 재현된다.

## 7. 문항 동기화와 버전 규칙

### 신규 문항

1. `source_key`로 `items`를 찾는다.
2. 없다면 결정적 UUIDv5 `item_id`로 `items` 행을 만든다.
3. `content_hash`가 같은 버전이 있는지 확인한다.
4. 없으므로 `item_version = 1`로 삽입한다.

### 변경 없는 문항 재동기화

1. 같은 `source_key`로 같은 `item_id`를 찾는다.
2. 같은 `content_hash`의 기존 버전을 찾는다.
3. 새 행을 만들지 않고 기존 버전을 재사용한다.

동기화 버튼을 다시 눌러도 동일 데이터가 무한히 복제되지 않는다.

### 수정된 문항 재동기화

1. 같은 `source_key`이므로 `item_id`는 유지된다.
2. 문제/메타데이터가 달라 `content_hash`가 바뀐다.
3. 해당 문항의 `MAX(item_version) + 1`로 새 버전을 추가한다.
4. 과거 문항 버전과 과거 세트 연결은 삭제하거나 덮어쓰지 않는다.

### 새 세트 발행

1. 선택한 50개 `source_key`를 모든 과거 세트 버전과 대조한다.
2. 정확히 같은 50문항·순서의 재시도면 기존 세트 영수증을 반환한다.
3. 일부라도 이미 사용됐다면 전체 작업을 거부하고 롤백한다.
4. 겹침이 없으면 문항 버전을 생성 또는 재사용한다.
5. 모델·영역의 `MAX(set_sequence) + 1`과 결정적 UUIDv5로 별도 세트를 만들고 `set_version=1`로 연결한다.

## 8. 트랜잭션과 동시성

- `publish_items()`와 `publish_set()`은 각각 하나의 연결/트랜잭션 안에서 전체 작업을 처리한다.
- 중간 오류 시 전체 트랜잭션이 롤백되어 일부만 저장되는 상황을 방지한다.
- 문항 일괄 동기화는 `topik_bank:item_sync` advisory transaction lock을 사용한다.
- 새 세트 발행은 전체 `source_key` 중복 검사를 직렬화하는 `topik_bank:set_membership` advisory transaction lock을 사용한다.
- 세트 발행은 영역·백엔드·provider key·model ID 조합의 advisory transaction lock을 사용한다.
- 같은 대상을 동시에 발행할 때 버전 번호 충돌 가능성을 줄인다.
- 세트 연결 대량 삽입은 psycopg 3의 `connection`이 아니라 `cursor.executemany()`로 수행한다.

## 9. 앱 운영 절차

사이드바의 `PostgreSQL 발행` 화면에는 다음 흐름이 있다.

### 9.1 연결 및 스키마 준비

1. `.env`의 `DATABASE_URL`을 읽는다.
2. PostgreSQL 연결을 확인한다.
3. `ensure_schema()`가 `topik_bank`와 마이그레이션 기록 테이블을 준비한다.
4. `topik_question_lab/migrations/*.sql`을 이름순으로 확인해 미적용 파일만 실행한다.

### 9.2 `문항 동기화`

- 승인 및 유효성 조건을 통과한 문항을 모델·영역별로 선택한다.
- 50문항 세트가 완성되지 않아도 개별 문항을 저장한다.
- `target_level`, `predicted_difficulty`, `primary_skill`을 확인/편집한다.
- 처음 기본값은 목표 급수 4, 예상 난이도 0.0이다.
- `primary_skill`은 읽기의 `target_grammar` 또는 듣기의 `target_skill`에서 기본 추출한다.

### 9.3 `50문항 세트`

- 동일 영역·동일 모델의 문항만 한 세트에 포함한다.
- 모든 과거 세트에 한 번이라도 포함된 `source_key`는 수정 여부와 관계없이 후보에서 제외한다.
- 개별 문항 동기화만 된 문항은 계속 새 세트 후보로 사용할 수 있다.
- 슬롯 1~50이 각각 정확히 하나여야 발행할 수 있다.
- 세트 발행 시 개별 문항도 함께 생성/재사용한다.
- 같은 모델·영역의 다음 `set_sequence`를 자동으로 사용하며 기존 세트와 별도 `set_id`를 만든다.
- 성공 시 `set_id`, `set_sequence`, `set_version`, 생성/재사용 문항 버전 수를 반환한다.

### 9.4 `이관 현황`

로컬 SQLite와 PostgreSQL 최신 문항을 `source_key`로 비교한다.

| 상태 | 의미 | 보통의 조치 |
| --- | --- | --- |
| `미발행` | 로컬 승인 문항은 있으나 PostgreSQL에 없음 | 문항 동기화 |
| `최신` | 로컬 확정 JSON과 PostgreSQL `content_json`이 같음 | 조치 없음 |
| `수정 후 미동기` | 로컬 문항이 발행 후 변경됨 | 다시 동기화하여 새 버전 생성 |
| `발행 후 승인 취소/오류` | PostgreSQL에는 있지만 현재 로컬 발행 조건을 통과하지 않음 | 원인 검토 및 상태 정책 결정 |
| `로컬 없음` | PostgreSQL에는 있지만 해당 로컬 원본을 찾을 수 없음 | 삭제하지 말고 원본 이동/삭제 조사 |
| `발행 제외` | 로컬에 있으나 승인/유효성 미충족이고 PG에도 없음 | 로컬 검수/오류 수정 |

`in_set` 값으로 해당 `source_key`가 최신·과거를 포함한 어떤 세트 버전에든 포함된 적이 있는지 확인한다.

주의: 대조에서 “최신”은 현재 로컬 문제 JSON과 PostgreSQL `content_json`을 비교한다. 메타데이터만 달라진 경우 정확한 버전 판단은 `content_hash` 및 동기화 결과도 함께 확인한다.

### 9.5 `발행 이력`

- 최근 세트 버전, 발행 시각, 상태, 문항 수를 보여 준다.
- 과거 세트 버전도 테이블에 남아 있으므로 직접 SQL로 이력을 조회할 수 있다.

### 9.6 `운영 Supabase 배포`

1. `DATABASE_URL`과 `PRODUCTION_DATABASE_URL`의 연결·마이그레이션·행 수를 별도로 확인한다.
2. 양쪽 DB의 모든 논리 세트와 정확한 의존성을 의미상 비교한다.
3. `운영 미반영` 또는 충돌 없는 `부분 연동` 세트만 선택한다.
4. 사전 검증에서 신규/재사용 행 수와 원본 스냅샷 해시를 확인한다.
5. 확인 문구 `운영 배포`를 입력한 뒤 선택 세트를 한 트랜잭션으로 전송한다.

| 상태 | 의미 | 배포 가능 |
| --- | --- | --- |
| `연동 완료` | 세트, 모든 버전, 문항 버전, 1~50번 순서가 동일 | 불필요 |
| `운영 미반영` | 운영에 논리 세트 행이 없음 | 예 |
| `부분 연동` | 운영에 있는 행은 동일하고 일부 의존성만 없음 | 예 |
| `충돌` | 동일 키/identity에 다른 내용·버전·순서가 존재 | 아니요 |
| `운영에만 존재` | 로컬 원본에 없는 세트가 운영에 존재 | 아니요 |

비교는 생성·발행 시각을 제외한다. ID, `source_key`, 전체 문항 메타데이터와 JSON,
`content_hash`, 세트 fingerprint, 버전과 위치는 모두 비교한다. 동일 키의 다른 데이터는
자동 덮어쓰지 않는다.

실제 배포는 원본의 읽기 전용 `REPEATABLE READ` 스냅샷과 대상의
`topik_bank:production_deployment` advisory transaction lock을 사용한다. 대상 삽입 순서는
`items → item_versions → question_sets → question_set_versions → question_set_items`이며,
커밋 전에 원본과 다시 비교한다. 선택 세트 하나라도 검증에 실패하면 전체 대상 트랜잭션이
롤백된다.

## 10. 자주 사용하는 검증 SQL

### 10.1 전체 행 수

```sql
SELECT 'items' AS object_name, COUNT(*) AS row_count FROM topik_bank.items
UNION ALL SELECT 'item_versions', COUNT(*) FROM topik_bank.item_versions
UNION ALL SELECT 'question_sets', COUNT(*) FROM topik_bank.question_sets
UNION ALL SELECT 'question_set_versions', COUNT(*) FROM topik_bank.question_set_versions
UNION ALL SELECT 'question_set_items', COUNT(*) FROM topik_bank.question_set_items
UNION ALL SELECT 'current_items', COUNT(*) FROM topik_bank.current_items
UNION ALL SELECT 'current_set_contents', COUNT(*) FROM topik_bank.current_set_contents;
```

### 10.2 모델별 최신 문항 수

```sql
SELECT section, generator_provider, generator_model, generator_version,
       COUNT(*) AS item_count
FROM topik_bank.current_items
GROUP BY section, generator_provider, generator_model, generator_version
ORDER BY section, generator_version;
```

### 10.3 특정 모델·영역 최신 문항

```sql
SELECT source_key, item_id, item_version, type_slot, item_type,
       primary_skill, review_status, stem
FROM topik_bank.current_items
WHERE section = 'reading'
  AND generator_version = 'gpt-5.6-luna'
ORDER BY type_slot, source_key;
```

### 10.4 특정 문항 전체 JSON과 원본 위치

```sql
SELECT source_key, item_id, item_version, content_json, source_provenance
FROM topik_bank.current_items
WHERE source_key = 'reading:content_match:1';
```

JSON 일부만 확인:

```sql
SELECT source_key,
       content_json ->> 'question_prompt' AS question_prompt,
       content_json -> 'choices' AS original_choices,
       choices AS display_choices,
       source_provenance ->> 'source_db' AS source_db,
       source_provenance ->> 'generated_question_id' AS generated_question_id,
       source_provenance ->> 'generation_preset' AS generation_preset,
       source_provenance -> 'request_parameters' AS request_parameters
FROM topik_bank.current_items
WHERE source_key = 'reading:content_match:1';
```

### 10.4.1 프리셋별 발행 문항과 API 적용 여부

```sql
SELECT section,
       generator_version,
       source_provenance ->> 'generation_preset' AS generation_preset,
       source_provenance -> 'request_parameters' ->> 'api_parameters_applied' AS api_parameters_applied,
       COUNT(*) AS item_count
FROM topik_bank.current_items
GROUP BY section, generator_version, generation_preset, api_parameters_applied
ORDER BY section, generator_version, generation_preset;
```

로컬 실행 원장은 읽기 `data/types/<type>.db`, 듣기 `data/listening/types/<type>.db`에 있다. 각 DB의 `settings.generation_presets`는 provider ID를 프리셋 ID에 매핑하고, `runs.result_json.generation_preset`과 `runs.result_json.request_parameters`는 실행 당시 확정값을 보존한다. 기본값은 `precise_generation`이다. DeepSeek thinking 프리셋은 temperature/top_p를 생략하고, GPT 계열은 temperature/top_p 없이 `reasoning_effort`와 `max_completion_tokens`를 사용한다. Claude·Gemini 등 미지원 모델은 기존 기본 API 요청을 유지한다.

### 10.5 특정 문항 전체 버전 이력

```sql
SELECT i.source_key, v.item_version, v.content_hash,
       v.generator_version, v.review_status, v.created_at
FROM topik_bank.items i
JOIN topik_bank.item_versions v ON v.item_id = i.item_id
WHERE i.source_key = 'reading:content_match:1'
ORDER BY v.item_version;
```

### 10.6 최신 세트 목록과 문항 수

```sql
WITH latest AS (
    SELECT set_id, MAX(set_version) AS set_version
    FROM topik_bank.question_set_versions
    GROUP BY set_id
)
SELECT s.set_id, s.set_sequence, s.section, s.generator_provider, s.generator_model,
       s.generator_version, l.set_version, v.review_status,
       v.published_at, COUNT(si.position) AS item_count
FROM topik_bank.question_sets s
JOIN latest l ON l.set_id = s.set_id
JOIN topik_bank.question_set_versions v
  ON v.set_id = l.set_id AND v.set_version = l.set_version
LEFT JOIN topik_bank.question_set_items si
  ON si.set_id = l.set_id AND si.set_version = l.set_version
GROUP BY s.set_id, s.set_sequence, s.section, s.generator_provider, s.generator_model,
         s.generator_version, l.set_version, v.review_status, v.published_at
ORDER BY s.section, s.generator_version, s.set_sequence;
```

### 10.7 특정 모델의 최신 세트 1~50번

```sql
SELECT position, source_key, item_id, item_version, type_slot,
       item_type, stem, choices, correct_answer
FROM topik_bank.current_set_contents
WHERE set_section = 'reading'
  AND set_generator_version = 'gpt-5.6-luna'
  AND set_sequence = 2
ORDER BY position;
```

### 10.8 문항이 들어간 세트 버전 확인

```sql
SELECT s.section, s.generator_version, si.set_id, si.set_version,
       si.position, si.item_version, sv.published_at
FROM topik_bank.items i
JOIN topik_bank.question_set_items si ON si.item_id = i.item_id
JOIN topik_bank.question_sets s ON s.set_id = si.set_id
JOIN topik_bank.question_set_versions sv
  ON sv.set_id = si.set_id AND sv.set_version = si.set_version
WHERE i.source_key = 'reading:content_match:1'
ORDER BY sv.published_at, si.position;
```

### 10.9 세트가 정확히 50문항인지 검사

```sql
SELECT set_id, set_version, COUNT(*) AS item_count,
       MIN(position) AS first_position, MAX(position) AS last_position,
       COUNT(DISTINCT position) AS distinct_positions
FROM topik_bank.question_set_items
GROUP BY set_id, set_version
HAVING COUNT(*) <> 50
    OR MIN(position) <> 1
    OR MAX(position) <> 50
    OR COUNT(DISTINCT position) <> 50;
```

정상이면 결과가 0행이어야 한다.

### 10.10 IRT 보정이 없는 문항

```sql
SELECT section, generator_version, COUNT(*) AS uncalibrated_count
FROM topik_bank.current_items
WHERE irt_difficulty IS NULL OR irt_discrimination IS NULL
GROUP BY section, generator_version
ORDER BY section, generator_version;
```

### 10.11 적용된 마이그레이션

```sql
SELECT version, applied_at
FROM topik_bank.schema_migrations
ORDER BY version;
```

### 10.12 운영 배포와 롤백 이력

```sql
SELECT run_id, target_label, status,
       requested_set_count, transferred_set_count, reused_set_count,
       created_row_count, reused_row_count,
       started_at, completed_at, error_message
FROM topik_bank.deployment_runs
ORDER BY started_at DESC;
```

특정 실행에 포함된 세트:

```sql
SELECT run_id, set_id, set_sequence, source_snapshot_hash, status, detail
FROM topik_bank.deployment_run_sets
WHERE run_id = '배포 실행 UUID'
ORDER BY set_sequence, set_id;
```

## 11. 프로덕션 권장 조회

최신 사용 가능 문항 풀:

```sql
SELECT *
FROM topik_bank.current_items
WHERE review_status IN ('reviewed', 'pilot', 'active');
```

발행된 최신 세트:

```sql
SELECT *
FROM topik_bank.current_set_contents
WHERE set_id = $1
ORDER BY position;
```

모델과 영역으로 조회:

```sql
SELECT *
FROM topik_bank.current_set_contents
WHERE set_section = $1
  AND set_generator_version = $2
ORDER BY position;
```

애플리케이션 SQL에서는 문자열을 직접 이어 붙이지 말고 드라이버의 파라미터 바인딩을 사용한다.

### 최신 문항과 세트 문항을 혼동하지 말 것

- 최신 개별 문항 풀: `current_items`
- 재현 가능한 발행 세트: `current_set_contents`
- 세트 조회 후 `item_id`만으로 `current_items`와 다시 조인하면 발행 당시 버전이 최신 버전으로 바뀔 수 있으므로 주의한다.

## 12. 검수 상태 정책

| 상태 | 의도 |
| --- | --- |
| `reviewed` | 사람이 검수했고 발행 저장된 기본 상태 |
| `pilot` | 파일럿 응답 수집 단계 |
| `active` | 실제 서비스에 활성화 |
| `retired` | 더 이상 신규 출제에 사용하지 않음 |

현재 발행 코드는 새 문항/세트 버전을 `reviewed`로 만든다. `pilot → active → retired`를 관리하는 별도 UI/API는 아직 없다.

문항 내용/메타데이터 변경은 로컬 원본을 수정한 뒤 동기화하여 새 버전을 만드는 것이 기본 정책이다. 상태 전환과 IRT 값 갱신을 어떤 감사 이력으로 남길지는 다음 단계에서 설계해야 한다.

## 13. 삭제 및 원본 변경 정책

- 로컬 승인이 취소되거나 SQLite에서 문항이 사라져도 PostgreSQL 행은 자동 삭제하지 않는다.
- 과거 세트 재현성과 감사 추적을 보존하기 위해서다.
- `로컬 없음`은 즉시 삭제 신호가 아니라 조사 신호다.
- 운영 제외는 물리 삭제보다 `retired` 전환을 우선한다.
- 세트 FK와 이력에 영향을 주므로 별도 보존 정책 없이 PostgreSQL 문항을 물리 삭제하지 않는다.

## 14. 마이그레이션 운영

현재 파일:

```text
topik_question_lab/migrations/
├── 001_question_bank.sql
├── 002_complete_item_bank.sql
├── 003_multi_question_sets.sql
└── 004_postgres_deployments.sql
```

새 구조 변경 절차:

1. 이미 적용된 `001`, `002`를 수정하지 않는다.
2. 예: `003_add_response_observations.sql`을 추가한다.
3. 가능한 한 `IF NOT EXISTS`와 명시적 제약 이름을 사용한다.
4. 테스트 PostgreSQL에서 기존 데이터가 있는 상태로 적용한다.
5. `PostgresQuestionBank.ensure_schema()`를 호출한다.
6. `schema_migrations`에서 새 버전을 확인한다.
7. 테이블/뷰/인덱스와 기존 조회를 통합 테스트한다.

`ensure_schema()`는 적용 여부를 버전 문자열로 판단한다. `migration_checksums()`는 파일 SHA-256을 계산할 수 있지만 DB에 checksum을 저장하거나 적용된 파일 변경을 자동 거부하지는 않는다. 적용된 파일을 수정하지 않는 규칙이 중요하다.

## 15. 연결 및 보안

필수 환경 변수:

```dotenv
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE
PRODUCTION_DATABASE_URL=postgresql://USER:PASSWORD@SUPABASE_HOST:PORT/DATABASE
```

- `DATABASE_URL`: SQLite에서 발행한 6세트가 있는 로컬 원본 PostgreSQL
- `PRODUCTION_DATABASE_URL`: 실제 서비스가 읽는 운영 Supabase PostgreSQL
- 앱은 비밀번호를 제외한 host/port/database/user 해시로 동일 연결을 감지하고 같은 연결이면 배포를 막는다.
- Supabase 직접 연결 또는 session pooler 연결을 사용할 수 있다. 실제 배포 프로세스처럼 지속 연결이 가능한 주소를 우선한다.

실제 값은 문서·커밋·화면 캡처에 넣지 않는다.

현재 코드:

- 오류 메시지의 전체 `DATABASE_URL`을 `[DATABASE_URL]`로 치환한다.
- URL에서 비밀번호를 파싱하면 비밀번호 문자열도 `***`로 가린다.
- 연결 실패가 기존 로컬 문제 생성·검수·출력 기능을 막지는 않는다.

드라이버 요구사항은 `psycopg[binary]>=3.2,<4`, 마지막 확인 버전은 `3.3.4`다.

## 16. 백업과 복원

운영 변경 전 최소한 `topik_bank` 스키마를 백업한다.

```powershell
pg_dump --dbname $env:DATABASE_URL --schema topik_bank --format custom --file topik_bank.backup
```

복원 예시:

```powershell
pg_restore --dbname $env:DATABASE_URL --schema topik_bank topik_bank.backup
```

주의:

- 복원 대상 DB와 기존 객체 충돌 정책을 먼저 정한다.
- 실제 프로덕션 복원은 권한, 소유자, 유지보수 창을 포함해 검증한다.
- 백업에는 문항/정답/해설이 들어 있으므로 민감한 운영 자산으로 취급한다.
- 비밀번호가 파일명/로그/문서에 노출되지 않게 한다.

## 17. 테스트와 검증

전체 테스트:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

실제 PostgreSQL 통합 테스트는 운영 DB가 아닌 별도 테스트 URL을 사용한다.

```powershell
$env:TEST_DATABASE_URL = "postgresql://.../test_database"
.\.venv\Scripts\python.exe -m pytest tests\test_postgres_integration.py -q
```

운영 배포 통합 테스트는 서로 다른 두 테스트 DB를 요구한다.

```powershell
$env:TEST_SOURCE_DATABASE_URL = "postgresql://.../source_test"
$env:TEST_TARGET_DATABASE_URL = "postgresql://.../target_test"
.\.venv\Scripts\python.exe -m pytest tests\test_postgres_deployment_integration.py -q
```

마지막 검증 결과:

- 전체: 116 passed, 2 skipped
- 로컬 실제 스키마 읽기 검증: 6세트, 300개 연결, 자기 비교 6세트 모두 `연동 완료`
- `004_postgres_deployments.sql`: 로컬 PostgreSQL 트랜잭션 안에서 생성 검증 후 롤백 완료
- 두 DB 배포 통합 테스트는 `TEST_SOURCE_DATABASE_URL`, `TEST_TARGET_DATABASE_URL` 미설정으로 skip

문서 변경 검증:

```powershell
git diff --check
```

## 18. 주요 코드 위치

| 파일 | 역할 |
| --- | --- |
| `topik_question_lab/migrations/001_question_bank.sql` | 최초 문항/세트 스키마 |
| `topik_question_lab/migrations/002_complete_item_bank.sql` | `type_slot`, 인덱스, 최신 조회 뷰 |
| `topik_question_lab/migrations/003_multi_question_sets.sql` | 독립 세트 번호, 다중 세트 유일 제약, 세트 조회 뷰 |
| `topik_question_lab/migrations/004_postgres_deployments.sql` | 운영 배포 실행·세트 감사 테이블 |
| `topik_question_lab/publication.py` | SQLite 후보 수집, stem/보기 구성, ID/hash/fingerprint, 50문항 검증, 이관 대조 |
| `topik_question_lab/postgres_storage.py` | 연결, 마이그레이션, 트랜잭션, 문항/세트 upsert, 조회 |
| `topik_question_lab/postgres_deployment.py` | 양쪽 DB 상태, 세트 비교, 원자적 복사, 결과 재확인, 감사 이력 |
| `topik_question_lab/postgres_publish_app.py` | 문항/세트 발행, 이관 현황, 운영 Supabase 배포 UI |
| `topik_question_lab/main.py` | 메인 앱에서 PostgreSQL 화면 연결 |
| `tests/test_publication.py` | 후보/검증/payload/대조 테스트 |
| `tests/test_postgres_integration.py` | 실제 PostgreSQL 통합 테스트 |
| `tests/test_postgres_deployment.py` | 연동 상태·충돌·연결 식별 단위 테스트 |
| `tests/test_postgres_deployment_integration.py` | 두 PostgreSQL 간 전체 롤백·재시도 통합 테스트 |
| `tests/test_app_smoke.py` | 앱 로딩 스모크 테스트 |

## 19. 이전에 해결한 주요 오류

### 읽기 39, 40, 41 GPT Luna가 없다고 표시

로컬 문항의 구조화된 보기가 일반 `choices`와 다른 필드에 있거나 발행 후보 판정이 표시용 보기를 인식하지 못한 경우였다. 일반 `choices`가 없으면 `visual_options[].description`을 표시용 보기로 인식하도록 처리했다.

확인 포인트:

- `content_json.choices`
- `content_json.visual_options`
- PostgreSQL `choices`
- `choice_count`
- 로컬 validation/review 상태

### 빈칸 `( )`이 정확히 한 개가 아니라고 오판

서로 다른 공백, 전각/유니코드 괄호, 분리된 구조 필드, 정규화 전후 차이 때문에 단순 문자열 카운트가 오판할 수 있었다. 발행 후보 수집 전에 해결된 validation 이슈를 폐기하고 빈칸 마커를 정규화한다.

관련 흐름:

- `normalize_question_blank_markers`
- `discard_resolved_validation_issues`
- 현재 확정 문제 JSON 기준 validation 판단

### `'Connection' object has no attribute 'executemany'`

psycopg 3의 `Connection`에 직접 `executemany()`를 호출해 발생했다. 현재는 커서를 사용한다.

```python
with connection.cursor() as cursor:
    cursor.executemany(sql, rows)
```

세트 발행 전체가 한 트랜잭션이므로 오류 시 부분 발행 없이 롤백되며 수정 후 재발행하면 된다.

## 20. 현재 한계와 다음 구현 후보

### 20.1 듣기 음원 자산

`content_json`에는 대화, 프롬프트, 설명, 상대 경로 등이 들어갈 수 있지만 음원 바이너리를 PostgreSQL에 저장하거나 객체 스토리지에 업로드하는 완성된 파이프라인은 없다.

권장:

- 음원은 S3/Cloudflare R2 같은 객체 스토리지에 저장
- PostgreSQL에는 immutable asset ID, object key/URL, checksum, MIME type, duration 저장
- 세트 버전이 정확한 asset version을 고정

### 20.2 IRT 응답 데이터

`irt_difficulty`, `irt_discrimination`은 준비되었지만 응답 로그 수집, 능력 추정, 캘리브레이션 배치, 추정 이력은 없다. 현재 값은 `NULL`이다.

다음 테이블 후보:

- `response_events`
- `item_calibration_runs`
- `item_parameter_estimates`
- 시험 세션/능력 추정 테이블

어떤 `item_version`에 어떤 데이터셋/알고리즘으로 추정했는지 남겨야 한다.

### 20.3 생명주기 전환 UI/API

새 데이터는 `reviewed`로 저장되지만 `pilot`, `active`, `retired` 전환 UI와 감사 로그가 없다. 권한, 전환 시각/사용자/사유, active 세트 정책을 설계해야 한다.

### 20.4 쓰기 영역

DB 제약은 `writing`을 허용하지만 후보 수집은 현재 읽기와 듣기 SQLite 디렉터리 중심이다. 쓰기 문항 스키마가 확정되면 수집/렌더링 규칙을 추가해야 한다.

### 20.5 DB 수준 50문항 보장

앱은 정확히 1~50번을 검사하지만 DB 자체는 한 세트 버전에 50행이 모두 들어왔는지 단독 보장하지 않는다. 외부 writer를 허용한다면 deferred constraint trigger 또는 저장 프로시저를 고려한다.

### 20.6 마이그레이션 checksum

코드는 checksum을 계산하지만 DB에는 버전만 기록한다. 장기 운영에서는 `schema_migrations`에 checksum을 추가해 적용된 파일 변조를 탐지하는 것이 좋다.

## 21. 다음 세션 시작 체크리스트

1. 이 문서를 읽는다.
2. `git status --short`로 기존 사용자 변경을 확인하고 보존한다.
3. `.env`에 `DATABASE_URL`이 있는지만 확인하되 값을 출력하지 않는다.
4. 앱 또는 `PostgresQuestionBank.check_connection()`으로 연결을 확인한다.
5. `schema_migrations`로 적용 버전을 확인한다.
6. 10.1 행 수와 10.2 모델 분포 쿼리를 실행한다.
7. 10.4.1 쿼리로 프리셋과 API 적용 여부가 provenance에 들어왔는지 확인한다.
8. `current_set_contents`에서 최신 세트마다 50행인지 확인한다.
9. 앱 `이관 현황`에서 문제 상태를 확인한다.
10. 구조 변경 전 관련 코드와 마이그레이션을 읽는다.
11. 새 구조는 다음 번호 마이그레이션으로 추가한다.
12. 단위 테스트와 별도 테스트 DB 통합 테스트를 실행한다.
13. 프로덕션 변경 전 백업/복원 가능성을 확인한다.

현재 작업 트리가 깨끗하다고 가정하지 않는다. 특히 기존 사용자 데이터인 `data/types/grammar_blank.db`와 `TOPIK자료화면/` 관련 파일은 임의로 되돌리거나 삭제하지 않는다.

## 22. 빠른 장애 진단

### 연결 실패

1. `DATABASE_URL` 존재 여부 확인(값은 출력하지 않음)
2. 서버/포트/SSL/방화벽 확인
3. 사용자에게 스키마 생성 및 테이블 접근 권한이 있는지 확인
4. psycopg 설치 확인
5. 가려진 오류 메시지의 PostgreSQL 오류 문구 확인

### 문항이 PostgreSQL에 안 보임

1. 로컬 승인 여부 확인
2. 해결되지 않은 validation 확인
3. 모델·영역 필터 확인
4. 앱 `이관 현황` 확인
5. `source_key`로 `current_items` 직접 조회
6. `source_provenance.source_db`와 로컬 SQLite 확인
7. `source_provenance.generation_preset`과 `request_parameters` 확인
8. 수정 후 미동기면 재동기화

### 문항은 있지만 세트에 없음

1. `current_items` 존재 확인
2. 해당 모델·영역의 1~50 슬롯 확인
3. 중복/누락 슬롯 확인
4. `question_set_items`와 `current_set_contents` 조회
5. 완전한 50문항 선택 후 세트 발행

### 재발행인데 세트 버전이 증가하지 않음

50개의 `(item_id, item_version)`, 순서, 세트 기본 급수/난이도, 상태가 모두 같으면 의도된 멱등 동작이다. `created_set_version = false`와 기존 버전 반환이 정상이다.

### 문항 수정인데 새 버전이 생기지 않음

1. 실제 확정/편집 JSON이 후보로 선택되는지 확인
2. 로컬 변경 저장 여부 확인
3. `content_json`, `stem/choices`, 메타데이터 비교
4. `content_hash`와 버전 이력 조회
5. 메타데이터만 바뀌었다면 동기화 영수증과 DB 이력을 함께 확인

## 23. 설계 원칙 요약

- **논리 ID와 내용 버전을 분리한다.**
- **내용을 덮어쓰지 않고 새 버전을 추가한다.**
- **동일 내용 재동기화는 기존 버전을 재사용한다.**
- **세트는 정확한 문항 버전을 고정한다.**
- **모델은 backend/key/exact version을 모두 기록한다.**
- **원본 SQLite 위치와 생성 실행을 추적한다.**
- **로컬 삭제/승인 취소가 과거 PostgreSQL 이력을 자동 파괴하지 않게 한다.**
- **프로덕션은 SQLite가 아니라 PostgreSQL 뷰를 읽는다.**
- **스키마 변경은 순차 마이그레이션으로만 진행한다.**
- **비밀번호와 실제 접속 문자열은 문서·로그·커밋에 남기지 않는다.**
