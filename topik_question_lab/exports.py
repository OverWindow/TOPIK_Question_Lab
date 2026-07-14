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
        parts.append(question["stem"])
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
    fields = ["provider", "model", "type_slot", "stem", "choice_1", "choice_2", "choice_3", "choice_4", "answer", "explanation", "target_grammar", "naturalness", "difficulty_fit", "distractor_quality", "topik_fit", "notes"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in approved_items(items):
        question = item["question"]
        review = item["review"]
        writer.writerow(
            {
                "provider": item["provider"],
                "model": item["model"],
                "type_slot": question["type_slot"],
                "stem": question["stem"],
                **{f"choice_{index}": choice for index, choice in enumerate(question["choices"], start=1)},
                "answer": question["answer"],
                "explanation": question["explanation"],
                "target_grammar": question["target_grammar"],
                **{key: review.get(key) for key in ["naturalness", "difficulty_fit", "distractor_quality", "topik_fit", "notes"]},
            }
        )
    return output.getvalue()
