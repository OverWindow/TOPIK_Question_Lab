import pytest

from topik_question_lab.step_06_publication.media_validation import LocalMediaValidator, file_digest, object_path


def completed_reading(root):
    rows={name:[] for name in ['items','question_sets','tts_audio_assets','item_audio_bindings','item_visual_assets']}
    rows['question_sets']=[{'set_id':'set','review_status':'reviewed'}]
    rows['items']=[{'item_id':str(n),'set_id':'set','position':n,'content_hash':'a'*64,
        'section':'reading','review_status':'reviewed','content_json':{},'choices':['a','b','c','d'],'correct_answer':1} for n in range(1,51)]
    data=b'synthetic graph'
    row={'visual_asset_id':'graph','item_id':'10','content_hash':'a'*64,'visual_role':'material','option_number':1,
        'storage_backend':'local','storage_bucket':'images','storage_path':'reading/graph.png','file_sha256':file_digest(data),
        'mime_type':'image/png','byte_size':len(data),'created_by':None}
    rows['item_visual_assets']=[row]
    path=object_path(root,row['storage_bucket'],row['storage_path'])
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data)
    return rows,data


@pytest.mark.parametrize('failure',['missing_question','answer','missing_image','remote','missing_file','hash','changed_after_preview'])
def test_incomplete_or_changed_content_is_blocked(tmp_path,failure):
    rows,data=completed_reading(tmp_path/'source')
    deployment=LocalMediaValidator(tmp_path/'source')
    manifest=deployment.prepare(rows)
    row=rows['item_visual_assets'][0]
    path=object_path(tmp_path/'source',row['storage_bucket'],row['storage_path'])
    if failure=='missing_question':rows['items'].pop()
    if failure=='answer':rows['items'][0]['correct_answer']=None
    if failure=='missing_image':rows['item_visual_assets']=[]
    if failure=='remote':row['storage_backend']='s3'
    if failure=='missing_file':path.unlink()
    if failure in {'hash','changed_after_preview'}:path.write_bytes(b'changed')
    with pytest.raises(ValueError):
        deployment.prepare(rows)
    assert not (tmp_path/'target').exists()


@pytest.mark.parametrize('path',['../outside.mp3','a/../../x','/root','a\\b','C:/outside','a//b'])
def test_paths_cannot_escape_storage_root(tmp_path,path):
    with pytest.raises(ValueError):object_path(tmp_path,'audio',path)


def test_shared_listening_recording_is_counted_only_once(tmp_path):
    rows,data=completed_reading(tmp_path/'source')
    rows['item_visual_assets']=[]
    for item in rows['items']:item['section']='listening'
    audio={'audio_asset_id':'audio','deleted_at':None,'narration_version':'exam_track_v4',
        'storage_backend':'local','storage_bucket':'audio','storage_path':'shared.mp3','file_sha256':file_digest(data),
        'mime_type':'audio/mpeg','byte_size':len(data)}
    rows['tts_audio_assets']=[audio]
    rows['item_audio_bindings']=[{'item_id':str(i),'content_hash':'a'*64,'audio_asset_id':'audio','is_current':True} for i in range(1,51)]
    path=object_path(tmp_path/'source','audio','shared.mp3');path.parent.mkdir(parents=True);path.write_bytes(data)
    assert len(LocalMediaValidator(tmp_path/'source').prepare(rows).files)==1
