import uuid

import psycopg
from psycopg import sql
import pytest

from topik_question_lab.common.media.media_validation import file_digest, object_path, preview_local_media
from topik_question_lab.common.question_bank.postgres_storage import PostgresQuestionBank
from topik_question_lab.common.question_bank.publication import PublicationCandidate, PublicationItemMetadata, build_set_draft


def draft(section):
    candidates=[PublicationCandidate(section=section,question_type='media_integration',generated_id=n,
        run_id=1,provider_key='test',model_id='synthetic',backend='test',
        question={'type_slot':n,'stem':f'Synthetic {section} {n}','choices':['a','b','c','d'],
            'answer':1,'explanation':'test','target_grammar':'reading',
            **({'dialogue_turns':[{'speaker':'남자','text':'합성 테스트 대본'}],'question_prompt':'고르십시오.'} if section=='listening' else {})},
        review={'approved':True},validation=[],prompt_system='system',prompt_user='prompt',
        created_at='2026-01-01',source_db='synthetic.db') for n in range(1,51)]
    return build_set_draft(candidates,{c.source_key:PublicationItemMetadata(4,0.0,'synthetic skill') for c in candidates},4,0.0)


def seed(source,root):
    bank=PostgresQuestionBank(source)
    sets=[bank.publish_set(draft(s)).set_id for s in ('reading','listening')]
    image=b'synthetic-image';audio=b'synthetic-audio'
    image_id,audio_id=uuid.uuid4(),uuid.uuid4()
    author=uuid.uuid4()
    with psycopg.connect(source) as c:
        item_id,content_hash=c.execute('SELECT item_id,content_hash FROM topik_bank.items WHERE set_id=%s AND position=10',(sets[0],)).fetchone()
        c.execute("""INSERT INTO topik_bank.item_visual_assets(visual_asset_id,item_id,content_hash,option_number,visual_role,
          storage_bucket,storage_path,storage_url,mime_type,byte_size,created_by,file_sha256)
          VALUES (%s,%s,%s,1,'material','images','graph.png','http://localhost:4000/graph.png','image/png',%s,%s,%s)""",
          (image_id,item_id,content_hash,len(image),author,file_digest(image)))
        c.execute("""INSERT INTO topik_bank.tts_audio_assets(audio_asset_id,source_hash,model_name,female_voice,male_voice,
          storage_bucket,storage_path,storage_url,byte_size,created_by,narration_version,script_snapshot,file_sha256)
          VALUES (%s,%s,'test','female','male','audio','shared.mp3','http://localhost:4000/shared.mp3',%s,%s,'exam_track_v4','{}',%s)""",
          (audio_id,'a'*64,len(audio),author,file_digest(audio)))
        c.execute("""INSERT INTO topik_bank.item_audio_bindings(item_id,content_hash,audio_asset_id,source_hash)
          SELECT item_id,content_hash,%s,%s FROM topik_bank.items WHERE set_id=%s""",(audio_id,'b'*64,sets[1]))
    for bucket,path,data in [('images','graph.png',image),('audio','shared.mp3',audio)]:
        p=object_path(root,bucket,path);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    return sets


@pytest.mark.parametrize('table', ['tts_audio_assets', 'item_visual_assets'])
def test_media_storage_defaults_and_supported_backends(postgres_database_factory, tmp_path, table):
    url = postgres_database_factory()
    PostgresQuestionBank(url).ensure_schema()
    seed(url, tmp_path)
    identifier = sql.Identifier('topik_bank', table)
    with psycopg.connect(url) as connection:
        assert connection.execute(sql.SQL('SELECT storage_backend FROM {}').format(identifier)).fetchall() == [('local',)]
        connection.execute(sql.SQL("UPDATE {} SET storage_backend='s3'").format(identifier))
        assert connection.execute(sql.SQL('SELECT storage_backend FROM {}').format(identifier)).fetchall() == [('s3',)]
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute(sql.SQL("UPDATE {} SET storage_backend='unsupported'").format(identifier))


def test_local_validation_is_readonly_and_checks_files(postgres_database_factory, tmp_path):
    url = postgres_database_factory()
    bank = PostgresQuestionBank(url)
    bank.ensure_schema()
    selected = seed(url, tmp_path)
    with psycopg.connect(url) as c:
        before = c.execute("SELECT to_jsonb(i) FROM topik_bank.items i ORDER BY item_id").fetchall()
    result = preview_local_media(url, selected, tmp_path)
    assert result['sets'] == 2 and result['items'] == 100 and result['files'] == 2
    assert preview_local_media(url, selected, tmp_path) == result
    with psycopg.connect(url) as c:
        assert c.execute("SELECT to_jsonb(i) FROM topik_bank.items i ORDER BY item_id").fetchall() == before
    object_path(tmp_path, 'audio', 'shared.mp3').write_bytes(b'changed')
    with pytest.raises(ValueError, match='크기·해시'):
        preview_local_media(url, selected, tmp_path)
    with pytest.raises(ValueError, match='없는 세트'):
        preview_local_media(url, [str(uuid.uuid4())], tmp_path)
