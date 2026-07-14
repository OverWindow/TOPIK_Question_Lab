from __future__ import annotations

import json

from .models import QuestionExample


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
                "stem": example.stem,
                "choices": example.choices,
                "answer": example.answer,
                "grammar_point": example.grammar_point,
                "rationale": example.rationale,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_analysis_prompt(examples: list[QuestionExample]) -> str:
    return f"""다음 승인된 TOPIK II 읽기 1~2번 기출 예시를 분석하십시오.

기출 예시:
{examples_json(examples)}

다음 JSON 객체 하나만 출력하십시오.
{{
  "analysis": {{
    "sentence_structure": "1번형과 2번형의 문장 구조 설명",
    "tested_grammar": ["주요 문법 범주"],
    "difficulty": "난이도 설명",
    "answer_conditions": ["정답이 유일해지는 조건"],
    "distractor_rules": ["그럴듯하지만 틀린 보기 설계 규칙"],
    "notes": "추가 관찰"
  }}
}}"""


def build_enrichment_prompt(examples: list[QuestionExample]) -> str:
    questions = [
        {
            "source_key": example.source_key,
            "question_number": example.question_number,
            "stem": example.stem,
            "choices": example.choices,
        }
        for example in examples
    ]
    return f"""다음 TOPIK II 읽기 1~2번 기출 문항의 누락된 정보를 보완하십시오.

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


def build_generation_prompt(
    examples: list[QuestionExample],
    analysis_guide: str,
    count: int,
    difficulty: str,
) -> str:
    first_count = count // 2
    second_count = count - first_count
    return f"""아래 승인된 기출 예시와 공통 유형 분석서를 바탕으로 새로운 문항 {count}개를 만드십시오.
1번형(type_slot=1)은 {first_count}개, 2번형(type_slot=2)은 {second_count}개를 만드십시오.

목표 난이도: {difficulty}

공통 유형 분석서:
{analysis_guide}

승인된 기출 예시:
{examples_json(examples)}

규칙:
- 모든 stem에 정확히 한 개의 '( )' 빈칸을 포함합니다.
- choices는 정확히 4개이며 정답은 하나뿐입니다.
- answer는 1부터 4까지의 정수입니다.
- 기출 문장, 인물명, 장소명, 핵심 어휘 조합을 그대로 복사하지 않습니다.
- explanation에는 정답 이유와 대표 오답이 틀린 이유를 간결하게 씁니다.
- target_grammar에는 핵심 문법 표현을 씁니다.

다음 JSON 객체 하나만 출력하십시오.
{{
  "questions": [
    {{
      "type_slot": 1,
      "stem": "새 문장 ( ) 새 문장.",
      "choices": ["보기1", "보기2", "보기3", "보기4"],
      "answer": 1,
      "explanation": "정답 및 오답 설명",
      "target_grammar": "핵심 문법",
      "difficulty": "{difficulty}"
    }}
  ]
}}"""
