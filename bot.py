"""
보스타임 알림 디스코드 봇 (고정 시간표 + 주기형 사이클 지원)

- 고정형: 매일 같은 시각 알림 (예: 발록 20:00)
- 주기형: "기준 시작 시각 + 진행 시간 + 대기 시간" 으로 무한 반복
    예) 오만의탑: 09:30 시작 → 1시간30분 진행 → 2시간 대기 → 다시 시작 (한 바퀴 3시간30분)
- 알림: 시작 N분 전 / 시작 / 종료 N분 전 / 종료, @역할 멘션과 함께 채널 전송
- 시간표는 bosses.json 에 저장, 슬래시 명령어로 관리
"""

import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

# ─────────────────────────────── 설정 파일 읽기 ───────────────────────────────
# 설정.txt 또는 .env 파일이 같은 폴더에 있으면 거기 적힌 값을 환경변수로 사용
def _load_settings_file():
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in ("설정.txt", ".env"):
        path = os.path.join(here, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        break

_load_settings_file()

# ─────────────────────────────── 기본 설정 ───────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
def _int_env(name: str, default: int = 0) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v.isdigit() else default

CHANNEL_ID = _int_env("CHANNEL_ID")
ROLE_ID = _int_env("ROLE_ID")
GUILD_ID = _int_env("GUILD_ID")
PRE_ALERT_MIN = _int_env("PRE_ALERT_MIN", 5)   # N분 전 예고 (0이면 끔)
DATA_FILE = os.environ.get("DATA_FILE", "bosses.json")
# 사이트 자동 보정 (l1justice 보스 상태 JSON) — 0 이면 끔
AUTO_SYNC = os.environ.get("AUTO_SYNC", "1").strip() != "0"
STATE_URL = os.environ.get("STATE_URL", "https://l1justice.com/lineage/database/bosses/state/")
HOME_URL = os.environ.get("HOME_URL", "https://l1justice.com/lineage/")   # "Server Status: Online/Offline" 표시


def parse_server_status(html: str) -> bool | None:
    """홈페이지 HTML 에서 서버 상태 읽기. Online=True, Offline=False, 못 찾으면 None"""
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    m = re.search(r"Server\s*Status\s*:\s*(Online|Offline)", text, re.I)
    if not m:
        return None
    return m.group(1).lower() == "online"
SYNC_TOLERANCE_MIN = 2   # 예측과 실제 시작이 이 분 이상 차이 나면 기준시각 보정
MASS_FLIP = 45           # 한 번에 이만큼 넘게 ACTIVE 로 바뀌면 = 서버 재시작(보스 일괄 복구)으로 판단
try:
    TZ = ZoneInfo(os.environ.get("TZ", "Asia/Seoul"))
except Exception:
    # 윈도우에 시간대 데이터(tzdata)가 없을 때: 한국시간(UTC+9) 고정으로 대체
    from datetime import timezone
    TZ = timezone(timedelta(hours=9), "KST")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bossbot")

if not TOKEN or "여기에" in TOKEN:
    raise SystemExit("❌ 설정.txt 에 DISCORD_TOKEN(봇 토큰)을 입력해 주세요.")
if not CHANNEL_ID:
    raise SystemExit("❌ 설정.txt 에 CHANNEL_ID(채널 ID 숫자)를 입력해 주세요.")

# ─────────────────────────────── 데이터 ───────────────────────────────
# bosses.json 구조:
# {
#   "발록":     {"type": "fixed", "times": ["20:00", "22:00"]},
#   "오만의탑": {"type": "cycle", "anchor": "2026-09-02 09:30", "active_min": 90, "rest_min": 120}
# }
def load_bosses() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_bosses(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


bosses: dict = load_bosses()

# ─────────────────────────────── 시간 유틸 ───────────────────────────────
def parse_hhmm(text: str) -> str | None:
    text = text.strip().replace(".", ":")
    if ":" not in text and len(text) == 4 and text.isdigit():
        text = text[:2] + ":" + text[2:]
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def parse_minutes(text: str) -> int | None:
    """'1시간30분', '90', '90분', '2h', '1.5h', '2:30' → 분 단위 정수"""
    t = text.strip().lower().replace(" ", "")
    if t.isdigit():
        return int(t)
    if t.endswith("분") and t[:-1].isdigit():
        return int(t[:-1])
    if ":" in t:
        h, m = t.split(":", 1)
        if h.isdigit() and m.isdigit():
            return int(h) * 60 + int(m)
    if "시간" in t:
        h, _, m = t.partition("시간")
        m = m.replace("분", "")
        try:
            return int(float(h) * 60) + (int(m) if m else 0)
        except ValueError:
            return None
    if t.endswith("h"):
        try:
            return int(float(t[:-1]) * 60)
        except ValueError:
            return None
    return None


def parse_anchor(text: str, now: datetime) -> datetime | None:
    """'09:30' (오늘) 또는 '2026-09-02 09:30' → tz-aware datetime"""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%m-%d %H:%M", "%m/%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            return dt.replace(tzinfo=TZ)
        except ValueError:
            pass
    hhmm = parse_hhmm(text)
    if hhmm:
        h, m = map(int, hhmm.split(":"))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    return None


def fmt_remaining(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    h, rem = divmod(total, 3600)
    m = rem // 60
    return f"{h}시간 {m}분" if h else f"{m}분"


def fmt_dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}시간 {m}분"
    return f"{h}시간" if h else f"{m}분"


# ─────────────────────────────── 이벤트 계산 ───────────────────────────────
# 이벤트 = (시각, 보스이름, 종류)  종류: "start" | "end"
def upcoming_events(name: str, info: dict, now: datetime, horizon_min: int = 60) -> list[tuple[datetime, str, str]]:
    """now 이전 1분 ~ now+horizon 사이의 이벤트 목록"""
    events = []
    lo = now - timedelta(minutes=1)
    hi = now + timedelta(minutes=horizon_min)

    if info.get("type", "fixed") == "fixed":
        for t in info.get("times", []):
            h, m = map(int, t.split(":"))
            for d in (-1, 0, 1):
                ev = (now + timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
                if lo <= ev <= hi:
                    events.append((ev, name, "start"))
    else:
        anchor = datetime.strptime(info["anchor"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        active = timedelta(minutes=info["active_min"])
        cycle = active + timedelta(minutes=info["rest_min"])
        k = int((lo - anchor) / cycle)
        for i in (k - 1, k, k + 1, k + 2):
            s = anchor + cycle * i
            e = s + active
            if lo <= s <= hi:
                events.append((s, name, "start"))
            if lo <= e <= hi:
                events.append((e, name, "end"))
    return events


def cycle_status(info: dict, now: datetime) -> tuple[str, datetime, datetime]:
    """주기형 보스의 현재 상태: ('진행중'|'대기중', 다음시작, 다음종료)"""
    anchor = datetime.strptime(info["anchor"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    active = timedelta(minutes=info["active_min"])
    cycle = active + timedelta(minutes=info["rest_min"])
    k = int((now - anchor) / cycle) if now >= anchor else -1
    s = anchor + cycle * k
    e = s + active
    if s <= now < e:
        return "진행중", s + cycle, e
    return "대기중", s + cycle if now >= e else s, (s + cycle if now >= e else s) + active


# ─────────────────────────────── 봇 본체 ───────────────────────────────
class BossBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.sent_keys: set[str] = set()
        self.prev_states: dict[str, str] | None = None   # 사이트 직전 상태
        self.last_sync: dict[str, datetime] = {}          # 보스별 마지막 자동보정 시각
        self.site_ok: bool | None = None
        self.server_online: bool | None = None           # 리니지 서버 온라인 여부

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.ticker.start()
        if AUTO_SYNC:
            self.site_poller.start()

    async def on_ready(self):
        log.info("로그인 완료: %s (보스 %d개)", self.user, len(bosses))

    @tasks.loop(seconds=30)
    async def ticker(self):
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        if self.server_online is False:
            return  # 서버 오프라인 중엔 알림 멈춤
        for name, info in list(bosses.items()):
            if info.get("unsynced"):
                continue  # 서버 재시작 후 아직 실제 시작 확인 전 → 틀린 알림 방지
            try:
                for ev_time, _, kind in upcoming_events(name, info, now, horizon_min=max(PRE_ALERT_MIN, 1)):
                    # 본 알림
                    if ev_time == now:
                        await self._send_once(f"{ev_time:%Y%m%d%H%M}|{name}|{kind}", name, kind, ev_time, pre=False)
                    # 예고 알림
                    if PRE_ALERT_MIN and ev_time == now + timedelta(minutes=PRE_ALERT_MIN):
                        await self._send_once(f"{ev_time:%Y%m%d%H%M}|{name}|{kind}|pre", name, kind, ev_time, pre=True)
            except Exception as e:
                log.error("이벤트 계산 실패 (%s): %s", name, e)
        if len(self.sent_keys) > 1000:
            self.sent_keys.clear()

    @ticker.before_loop
    async def before_ticker(self):
        await self.wait_until_ready()

    # ───────── 사이트 자동 보정: 60초마다 보스 상태 확인 ─────────
    @tasks.loop(seconds=60)
    async def site_poller(self):
        import aiohttp
        # 1) 서버 온라인/오프라인 확인
        status = None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(HOME_URL, headers={"User-Agent": "boss-alarm-bot"}) as r:
                    if r.status == 200:
                        status = parse_server_status(await r.text())
        except Exception as e:
            log.warning("서버 상태 확인 실패: %s", e)
        if status is False:
            if self.server_online is not False:
                self.server_online = False
                for info in bosses.values():
                    if info.get("type") == "cycle":
                        info["unsynced"] = True
                save_bosses(bosses)
                log.info("서버 오프라인 감지")
                await self._notify("⛔ 리니지 서버 오프라인 감지 — 보스 알림을 잠시 멈출게요.")
            return  # 오프라인 중 사이트 보스 상태는 멈춰 있으므로 무시
        if status is True and self.server_online is False:
            self.server_online = True
            self.last_sync.clear()
            self.prev_states = None   # 재시작 후 첫 상태를 새 기준값으로
            log.info("서버 온라인 복귀")
            await self._notify("✅ 서버 온라인! 보스가 실제로 뜨는 순간 사이클별로 시간표를 자동으로 맞출게요. (그 전까지 해당 알림은 쉬어요)")
        elif status is True:
            self.server_online = True

        # 2) 보스 상태 확인
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(STATE_URL, headers={"User-Agent": "boss-alarm-bot"}) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    states = await r.json(content_type=None)
            if self.site_ok is not True:
                log.info("사이트 상태 수신 OK (%d개)", len(states))
            self.site_ok = True
        except Exception as e:
            if self.site_ok is not False:
                log.warning("사이트 상태 수신 실패: %s", e)
            self.site_ok = False
            return

        now = datetime.now(TZ).replace(second=0, microsecond=0)
        prev = self.prev_states
        self.prev_states = states
        if prev is None:
            return  # 첫 수신은 기준값만 저장

        # 서버 재시작 감지: 사이트 전체에서 한꺼번에 많이 ACTIVE 로 바뀌면 보스타임 시작이 아님
        flipped = sum(1 for k, v in states.items() if v == "ACTIVE" and prev.get(k, "INACTIVE") != "ACTIVE")
        if flipped >= MASS_FLIP:
            self.last_sync.clear()   # 중복방지 기록 초기화 → 진짜 보스타임은 놓치지 않게
            log.info("서버 재시작으로 판단 (%d개 동시 ACTIVE) — 이번 변화는 무시", flipped)
            await self._notify("🔌 서버 재시작 감지! 보스타임이 실제로 열리는 순간 시간표를 자동으로 맞출게요.")
            return

        for name, info in list(bosses.items()):
            ids = [str(i) for i in info.get("spawn_ids", [])]
            if info.get("type") != "cycle" or not ids:
                continue
            # 비활성/죽음 → 활성 으로 바뀐 보스가 있으면 = 보스타임 시작
            respawned = [i for i in ids if states.get(i) == "ACTIVE" and prev.get(i, "INACTIVE") != "ACTIVE"]
            if not respawned:
                continue
            last = self.last_sync.get(name)
            if last and now - last < timedelta(minutes=info["active_min"]):
                continue  # 같은 보스타임 안에서 중복 감지 방지
            self.last_sync[name] = now
            await self._on_detected_start(name, info, now)

    @site_poller.before_loop
    async def before_poller(self):
        await self.wait_until_ready()

    async def _on_detected_start(self, name: str, info: dict, detected: datetime):
        """사이트에서 시작 감지 → 예측과 다르면 기준시각 보정 + 시작 알림"""
        anchor = datetime.strptime(info["anchor"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        cycle = timedelta(minutes=info["active_min"] + info["rest_min"])
        k = round((detected - anchor) / cycle)
        predicted = anchor + cycle * k
        diff_min = abs((detected - predicted).total_seconds()) / 60
        if info.pop("unsynced", None):
            info["anchor"] = detected.strftime("%Y-%m-%d %H:%M")
            save_bosses(bosses)
            log.info("재시작 후 확정: %s %s", name, detected.strftime("%H:%M"))
            key = f"{detected:%Y%m%d%H%M}|{name}|start"
            if key not in self.sent_keys:
                self.sent_keys.add(key)
                await self.announce(name, "start", detected, pre=False)
            await self._notify(f"📌 **{name}** 서버 재시작 후 첫 보스타임 확인 ({detected:%H:%M}) → 시간표 확정!")
            return
        if diff_min <= SYNC_TOLERANCE_MIN:
            log.info("자동확인: %s 예측대로 시작 (%s)", name, detected.strftime("%H:%M"))
            return
        info["anchor"] = detected.strftime("%Y-%m-%d %H:%M")
        save_bosses(bosses)
        log.info("자동보정: %s 기준 %s → %s", name, predicted.strftime("%H:%M"), detected.strftime("%H:%M"))
        # 새 기준으로 알림 (틱커와 중복 안 되게 키 등록)
        key = f"{detected:%Y%m%d%H%M}|{name}|start"
        if key not in self.sent_keys:
            self.sent_keys.add(key)
            await self.announce(name, "start", detected, pre=False)
        try:
            channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
            await channel.send(f"🔄 **{name}** 시간표 자동 보정: 예상 {predicted:%H:%M} → 실제 {detected:%H:%M} (사이트 기준)")
        except Exception as e:
            log.error("보정 안내 전송 실패: %s", e)

    async def _notify(self, text: str):
        try:
            channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
            await channel.send(text)
        except Exception as e:
            log.error("안내 전송 실패: %s", e)

    async def _send_once(self, key: str, name: str, kind: str, ev_time: datetime, pre: bool):
        if key in self.sent_keys:
            return
        self.sent_keys.add(key)
        await self.announce(name, kind, ev_time, pre)

    async def announce(self, name: str, kind: str, ev_time: datetime, pre: bool):
        channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        mention = f"<@&{ROLE_ID}> " if ROLE_ID else ""
        info = bosses.get(name, {})
        t = ev_time.strftime("%H:%M")
        if kind == "start":
            if pre:
                msg = f"{mention}⏰ **{name}** 시작 {PRE_ALERT_MIN}분 전! ({t})"
            else:
                extra = f" — {fmt_dur(info['active_min'])} 동안 진행" if info.get("type") == "cycle" else ""
                desc = f"\n└ {info['desc']}" if info.get("desc") else ""
                msg = f"{mention}🔥 **{name}** 시작! ({t}){extra}{desc}"
        else:
            if pre:
                msg = f"{mention}⌛ **{name}** 종료 {PRE_ALERT_MIN}분 전! ({t})"
            else:
                msg = f"{mention}🏁 **{name}** 종료 ({t}) — {fmt_dur(info['rest_min'])} 뒤 다시 시작"
        try:
            await channel.send(msg, allowed_mentions=discord.AllowedMentions(roles=True))
            log.info("알림: %s", msg)
        except Exception as e:
            log.error("알림 전송 실패: %s", e)


client = BossBot()

# ─────────────────────────────── 슬래시 명령어 ───────────────────────────────
@client.tree.command(name="보스추가", description="고정 시간표 보스 추가 (매일 반복). 예: 발록 20:00,22:00")
@app_commands.describe(이름="보스 이름", 시간="젠 시각(24시간제), 여러 개면 쉼표. 예: 14:30,20:30")
async def add_fixed(interaction: discord.Interaction, 이름: str, 시간: str):
    parsed = []
    for raw in 시간.split(","):
        p = parse_hhmm(raw)
        if not p:
            await interaction.response.send_message(f"❌ 시간 형식이 이상해요: `{raw.strip()}` (예: 14:30)", ephemeral=True)
            return
        parsed.append(p)
    cur = bosses.get(이름)
    times = set(cur["times"]) if cur and cur.get("type") == "fixed" else set()
    times.update(parsed)
    bosses[이름] = {"type": "fixed", "times": sorted(times)}
    save_bosses(bosses)
    await interaction.response.send_message(f"✅ **{이름}** (고정) 등록: {', '.join(bosses[이름]['times'])} 매일")


@client.tree.command(name="주기추가", description="주기형 보스 추가. 예: 오만의탑 / 시작 09:30 / 진행 1시간30분 / 대기 2시간")
@app_commands.describe(
    이름="보스 이름",
    시작시각="확실히 시작한 시각. 09:30 (오늘) 또는 2026-09-02 09:30",
    진행시간="진행 시간. 예: 1시간30분, 90분, 2h",
    대기시간="종료 후 대기 시간. 예: 2시간, 150분",
)
async def add_cycle(interaction: discord.Interaction, 이름: str, 시작시각: str, 진행시간: str, 대기시간: str):
    now = datetime.now(TZ)
    anchor = parse_anchor(시작시각, now)
    a = parse_minutes(진행시간)
    r = parse_minutes(대기시간)
    if not anchor:
        await interaction.response.send_message("❌ 시작시각 형식이 이상해요. 예: 09:30 또는 2026-09-02 09:30", ephemeral=True)
        return
    if not a or not r:
        await interaction.response.send_message("❌ 시간 형식이 이상해요. 예: 1시간30분, 90분, 2h", ephemeral=True)
        return
    bosses[이름] = {"type": "cycle", "anchor": anchor.strftime("%Y-%m-%d %H:%M"), "active_min": a, "rest_min": r}
    save_bosses(bosses)
    st, ns, ne = cycle_status(bosses[이름], now)
    await interaction.response.send_message(
        f"✅ **{이름}** (주기) 등록: {fmt_dur(a)} 진행 → {fmt_dur(r)} 대기 (한 바퀴 {fmt_dur(a + r)})\n"
        f"기준 시작 {anchor:%m/%d %H:%M} · 지금 {st} · 다음 시작 {ns:%H:%M}"
    )


@client.tree.command(name="기준시각", description="주기형 보스의 기준 시작 시각 재설정 (점검 후 밀렸을 때)")
@app_commands.describe(이름="보스 이름", 시작시각="새로 확인한 시작 시각. 09:30 또는 2026-09-02 09:30")
async def reset_anchor(interaction: discord.Interaction, 이름: str, 시작시각: str):
    info = bosses.get(이름)
    if not info or info.get("type") != "cycle":
        await interaction.response.send_message(f"❌ **{이름}** 은(는) 주기형 보스가 아니에요.", ephemeral=True)
        return
    now = datetime.now(TZ)
    anchor = parse_anchor(시작시각, now)
    if not anchor:
        await interaction.response.send_message("❌ 시작시각 형식이 이상해요.", ephemeral=True)
        return
    info["anchor"] = anchor.strftime("%Y-%m-%d %H:%M")
    info.pop("unsynced", None)
    save_bosses(bosses)
    st, ns, _ = cycle_status(info, now)
    await interaction.response.send_message(f"🔧 **{이름}** 기준 시작 {anchor:%m/%d %H:%M} 으로 재설정 · 지금 {st} · 다음 시작 {ns:%H:%M}")


@client.tree.command(name="보스삭제", description="보스 삭제")
@app_commands.describe(이름="보스 이름")
async def remove_boss(interaction: discord.Interaction, 이름: str):
    if 이름 not in bosses:
        await interaction.response.send_message(f"❌ **{이름}** 은(는) 등록되어 있지 않아요.", ephemeral=True)
        return
    del bosses[이름]
    save_bosses(bosses)
    await interaction.response.send_message(f"🗑️ **{이름}** 삭제 완료")


@client.tree.command(name="보스목록", description="현재 상태와 다음 시작까지 남은 시간 보기")
async def list_bosses(interaction: discord.Interaction):
    if not bosses:
        await interaction.response.send_message("등록된 보스가 없어요. `/보스추가` 또는 `/주기추가` 로 등록해 주세요.", ephemeral=True)
        return
    now = datetime.now(TZ)
    rows = []
    for name, info in bosses.items():
        if info.get("type") == "cycle" and info.get("unsynced"):
            rows.append((now, f"⏳ {name:<10} 서버 재시작 후 대기 · 보스 뜨면 자동 확정"))
            continue
        if info.get("type") == "cycle":
            st, ns, ne = cycle_status(info, now)
            if st == "진행중":
                # 진행중이면 현재 사이클 종료 시각 = ne - cycle... 다시 계산
                anchor = datetime.strptime(info["anchor"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                cycle = timedelta(minutes=info["active_min"] + info["rest_min"])
                k = int((now - anchor) / cycle)
                cur_end = anchor + cycle * k + timedelta(minutes=info["active_min"])
                rows.append((cur_end, f"🟢 {name:<10} 진행중 · 종료까지 {fmt_remaining(cur_end - now)} ({cur_end:%H:%M}) · 다음 시작 {ns:%H:%M}"))
            else:
                rows.append((ns, f"⚪ {name:<10} 대기중 · 시작까지 {fmt_remaining(ns - now)} ({ns:%H:%M})"))
        else:
            for t in info.get("times", []):
                h, m = map(int, t.split(":"))
                ns = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if ns <= now:
                    ns += timedelta(days=1)
                rows.append((ns, f"📌 {name:<10} 매일 {t} · 남은 시간 {fmt_remaining(ns - now)}"))
    rows.sort(key=lambda r: r[0])
    body = "\n".join(r[1] for r in rows)
    await interaction.response.send_message(f"📋 **보스 시간표** ({now:%H:%M} 기준)\n```\n{body}\n```")


@client.tree.command(name="사이트상태", description="l1justice 사이트 기준 보스 그룹 실시간 상태")
async def site_status(interaction: discord.Interaction):
    states = client.prev_states
    if not AUTO_SYNC:
        await interaction.response.send_message("자동 보정이 꺼져 있어요 (AUTO_SYNC=0).", ephemeral=True)
        return
    if not states:
        await interaction.response.send_message("아직 사이트 정보를 못 받았어요. 1분 뒤 다시 해보세요.", ephemeral=True)
        return
    lines = ["🌐 **사이트 실시간 상태** (ACTIVE / 전체)", "```"]
    for name, info in bosses.items():
        ids = [str(i) for i in info.get("spawn_ids", [])]
        if not ids:
            continue
        act = sum(1 for i in ids if states.get(i) == "ACTIVE")
        lines.append(f"{name:<10} ACTIVE {act}/{len(ids)}")
    lines.append("```")
    lines.append("※ 보스가 INACTIVE→ACTIVE 로 바뀌는 순간을 보스타임 시작으로 보고 자동 보정해요.")
    await interaction.response.send_message("\n".join(lines))


@client.tree.command(name="테스트알림", description="알림 채널/역할 멘션 테스트")
async def test_alert(interaction: discord.Interaction):
    await interaction.response.send_message("테스트 알림을 보낼게요.", ephemeral=True)
    await client.announce("테스트보스", "start", datetime.now(TZ), pre=False)


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
