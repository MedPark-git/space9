# 복구 가이드
1. Cafe24 AI SPACE 프로젝트 백업을 확보합니다.
2. 동일 런타임(Python 3.11)과 PostgreSQL 환경에서 소스를 복원합니다.
3. AI SPACE가 DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD를 자동 주입하는지 확인합니다.
4. `/app/user_data` 영구 파일을 복원합니다.
5. 애플리케이션 기동 후 `/health`가 HTTP 200 및 application_ready=true인지 확인합니다.
6. 관리자 로그인, 역할별 권한, 감사 로그를 점검합니다.

실제 비밀번호/토큰/DB 접속정보는 본 문서에 기록하지 않습니다.
