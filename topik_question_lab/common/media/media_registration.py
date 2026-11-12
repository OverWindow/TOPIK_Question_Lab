"""Atomic registration with durable, verifiable media receipts."""
from __future__ import annotations

from topik_question_lab.common.generation.exam_profiles import question_count, requires_material, profile_for
from topik_question_lab.common.media.media_provider import MediaError
from topik_question_lab.common.question_bank.direct_publication import DirectPublicationService, PublicationBlocked, _dicts, _hash, check_registered_set, make_package, package_draft, read_batches, require_target_schema
from topik_question_lab.common.question_bank.postgres_storage import _psycopg, _upsert_item
from topik_question_lab.common.question_bank.audio_bindings import bind_audio, require_item_audio_schema


def require_media_schema(c):
    require_target_schema(c)
    try:
        require_item_audio_schema(c)
    except ValueError as exc:
        raise PublicationBlocked(str(exc)) from None
    for table in ("items", "question_sets"):
        checks = c.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s::regclass AND contype='c'",
                           (f"topik_bank.{table}",)).fetchall()
        if not any("pending_review" in row[0] for row in checks):
            raise PublicationBlocked("자동 등록에는 마이그레이션 009_pending_media_review가 필요합니다.")
    for table in ("tts_audio_assets", "item_visual_assets"):
        c.execute(f"SELECT storage_backend,file_sha256,storage_url FROM topik_bank.{table} LIMIT 0")
        checks = c.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s::regclass AND contype='c'",
                           (f"topik_bank.{table}",)).fetchall()
        if any("storage_backend" in r[0] and "'s3'" not in r[0] for r in checks):
            raise PublicationBlocked("S3 저장소를 허용하는 마이그레이션 008_s3_media가 필요합니다.")


def check_media(c, package):
    assets = package["media"]
    expected_audio = {}
    expected_visual = {}
    for asset in assets:
        if asset["storage_backend"] != "s3" or not asset["storage_url"].startswith("https://") or len(asset["file_sha256"]) != 64:
            raise PublicationBlocked("검증된 S3 미디어 주소·해시가 필요합니다.")
        if asset["kind"] == "audio":
            for position in asset["positions"]:
                if position in expected_audio:
                    raise PublicationBlocked("음원 문항 연결이 중복되었습니다.")
                expected_audio[position] = (asset["audio_asset_id"], asset["binding_source_hash"])
        else:
            key = (asset["item_id"], asset["content_hash"], asset["visual_role"], asset["option_number"])
            if key in expected_visual:
                raise PublicationBlocked("이미지 문항 연결이 중복되었습니다.")
            expected_visual[key] = asset["visual_asset_id"]
    draft = package_draft(package)
    required_audio = set(range(1, question_count(draft.exam_format) + 1)) if draft.section == "listening" else set()
    required_visual = set()
    for item in draft.items:
        candidate = item.candidate
        for option in candidate.question.get("visual_options") or []:
            required_visual.add((str(candidate.item_id), item.content_hash, "choice", option["number"]))
        if requires_material(draft.section, candidate.slot, candidate.question):
            required_visual.add((str(candidate.item_id), item.content_hash, "material", 1))
    if set(expected_audio) != required_audio or set(expected_visual) != required_visual:
        raise PublicationBlocked("필수 음원·이미지 전체가 준비되어야 DB에 등록할 수 있습니다.")
    from topik_question_lab.common.media.audio_composer import narration_script
    for asset in assets:
        if asset["kind"] != "audio":
            continue
        positions = asset["positions"]
        leader = min(positions)
        first = next((i.candidate for i in draft.items if i.candidate.slot == leader), None)
        if first is None:
            raise PublicationBlocked("음원 문항 번호가 세트에 없습니다.")
        profile = profile_for("listening", first.question_type, first.question.get("generation_profile"))
        expected_positions = list(profile.question_numbers) if profile.shared_script else [leader]
        if positions != expected_positions:
            raise PublicationBlocked("시험 음원 그룹 구성이 다릅니다.")
        expected_script = narration_script([i.candidate for i in draft.items if i.candidate.slot in positions])
        if any(asset["script_snapshot"].get(k) != v for k, v in expected_script.items()):
            raise PublicationBlocked("음원 생성 대본이 현재 문항과 다릅니다.")
    bindings = c.execute("""SELECT i.position,b.audio_asset_id,b.source_hash
        FROM topik_bank.item_audio_bindings b JOIN topik_bank.items i ON i.item_id=b.item_id
        WHERE i.set_id=%s AND b.is_current AND b.content_hash=i.content_hash""",
                         (package["set_id"],)).fetchall()
    if {r[0]: (str(r[1]), r[2]) for r in bindings} != expected_audio:
        raise PublicationBlocked("DB의 음원 연결이 준비한 전체 목록과 다릅니다.")
    images = c.execute("""SELECT a.item_id,a.content_hash,a.visual_role,a.option_number,a.visual_asset_id
        FROM topik_bank.item_visual_assets a JOIN topik_bank.items i ON i.item_id=a.item_id
        WHERE i.set_id=%s AND a.is_current""", (package["set_id"],)).fetchall()
    if {(str(r[0]), r[1], r[2], r[3]): str(r[4]) for r in images} != expected_visual:
        raise PublicationBlocked("DB의 이미지 연결이 준비한 전체 목록과 다릅니다.")
    for asset in assets:
        audio = asset["kind"] == "audio"
        table, id_name = ("tts_audio_assets", "audio_asset_id") if audio else ("item_visual_assets", "visual_asset_id")
        rows = _dicts(c.execute(f"SELECT * FROM topik_bank.{table} WHERE {id_name}=%s", (asset[id_name],)))
        expected = {k: v for k, v in asset.items() if k not in ("kind", "positions", "binding_source_hash")}
        if not rows or any(str(rows[0][k]) != v if k.endswith("_id") else rows[0][k] != v for k, v in expected.items()):
            raise PublicationBlocked("DB에 저장된 미디어 주소·해시·설정이 다릅니다.")
        if audio and rows[0]["deleted_at"] is not None:
            raise PublicationBlocked("삭제 처리된 음원입니다.")


class MediaRegistration(DirectPublicationService):
    def preflight(self):
        from psycopg.conninfo import conninfo_to_dict
        config = conninfo_to_dict(self.target_url)
        if config.get("host") not in ("localhost", "127.0.0.1", "::1") or config.get("service") or (
            config.get("hostaddr") and config["hostaddr"] not in ("127.0.0.1", "::1")):
            raise PublicationBlocked("자동 등록 대상은 명시적인 로컬 PostgreSQL이어야 합니다.")
        try:
            with self.connect(readonly=True) as c:
                require_media_schema(c)
        except PublicationBlocked:
            raise
        except Exception:
            raise PublicationBlocked("로컬 PostgreSQL 연결·미디어 스키마를 확인하세요.") from None

    def package(self, batch_id):
        batch = next(b for b in read_batches(self.root) if b["id"] == batch_id)
        records = [r for r in self.catalog().records if r.candidate.batch_id == batch_id]
        package = make_package(batch, records, require_approval=False)
        reviewed = all(record.eligible for record in records)
        package["review_status"] = "reviewed" if reviewed else "pending_review"
        return package

    def _current(self, package):
        current = self.package(package["batch_id"])
        if _hash(current) != _hash({k: v for k, v in package.items() if k != "media"}):
            raise PublicationBlocked("생성 중 문항·검수 상태가 변경되었습니다. 변경분을 다시 실행하세요.")

    def _complete(self, c, package):
        return check_registered_set(c, package)

    def register(self, package):
        self._current(package)
        job = self.jobs.save(self.target, [package])
        # The previous receipt is also the optimistic concurrency baseline for edits.
        previous = [p for j in self.jobs.list() if j["target"] == self.target and j["status"] == "succeeded"
                    for p in j["payload"]["packages"] if p["batch_id"] == package["batch_id"] and "media" in p]
        self.jobs.update(job["id"], "publishing")
        try:
            with self.connect() as c:
                for lock in ("topik_bank:production_deployment", "topik_bank:set_membership", "topik_bank:item_sync"):
                    c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
                require_media_schema(c)
                self._current(package)
                existing = c.execute("SELECT review_status FROM topik_bank.question_sets WHERE set_id=%s FOR UPDATE", (package["set_id"],)).fetchone()
                if existing:
                    if existing[0] == "reviewed" and package.get("review_status") == "reviewed":
                        # A lost acknowledgement or double click must reuse the committed set.
                        self._complete(c, package)
                        complete = True
                    elif existing[0] != "pending_review":
                        raise PublicationBlocked("검수·운영 중인 세트는 자동 생성으로 덮어쓸 수 없습니다.")
                    else:
                        try:
                            complete = self._complete(c, package)["status"] == "complete"
                        except PublicationBlocked:
                            complete = False
                    if not complete:
                        if not previous:
                            raise PublicationBlocked("기존 검수 대기 세트의 등록 기록이 없어 덮어쓸 수 없습니다.")
                        self._complete(c, previous[0])
                        self._replace(c, package)
                else:
                    ids = [str(i.candidate.item_id) for i in package_draft(package).items]
                    if c.execute("SELECT 1 FROM topik_bank.items WHERE item_id=ANY(%s::uuid[]) LIMIT 1", (ids,)).fetchone():
                        raise PublicationBlocked("동일 ID의 기존 문항이 있어 자동 등록을 중단했습니다.")
                    self._insert(c, package)
                    self._bind(c, package)
                result = self._complete(c, package)
            # Check committed state through another connection, including consumer views.
            with self.connect(readonly=True) as c:
                result = self._complete(c, package)
            self.jobs.update(job["id"], "succeeded", {"sets": [result]})
            return job["id"]
        except Exception as exc:
            try:
                with self.connect(readonly=True) as c:
                    result = self._complete(c, package)
                if result["status"] == "complete":
                    self.jobs.update(job["id"], "succeeded", {"sets": [result]})
                    return job["id"]
                self.jobs.update(job["id"], "failed", message="DB 등록을 완료하지 못했습니다.")
            except Exception:
                self.jobs.update(job["id"], "outcome_unknown", message="DB 반영 결과를 다시 확인해야 합니다.")
            if isinstance(exc, PublicationBlocked):
                raise
            psycopg, _ = _psycopg()
            raise MediaError("DB 등록 또는 결과 확인에 실패했습니다.", retryable=isinstance(exc, psycopg.OperationalError)) from None

    def _replace(self, c, package):
        _, Jsonb = _psycopg()
        for item in package_draft(package).items:
            _upsert_item(c, item, Jsonb, review_status=package.get("review_status", "pending_review"))
        c.execute("UPDATE topik_bank.question_sets SET default_target_level=%s,review_status=%s WHERE set_id=%s", (package["level"], package.get("review_status", "pending_review"), package["set_id"]))
        c.execute("""UPDATE topik_bank.item_visual_assets SET is_current=FALSE WHERE item_id IN
                     (SELECT item_id FROM topik_bank.items WHERE set_id=%s)""", (package["set_id"],))
        self._bind(c, package)

    def _bind(self, c, package):
        _, Jsonb = _psycopg()
        for asset in package["media"]:
            audio = asset["kind"] == "audio"
            table, id_name = ("tts_audio_assets", "audio_asset_id") if audio else ("item_visual_assets", "visual_asset_id")
            values = {k: v for k, v in asset.items() if k not in ("kind", "positions", "binding_source_hash")}
            parameters = [Jsonb(v) if isinstance(v, dict) else v for v in values.values()]
            c.execute(f"INSERT INTO topik_bank.{table}({','.join(values)}) VALUES ({','.join(['%s'] * len(values))}) ON CONFLICT ({id_name}) DO NOTHING", parameters)
            if audio:
                for position in asset["positions"]:
                    row = c.execute("SELECT item_id,content_hash FROM topik_bank.items WHERE set_id=%s AND position=%s",
                                    (package["set_id"], position)).fetchone()
                    if row is None:
                        raise PublicationBlocked("음원을 연결할 문항이 세트에 없습니다.")
                    bind_audio(c, row[0], row[1], asset[id_name], asset["binding_source_hash"])
            else:
                c.execute("UPDATE topik_bank.item_visual_assets SET is_current=TRUE WHERE visual_asset_id=%s", (asset[id_name],))
