from __future__ import annotations

import json

from .listening_models import ListeningQuestionExample
from .listening_profiles import listening_type_profile
from .topic_bank import TopicBrief, generation_units, topic_plan_prompt


LISTENING_SYSTEM_PROMPT = """당신은 한국어능력시험 TOPIK II 듣기 문항 출제 전문가입니다.
기출의 소재와 고유명사를 복사하지 말고 대화 구조, 평가 목표와 난이도만 참고하십시오.
대본은 실제로 들었을 때 자연스러워야 하고 정답은 하나뿐이어야 합니다.
반드시 요청한 JSON 객체만 출력하고 사고 과정이나 마크다운을 출력하지 마십시오."""


TRANSCRIPTION_SYSTEM_PROMPT = """당신은 TOPIK II 듣기 통합 시험지 전사 전문가입니다.
첨부 페이지에서 대본, 발문, 보기와 시각 선택지의 의미를 정확히 구조화하십시오.
보이지 않는 내용을 추측하지 말고 불확실한 부분은 parse_warning에 기록하십시오.
반드시 JSON 객체만 출력하십시오."""


def examples_json(examples: list[ListeningQuestionExample]) -> str:
    values = []
    for example in examples:
        values.append(
            {
                "source": example.source_key,
                "type_slot": example.question_number,
                "question_role": example.question_role,
                "dialogue_turns": [turn.model_dump() for turn in example.dialogue_turns],
                "question_prompt": example.question_prompt,
                "choices": example.choices,
                "answer": example.answer,
                "target_skill": example.target_skill,
                "rationale": example.rationale,
                "repeat_count": example.repeat_count,
                "visual_kind": example.visual_kind,
                "visual_options": [option.model_dump() for option in example.visual_options],
                "set_key": example.set_key,
            }
        )
    return json.dumps(values, ensure_ascii=False, indent=2)


def build_listening_analysis_prompt(examples: list[ListeningQuestionExample], type_id: str) -> str:
    profile = listening_type_profile(type_id)
    return f"""다음 승인된 TOPIK II 듣기 {profile.number_range}번 {profile.label} 기출을 분석하십시오.

분석 초점: {profile.analysis_focus}
기출 예시:
{examples_json(examples)}

다음 JSON만 출력하십시오.
{{"analysis": {{"sentence_structure": "대본과 발문 구조", "tested_grammar": ["평가 기능"], "difficulty": "난이도", "answer_conditions": ["정답 조건"], "distractor_rules": ["오답 규칙"], "notes": "화자·길이·상황 관찰"}}}}"""


def _sample_question(type_id: str, slot: int, set_id: str = "") -> dict:
    profile = listening_type_profile(type_id)
    role_index = profile.question_numbers.index(slot) if slot in profile.question_numbers else 0
    value = {
        "question_type": type_id,
        "type_slot": slot,
        "dialogue_turns": [
            {"speaker": "여자", "text": "새 문제를 위한 자연스러운 발화입니다."},
            {"speaker": "남자", "text": "상황에 맞는 응답입니다."},
        ],
        "question_prompt": f"{profile.roles[min(role_index, len(profile.roles) - 1)]}을 묻는 발문",
        "choices": ["보기 1", "보기 2", "보기 3", "보기 4"],
        "answer": 1,
        "explanation": "정답 단서와 대표 오답의 차이",
        "target_skill": profile.roles[min(role_index, len(profile.roles) - 1)],
        "difficulty": "TOPIK II 듣기",
        "repeat_count": profile.repeat_count,
        "question_role": profile.roles[min(role_index, len(profile.roles) - 1)],
        "set_id": set_id,
        "visual_kind": profile.visual_kind,
        "visual_options": [],
        "answer_source": "ai_suggested",
        "topic_id": "",
        "topic_domain": "",
        "topic_title": "",
        "topic_angle": "",
    }
    if profile.visual_kind == "scene":
        value["choices"] = []
        value["visual_options"] = [
            {"number": i, "description": f"장면 {i}의 접근성 설명", "image_prompt": f"TOPIK 시험용 흑백 삽화 장면 {i}", "asset_path": "", "chart_spec": None}
            for i in range(1, 5)
        ]
    elif profile.visual_kind == "chart":
        value["choices"] = []
        value["visual_options"] = [
            {"number": i, "description": f"그래프 {i}", "image_prompt": f"TOPIK II 듣기 시험 선택지용 흑백 막대그래프 {i}. 제목은 조사 결과이며 항목명과 수치를 선명하게 표시", "asset_path": "", "chart_spec": {"chart_type": "bar", "title": "조사 결과", "labels": ["항목 A", "항목 B"], "values": [60, 40], "unit": "%"}}
            for i in range(1, 5)
        ]
    return value


def build_listening_generation_prompt(
    examples: list[ListeningQuestionExample],
    analysis_guide: str,
    count: int,
    difficulty: str,
    type_id: str,
    topic_briefs: list[TopicBrief] | None = None,
) -> str:
    profile = listening_type_profile(type_id)
    if profile.shared_script:
        set_count = max(1, count // len(profile.question_numbers))
        samples = [_sample_question(type_id, slot, "set-1") for slot in profile.question_numbers]
        count = set_count * len(profile.question_numbers)
        distribution = ", ".join(f"{slot}번 {set_count}개" for slot in profile.question_numbers)
        set_rule = (
            f"완전한 세트 {set_count}개를 만들고 각 세트는 동일한 dialogue_turns와 set_id를 공유해야 합니다. "
            f"type_slot별 생성 수는 정확히 {distribution}입니다."
        )
    else:
        units = generation_units(profile.question_numbers, count, False)
        slot_counts = {
            slot: sum(slot in unit_slots for _, unit_slots in units)
            for slot in profile.question_numbers
        }
        samples = [
            _sample_question(type_id, slot)
            for slot in profile.question_numbers
            if slot_counts[slot]
        ]
        distribution = ", ".join(
            f"{slot}번 {slot_counts[slot]}개" for slot in profile.question_numbers
        )
        set_rule = (
            "각 문항은 독립된 대본을 사용합니다. "
            f"type_slot별 생성 수는 정확히 {distribution}이며, 이 배분을 빠짐없이 지킵니다."
        )
    visual_rule = {
        "scene": "visual_options 네 개에 서로 분명히 다른 장면 설명과 이미지 생성 프롬프트를 작성하고 choices는 빈 배열로 둡니다.",
        "chart": "visual_options 네 개에 서로 구별되는 그래프 설명과 이미지 생성 프롬프트를 반드시 작성하고 choices는 빈 배열로 둡니다. chart_spec은 자동 렌더링이 가능할 때만 작성하며 없어도 됩니다.",
        "none": "choices에 비어 있지 않은 텍스트 보기 네 개를 작성하고 visual_options는 빈 배열로 둡니다.",
    }[profile.visual_kind]
    output = json.dumps({"questions": samples}, ensure_ascii=False, indent=2)
    topic_section = topic_plan_prompt(topic_briefs or [], shared=profile.shared_script)
    return f"""승인된 TOPIK II 듣기 {profile.number_range}번 {profile.label} 기출과 분석서를 바탕으로 새 문항 {count}개를 만드십시오.

목표 난이도: {difficulty}
유형 분석서:
{analysis_guide}

승인 기출:
{examples_json(examples)}
{topic_section}

규칙:
- {profile.analysis_focus}
- {profile.repeat_count}회 듣기 형식을 사용합니다.
- {set_rule}
- {visual_rule}
- 대본은 화자별 dialogue_turns로 작성합니다.
- 정답은 하나이며 answer는 1~4 정수입니다.
- 기출의 고유명사와 핵심 어휘 조합을 복사하지 않습니다.
- explanation에는 정답과 대표 오답의 차이를 씁니다.

다음 구조의 JSON 객체만 출력하십시오.
{output}"""


def build_transcription_prompt(exam: str, page_number: int) -> str:
    return f"""첨부한 {exam} 듣기 통합 시험지 {page_number}쪽을 전사하십시오.
페이지에 보이는 모든 문항을 포함하고 보이지 않는 정답은 추측하지 마십시오.
그림·그래프 선택지는 텍스트 설명으로 기록하십시오.

다음 JSON 객체만 출력하십시오.
{{
  "questions": [
    {{
      "question_number": 1,
      "instruction": "페이지의 지시문",
      "dialogue_turns": [{{"speaker": "여자", "text": "발화"}}],
      "question_prompt": "발문",
      "choices": ["보기1", "보기2", "보기3", "보기4"],
      "visual_options": [{{"number": 1, "description": "그림 또는 그래프 설명", "image_prompt": "", "asset_path": "", "chart_spec": null}}],
      "confidence": 0.9,
      "parse_warning": ""
    }}
  ]
}}"""


def build_listening_enrichment_prompt(examples: list[ListeningQuestionExample], type_id: str) -> str:
    profile = listening_type_profile(type_id)
    questions = [
        {
            "source_key": example.source_key,
            "question_number": example.question_number,
            "question_role": example.question_role,
            "dialogue_turns": [turn.model_dump() for turn in example.dialogue_turns],
            "question_prompt": example.question_prompt,
            "choices": example.choices or [option.description for option in example.visual_options],
        }
        for example in examples
    ]
    return f"""다음 TOPIK II 듣기 {profile.number_range}번 {profile.label} 문항의 정답과 해설을 한 번에 보완하십시오.

문항:
{json.dumps(questions, ensure_ascii=False, indent=2)}

규칙:
- 입력된 모든 source_key에 대해 결과를 정확히 하나씩 반환합니다.
- source_key는 입력값을 그대로 복사합니다.
- 대본, 발문과 보기만 근거로 정답 하나를 선택합니다.
- answer는 1~4 정수입니다.
- target_skill은 정답을 결정하는 듣기 평가 요소입니다.
- rationale에는 정답 근거와 대표 오답이 틀린 이유를 2~3문장으로 씁니다.
- 확신하기 어려우면 임의로 꾸미지 말고 confidence를 낮게 지정합니다.

다음 JSON 객체만 출력하십시오.
{{
  "enrichments": [
    {{
      "source_key": "시험명:1",
      "answer": 1,
      "target_skill": "핵심 듣기 평가 요소",
      "rationale": "정답 및 대표 오답 설명",
      "confidence": 0.9
    }}
  ]
}}"""
