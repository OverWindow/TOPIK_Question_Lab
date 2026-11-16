"""Verify this handoff offline. Run from any directory with Python -B."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from reference.evaluation_contract import (
    EvaluationResult, build_request, empty_response, validate_response,
)

ROOT = Path(__file__).resolve().parent


def read_json(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    manifest = read_json("manifest.json")
    actual_files = {
        path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*")
        if path.is_file() and path.name != "manifest.json" and "__pycache__" not in path.parts
    }
    require(actual_files == set(manifest["files"]), "Package file list differs from manifest")
    for relative, expected in manifest["files"].items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        require(actual == expected, f"SHA-256 mismatch: {relative}")
    require(EvaluationResult.model_json_schema() == read_json("output-schema.json"),
            "Generated output schema differs; check the Pydantic version")

    requests, responses = {}, {}
    empty_checks = 0
    for entry in manifest["examples"]:
        name = entry["name"]
        inputs = read_json(f"examples/{name}/input.json")
        request = build_request(**inputs, package_root=ROOT)
        expected = read_json(f"examples/{name}/request.json")
        require({"system": request.system, "user": request.payload} == expected,
                f"Rebuilt request differs: {name}")
        require(request.request_id == entry["request_id"], f"Request ID differs: {name}")
        require(request.payload["output_schema"] == read_json("output-schema.json"),
                f"Request schema differs: {name}")
        response = read_json(f"examples/{name}/response.json")
        checked = validate_response(response, request)
        require(checked.total == entry["expected_fixture_total"], f"Fixture total differs: {name}")
        if name == "empty-51":
            require(empty_response(request) == response, "Empty fixture differs from local result")
        else:
            require(empty_response(request) is None, f"Nonempty answer was skipped: {name}")
        requests[name], responses[name] = request, response

        empty_inputs = deepcopy(inputs)
        empty_inputs["answer"] = ({"a": " \n", "b": "\t"} if request.payload["type_slot"] < 53
                                  else {"text": "\r\n \t"})
        empty_request = build_request(**empty_inputs, package_root=ROOT)
        local_result = empty_response(empty_request)
        require(local_result is not None, f"Missing local zero response: {name}")
        require(validate_response(local_result, empty_request).total == 0, f"Nonzero empty answer: {name}")
        empty_checks += 1

    invalid_cases = [
        ("51", lambda r: r.update(request_id="0" * 64)),
        ("51", lambda r: r.update(type_slot=52)),
        ("51", lambda r: r["scores"]["a"].update(score=6)),
        ("51", lambda r: r["scores"]["a"].update(score=True)),
        ("51", lambda r: r["scores"]["a"].update(score="4")),
        ("51", lambda r: r["scores"]["a"].update(score=4.0)),
        ("51", lambda r: r["scores"].pop("b")),
        ("51", lambda r: r["scores"]["a"].update(quotes=[])),
        ("51", lambda r: r["scores"]["a"].update(quotes=["원본에 없는 인용"])),
        ("51", lambda r: r["scores"]["a"].update(quotes=["시작되기 전까지"])),
        ("51", lambda r: r["scores"]["a"].update(parts={"content": 4})),
        ("51", lambda r: r["task_checks"].pop()),
        ("51", lambda r: r["task_checks"].append(deepcopy(r["task_checks"][0]))),
        ("51", lambda r: r["task_checks"][0].update(quotes=[])),
        ("51", lambda r: r.update(total_score=9)),
        ("52", lambda r: r["scores"]["b"].update(score=1)),
        ("53", lambda r: r["scores"]["language"].update(score=11)),
        ("53", lambda r: r["scores"]["content"].update(score=8)),
        ("54", lambda r: r["corrections"][0].update(original="원본에 없는 수정 대상")),
        ("51-with-parts", lambda r: r["scores"]["a"]["parts"].update(content=0, form=0)),
    ]
    for name, mutate in invalid_cases:
        response = deepcopy(responses[name])
        mutate(response)
        try:
            validate_response(response, requests[name])
        except ValueError:
            continue
        raise AssertionError(f"Invalid response was accepted: {name}")

    print(json.dumps({"status": "passed", "hashed_files": len(manifest["files"]),
                      "examples": len(requests), "empty_answer_checks": empty_checks,
                      "invalid_responses_rejected": len(invalid_cases)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
