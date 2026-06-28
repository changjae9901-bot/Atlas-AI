# Atlas Stock Mailer 배포 안내

## 핵심 개념

로컬 실행은 `python app.py`가 켜져 있는 동안만 동작합니다.

실제 서비스처럼 쓰려면 다음이 필요합니다.

- 항상 켜져 있는 웹 서버
- 꺼져도 보존되는 데이터 저장소
- 운영자가 설정하는 메일 발송 API 키 또는 메일 발송 계정
- 운영자가 설정하는 시세/뉴스 데이터 API 키

## 추천 구조

초기 MVP는 Render 같은 PaaS에 올리는 방식이 가장 단순합니다.

사용자는 앱 화면에서 다음만 입력합니다.

- 이름
- 수신 이메일
- 발송 시간
- 보유 종목

운영자는 서버 환경변수로 다음을 설정합니다.

- `RESEND_API_KEY`
- `RESEND_FROM`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `ALPHA_VANTAGE_KEY`
- `ATLAS_DATA_DIR`
- `ADMIN_TOKEN`
- `CLEAR_USER_DATA_AFTER_SUCCESS`

## Render 배포 절차

1. 이 폴더를 GitHub 저장소에 올립니다.
2. Render에서 New Web Service를 생성합니다.
3. 저장소를 연결합니다.
4. Build Command:

```text
pip install -r requirements.txt
```

5. Start Command:

```text
python app.py
```

6. Health Check Path:

```text
/healthz
```

7. Persistent Disk를 추가합니다.

```text
Mount path: /var/data
Size: 1GB 이상
```

8. 환경변수를 설정합니다.

```text
ATLAS_DATA_DIR=/var/data
RESEND_API_KEY=Resend API 키
RESEND_FROM=Atlas AI <인증한 발송 주소>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=보내는 Gmail 주소
SMTP_PASSWORD=Google 앱 비밀번호
SMTP_FROM=보내는 Gmail 주소
ALPHA_VANTAGE_KEY=시세/뉴스 API 키
ADMIN_TOKEN=운영자 설정 접근 토큰
CLEAR_USER_DATA_AFTER_SUCCESS=1
```

Gmail 앱 비밀번호를 쓰지 않으려면 `RESEND_API_KEY`와 `RESEND_FROM`만 설정해도 됩니다. Resend 설정이 있으면 앱은 SMTP보다 Resend API 발송을 먼저 사용합니다.

사용자 화면에는 운영자 설정 입력칸을 노출하지 않습니다. `/api-settings` 같은 운영자 경로는 `ADMIN_TOKEN`이 있을 때만 접근할 수 있습니다.

공개 데모 또는 초기 배포에서는 개인정보 보호를 위해 `CLEAR_USER_DATA_AFTER_SUCCESS=1`을 권장합니다. 이 값이 켜져 있으면 이메일 발송 성공 후 사용자가 입력한 이메일, 보유 종목, 발송 기록, 임시 리포트가 초기화됩니다.

## 주의

SQLite는 초기 MVP에는 충분하지만, 사용자가 늘어나면 PostgreSQL로 바꾸는 것이 좋습니다.

예약 발송은 웹 서버 프로세스가 켜져 있어야 실행됩니다. 무료 플랜처럼 서버가 잠드는 환경에서는 예약 발송이 누락될 수 있으므로, 실제 운영에서는 항상 켜져 있는 플랜이나 별도 스케줄러가 필요합니다.

## 로컬 실행

```powershell
python app.py
```

브라우저:

```text
http://127.0.0.1:8080
```
