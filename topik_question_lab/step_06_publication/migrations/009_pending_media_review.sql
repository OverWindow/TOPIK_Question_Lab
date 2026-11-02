-- Authoring registration is distinct from human approval.
ALTER TABLE topik_bank.items DROP CONSTRAINT IF EXISTS items_review_status_check;
ALTER TABLE topik_bank.items ADD CONSTRAINT items_review_status_check
    CHECK (review_status IN ('pending_review','reviewed','pilot','active','retired'));
ALTER TABLE topik_bank.question_sets DROP CONSTRAINT IF EXISTS question_sets_review_status_check;
ALTER TABLE topik_bank.question_sets ADD CONSTRAINT question_sets_review_status_check
    CHECK (review_status IN ('pending_review','reviewed','pilot','active','retired'));
