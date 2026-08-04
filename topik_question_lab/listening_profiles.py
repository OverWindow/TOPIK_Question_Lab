from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ListeningTypeProfile:
    type_id: str
    label: str
    number_range: str
    question_numbers: tuple[int, ...]
    roles: tuple[str, ...]
    analysis_focus: str
    shared_script: bool = False
    visual_kind: str = "none"

    @property
    def repeat_count(self) -> int:
        return 1 if max(self.question_numbers) <= 20 else 2


def _profile(
    type_id: str,
    label: str,
    numbers: tuple[int, ...],
    roles: tuple[str, ...],
    focus: str,
    *,
    shared: bool = False,
    visual: str = "none",
) -> ListeningTypeProfile:
    start, end = min(numbers), max(numbers)
    return ListeningTypeProfile(
        type_id,
        label,
        str(start) if start == end else f"{start}~{end}",
        numbers,
        roles,
        focus,
        shared,
        visual,
    )


LISTENING_TYPE_PROFILES = {
    p.type_id: p
    for p in (
        _profile("visual_scene", "그림 고르기", (1, 2), ("그림 일치",), "짧은 대화와 일치하는 장면을 판별한다.", visual="scene"),
        _profile("visual_chart", "그래프 고르기", (3,), ("그래프 일치",), "수치·비율·순위를 듣고 일치하는 그래프를 판별한다.", visual="chart"),
        _profile("next_response", "이어질 말", (4, 5, 6, 7, 8), ("이어질 말",), "대화의 화행과 인접쌍에 맞는 다음 발화를 고른다."),
        _profile("followup_action", "여자의 후속 행동", (9, 10, 11, 12), ("후속 행동",), "대화 직후 여자가 할 행동을 추론한다."),
        _profile("content_match_once", "내용 일치", (13, 14, 15, 16), ("내용 일치",), "한 번 들은 대화·담화의 명시 정보를 대조한다."),
        _profile("main_idea_once", "남자의 중심 생각", (17, 18, 19, 20), ("중심 생각",), "남자가 주장하거나 중요하게 여기는 생각을 파악한다."),
        _profile("paired_21_22", "중심 생각·내용", (21, 22), ("남자의 중심 생각", "내용 일치"), "대화의 주장과 세부 내용을 함께 평가한다.", shared=True),
        _profile("paired_23_24", "행동·내용", (23, 24), ("남자가 하는 일", "내용 일치"), "대화의 목적 행동과 세부 내용을 함께 평가한다.", shared=True),
        _profile("paired_25_26", "중심 생각·내용", (25, 26), ("남자의 중심 생각", "내용 일치"), "설명 대화의 주장과 세부 내용을 평가한다.", shared=True),
        _profile("paired_27_28", "의도·내용", (27, 28), ("남자의 말하는 의도", "내용 일치"), "발화 의도와 세부 사실을 평가한다.", shared=True),
        _profile("paired_29_30", "화자 신분·내용", (29, 30), ("남자의 신분", "내용 일치"), "업무·상황 단서로 화자의 신분과 내용을 파악한다.", shared=True),
        _profile("paired_31_32", "중심 생각·태도", (31, 32), ("남자의 중심 생각", "남자의 태도"), "주장과 상대 의견에 대한 태도를 함께 평가한다.", shared=True),
        _profile("paired_33_34", "화제·내용", (33, 34), ("화제", "내용 일치"), "담화의 대상과 세부 내용을 평가한다.", shared=True),
        _profile("paired_35_36", "행동·내용", (35, 36), ("남자가 하는 일", "내용 일치"), "공식 발화의 수행 목적과 내용을 평가한다.", shared=True),
        _profile("paired_37_38", "중심 생각·내용", (37, 38), ("여자의 중심 생각", "내용 일치"), "전문 대화의 주장과 세부 내용을 평가한다.", shared=True),
        _profile("paired_39_40", "대화 전 내용·내용", (39, 40), ("대화 전 내용", "내용 일치"), "현재 대화로부터 앞선 맥락과 사실을 추론한다.", shared=True),
        _profile("paired_41_42", "강연 중심 내용·내용", (41, 42), ("강연 중심 내용", "내용 일치"), "강연의 중심 내용과 세부 사실을 평가한다.", shared=True),
        _profile("paired_43_44", "화제·설명", (43, 44), ("화제", "내용 일치"), "설명 담화의 화제와 세부 설명을 평가한다.", shared=True),
        _profile("paired_45_46", "내용·말하기 방식", (45, 46), ("내용 일치", "여자의 말하기 방식"), "전문 설명의 사실과 전개 방식을 평가한다.", shared=True),
        _profile("paired_47_48", "내용·태도", (47, 48), ("내용 일치", "남자의 태도"), "시사 대화의 사실과 화자의 태도를 평가한다.", shared=True),
        _profile("paired_49_50", "내용·말하기 방식", (49, 50), ("내용 일치", "남자의 말하기 방식"), "고급 강연의 사실과 전개 방식을 평가한다.", shared=True),
    )
}


def listening_type_profile(type_id: str) -> ListeningTypeProfile:
    return LISTENING_TYPE_PROFILES[type_id]


def profile_for_number(number: int) -> ListeningTypeProfile:
    return next(p for p in LISTENING_TYPE_PROFILES.values() if number in p.question_numbers)

