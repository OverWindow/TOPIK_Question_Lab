from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path


def approved_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item.get("review", {}).get("approved")]


def _script(question: dict) -> str:
    return "\n".join(f"{turn['speaker']}: {turn['text']}" for turn in question.get("dialogue_turns", []))


def to_txt(items: list[dict]) -> str:
    parts: list[str] = []
    for item in approved_items(items):
        q = item["question"]
        parts.append(f"[{q['type_slot']}번형] {q.get('question_role', '')}")
        if q.get("topic_id"):
            parts.append(
                f"소재: {q.get('topic_domain', '')} / {q.get('topic_title', '')}"
                f" · {q.get('topic_angle', '')}"
            )
        parts.extend([_script(q), q.get("question_prompt", "")])
        if q.get("choices"):
            parts.extend(f"{index}. {choice}" for index, choice in enumerate(q["choices"], start=1))
        else:
            parts.extend(f"{v['number']}. {v.get('description', '')}" for v in q.get("visual_options", []))
            parts.extend(
                f"그림·그래프 프롬프트 {v['number']}: {v.get('image_prompt', '')}"
                for v in q.get("visual_options", [])
            )
        parts.extend([f"정답: {q['answer']}", f"해설: {q.get('explanation', '')}", ""])
    return "\n".join(parts).rstrip() + ("\n" if parts else "")


def to_json(items: list[dict]) -> str:
    payload = [
        {
            "provider": item["provider"],
            "model": item["model"],
            "question": item["question"],
            "validation": item.get("validation", []),
            "review": item.get("review", {}),
        }
        for item in approved_items(items)
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_csv(items: list[dict]) -> str:
    output = io.StringIO()
    fields = ["provider", "model", "type_slot", "question_role", "script", "question_prompt", "choices", "visual_prompts", "answer", "explanation", "target_skill", "repeat_count", "visual_kind", "topic_id", "topic_domain", "topic_title", "topic_angle", "topic_fit"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in approved_items(items):
        q = item["question"]
        writer.writerow(
            {
                "provider": item["provider"],
                "model": item["model"],
                "type_slot": q["type_slot"],
                "question_role": q.get("question_role", ""),
                "script": _script(q),
                "question_prompt": q.get("question_prompt", ""),
                "choices": " | ".join(q.get("choices", [])) or " | ".join(v.get("description", "") for v in q.get("visual_options", [])),
                "visual_prompts": " | ".join(v.get("image_prompt", "") for v in q.get("visual_options", [])),
                "answer": q["answer"],
                "explanation": q.get("explanation", ""),
                "target_skill": q.get("target_skill", ""),
                "repeat_count": q.get("repeat_count", 1),
                "visual_kind": q.get("visual_kind", "none"),
                "topic_id": q.get("topic_id", ""),
                "topic_domain": q.get("topic_domain", ""),
                "topic_title": q.get("topic_title", ""),
                "topic_angle": q.get("topic_angle", ""),
                "topic_fit": item.get("review", {}).get("topic_fit"),
            }
        )
    return output.getvalue()


def to_zip(items: list[dict], root: Path) -> bytes:
    selected = approved_items(items)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("questions.json", to_json(items))
        archive.writestr("questions.txt", to_txt(items))
        archive.writestr("questions.csv", to_csv(items))
        added: set[Path] = set()
        for item in selected:
            for option in item["question"].get("visual_options", []):
                raw = option.get("asset_path", "")
                if not raw:
                    continue
                path = Path(raw)
                if not path.is_absolute():
                    path = root / path
                if path.exists() and path not in added:
                    archive.write(path, f"assets/{path.name}")
                    added.add(path)
    return output.getvalue()
