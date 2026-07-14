# TOPIK Question Lab

TOPIK II 읽기 1·2번 기출 예시를 검토하고 ChatKHU에서 GPT 5.3 Chat, Claude Haiku 4.5, Gemini 3.1 Flash Lite, K-EXAONE, Solar Pro 3, Llama 4 Maverick, Gemma 3 27B, GPT-5.4 Nano를 비교해 새 문항을 생성하는 로컬 앱입니다. 파인튜닝 대신 승인된 예시와 편집 가능한 유형 분석서를 같은 프롬프트로 전달합니다.

## 설치와 실행

PowerShell에서 다음을 실행합니다.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup_topik_lab.ps1
.\run_topik_lab.ps1
```

앱은 기본적으로 `http://localhost:8501`에서 열립니다. Python 3.11 가상환경은 프로젝트의 `.venv`에 만들어집니다.

## ChatKHU 연결

기본 방식은 키가 필요 없는 ChatKHU 웹 수동 모드입니다. 앱에서 완성된 프롬프트를 복사하고 [ChatKHU](https://chat.khu.ac.kr)에서 원하는 모델을 선택해 실행한 뒤 JSON 응답을 앱에 붙여넣습니다.

K-EXAONE을 포함한 기본 모델 ID는 현재 ChatKHU 계정에서 동기화한 허용 목록을 사용합니다. 조직별 허용 목록이 다를 수 있으므로 자동 호출 전 `허용 모델 목록 동기화` 결과를 확인하세요. GPT 5.1, GPT 5.2와 DeepSeek은 활성 비교 모델에서 제외했으며, 과거 실행 기록은 삭제하지 않고 기존 기록으로만 표시합니다.

조직에서 Gateway API 키 발급을 허용한 경우 `.env.example`을 참고해 프로젝트 루트의 `.env`에 `CHATKHU_API_KEY` 하나만 입력하면 됩니다. 이 키 하나로 계정에 허용된 여러 모델을 호출하며, 앱의 `허용 모델 목록 동기화`에서 실제 모델 ID를 확인할 수 있습니다. Gateway 호출은 ChatKHU 계정 크레딧을 사용하며 실행 버튼을 직접 누를 때만 발생합니다.

경희대학교 구성원은 학생 월 2,000크레딧, 교직원 월 5,000크레딧을 제공받고 일부 무료 모델은 크레딧이 차감되지 않을 수 있습니다. 사용 가능 모델과 크레딧 정책은 학교 설정에 따라 달라질 수 있습니다.

## 작업 흐름

1. `기출 데이터`에서 스캔한 문제를 확인합니다. 정답 정보가 없는 문제는 `AI로 누락 정보 보완`에서 한 모델의 제안을 받은 뒤 검토·수정하고 학습 예시로 승인합니다.
2. `유형 분석`에서 프롬프트를 ChatKHU에 넣고 모델별 분석을 가져와 비교하거나 공통 분석서를 직접 수정합니다.
3. `프롬프트 작업실`에서 생성 수, 난이도, 모델 ID와 실제 프롬프트를 확인합니다.
4. `문제 생성`에서 ChatKHU 웹 붙여넣기 또는 선택적 Gateway 호출로 결과를 가져옵니다.
5. `검수·비교`에서 문제를 수정하고 점수와 최종 승인 여부를 저장합니다.
6. `내보내기`에서 승인 문제만 TXT, JSON, CSV로 받습니다.

새 HTML 파일은 `html변환`에 넣은 뒤 `기출 데이터 > HTML 변환 후 스캔`을 누르면 됩니다.
