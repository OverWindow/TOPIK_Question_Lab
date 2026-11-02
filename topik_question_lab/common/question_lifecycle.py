"""Compatible reads for databases created before the question trash feature."""


def has_trash_column(connection):
    return any(row[1] == "trashed_at" for row in connection.execute("PRAGMA table_info(generated_questions)"))


def active_condition(connection, alias="g"):
    return f"{alias}.trashed_at IS NULL" if has_trash_column(connection) else "1=1"


def guard_active_edit(connection, question_id):
    connection.execute("BEGIN IMMEDIATE")
    if has_trash_column(connection):
        row = connection.execute("SELECT trashed_at FROM generated_questions WHERE id=?", (question_id,)).fetchone()
        if row and row[0] is not None:
            raise ValueError("휴지통에 있는 문제입니다. 복원 후 수정·검수하세요.")
