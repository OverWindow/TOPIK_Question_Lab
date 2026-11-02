import base64
import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest
from botocore.exceptions import ClientError

from topik_question_lab.step_06_publication.media_cloud import (
    CACHE_CONTROL, MediaCloudError, S3MediaStore, cloudfront_url,
    destination_key, digest_file,
)


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        return {}

    def put_object(self, **kwargs):
        assert kwargs['IfNoneMatch'] == '*'
        assert kwargs['CacheControl'] == CACHE_CONTROL
        data = kwargs['Body'].read()
        assert kwargs['ChecksumSHA256'] == base64.b64encode(hashlib.sha256(data).digest()).decode()
        self.objects[kwargs['Bucket'], kwargs['Key']] = data, kwargs['ContentType']
        self.puts += 1

    def get_object(self, Bucket, Key):
        data, mime = self.objects[Bucket, Key]
        return {'Body': io.BytesIO(data), 'ContentLength': len(data), 'ContentType': mime}


@pytest.mark.parametrize('mime,extension', [('audio/mpeg', 'mp3'), ('image/png', 'png'), ('image/webp', 'webp'), ('image/svg+xml', 'svg')])
def test_s3_cloudfront_full_hash_and_audio_range(tmp_path, mime, extension):
    data, s3 = b'synthetic media bytes', MemoryS3()
    path = tmp_path / extension
    path.write_bytes(data)
    size, digest = digest_file(path)
    key = destination_key('source-bucket', digest, mime)
    assert key.endswith('.' + extension)
    ranges = []

    def handler(request):
        if request.headers.get('range'):
            ranges.append(request.headers['range'])
            return httpx.Response(206, headers={'content-range': f'bytes 0-0/{size}'}, content=data[:1])
        return httpx.Response(200, headers={'content-type': mime}, content=data)

    store = S3MediaStore('new-bucket', 'https://example.cloudfront.net', s3=s3,
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    store.upload(path, key, size, digest, mime)
    store.upload(path, key, size, digest, mime)
    assert s3.puts == 1
    row = {'storage_bucket': 'new-bucket', 'storage_path': key,
           'storage_url': cloudfront_url(store.base_url, key), 'file_sha256': digest,
           'byte_size': size, 'mime_type': mime}
    store.verify(row, local_path=path)
    assert bool(ranges) == mime.startswith('audio/')
    s3.objects['new-bucket', key] = b'corrupt', mime
    with pytest.raises(MediaCloudError):
        store.upload(path, key, size, digest, mime)
    assert s3.puts == 1  # Never overwrite a conflicting object.
    store.close()


@pytest.mark.parametrize('failure', ['bytes', 'mime', 'range', 'url'])
def test_cloudfront_delivery_must_match_original(tmp_path, failure):
    data, s3, mime = b'ID3-audio', MemoryS3(), 'audio/mpeg'
    digest = hashlib.sha256(data).hexdigest()
    key = destination_key('audio', digest, mime)
    s3.objects['bucket', key] = data, mime

    def handler(request):
        if request.headers.get('range'):
            return httpx.Response(200 if failure == 'range' else 206,
                                  headers={'content-range': f'bytes 0-0/{len(data)}'}, content=data[:1])
        return httpx.Response(200, headers={'content-type': 'image/png' if failure == 'mime' else mime},
                              content=b'wrong' if failure == 'bytes' else data)

    store = S3MediaStore('bucket', 'https://example.cloudfront.net', s3=s3,
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    row = {'storage_bucket': 'bucket', 'storage_path': key,
           'storage_url': cloudfront_url(store.base_url, key) + ('?wrong' if failure == 'url' else ''),
           'file_sha256': digest, 'byte_size': len(data), 'mime_type': mime}
    with pytest.raises(MediaCloudError):
        store.verify(row)
    store.close()


def test_template_private_origin_and_public_cloudfront():
    template_path = Path(__file__).resolve().parents[1] / "topik_question_lab/step_06_publication/cloudformation/media.json"
    template = json.loads(template_path.read_text())
    resources = template['Resources']
    bucket = resources['MediaBucket']
    assert all(bucket['Properties']['PublicAccessBlockConfiguration'].values())
    assert bucket['DeletionPolicy'] == bucket['UpdateReplacePolicy'] == 'Retain'
    behavior = resources['Distribution']['Properties']['DistributionConfig']['DefaultCacheBehavior']
    assert behavior['ViewerProtocolPolicy'] == 'redirect-to-https'
    assert 'TrustedKeyGroups' not in behavior
    assert resources['OriginAccess']['Properties']['OriginAccessControlConfig']['SigningBehavior'] == 'always'
    assert resources['BucketPolicy']['Properties']['PolicyDocument']['Statement'][0]['Condition']['StringEquals']['AWS:SourceArn']
