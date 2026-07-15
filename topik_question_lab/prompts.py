from __future__ import annotations

import json

from .models import QuestionExample
from .prompt_profiles import question_type_profile


DEFAULT_SYSTEM_PROMPT = """당신은 한국어능력시험 TOPIK II 읽기 문항을 설계하는 출제 전문가입니다.
기출 문장을 복사하거나 고유명사를 재사용하지 말고, 제시된 형식과 난이도만 학습하십시오.
문법적으로 자연스럽고 정답이 하나뿐인 문항을 만드십시오.
반드시 요청한 JSON 형식만 출력하고 마크다운 코드 블록은 사용하지 마십시오."""

DEFAULT_ANALYSIS_GUIDE = """TOPIK II 읽기 1~2번은 짧은 문장의 빈칸에 들어갈 문법 표현이나 서술어를 고르는 4지선다형이다.
1번형은 주로 문장 중간의 연결 표현을 묻고, 앞뒤 절의 시간·조건·인과 관계가 정답을 결정한다.
2번형은 주로 문장 끝의 종결·보조 표현을 묻고, 시제·양태·추측·경험·변화 의미가 정답을 결정한다.
오답은 형태가 자연스러워 보여도 문맥의 시간 관계나 의미 기능 중 하나가 맞지 않도록 설계한다.
정답은 하나만 가능해야 하며 일상적이고 문화적으로 중립적인 어휘를 사용한다."""

ENRICHMENT_SYSTEM_PROMPT = """당신은 TOPIK II 읽기 기출 문항을 검토하는 한국어 문법 전문가입니다.
제공된 문장과 네 개의 보기만 근거로 가장 적절한 정답 하나를 판단하십시오.
정답을 확신하기 어려워도 임의의 정보를 만들지 말고 confidence를 낮게 지정하십시오.
반드시 요청한 JSON 객체만 출력하고 마크다운 코드 블록은 사용하지 마십시오."""


def examples_json(examples: list[QuestionExample]) -> str:
    payload = []
    for example in examples:
        payload.append(
            {
                "source": example.source_key,
                "type_slot": example.question_number,
                "question_type": example.question_type,
                "stem": example.stem,
                "highlight_text": example.highlight_text,
                "passage": example.passage,
                "question_prompt": example.question_prompt,
                "auxiliary_text": example.auxiliary_text,
                "set_key": example.set_key,
                "choices": example.choices,
                "answer": example.answer,
                "grammar_point": example.grammar_point,
                "rationale": example.rationale,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_analysis_prompt(examples: list[QuestionExample], type_id: str = "grammar_blank") -> str:
    profile = question_type_profile(type_id)
    return f"""다음 승인된 TOPIK II 읽기 {profile.number_range} {profile.label} 기출 예시를 분석하십시오.

분석 초점: {profile.analysis_focus}

기출 예시:
{examples_json(examples)}

다음 JSON 객체 하나만 출력하십시오.
{{
  "analysis": {{
    "sentence_structure": "이 문제 유형의 문장과 보기 구조 설명",
    "tested_grammar": ["주요 문법 범주"],
    "difficulty": "난이도 설명",
    "answer_conditions": ["정답이 유일해지는 조건"],
    "distractor_rules": ["그럴듯하지만 틀린 보기 설계 규칙"],
    "notes": "추가 관찰"
  }}
}}"""


def build_generation_prompt(
    examples: list[QuestionExample],
    analysis_guide: str,
    count: int,
    difficulty: str,
    type_id: str = "grammar_blank",
) -> str:
    profile = question_type_profile(type_id)
    slots = profile.question_numbers
    counts = {slot: count // len(slots) for slot in slots}
    for slot in slots[: count % len(slots)]:
        counts[slot] += 1
    distribution = ", ".join(f"{slot}번형 {counts[slot]}개" for slot in slots)
    type_rules = "\n".join(f"- {rule}" for rule in profile.generation_rules)

    base_question = {
        "question_type": type_id,
        "type_slot": slots[0],
        "stem": "",
        "highlight_text": "",
        "passage": "",
        "question_prompt": "",
        "auxiliary_text": "",
        "set_id": "",
        "choices": ["보기1", "보기2", "보기3", "보기4"],
        "answer": 1,
        "explanation": "정답 및 오답 설명",
        "target_grammar": "핵심 출제 포인트",
        "difficulty": difficulty,
    }
    if type_id == "grammar_blank":
        format_rules = (
            "- stem은 '( )' 빈칸을 정확히 하나 포함하는 완전한 문장입니다.\n"
            "- passage, question_prompt, auxiliary_text, highlight_text, set_id는 빈 문자열입니다."
        )
        base_question["stem"] = "새 문장 ( ) 새 문장."
        sample_questions = [base_question]
    elif type_id == "similar_expression":
        format_rules = (
            "- stem은 빈칸이 없는 완전한 문장입니다.\n"
            "- highlight_text는 stem 안에 정확히 한 번 등장하는 밑줄 대상 표현입니다.\n"
            "- choices는 밑줄 표현과 바꾸어 쓸 후보 4개입니다."
        )
        base_question.update(
            {
                "stem": "회의가 취소되는 바람에 일찍 집에 돌아왔다.",
                "highlight_text": "취소되는 바람에",
                "choices": ["취소된 탓에", "취소된 김에", "취소되는 대신", "취소되는 대로"],
                "target_grammar": "-는 바람에 / -(으)ㄴ 탓에",
            }
        )
        sample_questions = [base_question]
    else:
        format_rules = (
            "- passage에는 문제의 지문·자료·제목을 넣습니다.\n"
            "- question_prompt에는 수험자가 답해야 하는 질문을 넣습니다.\n"
            "- stem에는 auxiliary_text, passage, question_prompt를 합친 검색·표시용 텍스트를 넣습니다.\n"
            "- highlight_text는 밑줄 유형이 아니면 빈 문자열입니다."
        )
        if profile.content_mode == "sentence_insertion":
            format_rules += (
                "\n- auxiliary_text에는 주어진 문장을 넣고 passage에는 삽입 위치 (①)~(④)를 표시합니다."
            )
        if profile.shared_passage:
            format_rules += (
                "\n- 같은 세트의 문항은 동일한 set_id와 완전히 동일한 passage를 사용합니다."
            )
        sample_questions = []
        sample_slots = slots if profile.shared_passage else slots[:1]
        for slot in sample_slots:
            question = dict(base_question)
            passage = (
                "같은 세트에서 공유하는 새 지문입니다."
                if profile.shared_passage
                else "새로운 지문 또는 자료입니다."
            )
            auxiliary = ""
            choices = ["보기1", "보기2", "보기3", "보기4"]
            if profile.content_mode == "sentence_insertion":
                auxiliary = "삽입할 문장입니다."
                passage = (
                    "첫 문장입니다. (①) 둘째 문장입니다. (②) "
                    "셋째 문장입니다. (③) 마지막 문장입니다. (④)"
                )
                choices = ["①", "②", "③", "④"]
            prompt = f"{slot}번 평가 목표에 맞는 질문"
            question.update(
                {
                    "type_slot": slot,
                    "stem": "\n".join(value for value in (auxiliary, passage, prompt) if value),
                    "passage": passage,
                    "question_prompt": prompt,
                    "auxiliary_text": auxiliary,
                    "set_id": "set-1" if profile.shared_passage else "",
                    "choices": choices,
                }
            )
            sample_questions.append(question)

    set_rule = ""
    if profile.shared_passage:
        set_count = max(1, count // len(slots))
        set_rule = (
            f"\n완전한 공통 지문 세트 {set_count}개를 만들고 "
            f"세트마다 {len(slots)}개 문항을 모두 포함하십시오."
        )
    output_example = json.dumps({"questions": sample_questions}, ensure_ascii=False, indent=2)
    return f"""아래 승인된 TOPIK II 읽기 {profile.number_range} {profile.label} 기출 예시와 공통 유형 분석서를 바탕으로 새로운 문항 {count}개를 만드십시오.
문항 배분: {distribution}.{set_rule}

목표 난이도: {difficulty}

공통 유형 분석서:
{analysis_guide}

승인된 기출 예시:
{examples_json(examples)}

규칙:
{type_rules}
{format_rules}
- choices는 정확히 4개이며 정답은 하나뿐입니다.
- answer는 1부터 4까지의 정수입니다.
- 기출의 고유명사와 핵심 어휘 조합을 그대로 복사하지 않습니다.
- explanation에는 정답 이유와 대표 오답이 틀린 이유를 간결하게 씁니다.
- target_grammar에는 핵심 문법 또는 독해 포인트를 씁니다.

다음 JSON 객체 하나만 출력하십시오.
{output_example}"""


def build_enrichment_prompt(examples: list[QuestionExample], type_id: str = "grammar_blank") -> str:
    profile = question_type_profile(type_id)
    questions = [
        {
            "source_key": example.source_key,
            "question_number": example.question_number,
            "stem": example.stem,
            "highlight_text": example.highlight_text,
            "passage": example.passage,
            "question_prompt": example.question_prompt,
            "auxiliary_text": example.auxiliary_text,
            "set_key": example.set_key,
            "choices": example.choices,
        }
        for example in examples
    ]
    return f"""다음 TOPIK II 읽기 {profile.number_range} {profile.label} 기출 문항의 누락된 정보를 보완하십시오.

판단 초점: {profile.analysis_focus}

문항:
{json.dumps(questions, ensure_ascii=False, indent=2)}

규칙:
- source_key는 입력값을 정확히 그대로 사용합니다.
- answer는 1부터 4까지의 정수이며 가장 적절한 보기 하나만 선택합니다.
- grammar_point에는 정답을 결정하는 핵심 문법 표현 또는 의미 기능을 씁니다.
- rationale에는 정답 이유와 대표적인 오답이 맞지 않는 이유를 2~3문장으로 씁니다.
- confidence는 0부터 1 사이의 숫자입니다.

다음 JSON 객체 하나만 출력하십시오.
{{
  "enrichments": [
    {{
      "source_key": "시험명:1",
      "answer": 1,
      "grammar_point": "핵심 문법",
      "rationale": "정답과 주요 오답에 대한 설명",
      "confidence": 0.9
    }}
  ]
}}"""


def _build_legacy_generation_prompt(
    examples: list[QuestionExample],
    analysis_guide: str,
    count: int,
    difficulty: str,
    type_id: str = "grammar_blank",
) -> str:
    profile = question_type_profile(type_id)
    first_count = count // 2
    second_count = count - first_count
    first_slot, second_slot = profile.question_numbers[:2]
    type_rules = "\n".join(f"- {rule}" for rule in profile.generation_rules)
    if type_id == "similar_expression":
        format_rules = """- stem은 빈칸이 없는 완전한 문장입니다.
- highlight_text는 stem 안에 정확히 한 번 등장하는 밑줄 대상 표현입니다.
- choices는 밑줄 표현과 바꾸어 쓸 후보 4개이며 정답은 의미와 문법 기능이 가장 비슷한 하나입니다."""
        example_json = f"""{{
      "question_type": "similar_expression",
      "type_slot": {first_slot},
      "stem": "회의가 취소되는 바람에 일찍 집에 돌아왔다.",
      "highlight_text": "취소되는 바람에",
      "choices": ["취소된 탓에", "취소된 김에", "취소되는 대신", "취소되는 대로"],
      "answer": 1,
      "explanation": "정답 및 오답 설명",
      "target_grammar": "-는 바람에 / -(으)ㄴ 탓에",
      "difficulty": "{difficulty}"
    }}"""
    else:
        format_rules = """- 모든 stem에 정확히 한 개의 '( )' 빈칸을 포함합니다.
- highlight_text는 빈 문자열로 출력합니다."""
        example_json = f"""{{
      "question_type": "grammar_blank",
      "type_slot": {first_slot},
      "stem": "새 문장 ( ) 새 문장.",
      "highlight_text": "",
      "choices": ["보기1", "보기2", "보기3", "보기4"],
      "answer": 1,
      "explanation": "정답 및 오답 설명",
      "target_grammar": "핵심 문법",
      "difficulty": "{difficulty}"
    }}"""
    return f"""아래 승인된 TOPIK II 읽기 {profile.number_range} {profile.label} 기출 예시와 공통 유형 분석서를 바탕으로 새로운 문항 {count}개를 만드십시오.
{first_slot}번형(type_slot={first_slot})은 {first_count}개, {second_slot}번형(type_slot={second_slot})은 {second_count}개를 만드십시오.

목표 난이도: {difficulty}

공통 유형 분석서:
{analysis_guide}

승인된 기출 예시:
{examples_json(examples)}

규칙:
{type_rules}
{format_rules}
- choices는 정확히 4개이며 정답은 하나뿐입니다.
- answer는 1부터 4까지의 정수입니다.
- 기출 문장, 인물명, 장소명, 핵심 어휘 조합을 그대로 복사하지 않습니다.
- explanation에는 정답 이유와 대표 오답이 틀린 이유를 간결하게 씁니다.
- target_grammar에는 핵심 문법 표현을 씁니다.

다음 JSON 객체 하나만 출력하십시오.
{{
  "questions": [
    {example_json}
  ]
}}"""
