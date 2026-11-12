"""Read the Korean Basic Dictionary export using unigate-main's sense mapping."""
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryFile
from typing import TextIO
import unicodedata

from topik_question_lab.common.question_bank.connection import TransferError, digest


LANGUAGES = {
    "mn": "몽골어", "ar": "아랍어", "zh-Hans": "중국어", "vi": "베트남어",
    "th": "타이어", "id": "인도네시아어", "ru": "러시아어", "en": "영어",
    "ja": "일본어", "fr": "프랑스어", "es": "스페인어",
}
MAIN_HEADERS = [
    "표제어", "동형어 번호", "구분", "품사", "고유어 여부", "원어", "발음", "활용",
    "파생어", "☞(가 보라)", "어휘 등급", "의미 범주", "주제 및 상황 범주", "전체 참고",
    "검색용 이형태", "관련어", "의미 참고", "다중 매체 정보", "문형", "문형 참고", "뜻풀이", "용례",
]
SUB_HEADERS = [
    "부표제어", "부표제어 전체 참고", "부표제어 관련어", "부표제어 의미 참고",
    "부표제어 문형", "부표제어 문형 참고", "부표제어 뜻풀이", "부표제어 용례",
]


def translation_headers(prefix=""):
    return [f"{name} {prefix}{suffix}" for name in LANGUAGES.values()
            for suffix in ("대역어", "대역어 뜻풀이")]


HEADERS = MAIN_HEADERS + translation_headers() + SUB_HEADERS + translation_headers("부표제어 ")
PARTS_OF_SPEECH = {
    "명사", "대명사", "수사", "조사", "동사", "형용사", "관형사", "부사", "감탄사",
    "접사", "의존 명사", "보조 동사", "보조 형용사", "어미", "품사 없음",
}
ENTRY_TYPES = {"단어": "word", "구": "phrase", "문법‧표현": "expression"}
TYPE_LABELS = {"word": "단어", "phrase": "구", "expression": "문법 표현", "proverb": "속담", "idiom": "관용구"}


def dictionary_files(directory):
    directory = Path(directory)
    if not directory.is_dir():
        raise TransferError("한국어기초사전 폴더가 없습니다. 전체 분할 파일을 해당 폴더에 넣으세요.")
    paths = sorted((p for p in directory.iterdir() if p.is_file()
                    and p.suffix.lower() in {".xls", ".xlsx"} and not p.name.startswith("~$")),
                   key=lambda p: [int(s) if s.isdigit() else s for s in re.split(r"(\d+)", p.name.casefold())])
    if not paths:
        raise TransferError("한국어기초사전 폴더에 XLS/XLSX 파일이 없습니다.")
    if len({p.stem.casefold() for p in paths}) != len(paths):
        raise TransferError("같은 이름의 XLS/XLSX가 중복되어 있습니다. 원본 한 벌만 준비하세요.")
    if any(len(p.name) > 255 for p in paths):
        raise TransferError("원본 파일명은 255자 이하여야 합니다.")
    return paths


def file_fingerprints(directory):
    result = {}
    for path in dictionary_files(directory):
        with path.open("rb") as handle:
            result[path.name] = hashlib.file_digest(handle, "sha256").hexdigest()
    return result


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_row(row, source_file, source_row, orders):
    def invalid(reason):
        raise TransferError(f"{source_file}:{source_row}: {reason}")

    if any("\x00" in value for value in row.values()):
        invalid("NUL 문자는 저장할 수 없습니다.")
    if not row["표제어"] or not re.fullmatch(r"\d{1,5}", row["동형어 번호"]):
        invalid("표제어 또는 동형어 번호를 확인하세요.")
    homonym = int(row["동형어 번호"])
    if homonym > 32767 or row["품사"] not in PARTS_OF_SPEECH:
        invalid("동형어 번호 또는 품사가 올바르지 않습니다.")
    subentry = row["구분"] == "부표제어"
    prefix = "부표제어 " if subentry else ""
    definition, examples = row[f"{prefix}뜻풀이"], row[f"{prefix}용례"]
    if not definition:
        invalid("뜻풀이가 비어 있습니다.")
    if subentry:
        match = re.fullmatch(r"\[(속담|관용구)\]\s*(.+)", row["부표제어"])
        if not match or row["뜻풀이"]:
            invalid("부표제어 형식 또는 뜻풀이 구분이 올바르지 않습니다.")
        entry_type = {"속담": "proverb", "관용구": "idiom"}[match[1]]
        headword, part_of_speech, homonym_number = match[2].strip(), "", 0
        consumed = {"부표제어", f"{prefix}뜻풀이", f"{prefix}용례"}
    else:
        entry_type = ENTRY_TYPES.get(row["구분"])
        if entry_type is None or row["부표제어"] or row["부표제어 뜻풀이"]:
            invalid("지원하지 않는 구분이거나 부표제어 내용이 섞여 있습니다.")
        headword, part_of_speech, homonym_number = row["표제어"], row["품사"], homonym
        consumed = {"표제어", "동형어 번호", "구분", "품사", "뜻풀이", "용례"}
    headword = unicodedata.normalize("NFC", headword)
    translations = {}
    for language, name in LANGUAGES.items():
        word_key, definition_key = f"{name} {prefix}대역어", f"{name} {prefix}대역어 뜻풀이"
        translations[language] = {"translation": row[word_key], "translatedDefinition": row[definition_key]}
        consumed.update((word_key, definition_key))
    key = (headword, homonym_number, part_of_speech, entry_type)
    if subentry:
        # Parent identity distinguishes otherwise identical idioms across entries.
        key += (row["표제어"], homonym, row["품사"])
    orders[key] += 1
    return {
        "headword": headword, "homonym_number": homonym_number, "part_of_speech": part_of_speech,
        "entry_type": entry_type, "sense_order": orders[key], "definition": definition,
        "translations": translations, "examples": examples,
        "metadata": {k: v for k, v in row.items() if v and k not in consumed},
        "source_file": source_file, "source_row": source_row,
    }


def workbook_rows(path):
    if path.suffix.lower() == ".xls":
        import xlrd

        book = xlrd.open_workbook(str(path), on_demand=True)
        try:
            if book.nsheets != 1:
                raise TransferError(f"{path.name}: 시트가 정확히 하나여야 합니다.")
            sheet = book.sheet_by_index(0)
            for index in range(sheet.nrows):
                if xlrd.XL_CELL_ERROR in sheet.row_types(index):
                    raise TransferError(f"{path.name}:{index + 1}: Excel 오류 셀이 있습니다.")
                yield [cell_text(value) for value in sheet.row_values(index)]
        finally:
            book.release_resources()
    else:
        from openpyxl import load_workbook

        book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            if len(book.worksheets) != 1:
                raise TransferError(f"{path.name}: 시트가 정확히 하나여야 합니다.")
            for index, cells in enumerate(book.worksheets[0].iter_rows(), 1):
                if any(cell.data_type in {"f", "e"} for cell in cells):
                    raise TransferError(f"{path.name}:{index}: 수식 또는 Excel 오류 셀이 있습니다.")
                yield [cell_text(cell.value) for cell in cells]
        finally:
            book.close()


@dataclass
class DictionarySnapshot:
    directory: Path
    fingerprints: dict
    staged: TextIO
    hashes: dict
    counts: Counter
    types: Counter
    missing: Counter
    sample: list

    def records(self):
        self.staged.seek(0)
        for line in self.staged:
            yield json.loads(line)

    def close(self):
        self.staged.close()


def capture_dictionary(directory, progress=None):
    staged = TemporaryFile(mode="w+t", encoding="utf-8")
    try:
        fingerprints = file_fingerprints(directory)
        orders, counts, types, missing = Counter(), Counter(), Counter(), Counter()
        hashes, sample = {}, []
        for path in dictionary_files(directory):
            if progress:
                progress(f"{path.name} 검증 중")
            rows = workbook_rows(path)
            try:
                headers = next(rows, [])
                if len(headers) != len(HEADERS) or set(headers) != set(HEADERS):
                    raise TransferError(f"{path.name}: 한국어기초사전 원본 74개 열이 필요합니다.")
                for index, values in enumerate(rows, 2):
                    if len(values) != len(headers):
                        raise TransferError(f"{path.name}:{index}: 원본 열 수가 다릅니다.")
                    record = parse_row(dict(zip(headers, values)), path.name, index, orders)
                    hashes[(path.name, index)] = digest(record)
                    counts[path.name] += 1
                    types[record["entry_type"]] += 1
                    for language, pair in record["translations"].items():
                        if not all(pair.values()):
                            missing[language] += 1
                    staged.write(json.dumps(record, ensure_ascii=False) + "\n")
                    if len(sample) < 5:
                        sample.append({k: record[k] for k in ("headword", "part_of_speech", "definition")})
            finally:
                rows.close()
            if not counts[path.name]:
                raise TransferError(f"{path.name}: 사전 데이터가 없습니다.")
            if progress:
                progress(f"{path.name}: {counts[path.name]:,}행 검증 완료")
        if not types["word"]:
            raise TransferError("단어 항목이 포함된 전체 원본 파일을 준비하세요.")
        if fingerprints != file_fingerprints(directory):
            raise TransferError("검증 중 원본 파일이 변경되었습니다. 다시 확인하세요.")
        staged.flush()
        return DictionarySnapshot(Path(directory), fingerprints, staged, hashes, counts, types, missing, sample)
    except Exception as exc:
        staged.close()
        if isinstance(exc, TransferError):
            raise
        raise TransferError("사전 파일을 읽지 못했습니다. 파일 형식·접근 권한과 xlrd/openpyxl 설치를 확인하세요.") from None
