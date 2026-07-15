from __future__ import annotations

import csv
import io
import json


CIRCLED = ["①", "②", "③", "④"]


def approved_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item["review"].get("approved")]


def to_txt(items: list[dict]) -> str:
    parts: list[str] = []
    for index, item in enumerate(approved_items(items), start=1):
        question = item["question"]
        parts.append(f"## 문제 {index} [{item['provider']} / {item['model']}]")
        if question.get("set_id"):
            parts.append(f"세트: {question['set_id']}")
        auxiliary = question.get("auxiliary_text", "")
        if auxiliary:
            parts.append(f"주어진 문장: {auxiliary}")
        stem = question.get("passage") or question["stem"]
        highlight = question.get("highlight_text", "")
        if highlight and stem.count(highlight) == 1:
            stem = stem.replace(highlight, f"__{highlight}__", 1)
        parts.append(stem)
        if question.get("question_prompt"):
            parts.append(question["question_prompt"])
        parts.extend(f"{CIRCLED[i]} {choice}" for i, choice in enumerate(question["choices"]))
        parts.append(f"정답: {question['answer']}")
        parts.append(f"해설: {question['explanation']}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def to_json(items: list[dict]) -> str:
    payload = []
    for item in approved_items(items):
        payload.append(
            {
                "provider": item["provider"],
                "model": item["model"],
                "question": item["question"],
                "review": item["review"],
                "validation": item["validation"],
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_csv(items: list[dict]) -> str:
    output = io.StringIO()
    fields = [
        "provider", "model", "question_type", "type_slot", "set_id", "stem",
        "highlight_text", "passage", "question_prompt", "auxiliary_text",
        "choice_1", "choice_2", "choice_3", "choice_4", "answer", "explanation",
        "target_grammar", "difficulty", "naturalness", "difficulty_fit",
        "distractor_quality", "topik_fit", "notes",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in approved_items(items):
        question = item["question"]
        review = item["review"]
        writer.writerow(
            {
                "provider": item["provider"],
                "model": item["model"],
                "question_type": question.get("question_type", "grammar_blank"),
                "type_slot": question["type_slot"],
                "set_id": question.get("set_id", ""),
                "stem": question["stem"],
                "highlight_text": question.get("highlight_text", ""),
                "passage": question.get("passage", ""),
                "question_prompt": question.get("question_prompt", ""),
                "auxiliary_text": question.get("auxiliary_text", ""),
                **{f"choice_{index}": choice for index, choice in enumerate(question["choices"], start=1)},
                "answer": question["answer"],
                "explanation": question["explanation"],
                "target_grammar": question["target_grammar"],
                "difficulty": question.get("difficulty", ""),
                **{key: review.get(key) for key in ["naturalness", "difficulty_fit", "distractor_quality", "topik_fit", "notes"]},
            }
        )
    return output.getvalue()
