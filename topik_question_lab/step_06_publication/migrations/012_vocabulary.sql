CREATE TABLE topik_bank.vocabulary (
    vocabulary_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    korean TEXT NOT NULL CHECK (btrim(korean) <> ''),
    part_of_speech TEXT NOT NULL DEFAULT '',
    english TEXT NOT NULL CHECK (btrim(english) <> ''),
    vietnamese TEXT NOT NULL CHECK (btrim(vietnamese) <> ''),
    japanese TEXT NOT NULL CHECK (btrim(japanese) <> ''),
    example_korean TEXT NOT NULL CHECK (btrim(example_korean) <> ''),
    example_english TEXT NOT NULL CHECK (btrim(example_english) <> ''),
    topik_level SMALLINT NOT NULL CHECK (topik_level BETWEEN 3 AND 6),
    UNIQUE (korean, part_of_speech)
);
