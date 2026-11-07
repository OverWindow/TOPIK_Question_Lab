"""Read-only, paginated vocabulary queries for the local bank."""
from __future__ import annotations

from topik_question_lab.step_06_publication.postgres_storage import _dict_rows, _psycopg
from topik_question_lab.step_06_publication.vocabulary_audio import AUDIO_FIELDS
from topik_question_lab.step_06_publication.vocabulary_languages import LANGUAGE_FIELDS


PAGE_SIZE = 50
SEARCH_COLUMNS = ("korean", "english", "vietnamese", "japanese", "example_korean", "example_english")


def search_vocabulary(database_url: str, *, level: int | None = None, query: str = "", page: int = 1,
                      audio_filter: str = "all", audio_profile: dict | None = None, page_size: int = PAGE_SIZE) -> dict:
    if (level not in (None, 3, 4, 5, 6) or page < 1 or not 1 <= page_size <= 100
            or audio_filter not in ("all", "present", "missing", "stale")):
        raise ValueError("단어장 급수 또는 페이지가 올바르지 않습니다.")
    result = {
        "available": False, "message": "단어장 DB 연결이 설정되지 않았습니다.",
        "rows": [], "total": 0, "page": 1, "pages": 1,
        "level_counts": {level: 0 for level in range(3, 7)},
        "audio_schema": False, "audio_count": 0,
    }
    if not database_url.strip():
        return result
    conditions, params = [], []
    if level is not None:
        conditions.append("topik_level = %s")
        params.append(level)
    query = query.strip()
    try:
        psycopg, Jsonb = _psycopg()
        with psycopg.connect(database_url, connect_timeout=8,
                             options="-c default_transaction_read_only=on -c statement_timeout=15000") as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            if connection.execute("SELECT to_regclass('topik_bank.vocabulary')").fetchone()[0] is None:
                result["message"] = "단어장이 아직 준비되지 않았습니다. 단어장 CSV를 먼저 등록하세요."
                return result
            columns = {row[0] for row in connection.execute("""SELECT attname FROM pg_attribute
                WHERE attrelid='topik_bank.vocabulary'::regclass AND attnum>0 AND NOT attisdropped""")}
            language_fields = tuple(name for name in LANGUAGE_FIELDS if name in columns)
            if query:
                # Search literal text, including legacy banks without added languages.
                pattern = "%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
                search_columns = SEARCH_COLUMNS + language_fields
                conditions.append("(" + " OR ".join(f"{column} ILIKE %s ESCAPE '!'" for column in search_columns) + ")")
                params.extend([pattern] * len(search_columns))
            has_audio = all(name in columns for name in AUDIO_FIELDS)
            if has_audio:
                result["audio_schema"] = True
                result["audio_count"] = connection.execute(
                    "SELECT count(*) FROM topik_bank.vocabulary WHERE audio_storage_path IS NOT NULL").fetchone()[0]
                if audio_filter in ("present", "stale"):
                    conditions.append("audio_storage_path IS NOT NULL")
                elif audio_filter == "missing":
                    conditions.append("audio_storage_path IS NULL")
                if audio_filter == "stale":
                    if audio_profile is None:
                        conditions.append("audio_generation_config->>'text' IS DISTINCT FROM btrim(korean)")
                    else:
                        conditions.append("audio_generation_config IS DISTINCT FROM (%s::jsonb || jsonb_build_object('text', btrim(korean)))")
                        params.append(Jsonb(audio_profile))
            elif audio_filter in ("present", "stale"):
                conditions.append("FALSE")
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            counts = dict(connection.execute(
                "SELECT topik_level, count(*) FROM topik_bank.vocabulary GROUP BY topik_level"
            ).fetchall())
            total = connection.execute("SELECT count(*) FROM topik_bank.vocabulary" + where, params).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            cursor = connection.execute(
                "SELECT vocabulary_id, korean, part_of_speech, english, vietnamese, japanese, "
                "example_korean, example_english, topik_level"
                + (", " + ", ".join(language_fields) if language_fields else "")
                + (", " + ", ".join(AUDIO_FIELDS) if has_audio else "") + " FROM topik_bank.vocabulary" + where
                + " ORDER BY topik_level, korean, part_of_speech, vocabulary_id LIMIT %s OFFSET %s",
                [*params, page_size, (page - 1) * page_size],
            )
            rows = _dict_rows(cursor, cursor.fetchall())
        result.update(available=True, message="", rows=rows, total=total, page=page, pages=pages,
                      level_counts={level: counts.get(level, 0) for level in range(3, 7)})
    except Exception:
        # Driver errors may contain credentials or connection strings.
        result["message"] = "단어장을 불러오지 못했습니다. DB 연결 상태를 확인하고 새로고침하세요."
    return result
