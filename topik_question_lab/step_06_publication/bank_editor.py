"""Direct, transactional editing of registered questions without local originals."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uuid

from pydantic import ValidationError

from topik_question_lab.common.models import GeneratedQuestion
from topik_question_lab.common.listening_models import GeneratedListeningQuestion
from topik_question_lab.step_02_planning.prompt_profiles import QUESTION_TYPE_PROFILES
from topik_question_lab.step_02_planning.listening_profiles import LISTENING_TYPE_PROFILES
from topik_question_lab.step_04_review.validation import validate_question
from topik_question_lab.step_04_review.listening_validation import validate_listening_question
from topik_question_lab.step_06_publication.direct_publication import _hash, _target_fingerprint, PublicationBlocked
from topik_question_lab.step_06_publication.media_registration import require_media_schema
from topik_question_lab.step_06_publication.postgres_storage import _psycopg, PublicationStateChanged
from topik_question_lab.step_06_publication.publication import display_stem, display_choices, normalize_text
from topik_question_lab.step_06_publication.storage_overview import visual_requirements


LOCKS = ("topik_bank:production_deployment", "topik_bank:set_membership", "topik_bank:item_sync")
FIXED_FIELDS = ("question_type", "type_slot", "set_id")
MEDIA_TABLES = ("item_audio_bindings", "question_set_item_audio_bindings", "item_visual_assets")


def question_profile(row):
    profiles = QUESTION_TYPE_PROFILES if row["section"] == "reading" else LISTENING_TYPE_PROFILES
    profile = profiles.get(row["item_type"])
    if row["section"] not in ("reading", "listening") or profile is None:
        raise PublicationBlocked("지원하는 읽기·듣기 유형의 문항을 선택하세요.")
    return profile


def shared_members(snapshot):
    row = snapshot["item"]
    profile = question_profile(row)
    shared = profile.shared_passage if row["section"] == "reading" else profile.shared_script
    if not shared:
        return [row]
    return [r for r in snapshot["items"] if r["item_type"] == row["item_type"]
            and r["section"] == row["section"] and r["type_slot"] in profile.question_numbers]


def checked_question(row, question):
    question = deepcopy(question)
    old = row["content_json"]
    if any(question.get(k) != old.get(k) for k in FIXED_FIELDS):
        raise PublicationBlocked("세트·유형·문항 번호는 변경할 수 없습니다.")
    if question.get("question_type") != row["item_type"] or question.get("type_slot") != row["type_slot"]:
        raise PublicationBlocked("DB에 저장된 유형·번호와 문제 내용이 다릅니다.")
    model = GeneratedQuestion if row["section"] == "reading" else GeneratedListeningQuestion
    try:
        parsed = model.model_validate(question)
    except ValidationError:
        raise PublicationBlocked("문항 형식을 확인하세요. 필수 내용과 정답 1~4가 필요합니다.") from None
    # Preserve unknown stored fields while normalizing the fields understood by this app.
    question.update(parsed.model_dump())
    if row["section"] == "reading":
        if question_profile(row).content_mode != "single_sentence" and any(str(question.get(k, "")).strip() for k in ("passage", "question_prompt", "auxiliary_text")):
            question["stem"] = display_stem(SimpleNamespace(section="reading", question=question))
        parsed = model.model_validate(question)
    issues = (validate_question if row["section"] == "reading" else validate_listening_question)(parsed, [])
    errors = [i.message for i in issues if i.severity == "error" and i.code != "repeat_count"]
    if row["section"] == "listening" and question.get("visual_options"):
        if sorted(o["number"] for o in question["visual_options"]) != [1, 2, 3, 4]:
            errors.append("이미지 선지 번호는 중복 없이 1~4여야 합니다.")
        if question.get("choices") and (len(question["choices"]) != 4 or any(not c.strip() for c in question["choices"])):
            errors.append("텍스트 선지를 함께 사용할 때는 비어 있지 않은 선지 4개가 필요합니다.")
    if errors:
        raise PublicationBlocked(" / ".join(errors))
    return question


def content_values(row, question, metadata):
    try:
        level, difficulty = int(metadata["target_level"]), float(metadata["predicted_difficulty"])
        skill = str(metadata["primary_skill"]).strip()
        if not 1 <= level <= 6 or not -3 <= difficulty <= 3 or not skill:
            raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError):
        raise PublicationBlocked("급수 1~6, 예상 난이도 -3~3, 평가 요소를 확인하세요.") from None
    candidate = SimpleNamespace(section=row["section"], question=question)
    stem, choices = display_stem(candidate), display_choices(candidate)
    if row["section"] == "reading" and question_profile(row).content_mode == "single_sentence":
        stem = question["stem"].strip()
    payload = {k: row[k] for k in ("section", "item_type", "generator_provider", "generator_model", "generator_version", "prompt_version")}
    payload.update(primary_skill=skill, target_level=level, predicted_difficulty=round(difficulty, 4),
                   review_status="reviewed", stem=stem, choices=choices, correct_answer=question["answer"],
                   explanation=question["explanation"], content=question)
    provenance = deepcopy(row["source_provenance"])
    provenance.setdefault("publication_metadata", {})["predicted_difficulty"] = difficulty
    return {"content_json": question, "stem": stem, "choices": choices, "correct_answer": question["answer"],
            "explanation": question["explanation"], "primary_skill": skill, "target_level": level,
            "stem_length": len(normalize_text(stem)), "choice_count": len(choices), "content_hash": _hash(payload),
            "source_provenance": provenance}


def item_metadata(row):
    return {"target_level": row["target_level"], "primary_skill": row["primary_skill"],
            "predicted_difficulty": row["source_provenance"].get("publication_metadata", {}).get("predicted_difficulty", 0.0)}


def audio_signature(question):
    return {"dialogue_turns": question.get("dialogue_turns", []),
            "question_prompt": question.get("question_prompt", ""), "repeat_count": question.get("repeat_count", 1)}


def visual_signature(row, role, number):
    question = row["content_json"]
    if role == "material":
        return [question.get("visual_material"), question.get("passage") or question.get("stem")]
    option = next((o for o in question.get("visual_options", []) if o["number"] == number), {})
    return [question.get("visual_kind", "none"), option.get("description", ""), option.get("image_prompt", ""), option.get("chart_spec")]


def audio_members(snapshot):
    row = snapshot["item"]
    if row["section"] != "listening":
        return []
    slot = row["type_slot"]
    positions = [slot] if slot <= 20 else [21 + (slot - 21) // 2 * 2, 22 + (slot - 21) // 2 * 2]
    return [r for r in snapshot["items"] if r["section"] == "listening" and r["type_slot"] in positions]


def audio_script(rows):
    from topik_question_lab.step_03_generation.audio_composer import narration_script
    script = narration_script([SimpleNamespace(slot=r["type_slot"], question=r["content_json"]) for r in rows])
    repeats = {r["content_json"].get("repeat_count", 1) for r in rows}
    if len(repeats) != 1:
        raise PublicationBlocked("공유 음원 문항의 반복 횟수가 다릅니다.")
    repeat = repeats.pop()
    segments = [s for s in script["segments"] if s["kind"] != "silence"]
    if repeat == 1:
        segments = [s for s in segments if s.get("role") != "reread" and s.get("repeatIndex") != 2]
    elif len(rows) == 1:
        segments += [{"kind": "speech", "role": "reread", "speaker": "여자", "text": "다시 읽겠습니다."},
                     {"kind": "dialogue", "repeatIndex": 2, "turns": rows[0]["content_json"]["dialogue_turns"]}]
    script["segments"] = []
    for segment in segments:
        if script["segments"]:
            script["segments"].append({"kind": "silence", "durationMs": 1000})
        script["segments"].append(segment)
    return script


def prepare_edit(snapshot, question, metadata, replacements=()):
    row = snapshot["item"]
    if row["review_status"] == "retired":
        raise PublicationBlocked("사용 중지한 문항은 먼저 복원하세요.")
    question = checked_question(row, question)
    members = shared_members(snapshot)
    field = "passage" if row["section"] == "reading" else "dialogue_turns"
    shared_fields = [field] if row["section"] == "reading" else [field, "repeat_count"]
    shared_changed = any(question.get(k) != row["content_json"].get(k) for k in shared_fields)
    profile = question_profile(row)
    shared = profile.shared_passage if row["section"] == "reading" else profile.shared_script
    if shared_changed and shared and len(members) != len(profile.question_numbers):
        raise PublicationBlocked("공통 문항 구성이 불완전합니다. 연결 문항을 확인하세요.")
    changes = {}
    for member in members:
        own = member["item_id"] == row["item_id"]
        if not own and not shared_changed:
            continue
        if not own and member["review_status"] == "retired":
            raise PublicationBlocked("공통 지문·대본에 사용 중지한 문항이 있습니다. 먼저 복원하세요.")
        value = deepcopy(question if own else member["content_json"])
        if not own:
            value.update({k: deepcopy(question.get(k)) for k in shared_fields})
            value = checked_question(member, value)
        values = content_values(member, value, metadata if own else item_metadata(member))
        if any(values[k] != member[k] for k in values):
            changes[member["item_id"]] = values
    replacements = [dict(r) for r in replacements]
    targets = set()
    required = set(visual_requirements(row["section"], row["type_slot"], question))
    for replacement in replacements:
        kind = replacement.get("kind")
        target = ("audio",) if kind == "audio" else (replacement.get("visual_role"), replacement.get("option_number"))
        if target in targets or (kind == "audio" and row["section"] != "listening") or (kind == "image" and target not in required) or kind not in ("audio", "image"):
            raise PublicationBlocked("미디어 교체 대상이 올바르지 않습니다.")
        if not isinstance(replacement.get("data"), bytes) or not replacement["data"]:
            raise PublicationBlocked("교체할 파일이 비어 있습니다.")
        targets.add(target)
    audio_rows = audio_members(snapshot)
    if any(r["kind"] == "audio" for r in replacements):
        expected = 1 if row["type_slot"] <= 20 else 2
        if len(audio_rows) != expected:
            raise PublicationBlocked("공유 음원 문항 구성이 불완전합니다.")
        audio_script([{**r, "content_json": changes.get(r["item_id"], {}).get("content_json", r["content_json"])} for r in audio_rows])
    if replacements and row["item_id"] not in changes:
        changes[row["item_id"]] = content_values(row, question, metadata)
    affected = set(changes)
    invalidate_audio = any(r["section"] == "listening" and r["item_id"] in changes
                           and audio_signature(r["content_json"]) != audio_signature(changes[r["item_id"]]["content_json"])
                           for r in snapshot["items"])
    if invalidate_audio or any(r["kind"] == "audio" for r in replacements):
        affected.update(r["item_id"] for r in audio_rows)
    if not affected:
        raise PublicationBlocked("변경한 내용이 없습니다.")
    return {"snapshot": snapshot, "question": question, "metadata": dict(metadata), "changes": changes, "affected": sorted(affected),
            "invalidate_audio": invalidate_audio, "replacements": replacements}


class BankEditor:
    def __init__(self, url):
        self.url, self.target = url, _target_fingerprint(url)

    def connect(self, readonly=False):
        if not self.url:
            raise PublicationBlocked("PostgreSQL 연결이 필요합니다.")
        psycopg, _ = _psycopg()
        return psycopg.connect(self.url, connect_timeout=8, prepare_threshold=None,
                               options="-c statement_timeout=15000" + (" -c default_transaction_read_only=on" if readonly else ""))

    def load(self, item_id):
        try:
            with self.connect(True) as c:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                require_media_schema(c)
                return self._snapshot(c, str(uuid.UUID(str(item_id))))
        except (PublicationBlocked, PublicationStateChanged):
            raise
        except Exception:
            raise PublicationBlocked("문항 조회에 실패했습니다. DB 연결·스키마를 확인하세요.") from None

    def _snapshot(self, c, item_id, *, lock=False):
        suffix = " FOR UPDATE" if lock else ""
        found = c.execute("SELECT to_jsonb(i) FROM topik_bank.items i WHERE item_id=%s" + suffix, (item_id,)).fetchone()
        if not found:
            raise PublicationStateChanged("문항이 삭제되었거나 존재하지 않습니다. 새로고침하세요.")
        item = found[0]
        if item["set_id"]:
            items = c.execute("SELECT to_jsonb(i) FROM topik_bank.items i WHERE set_id=%s ORDER BY item_id" + suffix, (item["set_id"],)).fetchall()
            set_row = c.execute("SELECT to_jsonb(s) FROM topik_bank.question_sets s WHERE set_id=%s" + suffix, (item["set_id"],)).fetchone()[0]
        else:
            provenance = item["source_provenance"]
            items = c.execute("""SELECT to_jsonb(i) FROM topik_bank.items i WHERE item_id=%s OR
                (set_id IS NULL AND section=%s AND item_type=%s AND source_provenance->>'source_db'=%s
                 AND source_provenance->>'run_id'=%s AND content_json->>'set_id'=%s AND %s<>'')
                ORDER BY item_id""" + suffix, (item_id, item["section"], item["item_type"], provenance.get("source_db"),
                str(provenance.get("run_id", "")), item["content_json"].get("set_id", ""), item["content_json"].get("set_id", ""))).fetchall()
            set_row = None
        rows = [r[0] for r in items]
        ids = [r["item_id"] for r in rows]
        media = {}
        for table in MEDIA_TABLES:
            condition, params = ("set_id=%s", (item["set_id"],)) if table == "question_set_item_audio_bindings" else ("item_id=ANY(%s::uuid[])", (ids,))
            media[table] = [r[0] for r in c.execute(f"SELECT to_jsonb(m) FROM topik_bank.{table} m WHERE is_current AND {condition} ORDER BY to_jsonb(m)::text" + suffix, params).fetchall()]
        asset_ids = sorted({m["audio_asset_id"] for t in MEDIA_TABLES[:2] for m in media[t]})
        media["tts_audio_assets"] = [r[0] for r in c.execute("SELECT to_jsonb(a) FROM topik_bank.tts_audio_assets a WHERE audio_asset_id=ANY(%s::uuid[]) ORDER BY audio_asset_id" + suffix, (asset_ids,)).fetchall()]
        return {"target": self.target, "item": item, "items": rows, "set": set_row, "media": media}

    def _lock_and_check(self, c, snapshot):
        if snapshot["target"] != self.target:
            raise PublicationStateChanged("DB 대상이 변경되었습니다. 다시 조회하세요.")
        for key in LOCKS:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        current = self._snapshot(c, snapshot["item"]["item_id"], lock=True)
        if current != snapshot:
            raise PublicationStateChanged("조회 이후 문항·세트·미디어가 변경되었습니다. 새로고침 후 다시 확인하세요.")

    def save(self, plan, *, uploader=None):
        snapshot = plan["snapshot"]
        plan = prepare_edit(snapshot, plan["question"], plan["metadata"], plan["replacements"])
        # Validate the baseline before uploading, then again inside the write transaction.
        if self.load(snapshot["item"]["item_id"]) != snapshot:
            raise PublicationStateChanged("조회 이후 문항이 변경되었습니다. 다시 확인하세요.")
        uploaded = []
        if plan["replacements"]:
            if uploader is None:
                uploader = upload_media
            uploaded = [uploader(r) for r in plan["replacements"]]
        _, Jsonb = _psycopg()
        try:
            with self.connect() as c:
                self._lock_and_check(c, snapshot)
                stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                for item_id in plan["affected"]:
                    values = deepcopy(plan["changes"].get(item_id, {}))
                    row = next(r for r in snapshot["items"] if r["item_id"] == item_id)
                    provenance = values.setdefault("source_provenance", deepcopy(row["source_provenance"]))
                    provenance["bank_edited_at"] = stamp
                    values["review_status"] = "pending_review"
                    names = list(values)
                    c.execute("UPDATE topik_bank.items SET " + ",".join(f"{k}=%s" for k in names) + ",updated_at=CURRENT_TIMESTAMP WHERE item_id=%s",
                              tuple(Jsonb(values[k]) if isinstance(values[k], (dict, list)) else values[k] for k in names) + (item_id,))
                self._media(c, plan, uploaded, Jsonb)
                self._set_pending(c, snapshot)
            return {"changed": len(plan["affected"]), "message": "수정 내용을 저장했습니다. 검수 대기 상태입니다."}
        except (PublicationBlocked, PublicationStateChanged):
            raise
        except Exception:
            raise PublicationBlocked("저장 결과를 확인하지 못했습니다. 새로고침하여 현재 내용을 확인하세요.") from None

    def _set_pending(self, c, snapshot):
        if snapshot["set"]:
            c.execute("UPDATE topik_bank.question_sets SET review_status='pending_review' WHERE set_id=%s AND review_status<>'retired'", (snapshot["set"]["set_id"],))

    def _media(self, c, plan, uploaded, Jsonb):
        snapshot, changes = plan["snapshot"], plan["changes"]
        selected = snapshot["item"]
        old_media = snapshot["media"]
        audio_replacement = next((u for u in uploaded if u["kind"] == "audio"), None)
        audio_ids = [r["item_id"] for r in audio_members(snapshot)]
        if plan["invalidate_audio"] or audio_replacement:
            c.execute("UPDATE topik_bank.item_audio_bindings SET is_current=false WHERE item_id=ANY(%s::uuid[])", (audio_ids,))
            if selected["set_id"]:
                positions = [r["position"] for r in audio_members(snapshot)]
                c.execute("UPDATE topik_bank.question_set_item_audio_bindings SET is_current=false WHERE set_id=%s AND position=ANY(%s)", (selected["set_id"], positions))
        for row in snapshot["items"]:
            if row["item_id"] not in changes:
                continue
            new = {**row, **changes[row["item_id"]]}
            if row["item_id"] not in audio_ids or not (plan["invalidate_audio"] or audio_replacement):
                for binding in old_media["item_audio_bindings"]:
                    if binding["item_id"] == row["item_id"] and binding["content_hash"] == row["content_hash"]:
                        c.execute("UPDATE topik_bank.item_audio_bindings SET is_current=false WHERE item_id=%s AND content_hash=%s", (row["item_id"], row["content_hash"]))
                        c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
                            VALUES (%s,%s,%s,%s) ON CONFLICT(item_id,content_hash,audio_asset_id) DO UPDATE SET is_current=true""",
                            (row["item_id"], new["content_hash"], binding["audio_asset_id"], binding["source_hash"]))
            c.execute("UPDATE topik_bank.item_visual_assets SET is_current=false WHERE item_id=%s", (row["item_id"],))
            for asset in old_media["item_visual_assets"]:
                role, number = asset["visual_role"], asset["option_number"]
                replaced = row["item_id"] == selected["item_id"] and any(u["kind"] == "image" and u["visual_role"] == role and u["option_number"] == number for u in uploaded)
                if (asset["item_id"] == row["item_id"] and asset["content_hash"] == row["content_hash"] and not replaced
                        and visual_signature(row, role, number) == visual_signature(new, role, number)):
                    copy = {k: v for k, v in asset.items() if k not in ("created_at", "visual_asset_id")}
                    copy.update(visual_asset_id=str(uuid.uuid4()), content_hash=new["content_hash"], is_current=True)
                    self._insert_asset(c, "item_visual_assets", copy, Jsonb)
        for asset in uploaded:
            if asset["kind"] == "image":
                value = {k: v for k, v in asset.items() if k != "kind"}
                value.update(visual_asset_id=str(uuid.uuid4()), item_id=selected["item_id"],
                             content_hash=changes[selected["item_id"]]["content_hash"])
                self._insert_asset(c, "item_visual_assets", value, Jsonb)
            else:
                rows = [{**r, **changes.get(r["item_id"], {})} for r in audio_members(snapshot)]
                script = audio_script(rows)
                source_hash = _hash([script, {k: asset[k] for k in ("file_sha256", "storage_bucket", "storage_path", "storage_url")}])
                value = {k: v for k, v in asset.items() if k != "kind"}
                value.update(audio_asset_id=str(uuid.uuid4()), source_hash=source_hash, provider="manual_upload", model_name="manual_upload",
                             female_voice="manual", male_voice="manual", narration_version="exam_track_v4", script_snapshot=script)
                existing = c.execute("SELECT audio_asset_id FROM topik_bank.tts_audio_assets WHERE source_hash=%s AND model_name='manual_upload' AND female_voice='manual' AND male_voice='manual' AND deleted_at IS NULL", (source_hash,)).fetchone()
                if existing:
                    value["audio_asset_id"] = str(existing[0])
                else:
                    self._insert_asset(c, "tts_audio_assets", value, Jsonb)
                for row in rows:
                    c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
                        VALUES (%s,%s,%s,%s) ON CONFLICT(item_id,content_hash,audio_asset_id) DO UPDATE SET is_current=true""",
                        (row["item_id"], row["content_hash"], value["audio_asset_id"], source_hash))
                    if row["set_id"]:
                        c.execute("""INSERT INTO topik_bank.question_set_item_audio_bindings(set_id,position,audio_asset_id,source_hash)
                            VALUES (%s,%s,%s,%s) ON CONFLICT(set_id,position,audio_asset_id) DO UPDATE SET is_current=true,source_hash=EXCLUDED.source_hash""",
                            (row["set_id"], row["position"], value["audio_asset_id"], source_hash))

    @staticmethod
    def _insert_asset(c, table, values, Jsonb):
        c.execute(f"INSERT INTO topik_bank.{table}({','.join(values)}) VALUES ({','.join(['%s'] * len(values))})",
                  tuple(Jsonb(v) if isinstance(v, (dict, list)) else v for v in values.values()))

    def change_status(self, snapshot, action):
        if action not in ("retire", "restore", "approve", "delete"):
            raise PublicationBlocked("지원하지 않는 작업입니다.")
        try:
            with self.connect() as c:
                self._lock_and_check(c, snapshot)
                row = snapshot["item"]
                if action == "delete":
                    self._delete(c, row)
                else:
                    if action == "approve":
                        if row["review_status"] == "retired":
                            raise PublicationBlocked("사용 중지한 문항은 먼저 복원하세요.")
                        checked_question(row, row["content_json"])
                        profile = question_profile(row)
                        shared = profile.shared_passage if row["section"] == "reading" else profile.shared_script
                        if shared:
                            members = shared_members(snapshot)
                            fields = ("passage",) if row["section"] == "reading" else ("dialogue_turns", "repeat_count")
                            if len(members) != len(profile.question_numbers) or any(
                                    any(m["content_json"].get(k) != row["content_json"].get(k) for k in fields) for m in members):
                                raise PublicationBlocked("공통 지문·대본 문항의 구성과 내용이 일치해야 승인할 수 있습니다.")
                        if not media_ready(snapshot, row):
                            raise PublicationBlocked("필수 음원·이미지가 준비되지 않았습니다. 파일을 교체한 뒤 승인하세요.")
                    if action == "restore" and row["review_status"] != "retired":
                        raise PublicationBlocked("사용 중지한 문항만 복원할 수 있습니다.")
                    status = {"retire": "retired", "restore": "pending_review", "approve": "reviewed"}[action]
                    c.execute("UPDATE topik_bank.items SET review_status=%s,updated_at=CURRENT_TIMESTAMP WHERE item_id=%s", (status, row["item_id"]))
                self._set_pending(c, snapshot)
                if action == "approve" and snapshot["set"]:
                    current = self._snapshot(c, row["item_id"])
                    if set_health(current["items"])["ready"] and all(media_ready(current, r) for r in current["items"]):
                        c.execute("UPDATE topik_bank.question_sets SET review_status='reviewed' WHERE set_id=%s AND review_status<>'retired'", (row["set_id"],))
            return {"message": {"retire": "문항 사용을 중지했습니다.", "restore": "검수 대기로 복원했습니다.", "approve": "문항 검수를 승인했습니다.", "delete": "문항을 영구 삭제했습니다."}[action]}
        except (PublicationBlocked, PublicationStateChanged):
            raise
        except Exception:
            raise PublicationBlocked("처리 결과를 확인하지 못했습니다. 새로고침하여 현재 상태를 확인하세요.") from None

    def _delete(self, c, row):
        # Check referencing rows explicitly: a consumer's CASCADE must never delete usage history.
        from psycopg import sql
        cursor = c.execute("""SELECT n.nspname,r.relname,
            array_agg(a.attname ORDER BY k.ord),array_agg(b.attname ORDER BY k.ord)
            FROM pg_constraint f JOIN pg_class r ON r.oid=f.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
            CROSS JOIN LATERAL unnest(f.conkey,f.confkey) WITH ORDINALITY AS k(local_key,parent_key,ord)
            JOIN pg_attribute a ON a.attrelid=f.conrelid AND a.attnum=k.local_key
            JOIN pg_attribute b ON b.attrelid=f.confrelid AND b.attnum=k.parent_key
            WHERE f.contype='f' AND f.confrelid='topik_bank.items'::regclass GROUP BY f.oid,n.nspname,r.relname""")
        for schema, table, local, parent in cursor.fetchall():
            if schema == "topik_bank" and table in MEDIA_TABLES:
                continue
            condition = sql.SQL(" AND ").join(sql.SQL("r.{}=i.{}").format(sql.Identifier(a), sql.Identifier(b)) for a, b in zip(local, parent))
            query = sql.SQL("SELECT 1 FROM {} r JOIN topik_bank.items i ON {} WHERE i.item_id=%s LIMIT 1").format(sql.Identifier(schema, table), condition)
            if c.execute(query, (row["item_id"],)).fetchone():
                raise PublicationBlocked("문항을 참조하는 사용 기록이 있어 영구 삭제할 수 없습니다. 사용 중지를 이용하세요.")
        children = [f"topik_bank.{t}" for t in MEDIA_TABLES]
        if c.execute("SELECT 1 FROM pg_constraint WHERE contype='f' AND confrelid=ANY(%s::regclass[]) LIMIT 1", (children,)).fetchone():
            raise PublicationBlocked("미디어 연결을 참조하는 테이블이 있어 영구 삭제할 수 없습니다. 사용 중지를 이용하세요.")
        c.execute("DELETE FROM topik_bank.question_set_item_audio_bindings WHERE set_id=%s AND position=%s", (row["set_id"], row["position"]))
        c.execute("DELETE FROM topik_bank.item_audio_bindings WHERE item_id=%s", (row["item_id"],))
        c.execute("DELETE FROM topik_bank.item_visual_assets WHERE item_id=%s", (row["item_id"],))
        c.execute("DELETE FROM topik_bank.items WHERE item_id=%s", (row["item_id"],))


def media_ready(snapshot, row):
    media = snapshot["media"]
    visuals = {(v["visual_role"], v["option_number"]) for v in media["item_visual_assets"]
               if v["item_id"] == row["item_id"] and v["content_hash"] == row["content_hash"]}
    if not set(visual_requirements(row["section"], row["type_slot"], row["content_json"])).issubset(visuals):
        return False
    if row["section"] == "listening":
        assets = {a["audio_asset_id"] for a in media["tts_audio_assets"] if a["deleted_at"] is None and a["narration_version"] == "exam_track_v4"}
        return any(b["audio_asset_id"] in assets and b["item_id"] == row["item_id"] and b["content_hash"] == row["content_hash"] for b in media["item_audio_bindings"]) or any(
            b["audio_asset_id"] in assets and b["set_id"] == row["set_id"] and b["position"] == row["position"] for b in media["question_set_item_audio_bindings"])
    return True


def set_health(items):
    complete = len(items) == 50 and {r["position"] for r in items} == set(range(1, 51))
    retired = sum(r["review_status"] == "retired" for r in items)
    pending = sum(r["review_status"] == "pending_review" for r in items)
    return {"count": len(items), "complete": complete, "retired": retired, "pending": pending,
            "ready": complete and not retired and not pending,
            "label": "구성 미완성 · 사용 불가" if not complete else "사용 중지 문항 포함 · 사용 불가" if retired else "검수 대기" if pending else "검수 완료"}


def upload_media(replacement):
    from PIL import Image
    from topik_question_lab.step_03_generation.audio_composer import validate_mp3
    from topik_question_lab.step_03_generation.media_provider import MediaSettings
    from topik_question_lab.step_06_publication.media_cloud import S3MediaStore, digest_file, destination_key, cloudfront_url
    kind, data = replacement["kind"], replacement["data"]
    if len(data) > 100 * 1024 * 1024:
        raise PublicationBlocked("교체 파일은 100MB 이하여야 합니다.")
    with TemporaryDirectory(prefix="topik-bank-edit-") as directory:
        path = Path(directory) / ("upload.mp3" if kind == "audio" else "upload.png")
        if kind == "image":
            try:
                with Image.open(io.BytesIO(data)) as original:
                    original.load()
                    original.convert("RGBA").save(path, format="PNG")
            except Exception:
                raise PublicationBlocked("PNG·WebP·JPEG 이미지 파일을 확인하세요.") from None
            mime = "image/png"
            extra = {k: replacement[k] for k in ("visual_role", "option_number")}
        else:
            path.write_bytes(data)
            try:
                duration = validate_mp3(path, MediaSettings.from_env())
            except Exception:
                raise PublicationBlocked("재생 가능한 MP3와 ffmpeg·ffprobe 설정을 확인하세요.") from None
            mime, extra = "audio/mpeg", {"duration_ms": duration}
        size, digest = digest_file(path)
        store = None
        try:
            store = S3MediaStore(os.getenv("S3_BUCKET", ""), os.getenv("CLOUDFRONT_BASE_URL", ""))
            key = destination_key(store.bucket, digest, mime)
            row = {"kind": kind, "storage_backend": "s3", "storage_bucket": store.bucket, "storage_path": key,
                   "storage_url": cloudfront_url(store.base_url, key), "mime_type": mime,
                   "byte_size": size, "file_sha256": digest, **extra}
            store.upload(path, key, size, digest, mime)
            store.verify(row, local_path=path)
            return row
        except Exception:
            raise PublicationBlocked("S3·CloudFront 파일 검증에 실패했습니다. 설정과 파일을 확인한 뒤 다시 저장하세요.") from None
        finally:
            if store is not None:
                store.close()
