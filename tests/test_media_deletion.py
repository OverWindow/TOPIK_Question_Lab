import hashlib
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from topik_question_lab.common.media.media_deletion import MediaDeleter


def asset(backend='s3', **values):
    return {'storage_backend': backend, 'storage_bucket': 'test-bucket', 'storage_path': 'media/test.mp3',
            'storage_url': 'https://media.example/media/test.mp3', 'byte_size': 4,
            'file_sha256': hashlib.sha256(b'test').hexdigest(), **values}


class FakeS3:
    def __init__(self):
        self.versions = [{'Key': 'media/test.mp3', 'VersionId': 'old'}, {'Key': 'media/test.mp3', 'VersionId': 'new'},
                         {'Key': 'media/test.mp3-other', 'VersionId': 'keep'}]
        self.markers = [{'Key': 'media/test.mp3', 'VersionId': 'marker'}]
        self.deleted = []

    def get_paginator(self, name):
        assert name == 'list_object_versions'
        return SimpleNamespace(paginate=lambda **kwargs: [{'Versions': list(self.versions), 'DeleteMarkers': list(self.markers)}])

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)
        self.versions = [v for v in self.versions if (v['Key'], v['VersionId']) != (kwargs['Key'], kwargs['VersionId'])]
        self.markers = [v for v in self.markers if (v['Key'], v['VersionId']) != (kwargs['Key'], kwargs['VersionId'])]

    def head_object(self, **kwargs):
        error = RuntimeError('missing')
        error.response = {'Error': {'Code': '404'}}
        raise error


class FakeCloudFront:
    def __init__(self, *, domain='media.example', aliases=(), pages=None):
        self.requests = []
        self.domain, self.aliases = domain, list(aliases)
        self.pages = pages if pages is not None else [{'DistributionList': {'Items': [
            {'Id': 'test-distribution', 'DomainName': domain, 'Aliases': {'Items': self.aliases}}]}}]
        self.list_calls, self.get_calls = 0, []

    def get_paginator(self, name):
        assert name == 'list_distributions'
        self.list_calls += 1
        return SimpleNamespace(paginate=lambda: iter(self.pages))

    def get_distribution(self, **kwargs):
        self.get_calls.append(kwargs)
        return {'Distribution': {'DomainName': self.domain, 'DistributionConfig': {'Aliases': {'Items': self.aliases}}}}

    def create_invalidation(self, **kwargs):
        self.requests.append(kwargs)
        return {'Invalidation': {'Status': 'Completed' if len(self.requests) > 1 else 'InProgress'}}


@pytest.mark.parametrize('distribution_id', ['test-distribution', ' test-distribution '])
def test_s3_deletes_only_exact_key_versions_and_retries_same_cache_request(tmp_path, monkeypatch, distribution_id):
    monkeypatch.setenv('S3_BUCKET', 'test-bucket')
    monkeypatch.setenv('CLOUDFRONT_BASE_URL', 'https://media.example')
    monkeypatch.setenv('CLOUDFRONT_DISTRIBUTION_ID', distribution_id)
    s3, cdn = FakeS3(), FakeCloudFront()
    deleter = MediaDeleter(tmp_path, s3=s3, cloudfront=cdn)
    deleter.preflight([asset()])
    assert not deleter.delete(asset(), 'stable-token')
    assert len(s3.deleted) == 3
    assert s3.versions == [{'Key': 'media/test.mp3-other', 'VersionId': 'keep'}]
    assert deleter.delete(asset(), 'stable-token')
    assert len(s3.deleted) == 3 and cdn.requests[0] == cdn.requests[1]
    assert cdn.requests[0]['DistributionId'] == 'test-distribution'
    assert cdn.requests[0]['InvalidationBatch']['Paths']['Items'] == ['/media/test.mp3']
    assert not cdn.list_calls and cdn.get_calls == [{'Id': 'test-distribution'}]


@pytest.mark.parametrize('changes', [{'storage_bucket': 'other'}, {'storage_url': 'https://other.example/media/test.mp3'}, {'storage_path': 'media/*'}])
def test_wrong_storage_or_wildcard_is_rejected_before_deletion(tmp_path, monkeypatch, changes):
    monkeypatch.setenv('S3_BUCKET', 'test-bucket')
    monkeypatch.setenv('CLOUDFRONT_BASE_URL', 'https://media.example')
    monkeypatch.setenv('CLOUDFRONT_DISTRIBUTION_ID', 'test-distribution')
    s3, cdn = FakeS3(), FakeCloudFront()
    with pytest.raises(ValueError):
        MediaDeleter(tmp_path, s3=s3, cloudfront=cdn).delete(asset(**changes), 'test')
    assert not s3.deleted and not cdn.requests


@pytest.mark.parametrize('distribution_id', [None, '', '   '])
@pytest.mark.parametrize('domain,aliases', [('media.example', ()), ('d123.cloudfront.net', ('MEDIA.EXAMPLE',))])
def test_base_url_resolves_distribution_across_pages_and_reuses_it_for_deletion(
        tmp_path, monkeypatch, distribution_id, domain, aliases):
    monkeypatch.setenv('S3_BUCKET', 'test-bucket')
    monkeypatch.setenv('CLOUDFRONT_BASE_URL', 'https://media.example')
    if distribution_id is None:
        monkeypatch.delenv('CLOUDFRONT_DISTRIBUTION_ID', raising=False)
    else:
        monkeypatch.setenv('CLOUDFRONT_DISTRIBUTION_ID', distribution_id)
    s3, cdn = FakeS3(), FakeCloudFront(domain=domain, aliases=aliases)
    cdn.pages.insert(0, {'DistributionList': {'Items': [{'Id': 'other', 'DomainName': 'other.example'}]}})
    deleter = MediaDeleter(tmp_path, s3=s3, cloudfront=cdn)
    deleter.validate(asset())
    assert not cdn.list_calls
    deleter.preflight([asset()])
    assert not s3.deleted
    assert not deleter.delete(asset(), 'stable-token')
    assert deleter.delete(asset(), 'stable-token')
    assert len(s3.deleted) == 3
    assert cdn.requests[0] == cdn.requests[1] and cdn.requests[0]['DistributionId'] == 'test-distribution'
    assert cdn.list_calls == 1 and cdn.get_calls == [{'Id': 'test-distribution'}]
    assert os.getenv('CLOUDFRONT_DISTRIBUTION_ID') == distribution_id


@pytest.mark.parametrize('ambiguous', [False, True])
def test_unresolved_distribution_blocks_before_file_deletion(tmp_path, monkeypatch, ambiguous):
    monkeypatch.setenv('S3_BUCKET', 'test-bucket')
    monkeypatch.setenv('CLOUDFRONT_BASE_URL', 'https://media.example')
    monkeypatch.delenv('CLOUDFRONT_DISTRIBUTION_ID', raising=False)
    s3, cdn = FakeS3(), FakeCloudFront(pages=[{'DistributionList': {'Quantity': 0}}])
    if ambiguous:
        cdn.pages = [{'DistributionList': {'Items': [{'Id': name, 'DomainName': 'media.example'}]}}
                     for name in ('first', 'second')]
    deleter = MediaDeleter(tmp_path, s3=s3, cloudfront=cdn)
    for operation in (lambda: deleter.preflight([asset()]), lambda: deleter.delete(asset(), 'test')):
        with pytest.raises(ValueError, match='여러 개' if ambiguous else '찾지 못했습니다'):
            operation()
    assert not s3.deleted and not cdn.requests and not cdn.get_calls


@pytest.mark.parametrize('error', [
    ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'synthetic-private-details'}}, 'ListDistributions'),
    NoCredentialsError(),
    EndpointConnectionError(endpoint_url='https://synthetic-private-details.example'),
])
def test_lookup_failure_after_matching_page_blocks_deletion_with_safe_guidance(tmp_path, monkeypatch, error):
    monkeypatch.setenv('S3_BUCKET', 'test-bucket')
    monkeypatch.setenv('CLOUDFRONT_BASE_URL', 'https://media.example')
    monkeypatch.delenv('CLOUDFRONT_DISTRIBUTION_ID', raising=False)
    s3, cdn = FakeS3(), FakeCloudFront()
    def failing_pages():
        yield cdn.pages[0]
        raise error
    monkeypatch.setattr(cdn, 'get_paginator', lambda name: SimpleNamespace(paginate=failing_pages))
    deleter = MediaDeleter(tmp_path, s3=s3, cloudfront=cdn)
    for operation in (lambda: deleter.preflight([asset()]), lambda: deleter.delete(asset(), 'test')):
        with pytest.raises(ValueError, match='cloudfront:ListDistributions') as caught:
            operation()
        assert 'CLOUDFRONT_DISTRIBUTION_ID' in str(caught.value)
        assert 'synthetic-private-details' not in str(caught.value)
    assert not s3.deleted and not cdn.requests and not cdn.get_calls


@pytest.mark.parametrize('distribution_id', ['', 'test-distribution'])
def test_distribution_domain_mismatch_blocks_deletion(tmp_path, monkeypatch, distribution_id):
    monkeypatch.setenv('S3_BUCKET', 'test-bucket')
    monkeypatch.setenv('CLOUDFRONT_BASE_URL', 'https://media.example')
    monkeypatch.setenv('CLOUDFRONT_DISTRIBUTION_ID', distribution_id)
    s3, cdn = FakeS3(), FakeCloudFront()
    cdn.domain = 'other.example'
    with pytest.raises(ValueError, match='미디어 주소와 다릅니다'):
        MediaDeleter(tmp_path, s3=s3, cloudfront=cdn).delete(asset(), 'test')
    assert not s3.deleted and not cdn.requests
    assert cdn.list_calls == (0 if distribution_id else 1)


def test_preflight_rechecks_distribution_before_another_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv('S3_BUCKET', 'test-bucket')
    monkeypatch.setenv('CLOUDFRONT_BASE_URL', 'https://media.example')
    monkeypatch.delenv('CLOUDFRONT_DISTRIBUTION_ID', raising=False)
    s3, cdn = FakeS3(), FakeCloudFront()
    deleter = MediaDeleter(tmp_path, s3=s3, cloudfront=cdn)
    deleter.preflight([asset()])
    cdn.pages = []
    with pytest.raises(ValueError, match='찾지 못했습니다'):
        deleter.preflight([asset()])
    with pytest.raises(ValueError, match='찾지 못했습니다'):
        deleter.delete(asset(), 'test')
    assert not s3.deleted and not cdn.requests


def test_supabase_uses_exact_object_path_and_confirms_absence(tmp_path, monkeypatch):
    monkeypatch.setenv('SUPABASE_URL', 'https://storage.example')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'synthetic-service-key')
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[]) if request.method == 'DELETE' else httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handle))
    deleter = MediaDeleter(tmp_path, client=client)
    value = asset('supabase', storage_url='https://storage.example/storage/v1/object/public/test-bucket/media/test.mp3')
    assert deleter.delete(value, 'test')
    assert requests[0].url.path == '/storage/v1/object/test-bucket'
    assert json.loads(requests[0].content) == {'prefixes': ['media/test.mp3']}
    assert requests[1].method == 'HEAD' and requests[1].url.path.endswith('/authenticated/test-bucket/media/test.mp3')
    deleter.close()


def test_supabase_does_not_report_unverified_deletion_as_complete(tmp_path, monkeypatch):
    monkeypatch.setenv('SUPABASE_URL', 'https://storage.example')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'synthetic-service-key')
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])))
    value = asset('supabase', storage_url='https://storage.example/storage/v1/object/public/test-bucket/media/test.mp3')
    with pytest.raises(ValueError, match='결과'):
        MediaDeleter(tmp_path, client=client).delete(value, 'test')
    client.close()


def test_local_deletion_rejects_traversal_and_changed_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_MEDIA_ROOT', 'data/media')
    path = tmp_path / 'data/media/test-bucket/media/test.mp3'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'changed')
    deleter = MediaDeleter(tmp_path)
    with pytest.raises(ValueError, match='다릅니다'):
        deleter.delete(asset('local'), 'test')
    assert path.read_bytes() == b'changed'
    with pytest.raises(ValueError):
        deleter.delete(asset('local', storage_path='../outside'), 'test')
    path.write_bytes(b'test')
    assert deleter.delete(asset('local'), 'test') and not path.exists()
    assert deleter.delete(asset('local'), 'test')
