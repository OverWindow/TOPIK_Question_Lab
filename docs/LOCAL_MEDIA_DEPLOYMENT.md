# 로컬 문항·미디어 검증

`50문제 일괄 생성`은 필수 음원·그림을 생성해 S3에 저장하고 전체 파일 검증 후 문항·주소·연결을
로컬 PostgreSQL에 원자적으로 등록할 수 있습니다. 마이그레이션 009는 문항과 세트에
`pending_review` 상태를 허용합니다. 등록 완료는 검수·시험 공개를 뜻하지 않습니다.
설정과 재시도·복구 방법은 [README](../README.md#음원이미지-자동-처리-준비)를 참고하세요.

로컬 생성기의 `고급 관리 · PostgreSQL → 로컬 검증`에서 선택 세트를 검사합니다.
`로컬 파일` 모드에서는 `DATABASE_URL`의 PostgreSQL과 로컬 파일만 읽습니다.
`S3 · CloudFront` 모드에서는 설정한 S3와 CloudFront도 읽어 실제 파일을 검증합니다.

## 파일과 DB 기록

`topik_bank`의 `tts_audio_assets`, `item_audio_bindings`,
`question_set_item_audio_bindings`, `item_visual_assets`를 사용합니다.
기존 자산 ID·파일 주소·연결과 과거 배포 이력은 보존합니다.

```dotenv
LOCAL_MEDIA_ROOT=C:/path/to/Unigate-PBT-Hub/data/media
```

파일은 `LOCAL_MEDIA_ROOT/<storage_bucket>/<storage_path>`에 있어야 합니다.
`storage_backend=local`, 파일 크기와 실제 바이트의 SHA-256이 DB 기록과 일치해야 합니다.
경로가 저장 루트 밖으로 나가거나 파일이 없으면 검증을 중단합니다.
같은 경로의 같은 파일은 한 번만 집계합니다.

## 완성 조건

- 세트마다 중복 없는 1~50번 문항과 유효한 보기·정답이 있어야 합니다.
- 퇴역 처리된 문항·세트는 완성 검증을 통과할 수 없습니다.
- 듣기는 각 위치에 현재 시험 음성 `exam_track_v4`가 연결되어야 합니다.
- 그림 선택지와 읽기 10번 자료 이미지 등 필수 이미지가 있어야 합니다.
- 현재 문항의 내용 해시에 연결된 현재 미디어만 검사합니다.

신규 DB의 미디어 저장 방식은 `local`이 기본값이며 `local`, `s3`를 지원합니다.
DB 정보를 가져와도 원격 파일이 로컬에 생기지는 않으므로, 로컬 파일과 해시를
확보하기 전까지 로컬 파일 검증을 통과하지 않습니다. 기존 DB의 저장 위치는 자동 변환하지 않습니다.

S3 미디어는 `storage_backend=s3`와 실제 버킷·객체 경로, CloudFront HTTPS 주소,
파일 SHA-256을 기록해야 합니다. `AWS_REGION`, `S3_BUCKET`, `CLOUDFRONT_BASE_URL`을 설정하고
`S3 · CloudFront` 모드를 선택하면 S3·CloudFront의 전체 파일과 음성 부분 요청을 검증합니다.
AWS 인증은 표준 프로필 또는 환경변수를 사용하며 파일 업로드와 인프라 구성은 별도로 수행합니다.

## 이력과 백업

`발행 이력` 탭은 PostgreSQL 세트와 기존 `deployment_runs`, `deployment_run_sets`를 조회합니다.
생성 묶음의 등록 작업은 `data/publication_jobs.db`에 기록하며 SQLite 백업에 포함됩니다.
이전 DB 대상의 등록 작업은 조회 전용입니다.
PostgreSQL 전체 DB와 `LOCAL_MEDIA_ROOT`는 별도로 백업하세요.

## 검증 명령

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_media_validation.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_media_cloud.py
# TEST_PG_ADMIN_URL로 명시한 루프백 서버에만 임시 DB를 생성합니다.
.\.venv\Scripts\python.exe -m pytest -q tests/test_media_validation_integration.py tests/test_postgres_inspection.py
```
