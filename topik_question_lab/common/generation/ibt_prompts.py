"""IBT output contracts, with PBT examples retained as references."""
from __future__ import annotations

import json
from .exam_profiles import profile_for
from .topic_bank import generation_units, topic_plan_prompt


def build_ibt_prompt(section, examples, guide, count, difficulty, type_id, topics=None, profile=None):
    profile = profile or profile_for(section, type_id)
    shared = profile.shared_passage or profile.shared_script
    units = generation_units(profile.question_numbers, count, shared)
    samples = []
    for key, slots in units:
        for slot in slots:
            role = profile.roles[profile.question_numbers.index(slot)]
            item = dict(question_type=type_id, type_slot=slot, choices=["보기 1", "보기 2", "보기 3", "보기 4"],
                        answer=1, explanation="정답의 근거와 오답이 틀린 이유", difficulty=difficulty,
                        set_id=key if shared else "", topic_id="", topic_domain="", topic_title="", topic_angle="")
            if section == "reading":
                item.update(stem="지문과 질문을 합친 표시용 텍스트", passage="새 지문", question_prompt=f"{role}을 묻는 발문",
                            auxiliary_text="", highlight_text="", target_grammar=role)
                if profile.base_type_id == "grammar_blank":
                    item.update(stem="새 문장 ( ) 새 문장.", passage="", question_prompt="")
                if slot in profile.highlight_numbers:
                    item["highlight_text"] = "지문에 한 번 등장하는 심정 판단 대상 표현"
                if profile.content_mode == "sentence_insertion":
                    item.update(auxiliary_text="삽입할 문장", passage="첫 문장 (①) 둘째 문장 (②) 셋째 문장 (③) 마지막 문장 (④)", choices=["①", "②", "③", "④"])
                if profile.material_kind == "material_banner":
                    item["visual_material"] = {"text": "광고·표어의 실제 문구"}
                elif profile.material_kind == "material_combo_chart":
                    item["visual_material"] = {"chart_spec": {"chart_type": "combo", "title": "자료 제목", "labels": ["연도 A", "연도 B"], "values": [100, 120], "unit": "명", "series_name": "인구 수", "line_values": [10, 15], "line_unit": "%", "line_name": "고령 인구 비율"}}
            else:
                item.update(dialogue_turns=[{"speaker": "여자", "text": "새 발화"}, {"speaker": "남자", "text": "새 응답"}],
                            question_prompt=f"{role}을 묻는 발문", question_role=role, target_skill=role,
                            repeat_count=profile.repeat_count, visual_kind=profile.visual_kind, visual_options=[], answer_source="ai_suggested")
                if profile.visual_kind != "none":
                    item["choices"] = []
                    item["visual_options"] = [{"number": n, "description": f"선택지 {n}의 의미", "image_prompt": "자연스러운 컬러 삽화로 표현할 선택지 장면 설명", "chart_spec": None} for n in range(1, 5)]
                    if profile.visual_kind == "chart":
                        for option in item["visual_options"]:
                            option["chart_spec"] = {"chart_type": "pie" if option["number"] <= 2 else "bar", "title": "자료 제목", "labels": ["항목 A", "항목 B"], "values": [40, 60], "unit": "%"}
            samples.append(item)
    from .prompts import examples_json as reading_examples
    from .listening_prompts import examples_json as listening_examples
    references = (reading_examples if section == "reading" else listening_examples)(examples)
    rules = "\n".join("- " + rule for rule in profile.generation_rules)
    return f"""TOPIK II IBT {'읽기' if section == 'reading' else '듣기'} 새 문항 {len(samples)}개를 생성하십시오.
목표 난도: {difficulty}
IBT 문항 번호: {list(profile.question_numbers)}
번호 순서별 질문 역할: {list(profile.roles)}
추가 분석 지침: {guide}
유형별 필수 규칙:
{rules}

아래 PBT 예시는 평가 목표·문장 구조만 참고하십시오. 원래 번호·문항 수·소재·고유명사를 복사하지 마십시오.
IBT 출력의 번호·역할·문항 수는 아래 JSON 계약이 우선입니다.
PBT 참고 예시:
{references}

공통 지문·대본 여부: {shared}. 공통이면 같은 묶음의 passage 또는 dialogue_turns를 완전히 동일하게 하고 set_id를 공유합니다.
독립 문항은 서로 다른 지문·대본으로 만듭니다. 완전한 공유 묶음을 빠짐없이 출력합니다.
읽기 빈칸 역할에는 '( )' 하나, 목적·내용만 묻는 지문에는 불필요한 빈칸을 넣지 않습니다.
문장 배열에는 (가)~(라), 문장 삽입에는 (①)~(④) 위치와 별도 제시 문장이 필요합니다.
밑줄은 지정 문항에만 두며 해당 표현은 지문에 정확히 한 번 있어야 합니다.
광고·표어는 visual_material.text를 passage의 문구와 동일하게 합니다.
복합 도표는 두 계열의 이름·단위·항목·수치를 passage에 명시합니다. 각 계열은 '계열명: 항목: 수치단위'로 표현합니다.
듣기 시각 선택지는 네 개이며 의미가 분명히 달라야 합니다. 그래프 종류는 선택지별로 지정합니다.
IBT 이미지는 컬러입니다. image_prompt에 컬러 삽화를 지시하고 PBT 예시의 흑백 스타일 지시는 따르지 마십시오.
정답은 보기 1~4 중 하나이며 정답과 해설을 반드시 검증하십시오.
{topic_plan_prompt(topics or [], shared=shared)}
다음 필드와 문항별 번호·역할을 지켜 JSON만 출력하십시오. 예시의 빈 문구는 실제 내용으로 채우십시오.
{json.dumps({'questions': samples}, ensure_ascii=False, indent=2)}"""
