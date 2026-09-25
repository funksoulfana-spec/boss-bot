# 🔔 보스타임 알림 디스코드 봇

매일 같은 시간에 보스 젠 알림을 채널로 보내주는 봇입니다. (한국시간 기준)

## 기능
| 명령어 | 설명 |
|---|---|
| `/보스추가 이름:발록 시간:20:00,22:00` | 고정 시간표 (매일 반복) |
| `/주기추가 이름:오만의탑 시작시각:09:30 진행시간:1시간30분 대기시간:2시간` | 주기형 (시작→진행→대기→반복) |
| `/기준시각 이름:오만의탑 시작시각:09:30` | 점검 뒤 주기가 밀렸을 때 기준점 재설정 |
| `/사이트상태` | l1justice 사이트 기준 그룹별 ACTIVE 보스 수 |
| `/보스삭제 이름:발록` | 삭제 |

### 🌐 자동 보정 (기본 켜짐)
봇이 1분마다 `https://l1justice.com/lineage/database/bosses/state/` 를 확인해서,
각 보스 그룹(bosses.json 의 `spawn_ids`) 중 하나라도 INACTIVE/DEAD → ACTIVE 로 바뀌면 그 순간을 보스타임 시작으로 봅니다.
예측과 2분 넘게 다르면 기준시각을 자동으로 고치고 채널에 알려줍니다. 끄려면 환경변수 `AUTO_SYNC=0`.

### ⛔ 서버 다운 대응
- 홈페이지의 `Server Status: Offline` 을 읽으면 ⛔ 안내 후 **알림을 멈춥니다** (다운 중 사이트 보스 상태는 멈춰 있으므로 무시).
- `Online` 으로 돌아오면 ✅ 안내. 각 사이클은 ⏳ 대기 상태가 되고, 사이트에서 보스가 **처음 ACTIVE 로 뜨는 순간** 📌 시간표를 확정한 뒤 알림을 재개합니다.
- 자동 확정이 안 되면 `/기준시각` 으로 직접 넣으면 바로 대기 상태가 풀립니다.
| `/보스목록` | 진행중/대기중 상태 + 남은 시간 |
| `/테스트알림` | 채널·역할 멘션 테스트 |

알림 종류: 시작 N분 전 ⏰ / 시작 🔥 / 종료 N분 전 ⌛ / 종료 🏁 (`PRE_ALERT_MIN` 기본 5분)

### 기본 등록된 시간표 (bosses.json, 2026-09-02 기준)
| 보스 | 진행 | 대기 | 한 바퀴 | 기준 시작 |
|---|---|---|---|---|
| 오만의탑 (TOI) | 1시간 30분 | 2시간 | 3시간 30분 | 09:30 |
| 라스타바드4층 (L4) | 2시간 | 2시간 30분 | 4시간 30분 | 02:00 |
| 본토(메인랜드) | 2시간 30분 | 1시간 30분 | 4시간 | 02:30 |

⚠️ 서버 점검 등으로 주기가 밀리면 `/기준시각` 으로 새 시작 시각을 한 번만 알려주면 됩니다.

---

## 1단계. 디스코드 봇 만들기 (5분)
1. https://discord.com/developers/applications → **New Application**
2. 왼쪽 **Bot** 메뉴 → **Reset Token** → 토큰 복사 (= `DISCORD_TOKEN`)
3. 왼쪽 **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Mention Everyone`(역할 멘션용)
   - 생성된 URL 열어서 내 서버에 초대
4. 디스코드 앱 설정 → 고급 → **개발자 모드 ON**
   - 서버 이름 우클릭 → ID 복사 (= `GUILD_ID`)
   - 알림 채널 우클릭 → ID 복사 (= `CHANNEL_ID`)
   - 서버 설정 → 역할 → 알림받을 역할 우클릭 → ID 복사 (= `ROLE_ID`)
   - ⚠️ 역할 설정에서 **"누구나 이 역할을 멘션할 수 있음"** 켜기 (안 켜면 멘션 안 울림)

## 2단계. 내 PC에서 먼저 테스트
```bash
pip install -r requirements.txt
# .env.example 참고해서 환경변수 설정 후
python bot.py
```
디스코드에서 `/테스트알림` 쳐보고 메시지 오면 성공.

## 3단계. 클라우드에 올리기 (Railway 기준)
1. 이 폴더를 GitHub 저장소에 올리기
2. https://railway.app → New Project → **Deploy from GitHub repo**
3. **Variables** 탭에 아래 환경변수 입력
   `DISCORD_TOKEN`, `CHANNEL_ID`, `ROLE_ID`, `GUILD_ID`, `PRE_ALERT_MIN`, `TZ=Asia/Seoul`
4. Settings → Start Command 가 `python bot.py` 인지 확인 (Procfile 있어서 자동 인식됨)
5. 배포 로그에 `로그인 완료` 뜨면 끝

### ⚠️ "무료" 호스팅 솔직한 현황 (2026년 기준)
- **Railway**: 월 $5 크레딧 주지만 카드 등록 필요. 이 봇은 아주 가벼워서 보통 크레딧 안에서 돌아가지만, 초과하면 과금됨.
- **Render 무료 플랜**: 15분 활동 없으면 잠들어서 **알림 봇에 부적합**. 유료($7/월)부터 가능.
- **Fly.io**: 카드 등록하면 소규모 VM 무료 범위 있음. 24시간 유지됨. 설정이 Railway보다 조금 복잡.
- **Oracle Cloud Always Free**: 진짜 영구 무료 VM이지만 리눅스 세팅을 직접 해야 함.
- 👉 제일 편한 건 Railway, 진짜 0원 목표면 Fly.io 또는 Oracle.

### 💾 시간표 저장 주의
`bosses.json`에 저장되는데, Railway/Fly는 **재배포하면 파일이 초기화**됩니다.
- 방법 A: 시간표가 정해지면 `bosses.json`을 직접 채워서 GitHub에 커밋 (제일 간단)
- 방법 B: Railway **Volume** 붙이고 `DATA_FILE=/data/bosses.json` 환경변수 설정

## 파일 구성
```
bot.py            봇 본체
bosses.json       시간표 (예시 들어있음)
requirements.txt  의존성
Procfile          Railway/Heroku 실행 명령
.env.example      환경변수 예시
```
