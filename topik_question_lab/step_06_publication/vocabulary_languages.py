"""Additional vocabulary meanings, authored in the source CSV."""
from topik_question_lab.step_06_publication.postgres_storage import MIGRATION_DIR


LANGUAGE_FIELDS = ("chinese_simplified", "indonesian")
LANGUAGE_MIGRATION = MIGRATION_DIR / "015_vocabulary_languages.sql"


def apply_language_migration(connection):
    connection.execute(LANGUAGE_MIGRATION.read_text(encoding="utf-8"))
    if connection.execute("SELECT to_regclass('topik_bank.schema_migrations')").fetchone()[0]:
        connection.execute(
            "INSERT INTO topik_bank.schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
            (LANGUAGE_MIGRATION.stem,),
        )
