# 인스턴스 자동 재시작 · 설정 안내

봇이 도는 Oracle 인스턴스가 **유휴 판정으로 정지되면 자동으로 다시 켜지도록**
하는 장치입니다. 카드·유료 전환 없이 전부 무료로 동작합니다.

운영자가 오래 자리를 비우는 것이 이 봇의 전제라, "이메일을 보고 사람이 켠다"에
기댈 수 없어서 만들었습니다.

---

## 왜 필요한가

Always Free 계정은 7일간 CPU·네트워크 사용률이 낮으면 인스턴스를 **정지**합니다
(삭제가 아니라 정지입니다 — 데이터는 남습니다).

디스코드 봇은 대부분 대기 상태라 **잘 돌고 있어도** 이 기준에 걸립니다.

```
실측    CPU 사용 0.5% / 유휴 99.5%     ← 부팅 이후 전체 평균
기준    CPU 95퍼센타일 10% 미만이면 유휴
```

정지된 인스턴스를 다시 켜는 것은 Oracle 이 명시적으로 허용한 동작입니다.
이 워크플로는 그걸 3시간마다 대신 해 줍니다.

---

## 1단계 · OCI API 키 발급 (약 5분)

```
OCI 콘솔 로그인
  → 우측 상단 프로필 아이콘
  → My profile (내 프로필)
  → 왼쪽 아래 Resources → API keys
  → Add API key
  → "Generate API key pair" 선택
  → ★ Download private key 를 반드시 눌러 파일 저장 ★
  → Add
```

`Add` 를 누르면 **Configuration file preview** 창이 뜹니다.
여기에 있는 값 중 두 개를 복사해 두세요.

```
user=ocid1.user.oc1..xxxxxxxx          ← 복사
fingerprint=aa:bb:cc:...:zz            ← 복사
tenancy=...                            (이미 알고 있음)
region=ap-tokyo-1                      (이미 알고 있음)
```

> 개인키 파일은 **다시 받을 수 없습니다.** 못 받으셨으면 키를 지우고 다시 만드세요.
> 그리고 이 파일은 누구에게도 보내지 마세요 — 계정 조작 권한이 그대로 담겨 있습니다.

---

## 2단계 · GitHub 시크릿 등록 (약 5분)

```
GitHub → windswepty/KRBiomeHuntBot
  → Settings → Secrets and variables → Actions
  → New repository secret
```

아래 6개를 등록합니다. **아래 4개는 값이 이미 정해져 있으니 그대로 붙여넣으세요.**

| 이름 | 값 |
| :--- | :--- |
| `OCI_CLI_USER` | 1단계에서 복사한 `user=` 값 |
| `OCI_CLI_FINGERPRINT` | 1단계에서 복사한 `fingerprint=` 값 |
| `OCI_CLI_KEY_CONTENT` | 내려받은 개인키 `.pem` 파일을 **메모장으로 열어 전문 복사** (`-----BEGIN` 줄부터 `-----END` 줄까지, 줄바꿈 포함) |
| `OCI_CLI_REGION` | `ap-tokyo-1` |
| `OCI_CLI_TENANCY` | `ocid1.tenancy.oc1..aaaaaaaawybaw2eo7ts53w5mshjfvhig2ddbcbsz7nfm77msxr3hy6huogna` |

> 예전에는 `OCI_INSTANCE_ID` 도 필요했습니다. 지금은 **구획에서 인스턴스를
> 찾아오므로 필요 없습니다.** 이미 등록해 두셨다면 그냥 두셔도 무해합니다.

선택 사항 (넣으면 재시작 후 봇이 실제로 살아났는지까지 확인합니다):

| 이름 | 값 |
| :--- | :--- |
| `BOT_HEALTH_URL` | `http://<서버 IP>:8080/health` |

---

## 3단계 · 동작 확인 (약 1분)

```
GitHub → Actions 탭
  → 왼쪽에서 "인스턴스 자동 재시작"
  → Run workflow → Run workflow
```

초록색으로 끝나고 로그에 아래가 보이면 성공입니다.

```
현재 상태: RUNNING
정상 가동 중 — 할 일 없음
```

빨간색이면 로그의 오류 메시지를 확인하세요.
`NotAuthenticated` 계열이면 시크릿 값(특히 개인키 전문)을 다시 보세요.

---

## 4단계 · 알림 추가 (약 5분, 권장)

자동 재시작은 **정지**를 고쳐 주지만, 봇 자체가 죽는 경우는 못 잡습니다.
그쪽이 사실 더 자주 일어납니다.

```
https://uptimerobot.com  무료 가입 (카드 불필요)
  → Add New Monitor
  → Monitor Type : HTTP(s)
  → Friendly Name: 한바헌봇
  → URL          : http://<서버 IP>:8080/health
  → Interval     : 5 minutes
  → 알림 이메일 지정 → Create Monitor
```

`/health` 는 **디스코드 연결이 끊긴 좀비 상태에서 503 을 돌려줍니다.**
Flask 만 살아 있고 봇이 죽은 경우까지 잡힌다는 뜻입니다.
기동 유예 180초가 있어 재배포 때는 오탐하지 않습니다.

---

## 어떤 인스턴스를 지키나

**계정(루트 구획)의 인스턴스를 전부** 확인해 꺼져 있으면 켭니다.
OCID 를 박아 두지 않으므로 인스턴스를 새로 만들거나 다시 만들어도
설정을 고칠 필요가 없습니다.

```
현재    krbiomehuntbot (봇)  ·  windweaver
```

**일부러 꺼 두고 싶은 인스턴스가 생기면** 저장소 변수로 제외하세요.
시크릿이 아니라 변수(Variables)입니다 — 이름은 비밀이 아니니까요.

```
Settings → Secrets and variables → Actions → Variables 탭
  → New repository variable
     Name  : KEEPALIVE_SKIP
     Value : windweaver          (여러 개면 쉼표로: a,b)
```

삭제된(TERMINATED) 인스턴스는 **경고만** 내고 실패로 잡지 않습니다.
일부러 지운 인스턴스 하나 때문에 워크플로가 영원히 빨간불이 되면,
정작 진짜 실패를 놓치게 됩니다. 봇 인스턴스가 삭제된 경우는
UptimeRobot 이 즉시 잡습니다.

## 알아 두실 것

- **워크플로 실패 = 이메일** 입니다. 스스로 고칠 수 없는 경우에만 실패하도록
  만들었습니다(인스턴스 삭제됨, 켰는데 봇이 안 뜸 등). 자동 재시작에
  성공한 경우는 실패로 잡지 않습니다 — 늘 빨간 표시는 없는 것보다 나쁩니다.
- **60일 비활성 방지가 들어 있습니다.** GitHub 은 커밋이 60일간 없으면 예약
  워크플로를 자동으로 끄고, 꺼진 뒤에는 스스로 되살릴 수 없습니다.
  그래서 45일이 지나면 빈 커밋을 하나 남깁니다. 파일 변경은 없습니다.
- **사용량**: 3시간마다 × 월 약 240회 ≈ 360분. 비공개 저장소 무료 한도
  2,000분의 18% 입니다.
- 키를 새로 만들거나 계정을 바꾸면 `OCI_CLI_*` 시크릿을 다시 넣어야 합니다.
