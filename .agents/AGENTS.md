# 프로젝트 규칙 (Project Rules)

## Discord Bot Response Visibility Rule
- 모든 디스코드 슬래시 명령어(Slash Command) 및 뷰(View/Select/Button) 상호작용 응답은 반드시 `ephemeral=True`를 설정하여 **명령어를 입력한 본인에게만 메시지가 보이도록** 합니다.
- 앞으로 추가되는 모든 신규 명령어 및 커스텀 응답에도 예외 없이 `ephemeral=True` 옵션을 적용합니다.

## Auto Deploy Rule
- 코드 변경 작업이 완료되면 반드시 자동으로 `git add` → `git commit` → `git push`를 실행하여 GitHub에 푸시합니다.
- Render는 GitHub push를 자동 감지하여 재배포하므로 별도의 배포 작업은 필요하지 않습니다.
- 커밋 메시지는 한국어로 작성하며, 변경 내용을 간결하게 요약합니다.
