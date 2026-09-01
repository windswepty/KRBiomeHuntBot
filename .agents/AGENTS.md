# 프로젝트 규칙 (Project Rules)

## Discord Bot Response Visibility Rule
- 모든 디스코드 슬래시 명령어(Slash Command) 및 뷰(View/Select/Button) 상호작용 응답은 반드시 `ephemeral=True`를 설정하여 **명령어를 입력한 본인에게만 메시지가 보이도록** 합니다.
- 앞으로 추가되는 모든 신규 명령어 및 커스텀 응답에도 예외 없이 `ephemeral=True` 옵션을 적용합니다.

## Auto Deploy & Hosting Rule
- 현재 호스팅 환경: Oracle Cloud Always Free VM (Ubuntu 24.04 LTS, systemd `krbiomehuntbot.service`).
- 코드 변경 작업이 완료되면 반드시 자동으로 `git add` → `git commit` → `git push`를 실행하여 GitHub에 푸시합니다.
- 커밋 메시지는 한국어로 작성하며, 변경 내용을 간결하게 요약합니다.
- 서버 반영이 필요한 경우 SSH를 통해 VM의 코드를 갱신하고 서비스를 재시작합니다.
