"""Question previews and field editors shared by the writing stages."""
from __future__ import annotations

import hashlib

import streamlit as st

from topik_question_lab.common.writing_models import PROFILES, WritingQuestion
from topik_question_lab.menus.writing.writing_images import IMAGE_ERROR, generate_image, image_path
from topik_question_lab.menus.writing.writing_prompts import build_image_prompt, prompt_settings


@st.cache_data
def pdf_page(path: str, modified: int, page_number: int) -> bytes:
    import io
    from contextlib import closing
    import pypdfium2 as pdfium

    with closing(pdfium.PdfDocument(path)) as document, closing(document[page_number - 1]) as page, closing(page.render(scale=1.5)) as bitmap:
        buffer = io.BytesIO()
        bitmap.to_pil().save(buffer, format="PNG")
        return buffer.getvalue()


def render_question(question: WritingQuestion, *, instruction: str | None = None):
    st.subheader(f"{question.type_slot}번 · {question.topic}")
    st.write(f"{instruction or question.instruction} ({PROFILES[question.type_slot].points}점)")
    if question.context_title:
        st.write(f"제목: {question.context_title}")
    if question.passage:
        st.markdown(question.passage.replace("\n", "  \n"))
    path = image_path(question)
    if question.type_slot == 53 and path.is_file():
        st.image(path.read_bytes(), caption="53번 자료 이미지", width="stretch")
        with st.expander("원본 자료 확인"):
            render_materials(question)
    else:
        render_materials(question)


def render_materials(question: WritingQuestion):
    if question.data_source:
        st.caption(question.data_source)
    for material in question.materials:
        st.write(f"**{material.title}**")
        if material.unit:
            st.caption(f"단위: {material.unit}")
        if material.kind == "table":
            st.dataframe([dict(zip(material.columns, row)) for row in material.rows], hide_index=True, use_container_width=True)
            continue
        rows = [{"항목": category, "계열": series.label, "값": series.values[i]}
                for series in material.series for i, category in enumerate(material.categories)]
        encoding = {
            "x": {"field": "항목", "type": "ordinal", "sort": material.categories, "axis": {"labelAngle": 0}},
            "y": {"field": "값", "type": "quantitative", "title": material.unit, "scale": {"zero": True}},
            "color": {"field": "계열", "type": "nominal"},
            "tooltip": [{"field": "항목"}, {"field": "계열"}, {"field": "값"}],
        }
        if material.kind == "bar":
            encoding["xOffset"] = {"field": "계열"}
        # A content-specific dataset refreshes layered charts after saved edits.
        dataset = "writing_" + hashlib.sha256(material.model_dump_json().encode()).hexdigest()[:16]
        st.vega_lite_chart(spec={
            "data": {"name": dataset}, "datasets": {dataset: rows},
            "height": 280, "encoding": encoding,
            "layer": [
                {"mark": {"type": material.kind, **({"point": True} if material.kind == "line" else {})}},
                {"mark": {"type": "text", "dy": -10},
                 "encoding": {"text": {"field": "값", "type": "quantitative", "format": ",.2~f"}}},
            ],
        }, use_container_width=True)
    for value in question.material_notes + question.requirements:
        st.write(f"• {value}")


def render_image_actions(question: WritingQuestion, storage, *, key: str):
    if question.type_slot != 53:
        return
    directory = storage.path.parent / "media/writing"
    path = image_path(question, directory)
    style = prompt_settings(storage, 53)["image_style"]
    with st.expander("자료 이미지 생성 프롬프트"):
        st.code(build_image_prompt(question, style), language=None)
        st.caption("디자인은 ‘3. 프롬프트 작업실’에서, 서비스·모델은 ‘설정’의 이미지 생성 설정에서 변경할 수 있습니다.")
    if not path.is_file():
        st.info("현재 저장된 내용의 자료 이미지가 없습니다. 문항을 수정했다면 이미지를 다시 생성하세요.")
    if st.button("자료 이미지 다시 생성" if path.is_file() else "자료 이미지 생성", key=f"{key}-generate"):
        with st.spinner("기출 스타일의 자료 이미지 생성 중…"):
            try:
                generate_image(question, directory=directory, style=style)
            except ValueError:
                st.error(IMAGE_ERROR)
            else:
                st.rerun()
    if path.is_file():
        st.download_button("자료 이미지 PNG 받기", path.read_bytes(), "topik_writing_53.png", "image/png",
                           key=f"{key}-download")
        st.caption("이미지의 수치·단위·한글 표기를 ‘원본 자료 확인’과 비교해 확인하세요.")


def edit_material(material: dict, key: str) -> dict:
    kind = st.selectbox("자료 형식", ["bar", "line", "table"],
                        index=["bar", "line", "table"].index(material.get("kind", "bar")),
                        format_func={"bar": "막대그래프", "line": "꺾은선그래프", "table": "표"}.get, key=f"{key}-kind")
    title = st.text_input("자료 제목", material.get("title", ""), key=f"{key}-title")
    unit = st.text_input("단위", material.get("unit", ""), key=f"{key}-unit")
    if kind == "table":
        columns = st.text_input("열 제목 (쉼표로 구분)", ", ".join(material.get("columns") or ["항목", "내용"]),
                                key=f"{key}-columns").split(",")
        columns = [value.strip() for value in columns]
        rows = material.get("rows") or [["", ""]]
        # Internal IDs let duplicate/blank labels reach validation without losing cells.
        records = [{f"c{i}": row[i] if i < len(row) else "" for i in range(len(columns))} for row in rows]
        edited = st.data_editor(records, num_rows="dynamic", hide_index=True, key=f"{key}-table-{len(columns)}",
                                column_config={f"c{i}": st.column_config.TextColumn(name or "열 제목 필요", required=True)
                                               for i, name in enumerate(columns)})
        return {"kind": kind, "title": title, "unit": unit, "columns": columns,
                "rows": [[str(row.get(f"c{i}") or "") for i in range(len(columns))] for row in edited]}
    categories = material.get("categories") or ["항목 1", "항목 2"]
    series = material.get("series") or [{"label": "값", "values": [0.0, 0.0]}]
    count = st.number_input("계열 수", 1, 6, value=len(series), key=f"{key}-count")
    labels = [st.text_input(f"계열 {i + 1} 이름", series[i]["label"] if i < len(series) else f"계열 {i + 1}",
                            key=f"{key}-label-{i}") for i in range(count)]
    records = [{"category": category, **{
        f"v{j}": series[j]["values"][i] if j < len(series) and i < len(series[j]["values"]) else 0.0
        for j in range(count)}} for i, category in enumerate(categories)]
    edited = st.data_editor(records, num_rows="dynamic", hide_index=True, key=f"{key}-{kind}-data-{count}",
                            column_config={"category": st.column_config.TextColumn("항목", required=True),
                                           **{f"v{j}": st.column_config.NumberColumn(label or "계열 이름 필요", required=True)
                                              for j, label in enumerate(labels)}})
    return {"kind": kind, "title": title, "unit": unit,
            "categories": [str(row.get("category") or "") for row in edited],
            "series": [{"label": label, "values": [row.get(f"v{j}") for row in edited]}
                       for j, label in enumerate(labels)]}


def edit_question(question: WritingQuestion, question_id: int | str) -> dict:
    revision = hashlib.sha256(question.model_dump_json().encode()).hexdigest()[:12]
    key = f"writing-edit-{question_id}-{revision}"
    draft = question.model_dump()
    draft["instruction"] = ""
    draft["topic"] = st.text_input("문항 소재", question.topic, key=f"{key}-topic")
    if question.type_slot == 51:
        draft["context_title"] = st.text_input("실용문 제목 (선택)", question.context_title, key=f"{key}-title")
    if question.type_slot in (51, 52, 54):
        draft["passage"] = st.text_area("지문" if question.type_slot != 54 else "주제 설명", question.passage,
                                         height=230, key=f"{key}-passage")
    if question.type_slot == 53:
        draft["data_source"] = st.text_input("자료 출처", question.data_source, key=f"{key}-source")
        count = st.number_input("자료 개수", 1, 4, value=len(question.materials), key=f"{key}-materials")
        materials = []
        for i in range(count):
            with st.expander(f"자료 {i + 1} 편집", expanded=True):
                materials.append(edit_material(draft["materials"][i] if i < len(draft["materials"]) else {}, f"{key}-m{i}"))
        draft["materials"] = materials
        notes = st.text_area("원인·전망 등 자료 설명 (한 줄에 하나)", "\n".join(question.material_notes), key=f"{key}-notes")
        draft["material_notes"] = [line.strip() for line in notes.splitlines() if line.strip()]
    if question.type_slot == 54:
        draft["requirements"] = [st.text_input(f"요구 사항 {i + 1}", value, key=f"{key}-requirement-{i}")
                                 for i, value in enumerate(question.requirements)]
    return draft
