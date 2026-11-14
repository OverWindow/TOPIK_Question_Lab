"""Delete only explicitly approved objects; callers must hold database reference locks."""
from pathlib import Path
import os
from urllib.parse import quote, urlsplit

import httpx

from topik_question_lab.common.media.media_cloud import aws_session, cloudfront_url, digest_file, https_origin
from topik_question_lab.common.media.media_validation import object_path


class MediaDeleter:
    def __init__(self, root, *, s3=None, cloudfront=None, client=None):
        self.root = Path(root)
        self.s3, self.cloudfront, self.client = s3, cloudfront, client
        self._distribution_id = None

    def validate(self, asset):
        backend, bucket, key = (asset[k] for k in ('storage_backend', 'storage_bucket', 'storage_path'))
        if not bucket or not key or any(c in key for c in ('\0', '\\', '*', '?')):
            raise ValueError('삭제할 미디어 경로가 올바르지 않습니다.')
        if backend == 's3':
            if bucket != os.getenv('S3_BUCKET') or asset['storage_url'] != cloudfront_url(os.getenv('CLOUDFRONT_BASE_URL', ''), key):
                raise ValueError('삭제 파일이 설정된 S3·CloudFront 저장소와 다릅니다.')
        elif backend == 'supabase':
            origin = https_origin(os.getenv('SUPABASE_URL', ''))
            expected = f'{origin}/storage/v1/object/public/{quote(bucket, safe="")}/{quote(key, safe="/")}'
            if asset['storage_url'] != expected or not os.getenv('SUPABASE_SERVICE_ROLE_KEY', '').strip():
                raise ValueError('Supabase 파일 주소와 SUPABASE_URL·SUPABASE_SERVICE_ROLE_KEY 설정을 확인하세요.')
        elif backend == 'local':
            object_path(self.local_root, bucket, key)
        else:
            raise ValueError('삭제를 지원하지 않는 미디어 저장소입니다.')

    @property
    def local_root(self):
        return self.root / os.getenv('LOCAL_MEDIA_ROOT', 'data/media')

    def _aws(self):
        if self.s3 is None or self.cloudfront is None:
            from botocore.config import Config
            config = Config(retries={'total_max_attempts': 1}, connect_timeout=8, read_timeout=30)
            session = aws_session()
            self.s3 = self.s3 or session.client('s3', config=config)
            self.cloudfront = self.cloudfront or session.client('cloudfront', config=config)

    def preflight(self, assets):
        self._distribution_id = None
        for asset in assets:
            self.validate(asset)
        if any(a['storage_backend'] == 's3' for a in assets):
            self._aws()
            domain = urlsplit(os.environ['CLOUDFRONT_BASE_URL']).hostname
            distribution_id = os.getenv('CLOUDFRONT_DISTRIBUTION_ID', '').strip()
            if not distribution_id:
                from botocore.exceptions import BotoCoreError, ClientError
                matches = set()
                try:
                    for page in self.cloudfront.get_paginator('list_distributions').paginate():
                        for info in page.get('DistributionList', {}).get('Items', []):
                            names = [info['DomainName'], *info.get('Aliases', {}).get('Items', [])]
                            if domain in (name.lower() for name in names):
                                matches.add(info['Id'])
                except (BotoCoreError, ClientError):
                    raise ValueError('CloudFront 배포 자동 조회에 실패했습니다. AWS 인증과 cloudfront:ListDistributions 권한을 확인하거나 '
                                     'CLOUDFRONT_DISTRIBUTION_ID를 직접 설정하세요.') from None
                if not matches:
                    raise ValueError('CLOUDFRONT_BASE_URL과 일치하는 CloudFront 배포를 찾지 못했습니다. '
                                     '주소와 AWS 계정을 확인하거나 CLOUDFRONT_DISTRIBUTION_ID를 직접 설정하세요.')
                if len(matches) != 1:
                    raise ValueError('CLOUDFRONT_BASE_URL과 일치하는 CloudFront 배포가 여러 개입니다. '
                                     'CLOUDFRONT_DISTRIBUTION_ID를 직접 설정하세요.')
                distribution_id = matches.pop()
            info = self.cloudfront.get_distribution(Id=distribution_id)['Distribution']
            names = [info['DomainName'], *info['DistributionConfig'].get('Aliases', {}).get('Items', [])]
            if domain not in (name.lower() for name in names):
                raise ValueError('CloudFront 배포 ID가 미디어 주소와 다릅니다.')
            self._distribution_id = distribution_id

    def delete(self, asset, token):
        self.validate(asset)
        backend, bucket, key = (asset[k] for k in ('storage_backend', 'storage_bucket', 'storage_path'))
        if backend == 'local':
            path = object_path(self.local_root, bucket, key)
            if path.exists():
                if not asset.get('file_sha256') or digest_file(path) != (asset['byte_size'], asset['file_sha256']):
                    raise ValueError('삭제할 로컬 파일이 확인한 파일과 다릅니다.')
                path.unlink()
            return True
        if backend == 'supabase':
            if self.client is None:
                self.client = httpx.Client(timeout=30, follow_redirects=False)
            base = https_origin(os.environ['SUPABASE_URL']) + '/storage/v1/object/'
            headers = {'Authorization': 'Bearer ' + os.environ['SUPABASE_SERVICE_ROLE_KEY'],
                       'apikey': os.environ['SUPABASE_SERVICE_ROLE_KEY']}
            response = self.client.request('DELETE', base + quote(bucket, safe=''),
                                           json={'prefixes': [key]}, headers=headers)
            response.raise_for_status()
            response = self.client.head(base + 'authenticated/' + quote(bucket, safe='') + '/' + quote(key, safe='/'), headers=headers)
            if response.status_code != 404:
                raise ValueError('Supabase 파일 삭제 결과를 확인하지 못했습니다.')
            return True
        if self._distribution_id is None:
            self.preflight([asset])
        # The deployed bucket is versioned: remove exact-key versions and delete markers too.
        versions = []
        for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=bucket, Prefix=key):
            versions.extend(v for group in ('Versions', 'DeleteMarkers') for v in page.get(group, []) if v['Key'] == key)
        for version in versions:
            self.s3.delete_object(Bucket=bucket, Key=key, VersionId=version['VersionId'])
        # Also handles unversioned buckets; no prefix/bucket deletion is ever issued.
        if not versions:
            try:
                head = self.s3.head_object(Bucket=bucket, Key=key)
            except Exception as exc:
                if getattr(exc, 'response', {}).get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
                    raise
            else:
                self.s3.delete_object(Bucket=bucket, Key=key, IfMatch=head['ETag'])
        for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=bucket, Prefix=key):
            if any(v['Key'] == key for group in ('Versions', 'DeleteMarkers') for v in page.get(group, [])):
                raise ValueError('S3 파일 버전 삭제 결과를 확인하지 못했습니다.')
        response = self.cloudfront.create_invalidation(
            DistributionId=self._distribution_id,
            InvalidationBatch={'Paths': {'Quantity': 1, 'Items': ['/' + quote(key, safe='/')]}, 'CallerReference': token})
        return response['Invalidation']['Status'] == 'Completed'

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None
