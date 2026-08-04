from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .listening_models import ChartSpec, VisualOption


def _font(size: int):
    candidates = [Path("C:/Windows/Fonts/malgun.ttf"), Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def render_chart(spec: ChartSpec, width: int = 640, height: int = 420) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font = _font(24), _font(17)
    draw.text((width // 2, 24), spec.title or "그래프", fill="#202124", font=title_font, anchor="ma")
    labels, values = spec.labels, spec.values
    if not labels or not values or len(labels) != len(values):
        raise ValueError("그래프 labels와 values가 필요합니다.")
    left, top, right, bottom = 70, 80, width - 35, height - 60
    colors = ["#176b5b", "#a34b27", "#4979a7", "#8a6d3b", "#775d91"]
    if spec.chart_type == "pie":
        total = sum(values)
        if total <= 0:
            raise ValueError("원 그래프 값의 합은 0보다 커야 합니다.")
        box = (105, 85, 405, 385)
        start = 0.0
        for index, (label, value) in enumerate(zip(labels, values)):
            end = start + value / total * 360
            draw.pieslice(box, start=start, end=end, fill=colors[index % len(colors)], outline="white", width=2)
            draw.text((430, 105 + index * 42), f"{label} {value:g}{spec.unit}", fill="#202124", font=label_font)
            start = end
    elif spec.chart_type == "line":
        maximum = max(values) or 1
        draw.line((left, top, left, bottom, right, bottom), fill="#555", width=2)
        points = []
        step = (right - left) / max(1, len(values) - 1)
        for index, (label, value) in enumerate(zip(labels, values)):
            x = left + index * step
            y = bottom - value / maximum * (bottom - top - 20)
            points.append((x, y))
            draw.text((x, bottom + 12), label, fill="#202124", font=label_font, anchor="ma")
            draw.text((x, y - 10), f"{value:g}{spec.unit}", fill="#202124", font=label_font, anchor="ms")
        if len(points) > 1:
            draw.line(points, fill=colors[0], width=4)
        for x, y in points:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=colors[0])
    else:
        maximum = max(values) or 1
        gap = 20
        bar_width = (right - left - gap * (len(values) + 1)) / len(values)
        draw.line((left, top, left, bottom, right, bottom), fill="#555", width=2)
        for index, (label, value) in enumerate(zip(labels, values)):
            x0 = left + gap + index * (bar_width + gap)
            y0 = bottom - value / maximum * (bottom - top - 25)
            draw.rectangle((x0, y0, x0 + bar_width, bottom), fill=colors[index % len(colors)])
            draw.text((x0 + bar_width / 2, y0 - 8), f"{value:g}{spec.unit}", fill="#202124", font=label_font, anchor="ms")
            draw.text((x0 + bar_width / 2, bottom + 12), label, fill="#202124", font=label_font, anchor="ma")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def suggest_visual_prompt(option: VisualOption, visual_kind: str) -> str:
    if option.image_prompt.strip():
        return option.image_prompt
    description = option.description.strip() or f"선택지 {option.number}"
    if visual_kind == "chart":
        details = ""
        if option.chart_spec:
            spec = option.chart_spec
            pairs = ", ".join(f"{label} {value:g}{spec.unit}" for label, value in zip(spec.labels, spec.values))
            details = f" 제목은 '{spec.title or '그래프'}', {spec.chart_type} 형식, 데이터는 {pairs}."
        return (
            f"TOPIK II 듣기 시험 선택지용 깔끔한 흑백 그래프. {description}.{details} "
            "항목명과 수치를 선명하게 표시하고 장식과 불필요한 문구가 없는 인쇄 시험지 스타일."
        )
    return (
        f"TOPIK II 듣기 시험 선택지용 깔끔한 흑백 선화 삽화. {description}. "
        "인물의 행동과 사물이 명확하고 배경은 단순하며 글자, 번호, 정답 표시는 넣지 않는 인쇄 시험지 스타일."
    )


def save_uploaded_asset(data: bytes, name: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z가-힣._-]", "_", Path(name).name)
    suffix = Path(safe).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("PNG 또는 JPEG 이미지만 사용할 수 있습니다.")
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("이미지는 10MB 이하여야 합니다.")
    Image.open(io.BytesIO(data)).verify()
    path = target_dir / safe
    path.write_bytes(data)
    return path
