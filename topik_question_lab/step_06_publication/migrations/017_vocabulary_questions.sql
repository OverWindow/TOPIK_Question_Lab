CREATE TABLE topik_bank.vocabulary_questions (
    question_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vocabulary_id BIGINT NOT NULL REFERENCES topik_bank.vocabulary(vocabulary_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    question_type TEXT NOT NULL CHECK (question_type IN ('sentence_blank','conjugation','synonym','antonym')),
    position SMALLINT NOT NULL CHECK (position BETWEEN 1 AND 7),
    instruction TEXT NOT NULL CHECK (btrim(instruction) <> ''),
    stem TEXT NOT NULL CHECK (btrim(stem) <> ''),
    choices JSONB NOT NULL CHECK (CASE WHEN jsonb_typeof(choices)='array' THEN jsonb_array_length(choices)=4 ELSE FALSE END),
    correct_answer SMALLINT NOT NULL CHECK (correct_answer BETWEEN 1 AND 4),
    accepted_answers JSONB NOT NULL DEFAULT '[]'::jsonb,
    explanation TEXT NOT NULL CHECK (btrim(explanation) <> ''),
    provider TEXT NOT NULL CHECK (btrim(provider) <> ''),
    model TEXT NOT NULL CHECK (btrim(model) <> ''),
    generation_id UUID NOT NULL,
    source_hash TEXT NOT NULL CHECK (source_hash ~ '^[a-f0-9]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (vocabulary_id, position),
    CHECK ((question_type='sentence_blank' AND position BETWEEN 1 AND 3)
        OR (question_type='conjugation' AND position BETWEEN 4 AND 6)
        OR (question_type IN ('synonym','antonym') AND position=7)),
    CHECK (CASE WHEN jsonb_typeof(accepted_answers)='array'
        THEN CASE WHEN question_type='sentence_blank' THEN jsonb_array_length(accepted_answers)>0
                  ELSE jsonb_array_length(accepted_answers)=0 END ELSE FALSE END)
);
