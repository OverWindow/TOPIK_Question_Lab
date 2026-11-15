"""Writing topics transcribed from the two user-provided textbook photographs."""

WRITING_POOL_ID = "default_writing_pbt_topics"
WRITING_SLOT_LEVELS = {51: "중급", 52: "중급", 53: "고급", 54: "고급"}
WRITING_TOPIC_SOURCES = {
    "중급": "소재데이터/KakaoTalk_20261115_113121321_01.jpg",
    "고급": "소재데이터/KakaoTalk_20261115_113121321.jpg",
}

WRITING_TOPICS = {
    "중급": {
        "가정생활": ["연애/결혼", "음식/요리", "집/주거", "이사", "초대", "방문"],
        # The source lists these together without a category heading.
        "건강·운동·쇼핑": ["건강", "운동", "쇼핑"],
        "교육": ["학교생활", "친구/동료/선후배 관계", "교육 제도", "유학"],
        "뉴스/시사 문제": ["사건/사고/재해", "환경 문제", "공공장소(기관)"],
        "미디어": ["컴퓨터와 인터넷", "대중 매체", "신문"],
        "생활 방식": ["전화", "모임", "저축", "교통", "문화 차이", "일상생활", "봉사", "한국 생활", "외모/복장", "성격/감정", "날씨와 계절(기후)", "편의점"],
        "예술": ["공연과 감상", "영화"],
        "인사": ["감사 및 사과"],
        "일과 직업": ["직장 생활", "직업", "진로와 취업(면접)", "동료 간 갈등", "업무"],
        "취미와 여가": ["문화생활", "동아리"],
        "휴일": ["여행/숙박", "휴일/방학"],
    },
    "고급": {
        "가정생활": ["음식/요리", "집/주거"],
        "건강": ["운동", "건강 관리"],
        "과학": ["기술", "미래 사회", "생활 과학"],
        "교육": ["학교생활", "교육 제도", "유학"],
        "뉴스/시사 문제": ["환경 문제", "공공 사안"],
        "미디어": ["대중 매체", "신문"],
        "생활 방식": ["사회 제도", "일상생활", "봉사", "한국 생활", "날씨와 계절(기후)"],
        "예술": ["미술", "영화"],
        "일과 직업": ["직장 생활", "직업", "진로와 취업(면접)", "업무"],
        "전문 분야": ["언어", "역사", "언론", "경영/경제", "종교", "심리", "건축", "사회", "정치", "문화", "과학", "성", "철학/윤리"],
        "책과 문학": ["한국의 시와 수필", "한국의 소설"],
        "취미와 여가": ["스포츠"],
        "휴일": ["여행/숙박"],
    },
}


def split_writing_topic(item: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in item.split(" > ", 2)]
    if len(parts) == 3 and all(parts) and parts[0] in WRITING_TOPICS:
        return tuple(parts)
    return "미분류", "직접 등록", item


def default_writing_pool() -> dict:
    return {
        "id": WRITING_POOL_ID,
        "section": "writing",
        "slot_numbers": [51, 52, 53, 54],
        "name": "쓰기 PBT 중급·고급 소재",
        "prompt_instruction": (
            "교재 사진의 중급·고급 분류를 유지한 소재입니다. 51·52번은 중급, 53·54번은 고급 소재를 사용하십시오. "
            "선택 소재를 51번은 실용적 의사소통, 52번은 인과·대조·결론 설명, "
            "53번은 가상 조사·변화 자료, 54번은 필요성·영향·해결 방안 등의 논술 과제로 구체화하십시오. "
            "전문 분야도 일반 독자가 이해할 수 있는 생활·사회적 맥락으로 다루고 전문 지식을 요구하지 마십시오."
        ),
        "items": [f"{level} > {category} > {topic}"
                  for level, categories in WRITING_TOPICS.items()
                  for category, topics in categories.items() for topic in topics],
    }
