# TOPIK II PBT 쓰기 평가 v2 — 참고답안 없는 평가

2026-11-16(Asia/Seoul) 기준 Unigate-PBT-Hub의 수정된 평가 코드와 프롬프트를 고정한 백엔드 전달본이다. **문제·학생 원본 답안·공통 채점 기준만으로 51~54번을 평가한다. 참고답안은 입력하거나 전송하지 않는다.**

## v1에서 바뀐 내용

- 시스템 프롬프트에서 참고답안을 전제로 한 판단 규칙을 제거했다. 가상의 정답 문장을 만들어 일치도를 비교하지 않도록 명시했다.
- 입력의 `answer_reference`와 모델 요청의 `reference_answer`가 없어졌다. 모든 영역의 `parts`는 `{}`다.
- 기존 배점, 53·54번 언어 점수의 2배 적용, 원문 인용 검증, 완전 미응답의 로컬 0점 처리는 유지한다.
- 운용 문항의 `content_json`과 실제 문항 식별자로 요청을 만들 수 있다. 기출 회차·PDF 출처·모범답안을 만들 필요가 없다.
- 로컬 앱의 새 개별 평가·새 평가 차수·새 실험의 사전 판단에 적용된다. 기존 참고답안 포함 차수는 조회용이며 새 차수로 비교한다. 합성 답안 생성 단계는 기존처럼 참고답안을 사용한다. 이 ZIP에는 답안 생성 단계가 포함되지 않는다.

## 파일 구성

| 파일 | 용도 |
|---|---|
| `prompts/51.txt` ~ `54.txt` | 유형별 system 메시지 전문. UTF-8, BOM 없음 |
| `rubrics.json` | `"51"` ~ `"54"` 키의 공통 채점 기준 |
| `input-schemas/question.json` | 문항 `WritingQuestion` 스키마 |
| `input-schemas/learner-answer.json` | 학생 원본 답안 스키마 |
| `output-schema.json` | 평가 응답 스키마 |
| `examples/*/input.json` | `source_key`, `question`, `answer`만 포함한 입력 예시 |
| `examples/*/request.json` | 완성된 `{ "system": 문자열, "user": 객체 }` |
| `examples/*/response.json` | 검증용 모의 응답 |
| `reference/` | 현재 코드에서 추출한 최소 Python 요청 구성·응답 검증 구현 |
| `verify.py` | 네트워크·DB 없이 실행하는 패키지 검증 |
| `manifest.json`, `verification.json` | 버전·원본 및 전달 파일 SHA-256·검증 결과 |

예시 문제는 저장소의 102회 기출 자료이며, 학생 답안은 합성 답안이다. 예시 점수는 **연동 검증용 모의 값**이며 실제 모델 결과나 사람 채점 정답이 아니다. 실제 학생 데이터·DB·비밀 정보·기출 PDF·모범답안은 포함하지 않는다.

## 백엔드 적용

1. 문항 은행의 `topik_bank.writing_questions.content_json`을 `question`으로 읽는다.
2. 실제 `set_id`와 `type_slot`을 묶은 문항 식별자를 `source_key`로 사용한다. 예: `writing:{set_id}:{type_slot}`. 학생 식별정보를 넣지 않는다.
3. 51·52번 학생 답안은 `{ "a": "...", "b": "..." }`, 53·54번은 `{ "text": "..." }`로 준비한다.
4. 문항 번호에 맞는 system 프롬프트와 rubric을 읽고 요청을 구성한다.
5. 응답을 검증한 뒤 서버에서 합계를 계산한다.

**참고답안 공급·연결은 필요 없다.** 참고답안 관련 키를 빈 객체나 null로 붙이지 말고 요청에서 완전히 제외한다.

Python 3.11 / Pydantic 2.11.7에서 검증했다. `requirements.txt`는 참고 구현에 필요한 Pydantic만 명시한다. 기존 백엔드에 호환 의존성을 준비한 뒤 먼저 실행한다.

```shell
python -B verify.py
```

다음 예제의 `client`와 `model_id`는 백엔드에서 설정한 OpenAI 호환 클라이언트와 모델이다.

```python
import json
from pathlib import Path
from reference.evaluation_contract import build_request, empty_response, validate_response

root = Path("/path/to/writing-evaluation-v2")
inputs = json.loads((root / "examples/53/input.json").read_text(encoding="utf-8"))
# 실제 서비스에서는 inputs를 source_key, content_json, 학생 답안으로 구성한다.
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

`request.user`는 JSON 문자열이다. `request.json` 파일을 직접 읽으면 `user`는 객체이므로 JSON 문자열로 변환해서 보낸다. 파일 전체를 user 메시지에 넣지 않는다.

현재 호출은 `model`과 `messages`를 지정한다. temperature·top_p·seed·출력 토큰 제한·response_format은 명시하지 않으며 특정 모델도 고정하지 않았다. 생성용 프리셋은 평가에 적용되지 않는다. 운용 시 모델 ID와 실제 호출 옵션을 기록한다. 출력 스키마는 user 메시지에 포함하는 규격이며 제공자별 strict structured output 설정과 같지 않다. 위 예제는 JSON 응답을 엄격히 파싱한다. Hub의 기존 파서는 코드 펜스 제거·객체 추출 보정도 하지만 검증 규칙은 동일하다.

## 실제 모델 입력

`build_request`가 문제·답안·기준에서 다음 필드를 계산한다. `source_key`와 `request_id`는 요청 연결용이며 추가 채점 자료가 아니다.

| user 필드 | 값 |
|---|---|
| `source_key`, `type_slot` | 실제 문항 식별자, 정수 문항 번호 |
| `question` | `{ "question": WritingQuestion, "source_instruction": 표준 발문 }`. 실제 문항은 `question.question`에 들어감 |
| `answer` | 원본 학생 답안 |
| `rubric` | 유형별 공통 채점 기준 |
| `experimental_notes` | 고정 전달본에서는 빈 문자열 |
| `tasks` | 문항의 빈칸·자료·요구 사항에서 파생한 과제 목록 |
| `character_counts` | 원본 answer 필드별 코드 계산값 |
| `score_limits` | rubric의 `max_scores`와 같은 영역별 배점 |
| `output_schema` | `output-schema.json` 객체 전체 |
| `completed_passage` | 51·52번에만 존재. 두 빈칸에 학생 답안을 동시에 넣은 지문 |
| `request_id` | system과 요청 본문을 결합한 SHA-256 |

51·52번 지문의 표식은 `(㉠)`, `(㉡)`을 사용한다. 정규식 `\((㉠|㉡)\)`으로 두 표식을 한 번에 치환한다. 순차 치환으로 학생 답안 속 표식을 다시 바꾸지 않는다. 원본 답안의 공백·줄바꿈·표현은 보존한다. 51·52번은 빈칸별 2,000자, 53·54번은 20,000자가 입력 모델의 최대 길이이며 채점용 권장 분량과는 별개다.

53번은 이미지 URL 대신 `materials`의 수치·단위·항목·계열 또는 표의 열·행을 전달한다. 문제에 제시된 원인·추가 정보는 `material_notes`에 담는다. 54번은 지문과 요구 사항 3개를 전달한다. 검증되지 않은 추정 자료나 추가 정답을 넣지 않는다.

### 과제와 글자 수

- 51·52번 tasks: `a` → `㉠의 문맥·의미·문법·격식`, `b` → `㉡의 문맥·의미·문법·격식`.
- 53번: 자료별 `material:1` 등의 값은 `자료의 정보와 관계: {title}`. 추가 설명별 `note:1` 등의 값은 material_notes 원문.
- 54번: `requirement:1` ~ `requirement:3`에 requirements 원문을 순서대로 넣는다.

글자 수 계산용 문자열에서는 CRLF와 CR을 LF로 바꾼다. `공백_포함_줄바꿈_제외`는 LF를 제거한 Unicode 코드 포인트 수, `모든_공백_제외`는 Python `str.isspace()`에 해당하지 않는 코드 포인트 수다. 원본 answer는 바꾸지 않는다. JavaScript의 UTF-16 `string.length`나 언어별 공백 판정과 차이가 있을 수 있다. 공식 원고지 칸 수가 아니며 분량만으로 자동 감점하지 않는다.

## 응답 검증

| 유형 | scores에 정확히 필요한 키 | 최대점수 |
|---|---|---|
| 51·52 | `a`, `b` | 각각 5 |
| 53 | `content`, `organization`, `language` | 7, 7, 16 |
| 54 | `content`, `organization`, `language` | 12, 12, 26 |

최상위 응답은 `request_id`, `type_slot`, `scores`, `task_checks`, `corrections`, `summary`, `review_reasons`로 구성된다. 정확한 필수 필드·기본값·타입은 output-schema를 따른다. 서버는 구조 검사와 함께 다음 요청 의존 규칙을 검증한다.

1. request_id·type_slot은 서버가 보존한 요청과 일치해야 한다.
2. 영역 키가 정확히 일치하고 점수는 범위 안의 정수여야 한다. 문자열·불리언·실수를 점수로 받지 않는다. 53·54번 language는 이미 2배가 적용된 **짝수**이며 서버에서 다시 2배 하지 않는다.
3. 모든 `scores.*.parts`는 `{}`다. 출처 없는 내용·형식 세부 배점을 새로 만들지 않는다.
4. 빈 답안의 영역 점수는 0이다. 양의 점수를 준 영역에는 원본 quotes가 필요하다.
5. 인용은 비어 있지 않은 원본의 정확한 부분 문자열이어야 한다. 51·52번은 해당 빈칸에서만 인용한다.
6. task_checks는 tasks의 모든 ID를 중복 없이 포함한다. 충족·부분 충족에는 원문 인용이 필요하다.
7. corrections.original 역시 원본의 비어 있지 않은 부분 문자열이어야 한다.
8. 검증 성공 후 영역 점수를 합산한다. 모델 응답에 total·max 필드를 추가하지 않는다. 실패 응답을 점수로 확정하지 않는다.

완전 미응답은 모델 호출 없이 모든 영역 0점·모든 과제 미충족으로 처리한다. 51·52번에서 한 빈칸만 비면 모델을 호출하고 해당 빈칸은 0점으로 검증한다. `empty-51`, `empty-53`, `52` 예시를 참고한다.

review_reasons는 판단 유보·기준 부족에 대한 검토 사유다. 인용 존재 검증이 AI의 내용 판단 정확도까지 보증하지는 않는다. 학습용 평가이며 공식 성적이 아니다.

## 해시와 버전

현재 해시 함수는 `sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))`의 16진 문자열이다. request_id를 넣기 전 payload와 system을 `{ "system": system, "payload": payload }`로 묶어 계산한다. 기본 JSON 구분자 공백, 중첩 키 정렬, 기본 필드와 실수 표현도 해시에 영향을 준다. 다른 언어에서 재현할 때 참고 구현·예시와 대조한다. 같은 입력은 같은 request_id이므로 개별 실행 ID는 별도 관리한다.

유형별 prompt_version은 `{ "system": ..., "rubric": ..., "notes": "", "schema": ... }`에 같은 함수를 적용한 값이다. 전달본 버전은 `writing-evaluation-v2`, 입력 버전은 2이며, 출처 기준표의 버전 `pbt-writing-2017-v1`은 그대로다. manifest의 files는 manifest 자체를 제외한 원시 바이트 SHA-256이다. ZIP 전체 해시는 옆의 `.zip.sha256` 파일에 제공한다.

## 검증 범위

참고 구현은 현재 요청·검증 코드를 추출하고 ProviderResult 대신 dict를 받는 응답 어댑터와 파일 로딩·미응답 함수를 붙였다. `AnswerReference` 모델은 제외했다. 과거 기록을 읽는 Hub의 검증 호환 코드는 원본에 남아 있지만, 패키지의 `validate_response`는 참고답안 포함 요청을 거부한다.

관련 테스트, 기존 성공 기록의 읽기 호환성, 원본 코드와 패키지의 요청·응답 일치, JSON 스키마, 참고답안 제외, 부정 응답 거부, ZIP 압축 해제 후 오프라인 실행을 확인한다. 상세 결과는 `verification.json`에 기록한다. 실제 모델을 다시 호출하여 점수 정확도를 비교한 결과는 포함하지 않는다.

새 HTTP API·DB 테이블·컬럼·백그라운드 작업은 추가하지 않았다. 운용 서버 접속·배포도 수행하지 않았다.
