from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuestionTypeProfile:
    type_id: str
    label: str
    number_range: str
    analysis_focus: str
    generation_rules: tuple[str, ...]
    implemented: bool = False
    question_numbers: tuple[int, ...] = ()
    content_mode: str = "passage"
    shared_passage: bool = False
    skip_undisclosed: bool = True
    highlight_numbers: tuple[int, ...] = ()


QUESTION_TYPE_PROFILES = {
    "grammar_blank": QuestionTypeProfile(
        "grammar_blank",
        "문법 빈칸",
        "1~2번",
        "연결 표현, 시제, 양태와 문맥의 의미 관계를 구별하고 정답의 유일성을 확인한다.",
        (
            "문장 안에 '( )' 빈칸을 정확히 하나 둔다.",
            "형태가 비슷하지만 의미 기능이 다른 문법 표현으로 오답을 만든다.",
            "정답을 넣었을 때 문법과 문맥이 모두 자연스러워야 한다.",
        ),
        implemented=True,
        question_numbers=(1, 2),
        content_mode="single_sentence",
    ),
    "similar_expression": QuestionTypeProfile(
        "similar_expression", "유사 표현", "3~4번", "밑줄 표현과 의미·기능이 가장 가까운 문법 표현을 판별한다.",
        ("원문 표현과 정답 표현의 의미 기능을 일치시킨다.", "형태만 비슷한 표현을 오답으로 사용한다."),
        implemented=True,
        question_numbers=(3, 4),
        content_mode="single_sentence",
        highlight_numbers=(3, 4),
    ),
    "short_text_topic": QuestionTypeProfile(
        "short_text_topic", "짧은 글의 소재", "5~8번", "짧은 안내문이나 광고의 핵심 소재를 파악한다.",
        ("일상적인 짧은 글을 사용한다.", "세부 단어가 아니라 글 전체의 소재를 정답으로 삼는다."),
        implemented=True,
        question_numbers=(5, 6, 7, 8),
        content_mode="short_text",
    ),
    "content_match_short": QuestionTypeProfile(
        "content_match_short", "안내문·도표 내용 일치", "9~12번", "안내문·도표의 명시적 정보와 선택지를 대조한다.",
        ("정답은 자료에서 직접 확인 가능하게 한다.", "숫자·대상·조건 중 하나를 바꿔 오답을 만든다."),
        implemented=True,
        question_numbers=(9, 10, 11, 12),
        content_mode="short_text",
    ),
    "sentence_order": QuestionTypeProfile(
        "sentence_order", "문장 순서 배열", "13~15번", "지시어, 접속어와 사건의 선후 관계로 문장 순서를 결정한다.",
        ("(가)~(라)의 연결 단서를 분산한다.", "가능한 배열이 하나만 남도록 지시어와 접속어를 배치한다."),
        implemented=True,
        question_numbers=(13, 14, 15),
        content_mode="sentence_order",
    ),
    "paragraph_blank_short": QuestionTypeProfile(
        "paragraph_blank_short", "짧은 글 빈칸", "16~18번", "짧은 문단의 논리 관계와 담화 기능에 맞는 내용을 고른다.",
        ("빈칸 전후에 충분한 단서를 둔다.", "주제는 비슷하지만 논리 관계가 맞지 않는 오답을 만든다."),
        implemented=True,
        question_numbers=(16, 17, 18),
        content_mode="passage_blank",
    ),
    "paired_19_20": QuestionTypeProfile(
        "paired_19_20", "공통 지문: 빈칸·주제", "19~20번", "공통 지문의 연결 표현과 중심 주제를 각각 판단한다.",
        ("19번과 20번이 같은 지문을 공유하게 한다.", "빈칸 문항과 주제 문항의 정답 근거를 분리한다."),
        implemented=True,
        question_numbers=(19, 20),
        content_mode="paired_passage",
        shared_passage=True,
    ),
    "paired_21_22": QuestionTypeProfile(
        "paired_21_22", "공통 지문: 표현·내용", "21~22번", "공통 지문의 문맥 표현과 세부 내용 일치를 각각 판단한다.",
        ("21번과 22번이 같은 지문을 공유하게 한다.", "표현 문항과 내용 일치 문항을 함께 구성한다."),
        implemented=True,
        question_numbers=(21, 22),
        content_mode="paired_passage",
        shared_passage=True,
    ),
    "paired_23_24": QuestionTypeProfile(
        "paired_23_24", "서사 지문: 심정·내용", "23~24번", "서사 지문 속 인물의 심정과 세부 내용 일치를 각각 판단한다.",
        ("23번과 24번이 같은 서사 지문을 공유하게 한다.", "심정의 근거와 내용 일치 문항의 근거를 구분한다."),
        implemented=True,
        question_numbers=(23, 24),
        content_mode="paired_passage",
        shared_passage=True,
        highlight_numbers=(23,),
    ),
    "headline_interpretation": QuestionTypeProfile(
        "headline_interpretation", "신문 제목 해석", "25~27번", "압축된 제목의 사건과 변화 방향을 풀어 해석한다.",
        ("제목체의 생략과 비유를 적절히 사용한다.", "주체·방향·정도를 바꾼 오답을 만든다."),
        implemented=True,
        question_numbers=(25, 26, 27),
        content_mode="headline",
    ),
    "paragraph_blank": QuestionTypeProfile(
        "paragraph_blank", "긴 글 빈칸", "28~31번", "문단 전개의 논리와 핵심 내용을 이용해 빈칸을 복원한다.",
        ("빈칸 앞뒤의 결속 장치를 명확히 한다.", "지문 주제와 관련 있지만 논리적으로 맞지 않는 오답을 만든다."),
        implemented=True,
        question_numbers=(28, 29, 30, 31),
        content_mode="passage_blank",
    ),
    "content_match": QuestionTypeProfile(
        "content_match", "긴 글 내용 일치", "32~34번", "지문의 사실과 선택지의 세부 정보를 정확히 대조한다.",
        ("정답 근거를 지문에 명시한다.", "범위·인과·시점 중 하나를 변형해 오답을 만든다."),
        implemented=True,
        question_numbers=(32, 33, 34),
        content_mode="passage",
    ),
    "main_topic": QuestionTypeProfile(
        "main_topic", "글의 주제", "35~38번", "반복되는 핵심 주장과 필자의 초점을 종합한다.",
        ("일관된 중심 주장을 유지한다.", "부분 내용이나 지나치게 넓은 진술을 오답으로 만든다."),
        implemented=True,
        question_numbers=(35, 36, 37, 38),
        content_mode="passage",
    ),
    "sentence_insertion": QuestionTypeProfile(
        "sentence_insertion", "문장 삽입", "39~41번", "지시어와 문단 결속을 이용해 주어진 문장의 위치를 정한다.",
        ("삽입 위치 기호를 명확히 표시한다.", "앞뒤 문장의 지시 대상이 한 위치에서만 연결되게 한다."),
        implemented=True,
        question_numbers=(39, 40, 41),
        content_mode="sentence_insertion",
    ),
    "paired_42_43": QuestionTypeProfile(
        "paired_42_43", "문학 지문: 심정·내용", "42~43번", "서사 지문의 인물 심정과 내용을 함께 평가한다.",
        ("42번과 43번이 같은 서사 지문을 공유하게 한다.", "심정의 근거와 내용 추론의 근거를 구분한다."),
        implemented=True,
        question_numbers=(42, 43),
        content_mode="paired_passage",
        shared_passage=True,
        highlight_numbers=(42,),
    ),
    "paired_44_45": QuestionTypeProfile(
        "paired_44_45", "논설 지문: 빈칸·주제", "44~45번", "회차별 출제 순서를 구분하면서 긴 지문의 빈칸과 주제를 함께 평가한다.",
        (
            "44번과 45번이 같은 지문을 공유하게 한다.",
            "기출은 질문 문구를 기준으로 빈칸·주제 역할을 판별하며 회차별 순서 차이를 보존한다.",
            "새 문항은 현행 형식에 맞춰 44번을 빈칸, 45번을 주제로 구성한다.",
            "빈칸과 주제 문항의 근거를 일관되게 구성한다.",
        ),
        implemented=True,
        question_numbers=(44, 45),
        content_mode="paired_passage",
        shared_passage=True,
    ),
    "paired_46_47": QuestionTypeProfile(
        "paired_46_47", "논설 지문: 태도·내용", "46~47번", "필자의 태도와 세부 내용 일치를 함께 평가한다.",
        ("46번과 47번이 같은 지문을 공유하게 한다.", "태도와 사실 판단의 근거를 구분한다."),
        implemented=True,
        question_numbers=(46, 47),
        content_mode="paired_passage",
        shared_passage=True,
    ),
    "paired_48_50": QuestionTypeProfile(
        "paired_48_50", "고급 지문: 목적·빈칸·내용", "48~50번", "긴 지문의 목적, 빈칸, 세부 내용을 세 문항으로 평가한다.",
        ("48~50번이 같은 지문을 공유하게 한다.", "세 문항의 평가 목표와 정답 근거를 분리한다."),
        implemented=True,
        question_numbers=(48, 49, 50),
        content_mode="paired_passage",
        shared_passage=True,
    ),
}


DEFAULT_PROVIDER_INSTRUCTIONS = {
    "gpt_5_6_luna": "비용 효율적인 짧은 추론으로 형식과 정답 유일성을 점검하고 최종 JSON만 출력하십시오.",
    "gpt_5_3_chat": "출력 전 문항 수, 정답 유일성, JSON 필드 타입을 내부적으로 점검하고 사고 과정은 출력하지 마십시오.",
    "claude": "문법 형태와 문맥 의미를 각각 점검하되 최종 JSON 외의 서문, 주석, 분석 과정은 출력하지 마십시오.",
    "gemini_3_5_flash": "빠르게 생성하되 각 문항의 보기 수, 정답 범위, 필수 필드를 체크리스트로 내부 점검하십시오.",
    "gemini": "각 문항을 규칙 체크리스트와 대조하고 JSON 키, 배열 길이, 정답 범위를 정확히 지키십시오.",
    "k_exaone": "한국어 교육 문법의 표준 명칭을 사용하고 시제·양태·연결 관계의 미세한 차이를 우선 점검하십시오.",
    "solar_pro3": "한국어 모어 화자 관점의 자연스러움과 TOPIK 오답의 그럴듯함을 함께 점검하십시오.",
    "llama": "요청된 필드 이름과 개수를 문자 그대로 따르고 추가 키나 JSON 바깥의 문장을 출력하지 마십시오.",
    "gemma": "짧고 완전한 JSON을 출력하고 누락 필드, 문항 수, 보기 수를 마지막에 내부 점검하십시오.",
    "gpt_5_4_nano": "설명은 간결하게 유지하되 필수 필드와 정답 근거는 생략하지 말고 JSON 형식을 우선하십시오.",
    "deepseek": "한국어 문맥과 정답 유일성을 먼저 점검하고 사고 과정 없이 요청된 JSON 객체만 출력하십시오.",
    "deepseek_v4_pro": "충분히 검토해 정답 유일성과 오답 타당성을 확인하되 사고 과정 없이 최종 JSON 객체만 출력하십시오.",
}


def question_type_profile(type_id: str = "grammar_blank") -> QuestionTypeProfile:
    return QUESTION_TYPE_PROFILES[type_id]


def question_role_label(type_id: str, question_number: int, question_prompt: str = "") -> str:
    """Return the semantic role for types whose slot order changed over time."""
    if type_id != "paired_44_45":
        return ""
    prompt = question_prompt.replace(" ", "")
    if "주제" in prompt:
        return "주제"
    if "들어갈" in prompt or "빈칸" in prompt:
        return "빈칸"
    return {44: "빈칸", 45: "주제"}.get(question_number, "")


def default_analysis_guide(type_id: str = "grammar_blank") -> str:
    profile = question_type_profile(type_id)
    rules = " ".join(profile.generation_rules)
    return f"{profile.number_range} {profile.label} 유형이다. {profile.analysis_focus} {rules}"


def apply_provider_instruction(
    system_prompt: str,
    user_prompt: str,
    provider: str | None,
    optimized: bool,
    instruction: str = "",
) -> tuple[str, str]:
    if not optimized or not provider:
        return system_prompt, user_prompt
    hint = instruction.strip() or DEFAULT_PROVIDER_INSTRUCTIONS.get(provider, "")
    if not hint:
        return system_prompt, user_prompt
    return f"{system_prompt}\n\n모델별 실행 지침:\n{hint}", user_prompt
