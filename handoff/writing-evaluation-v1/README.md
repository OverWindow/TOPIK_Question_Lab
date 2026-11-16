# TOPIK II PBT 쓰기 평가 백엔드 전달본 v1

이 패키지는 2026-11-16(Asia/Seoul) 기준 Unigate-PBT-Hub 로컬 작업본의 **현재 51~54번 평가 프롬프트**를 고정한 전달본이다. 문구·배점·출력 스키마를 그대로 추출했다. 과거 실험 차수에 저장된 프롬프트는 포함하지 않는다. 학습용 평가이며 공식 성적이나 최신 공식 채점 세칙 전체를 의미하지 않는다.

## 파일 구성

| 파일 | 용도 |
|---|---|
| `prompts/51.txt` ~ `54.txt` | 문항별 system 메시지 전문. UTF-8, BOM 없음. 앞뒤 공백과 줄바꿈도 그대로 사용 |
| `rubrics.json` | 문자열 키 `"51"` ~ `"54"`별 채점 기준표 |
| `output-schema.json` | 현재 `EvaluationResult.model_json_schema()` 원본 |
| `input-schemas/` | 문항, 기출 출처, 참고답안, 학습자 답안의 구조 |
| `examples/<이름>/input.json` | 문제·참고답안·합성 답안 입력 |
| `examples/<이름>/request.json` | 완성된 `{ "system": 문자열, "user": 객체 }` 요청 |
| `examples/<이름>/response.json` | 응답 구조 검증용 모의 결과 |
| `reference/` | 현재 요청 구성·응답 검증 로직에서 추출한 최소 Python 참고 구현 |
| `verify.py` | 파일 해시, 예시 요청 재생성, 응답 및 거부 조건을 오프라인 검사 |
| `manifest.json` | 전달본 버전, 원본 코드 해시, 유형별 prompt_version, 파일 SHA-256 |
| `verification.json` | 제작 시 실행한 검증 결과와 검증 범위 |

예시의 문제·참고답안은 저장소에 구조화된 기출 자료이며, 학생 답안은 인위적으로 작성했다. `response.json`의 점수는 **사람 채점 정답이나 실제 모델의 결과가 아니다**. 점수 정확도 벤치마크로 사용하지 않는다. 개인정보·실제 학생 답안·실험 DB·인증 정보·원본 PDF는 포함하지 않는다.

## 빠른 확인

Python 3.11, Pydantic 2.11.7에서 확인했다. 참고 구현의 의존성은 `requirements.txt`의 Pydantic 하나다. 기존 백엔드 환경에서 호환되는 의존성을 준비한 뒤 압축 해제한 폴더에서 실행한다.

```shell
python -B verify.py
```

이 명령은 네트워크와 DB를 사용하지 않는다. 다른 언어의 백엔드에서는 JSON 파일과 아래 규칙을 구현하고 예시를 계약 검증 자료로 사용한다.

## 모델 호출

문항마다 모델을 한 번 호출한다. 51·52번의 두 빈칸은 같은 호출로 평가한다. 아래 코드는 백엔드에 이미 설정된 OpenAI 호환 `client`와 선택한 `model_id`를 사용한다.

```python
import json
from pathlib import Path
from reference.evaluation_contract import build_request, empty_response, validate_response

root = Path("/path/to/writing-evaluation-v1")
inputs = json.loads((root / "examples/53/input.json").read_text(encoding="utf-8"))
request = build_request(**inputs, package_root=root)

result = empty_response(request)
if result is None:
    completion = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ],
    )
    result = json.loads(completion.choices[0].message.content)

checked = validate_response(result, request)
total_score = checked.total
```

`request.json`을 직접 사용하는 경우 `user` 객체를 JSON 문자열로 직렬화해서 `messages[1].content`에 넣는다. `request.json` 파일 전체를 user 메시지로 보내지 않는다. Python 참고 구현의 `request.user`는 이미 문자열이다.

현재 평가 경로는 `model`과 `messages`를 전달하며 **temperature, top_p, seed, max_tokens, response_format을 명시하지 않는다**. 생성용 프리셋은 평가에 적용되지 않는다. 이 전달본은 특정 제공자·모델을 확정하지 않는다. 운용 서버에서 모델 ID와 실제 호출 옵션을 기록한다. 이를 변경하면 동일 프롬프트라도 점수가 달라질 수 있다.

`output-schema.json`은 user 메시지에 넣는 출력 규격이다. 현재 코드의 API 수준 strict structured output 설정이 아니다. 제공자별 `response_format`에 그대로 넣을 수 있다고 가정하지 않는다. 현재 Hub의 JSON 파서는 코드 펜스를 제거하거나 JSON 객체를 추출하는 보정도 하지만, 위 최소 예제는 JSON 전용 응답을 엄격하게 파싱한다. 어느 경로든 아래 응답 검증이 필요하다.

## 입력 매핑

| user 필드 | 값 및 구성 규칙 |
|---|---|
| `source_key` | 현재 기출 형식은 `회차:문항번호`, 예: `102:53` |
| `type_slot` | 정수 51, 52, 53, 54 |
| `question` | 출처 래퍼. 실제 문제는 **`question.question`** 안에 들어감 |
| `question.source_instruction` | 실제 출처의 발문 |
| `question.question` | `WritingQuestion` 구조. topic, instruction, passage, materials, material_notes, requirements 등 |
| `answer` | 51·52: `{ "a": "...", "b": "..." }`, 53·54: `{ "text": "..." }` |
| `reference_answer` | 해당 문제의 참고답안. 예시 답안을 다른 문제에 재사용하지 않음 |
| `rubric` | `rubrics.json[str(type_slot)]` |
| `experimental_notes` | 이 버전에서는 빈 문자열 `""` |
| `tasks` | 아래 규칙으로 서버가 만든 과제 ID와 설명 |
| `character_counts` | 각 answer 필드별 서버 계산값 |
| `score_limits` | 해당 rubric의 `max_scores`와 동일 |
| `output_schema` | `output-schema.json` 객체 전체 |
| `completed_passage` | 51·52번에만 존재. 두 빈칸에 원본 답안을 동시에 대입한 지문 |
| `request_id` | system과 request_id를 넣기 전 payload를 묶어 계산한 SHA-256 |

제출 답안은 앞뒤 공백·줄바꿈·표현을 보존한다. 51·52번은 각 빈칸 최대 2,000자, 53·54번은 최대 20,000자로 입력을 제한하는 것이 현재 입력 모델의 동작이다. 이는 점수 산정용 권장 분량과 별개다. 원문을 교정하거나 요약한 뒤 평가에 넣지 않는다.

51·52번 `completed_passage`는 정규식 `\((㉠|㉡)\)`으로 원본 지문의 두 표식을 **한 번에** 치환한다. 순차 치환하면 학생 답안에 들어간 표식을 다시 바꿀 수 있다. 참고 입력은 `(㉠)`, `(㉡)` 형식으로 준비한다.

53번 도표는 이미지 URL이 아닌 수치·단위·항목·계열 또는 표의 열·행을 담은 구조화된 `materials`로 전달한다. 자료에 실제로 제시된 원인·추가 설명은 `material_notes`에 넣는다.

### 운용 문항 은행 연결 시 필요한 데이터

현재 Hub의 `topik_bank.writing_questions.content_json`은 `WritingQuestion`에 해당하므로 **`question.question`으로 매핑**한다. `set_id`, `type_slot`은 서버 내부 문항 연결에 사용한다.

현재 로컬 평가 입력 모델은 기출 자료용이다. `WritingReference`는 회차·`숫자_writing.pdf`·쪽수를, `AnswerReference`는 `회차:문항번호`·`숫자_answer.pdf`·쪽수를 요구한다. **생성 문항을 이 모델에 넣기 위해 가짜 회차나 PDF 출처를 만들면 안 된다.**

또한 현재 Hub 문항 저장 모델에는 생성 문항별 참고답안이 없다. 운용 서버에서는 해당 문항에 대응하는 참고답안을 공급하고, 실제 문항 ID·출처를 사용하는 입력 어댑터가 필요하다. 이번 전달본의 Python `build_request`는 현재 기출 입력 계약을 그대로 재현하므로, 생성 문항용 어댑터까지 구현된 것은 아니다. 프롬프트의 `question.question` 구조와 채점 관련 필드는 유지하고, 출처·식별자 모델 확장은 백엔드의 실제 입력 계약에 맞춰 적용해야 한다. 운용 서버 코드나 스키마는 이 패키지에서 변경하지 않았다.

51·52번 참고답안은 `blank_answers.a`, `blank_answers.b`의 문자열 배열로, 53·54번 참고답안은 `text`로 제공한다. 참고답안에는 사람의 만점 채점 기록이라는 의미를 부여하지 않는다. `blank_weights`는 해당 문항에 출처가 있는 경우에만 제공한다. 출처가 없으면 `{}`, `published_rules`는 `[]`로 둔다. 참고답안이 없을 때 임의로 채우는 동작은 현재 계약에 없다.

## tasks와 글자 수

- 51·52번: `a` → `㉠의 문맥·의미·문법·격식`, `b` → `㉡의 문맥·의미·문법·격식`.
- 53번: materials 순서대로 `material:1`, `material:2` 등. 값은 `자료의 정보와 관계: {title}`. material_notes 순서대로 `note:1`, `note:2` 등의 값에 원문 설명을 넣는다.
- 54번: requirements 순서대로 `requirement:1`, `requirement:2`, `requirement:3`을 만들고 요구 사항 원문을 값으로 넣는다.

각 answer 값에서 CRLF와 CR을 LF로 바꾼 계산용 문자열을 만든다. 원본 answer 값 자체는 바꾸지 않는다.

- `공백_포함_줄바꿈_제외`: LF를 제거한 Unicode 코드 포인트 수.
- `모든_공백_제외`: Python `str.isspace()`가 거짓인 코드 포인트 수.
- `계산_방식`: 예시에 포함된 현재 설명 문자열을 그대로 사용.

JavaScript `string.length`는 UTF-16 코드 유닛 수이므로 Python `len()`과 다를 수 있다. 공백 판정도 언어별 차이를 확인한다. 이 값은 공식 원고지 칸 수가 아니며, 분량만으로 자동 감점·0점 처리하지 않는다.

## 요청 ID와 버전

현재 Python 코드의 해시 함수는 다음과 같다.

```python
hashlib.sha256(
    json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()
```

`request_id`는 `value = {"system": system, "payload": payload_without_request_id}`로 계산한 뒤 payload에 추가한다. 기본 JSON 구분자인 쉼표 뒤 공백·콜론 뒤 공백도 해시 입력에 포함된다. 도표의 실수 표현, 기본값이 채워진 모델 필드, Unicode 및 중첩 키 정렬도 영향을 준다. 예시의 request_id와 일치시키려면 동일 직렬화가 필요하며 참고 Python 구현을 기준으로 삼는다. 이 ID는 입력 결합 확인용으로, 요청마다 고유한 실행 ID가 아니다. 같은 입력을 반복 평가하면 동일 ID가 되므로 실행 식별자는 별도로 관리한다.

유형별 `prompt_version`은 `{ "system": ..., "rubric": ..., "notes": "", "schema": ... }`에 위 함수를 적용한 값이며 `manifest.json`에 기록했다. rubric의 기존 버전 `pbt-writing-2017-v1`과 이 ZIP의 전달 버전 `writing-evaluation-v1`은 서로 다르다. 프롬프트·기준·스키마·보조 지침이 바뀌면 해당 prompt_version도 바뀐다.

`manifest.json`의 `files`는 manifest 자체를 제외한 파일의 원시 바이트 SHA-256이다. ZIP 전체 해시는 ZIP 옆의 `.sha256` 파일에 제공한다. manifest에는 로컬 작업본의 원본 파일 해시를 기록했으므로 기준 커밋만으로 현재 문구를 식별하지 않는다.

## 응답과 서버 검증

모델 응답 최상위 필드는 `request_id`, `type_slot`, `scores`, `task_checks`, `corrections`, `summary`, `review_reasons`이다. 전체 구조는 output-schema를 따른다. 스키마가 허용하는 기본값은 Pydantic 참고 구현이 채운다. 알 수 없는 필드를 추가하지 않는다. 정수 점수에 문자열·실수·불리언을 허용하지 않는다.

| 유형 | scores에 정확히 필요한 키 | 최대점수 |
|---|---|---|
| 51·52 | `a`, `b` | 5, 5 |
| 53 | `content`, `organization`, `language` | 7, 7, 16 |
| 54 | `content`, `organization`, `language` | 12, 12, 26 |

서버는 JSON 구조 검사와 함께 다음을 검증한다. `output-schema.json` 하나만으로 아래의 요청 의존 규칙이 모두 검증되지는 않는다.

1. `request_id`, `type_slot`이 서버에 보존한 요청과 일치해야 한다.
2. 점수 키가 문항의 영역과 정확히 일치하고 각 점수가 정수 범위 안이어야 한다. 53·54번 language는 2배 적용 후의 **짝수**여야 한다. 서버에서 다시 2배 하지 않는다.
3. 빈 답안의 해당 영역 점수는 0점이다. 양의 점수를 준 영역에는 원문 `quotes`가 하나 이상 필요하다.
4. 모든 인용은 비어 있지 않고 원본 답안의 정확한 부분 문자열이어야 한다. 51·52번은 해당 빈칸 답안에서만 인용한다.
5. `parts` 키는 해당 빈칸의 `blank_weights`와 정확히 같아야 한다. 세부 점수는 각 배점 안의 정수이며 합계가 해당 빈칸 점수와 같아야 한다. 배점이 없거나 53·54번이면 `{}`다.
6. task_checks의 `requirement_id`는 tasks의 모든 ID를 중복 없이 포함한다. status는 `충족`, `부분 충족`, `미충족` 중 하나다. 충족·부분 충족에는 원문 인용이 필요하다.
7. `corrections.original`도 원본 답안의 비어 있지 않은 부분 문자열이어야 한다.
8. 검증이 성공한 뒤 `sum(score)`로 합계를 계산한다. 모델 응답에 total·max 같은 필드를 추가하도록 요구하지 않는다.

검증 실패 응답은 점수로 확정하지 않는다. 실제 원문과 요청을 기준으로 오류를 처리한다. `review_reasons`는 사람이 확인할 판단 유보·기준 부족 사유이며 오류 코드나 자동 감점 지시가 아니다. 원문 인용 검사는 인용의 존재를 검사하며 AI의 내용 판단 정확도를 보증하지 않는다.

### 미응답

모든 answer 값이 공백뿐이거나 비어 있으면 모델을 호출하지 않고 모든 점수 0, 모든 과제 미충족, summary `작성된 답안이 없어 0점으로 처리했습니다.`로 처리한다. 출처 세부 배점이 있으면 parts의 각 점수도 0이다. `examples/empty-51`을 참고한다. 51·52번에서 한 빈칸만 비면 모델 호출은 수행하되 빈칸의 점수는 0이어야 한다. `examples/52`에 이 경우를 포함했다.

## 참고 구현과 검증 범위

`reference/writing_models.py`는 현재 소스의 사본이다. `reference/evaluation_contract.py`는 평가 모델·요청 구성·점수 검증 함수들을 원문에서 추출했다. 응답 검증의 함수명·인자만 `validate_response(dict, request)`로 바꾸어 ProviderResult 의존성을 제거했고, JSON 파일을 읽는 `build_request` 및 미응답 처리용 `empty_response`를 붙였다. 모델 클라이언트·Streamlit·DB 저장 기능은 참고 구현에 들어 있지 않다.

관련 기존 평가·실험 테스트, 예시의 원본 코드와 참고 구현 간 일치, 출력 스키마, 부정 응답 거부, ZIP 무결성을 확인했다. 구체적인 결과는 `verification.json`을 참고한다. 이번 패키징 과정에서 실제 모델 호출·운용 서버 접속·채점 정확도 재검증은 수행하지 않았다.

새 HTTP API, DB 테이블·컬럼, 백그라운드 작업은 추가하지 않았다.
