import os
import logging
import sqlite3
import zipfile
import random
from datetime import datetime, timedelta, time, timezone, date
from math import sqrt

from dotenv import load_dotenv

from telegram import (
    Update,
    ChatMemberUpdated,
    ChatInviteLink,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# -----------------------
# .env 로드 & 기본 설정
# -----------------------
load_dotenv()  # .env 파일 읽기

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "xp_bot.db")

# 메인 그룹 (랭킹/요약 기준 채팅)
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))  # 0이면 미지정

# Bot owner (BotFather로 만든 계정)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# 최초 관리자 목록 (.env의 ADMIN_USER_IDS)
_admin_env = os.getenv("ADMIN_USER_IDS", "")
INITIAL_ADMIN_IDS: set[int] = set()
for part in _admin_env.split(","):
    part = part.strip()
    if part:
        try:
            INITIAL_ADMIN_IDS.add(int(part))
        except ValueError:
            pass

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 환경 변수가 설정되어 있지 않습니다.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 현재 프로세스 메모리에 들고 있는 관리자 목록
ADMIN_USER_IDS: set[int] = set()

# 간단한 로터리(추첨) 상태 (chat_id 기준)
# 예: LOTTERY_STATE[chat_id] = {
#   "active": True,
#   "participants": set([...]),
#   "duration": 60,        # 분 단위 (또는 None)
#   "winners": 3,          # 자동 종료 시 사용할 당첨자 수 (또는 None)
#   "job": Job 객체 또는 None
# }
LOTTERY_STATE: dict[int, dict] = {}


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or user_id in ADMIN_USER_IDS


def all_admin_targets() -> set[int]:
    targets = set(ADMIN_USER_IDS)
    if OWNER_ID:
        targets.add(OWNER_ID)
    return targets


def is_main_chat(chat_id: int) -> bool:
    if MAIN_CHAT_ID == 0:
        return True
    return chat_id == MAIN_CHAT_ID


def is_private_chat(chat) -> bool:
    return chat and chat.type == "private"


# ✅ 그룹 관리자 OR DB 관리자 OR OWNER 판정
async def is_chat_admin_or_bot_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    - DB 관리자(or OWNER)이면 True
    - 아니면, 해당 그룹에서 status 가 creator/administrator 인지 확인
    """
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    # DB 관리자 / OWNER 우선
    if is_admin(user.id):
        return True

    # 그룹/슈퍼그룹 관리자 체크
    if chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            if member.status in ("creator", "administrator"):
                return True
        except Exception:
            logger.exception("get_chat_member 실패 (chat_id=%s, user_id=%s)", chat.id, user.id)

    return False


# -----------------------
# DB 유틸
# -----------------------


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def reload_admins():
    """admin_users 테이블에서 관리자 리스트 다시 읽기"""
    global ADMIN_USER_IDS
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT admin_id FROM admin_users")
    rows = cur.fetchall()
    conn.close()
    ADMIN_USER_IDS = {int(r["admin_id"]) for r in rows}
    logger.info("Loaded admins: %s", ADMIN_USER_IDS)


def ensure_user_stats_columns(cur):
    """
    기존 DB에 새로운 컬럼 추가 (이미 있으면 skip)
    - last_xp_at    : 마지막 XP 부여 시각(UTC ISO)
    - daily_xp      : 마지막 일자 기준 오늘 누적 XP
    - daily_xp_date : 일일 XP 기준 날짜(KST, YYYY-MM-DD)
    """
    cur.execute("PRAGMA table_info(user_stats)")
    cols = {row["name"] for row in cur.fetchall()}

    if "last_xp_at" not in cols:
        cur.execute("ALTER TABLE user_stats ADD COLUMN last_xp_at TEXT")
    if "daily_xp" not in cols:
        cur.execute("ALTER TABLE user_stats ADD COLUMN daily_xp INTEGER DEFAULT 0")
    if "daily_xp_date" not in cols:
        cur.execute("ALTER TABLE user_stats ADD COLUMN daily_xp_date TEXT")


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # 유저 XP / 메세지 / 초대수
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_stats (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            messages_count INTEGER DEFAULT 0,
            last_daily TEXT,
            invites_count INTEGER DEFAULT 0,
            last_xp_at TEXT,
            daily_xp INTEGER DEFAULT 0,
            daily_xp_date TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )

    # 초대 링크 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invite_links (
            invite_link TEXT PRIMARY KEY,
            chat_id INTEGER,
            inviter_id INTEGER,
            created_at TEXT,
            joined_count INTEGER DEFAULT 0
        )
        """
    )

    # 어떤 유저가 어떤 초대 링크로 들어왔는지
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invited_users (
            chat_id INTEGER,
            user_id INTEGER,
            inviter_id INTEGER,
            invite_link TEXT,
            joined_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )

    # 관리자 목록
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            admin_id INTEGER PRIMARY KEY
        )
        """
    )

    # XP 키워드 (bonus / block)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS xp_keywords (
            word TEXT PRIMARY KEY,
            mode TEXT NOT NULL,   -- 'bonus' 또는 'block'
            delta INTEGER DEFAULT 0
        )
        """
    )

    # XP 로그 (기간 통계용)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS xp_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            xp_delta INTEGER,
            msg_len INTEGER,
            created_at TEXT
        )
        """
    )

    # 봇 설정값 (안티스팸, 초대 XP, 캠페인 기간 등)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cooldown_seconds INTEGER DEFAULT 7,
            daily_xp_cap INTEGER DEFAULT 500,
            invite_xp INTEGER DEFAULT 100,
            campaign_start TEXT,
            campaign_end TEXT
        )
        """
    )

    # user_stats에 새 컬럼이 없는 경우 추가
    ensure_user_stats_columns(cur)

    # 최초 관리자 등록
    for aid in INITIAL_ADMIN_IDS:
        cur.execute("INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)", (aid,))

    # 기본 키워드(리스트용): ㅋㅋ, ㄱㄱ (단독 처리용, block으로 두지만 로직에서 별도 처리)
    cur.execute(
        "INSERT OR IGNORE INTO xp_keywords (word, mode, delta) VALUES (?, 'block', 0)",
        ("ㅋㅋ",),
    )
    cur.execute(
        "INSERT OR IGNORE INTO xp_keywords (word, mode, delta) VALUES (?, 'block', 0)",
        ("ㄱㄱ",),
    )

    # bot_settings 기본 1행 생성
    cur.execute("SELECT id FROM bot_settings WHERE id=1")
    row = cur.fetchone()
    if not row:
        cur.execute(
            """
            INSERT INTO bot_settings (id, cooldown_seconds, daily_xp_cap, invite_xp)
            VALUES (1, 7, 500, 100)
            """
        )

    conn.commit()
    conn.close()

    reload_admins()


# -----------------------
# 설정 로딩/변경
# -----------------------


def get_settings():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT cooldown_seconds, daily_xp_cap, invite_xp,
               campaign_start, campaign_end
        FROM bot_settings WHERE id=1
        """
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {
            "cooldown_seconds": 7,
            "daily_xp_cap": 500,
            "invite_xp": 100,
            "campaign_start": None,
            "campaign_end": None,
        }
    return {
        "cooldown_seconds": row["cooldown_seconds"] or 0,
        "daily_xp_cap": row["daily_xp_cap"] or 0,
        "invite_xp": row["invite_xp"] or 0,
        "campaign_start": row["campaign_start"],
        "campaign_end": row["campaign_end"],
    }


def update_settings(**kwargs):
    """
    예: update_settings(cooldown_seconds=10, daily_xp_cap=1000)
    """
    allowed = {"cooldown_seconds", "daily_xp_cap", "invite_xp", "campaign_start", "campaign_end"}
    fields = []
    values = []
    for k, v in kwargs.items():
        if k in allowed:
            fields.append(f"{k}=?")
            values.append(v)
    if not fields:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE bot_settings SET {', '.join(fields)} WHERE id=1",
        tuple(values),
    )
    conn.commit()
    conn.close()


# -----------------------
# XP / 레벨 계산 & 로그
# -----------------------


def calc_level(xp: int) -> int:
    # xp가 커질수록 레벨업이 점점 어려워지도록
    return int(sqrt(xp / 100)) + 1 if xp > 0 else 1


def xp_for_next_level(level: int) -> int:
    next_level = level + 1
    return int((next_level - 1) ** 2 * 100)


def log_xp(chat_id: int, user_id: int, xp_delta: int, msg_len: int = 0):
    """xp_log에 기록 (캠페인/월별 통계를 위해 모든 XP 소스 기록)"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO xp_log (chat_id, user_id, xp_delta, msg_len, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            chat_id,
            user_id,
            xp_delta,
            msg_len,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def add_xp(chat_id: int, user, base_xp: int):
    """XP 추가 후 (xp, level, messages_count) 반환"""
    user_id = user.id
    username = user.username
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT xp, level, messages_count FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    )
    row = cur.fetchone()

    if not row:
        xp = max(0, base_xp)
        level = calc_level(xp)
        messages_count = 1
        cur.execute(
            """
            INSERT INTO user_stats
            (chat_id, user_id, username, first_name, last_name, xp, level, messages_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user_id,
                username,
                first_name,
                last_name,
                xp,
                level,
                messages_count,
            ),
        )
    else:
        xp = row["xp"] + max(0, base_xp)
        level = calc_level(xp)
        messages_count = row["messages_count"] + 1
        cur.execute(
            """
            UPDATE user_stats
            SET username=?, first_name=?, last_name=?, xp=?, level=?, messages_count=?
            WHERE chat_id=? AND user_id=?
            """,
            (
                username,
                first_name,
                last_name,
                xp,
                level,
                messages_count,
                chat_id,
                user_id,
            ),
        )

    conn.commit()
    conn.close()
    return xp, level, messages_count


def get_xp_keywords():
    """xp_keywords 전체 조회"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT word, mode, delta FROM xp_keywords")
    rows = cur.fetchall()
    conn.close()
    return rows


# -----------------------
# 초대수 계산 (invite_links 기준)
# -----------------------


def get_invite_count_for_user(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    if MAIN_CHAT_ID != 0:
        cur.execute(
            """
            SELECT COALESCE(SUM(joined_count),0) AS c
            FROM invite_links
            WHERE inviter_id=? AND chat_id=?
            """,
            (user_id, MAIN_CHAT_ID),
        )
    else:
        cur.execute(
            """
            SELECT COALESCE(SUM(joined_count),0) AS c
            FROM invite_links
            WHERE inviter_id=?
            """,
            (user_id,),
        )
    row = cur.fetchone()
    conn.close()
    return int(row["c"] or 0)


# -----------------------
# 일반 메시지 → XP
# -----------------------


def _is_emoji_only(text: str) -> bool:
    """대충 이모지/기호만 있는지 검사 (한글/영문/숫자 없으면 이모지로 간주)"""
    stripped = "".join(ch for ch in text if not ch.isspace())
    if not stripped:
        return False
    for ch in stripped:
        if ch.isalnum():
            return False
        # 한글
        if "가" <= ch <= "힣":
            return False
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user

    if not chat or not user or not message:
        return
    if chat.type not in ("group", "supergroup"):
        return

    text = message.text or message.caption or ""
    raw = text.strip()
    no_space = "".join(ch for ch in raw if not ch.isspace())

    # 0) 특수 케이스: ㅋㅋㅋ / ㄱㄱ가 "단독"일 때 (공백 제거 후 전부 ㅋ 또는 전부 ㄱ)
    only_kek = bool(no_space) and all(ch == "ㅋ" for ch in no_space)
    only_gg = bool(no_space) and all(ch == "ㄱ" for ch in no_space)
    only_kek_or_gg = only_kek or only_gg

    # 기본 XP (메시지 길이 기반)
    base_xp = 3 + len(no_space) // 20

    # 1) 아주 짧은 메시지 → XP 0
    if len(no_space) < 5:
        base_xp = 0

    # 2) 이모지만 있는 메시지 → XP 0
    if _is_emoji_only(text):
        base_xp = 0

    # 3) 단독 ㅋㅋ / 단독 ㄱㄱ → XP 0 (길이에 상관없이)
    if only_kek_or_gg:
        base_xp = 0

    # 4) 키워드 기반 보너스/차단
    keywords = get_xp_keywords()
    lower_text = text.lower()
    blocked = False
    bonus_total = 0

    # 단독 ㅋㅋ / ㄱㄱ 는 위에서 이미 처리했으므로
    # 키워드 블록/보너스 로직에서는 더 이상 영향을 주지 않도록 한다.
    if not only_kek_or_gg:
        for row in keywords:
            word = row["word"]
            mode = row["mode"]
            delta = row["delta"] or 0

            if not word:
                continue

            # ㅋㅋ / ㄱㄱ 는 여기서는 스킵 (단독일 때만 처리)
            if word in ("ㅋㅋ", "ㄱㄱ"):
                continue

            if word.lower() in lower_text:
                if mode == "block":
                    blocked = True
                elif mode == "bonus":
                    bonus_total += delta

    if blocked:
        xp_delta = 0
    else:
        xp_delta = base_xp + bonus_total

    if xp_delta < 0:
        xp_delta = 0

    # -----------------------
    # XP 안티 스팸 적용 (쿨다운 + 일일 상한)
    # -----------------------
    settings = get_settings()
    cooldown_sec = settings["cooldown_seconds"]
    daily_cap = settings["daily_xp_cap"]

    now_utc = datetime.utcnow()
    now_kst = now_utc + timedelta(hours=9)
    today_kst_str = now_kst.date().isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT last_xp_at, daily_xp, daily_xp_date
        FROM user_stats
        WHERE chat_id=? AND user_id=?
        """,
        (chat.id, user.id),
    )
    row = cur.fetchone()

    last_xp_at = None
    daily_xp_current = 0
    daily_date = None

    if row:
        if row["last_xp_at"]:
            try:
                last_xp_at = datetime.fromisoformat(row["last_xp_at"])
            except Exception:
                last_xp_at = None
        daily_xp_current = row["daily_xp"] or 0
        daily_date = row["daily_xp_date"]

    # 날짜가 바뀌면 오늘 일일 XP 0으로 리셋
    if daily_date != today_kst_str:
        daily_xp_current = 0

    # 쿨다운 적용
    if xp_delta > 0 and cooldown_sec > 0 and last_xp_at is not None:
        if (now_utc - last_xp_at).total_seconds() < cooldown_sec:
            xp_delta = 0

    # 일일 상한 적용
    if xp_delta > 0 and daily_cap > 0:
        if daily_xp_current >= daily_cap:
            xp_delta = 0
        else:
            allowed = daily_cap - daily_xp_current
            if xp_delta > allowed:
                xp_delta = allowed

    conn.close()

    # XP 반영 + messages_count 증가
    xp, level, _ = add_xp(chat.id, user, xp_delta)

    # 안티스팸 관련 필드 업데이트 (XP가 실제로 부여된 경우만)
    if xp_delta > 0:
        conn = get_conn()
        cur = conn.cursor()
        new_daily_xp = daily_xp_current + xp_delta
        cur.execute(
            """
            UPDATE user_stats
            SET last_xp_at=?, daily_xp=?, daily_xp_date=?
            WHERE chat_id=? AND user_id=?
            """,
            (
                now_utc.isoformat(),
                new_daily_xp,
                today_kst_str,
                chat.id,
                user.id,
            ),
        )
        conn.commit()
        conn.close()

    # XP 로그 기록 (메시지 수/기간 통계용, xp_delta가 0이어도 기록)
    try:
        log_xp(chat.id, user.id, xp_delta, msg_len=len(no_space))
    except Exception:
        logger.exception("xp_log insert 실패")

    # 레벨업 알림
    old_xp = xp - xp_delta
    if xp_delta > 0 and level > calc_level(old_xp):
        await message.reply_text(
            f"🎉 {user.mention_html()} 님이 레벨업 했습니다!\n➡️ 현재 레벨: {level}",
            parse_mode="HTML",
        )


# -----------------------
# /start — 단일 도움말 명령어
# -----------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    base_text = (
        "안녕하세요! Terminal.Fi XP Bot입니다.\n"
        "커뮤니티에서 활동하면 XP를 얻고 레벨이 올라갑니다.\n\n"
        "📌 일반 명령어\n"
        "/stats - 내 스탯\n"
        "/ranking - 경험치 TOP 10\n"
        "/daily - 일일보상\n"
        "/mylink - 초대 링크 생성 (Terminal.Fi)\n"
        "/myinvites - 내 초대 인원\n"
        "/invites_ranking - 초대 랭킹\n"
        "/join - 진행 중인 추첨 참가\n"
    )

    text = base_text

    # 그룹에서는 관리자도 유저와 동일하게 일반 명령어만 표시
    if is_private_chat(chat):
        if is_admin(user.id):
            text += (
                "\n🔧 관리자 명령어 (DM에서 사용 권장)\n"
                "/chat_id <@handle 또는 user_id> - 해당 유저 ID 조회\n"
                "/list_admins - 관리자 목록\n"
                "/ref_user <@handle 또는 user_id> - 특정 유저 초대수\n"
                "/user_stats <@handle 또는 user_id> - 특정 유저 스탯\n"
                "/today - 오늘 기준 메인 그룹 요약(KST)\n"
                "/week - 최근 7일 메인 그룹 요약(KST)\n"
                "/range YYYY-MM-DD YYYY-MM-DD - 기간별 요약(KST)\n"
                "/add_xp_bonus <word> <xp> - 키워드 보너스 XP 등록\n"
                "/add_xp_block <word> - 키워드 차단 등록\n"
                "/del_xp_word <word> - 키워드 삭제\n"
                "/list_xp_words - 키워드 목록\n"
                "/set_cooldown <초> - XP 쿨다운 설정\n"
                "/set_daily_cap <XP> - 일일 XP 상한 설정\n"
                "/set_inv_xp <XP> - 초대 1명당 XP 설정\n"
                "/set_campaign <YYYY-MM-DD> <YYYY-MM-DD> - 캠페인 기간 설정\n"
                "/clear_campaign - 캠페인 기간 초기화\n"
                "/add_xp <@handle 또는 user_id> <XP> - 특정 유저에게 XP 수동 지급\n"
                "/lottery [분] [당첨자수] - 그룹에서 추첨 시작\n"
                "/lottery_end <인원수> - 추첨 종료 및 당첨자 추첨\n"
            )

        if is_owner(user.id):
            text += (
                "\n😎 OWNER 전용 명령어 (DM 전용 권장)\n"
                "/add_admin <user_id 또는 @handle> - 관리자 추가\n"
                "/del_admin <user_id 또는 @handle> - 관리자 제거\n"
                "/reset_xp total - 메인 그룹 XP 전체 초기화 (2단계 확인, 백업 후 진행)\n"
            )

    await message.reply_text(text)


# -----------------------
# 월별 / 캠페인 XP 계산 헬퍼
# -----------------------


def _get_month_range_kst(target_date: date):
    """해당 날짜가 속한 월의 KST 기준 시작/끝(UTC ISO)"""
    start_kst = datetime(target_date.year, target_date.month, 1)
    if target_date.month == 12:
        next_kst = datetime(target_date.year + 1, 1, 1)
    else:
        next_kst = datetime(target_date.year, target_date.month + 1, 1)

    start_utc = start_kst - timedelta(hours=9)
    end_utc = next_kst - timedelta(hours=9)
    return start_utc.isoformat(), end_utc.isoformat()


def _sum_xp_in_range(chat_id: int, user_id: int, start_iso: str, end_iso: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(SUM(xp_delta),0) AS s
        FROM xp_log
        WHERE chat_id=? AND user_id=? AND created_at >= ? AND created_at < ?
        """,
        (chat_id, user_id, start_iso, end_iso),
    )
    row = cur.fetchone()
    conn.close()
    return int(row["s"] or 0)


# -----------------------
# 공용 / 유저 명령어
# -----------------------


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /chat_id <@handle 또는 user_id>
    → 해당 유저의 user_id 를 찾아서 보여줌
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용할 수 있습니다.")
        return

    if not args:
        await msg.reply_text("사용법: /chat_id <@handle 또는 user_id>")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        await msg.reply_text("해당 유저를 찾을 수 없습니다.")
        return

    await msg.reply_text(f"해당 유저의 ID는 `{target_id}` 입니다.", parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /stats:
    - 총 XP, 레벨, 메시지 수
    - 이번 달 XP, 지난 달 XP
    - 캠페인 XP (설정된 경우)
    초대 인원(invites_count)는 표기하지 않음 (/myinvites에서만 확인)
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    # 통계는 MAIN_CHAT_ID 기준으로 보는게 직관적이라, DM에서도 MAIN_CHAT_ID 기준 사용
    chat_id = MAIN_CHAT_ID or chat.id

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT xp, level, messages_count, last_daily "
        "FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat_id, user.id),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await msg.reply_text("아직 경험치 기록이 없습니다.")
        return

    xp = row["xp"]
    level = row["level"]
    msgs = row["messages_count"]
    next_xp = xp_for_next_level(level)

    now_kst = datetime.utcnow() + timedelta(hours=9)
    today = now_kst.date()

    # 이번 달 / 지난 달 XP (xp_log 기반)
    # 이번 달
    cur_month_start_iso, cur_month_end_iso = _get_month_range_kst(today)
    cur_month_xp = _sum_xp_in_range(chat_id, user.id, cur_month_start_iso, cur_month_end_iso)

    # 지난 달
    if today.month == 1:
        prev_date = date(today.year - 1, 12, 1)
    else:
        prev_date = date(today.year, today.month - 1, 1)
    prev_month_start_iso, prev_month_end_iso = _get_month_range_kst(prev_date)
    prev_month_xp = _sum_xp_in_range(chat_id, user.id, prev_month_start_iso, prev_month_end_iso)

    # 캠페인 XP
    settings = get_settings()
    campaign_xp = None
    if settings["campaign_start"] and settings["campaign_end"]:
        try:
            cs = date.fromisoformat(settings["campaign_start"])
            ce = date.fromisoformat(settings["campaign_end"])
            cs_kst = datetime.combine(cs, time(0, 0))
            ce_kst = datetime.combine(ce + timedelta(days=1), time(0, 0))
            cs_utc = cs_kst - timedelta(hours=9)
            ce_utc = ce_kst - timedelta(hours=9)
            campaign_xp = _sum_xp_in_range(
                chat_id, user.id, cs_utc.isoformat(), ce_utc.isoformat()
            )
        except Exception:
            campaign_xp = None

    text = (
        f"📊 {user.full_name} 님의 통계\n\n"
        f"🎯 레벨: {level}\n"
        f"⭐ 총 경험치(Total XP): {xp}\n"
        f"📈 다음 레벨까지: {max(0, next_xp - xp)} XP\n"
        f"💬 메시지 수: {msgs}\n\n"
        f"📆 이번 달 XP: {cur_month_xp}\n"
        f"📆 지난 달 XP: {prev_month_xp}\n"
    )

    if campaign_xp is not None:
        text += f"🏁 현재 설정된 캠페인 기간 XP: {campaign_xp}\n"

    await msg.reply_text(text)


async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, first_name, last_name, xp, level
        FROM user_stats
        WHERE chat_id=?
        ORDER BY xp DESC
        LIMIT 10
        """,
        (chat.id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("아직 데이터가 없습니다.")
        return

    lines = ["🏆 경험치 TOP 10\n"]
    medals = ["🥇", "🥈", "🥉"]

    for i, row in enumerate(rows, start=1):
        username = row["username"]
        if username:
            name = f"@{username}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or "이름없음"
        xp = row["xp"]
        level = row["level"]
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{prefix} {name} - Lv.{level} ({xp} XP)")

    await update.message.reply_text("\n".join(lines))


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /daily:
    - 24시간이 아니라, "KST 자정 기준 1일 1회"로 변경
    - last_daily를 YYYY-MM-DD(KST) 문자열로 저장
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    chat_id = MAIN_CHAT_ID or chat.id

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT xp, level, messages_count, last_daily "
        "FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat_id, user.id),
    )
    row = cur.fetchone()

    now_kst = datetime.utcnow() + timedelta(hours=9)
    today_str = now_kst.date().isoformat()
    bonus = 50

    if not row:
        xp = bonus
        level = calc_level(xp)
        cur.execute(
            """
            INSERT INTO user_stats
            (chat_id,user_id,username,first_name,last_name,xp,level,messages_count,last_daily)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                chat_id,
                user.id,
                user.username,
                user.first_name or "",
                user.last_name or "",
                xp,
                level,
                0,
                today_str,
            ),
        )
        conn.commit()
        conn.close()

        # 로그 기록
        log_xp(chat_id, user.id, bonus, msg_len=0)

        await msg.reply_text(f"🎁 일일 보상으로 {bonus} XP를 받았습니다!")
        return

    last = row["last_daily"]
    already_today = False
    if last:
        # 예전 데이터가 ISO일 수도 있고, 이미 YYYY-MM-DD일 수도 있음
        if len(last) == 10:
            # YYYY-MM-DD
            already_today = (last == today_str)
        else:
            try:
                last_dt = datetime.fromisoformat(last) + timedelta(hours=9)
                already_today = (last_dt.date().isoformat() == today_str)
            except Exception:
                already_today = False

    if already_today:
        await msg.reply_text("⏰ 이미 오늘 일일 보상을 받았습니다.\n내일 00시(KST) 이후에 다시 시도해 주세요.")
        conn.close()
        return

    xp = row["xp"] + bonus
    level = calc_level(xp)
    cur.execute(
        "UPDATE user_stats SET xp=?,level=?,last_daily=? WHERE chat_id=? AND user_id=?",
        (xp, level, today_str, chat_id, user.id),
    )
    conn.commit()
    conn.close()

    # 로그 기록
    log_xp(chat_id, user.id, bonus, msg_len=0)

    await msg.reply_text(f"🎁 일일 보상으로 {bonus} XP를 받았습니다!\n현재 XP: {xp}, 레벨: {level}")


# -----------------------
# /mylink & 초대 관련
# -----------------------


async def cmd_mylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("이 명령어는 그룹에서만 사용할 수 있습니다.")
        return

    if not is_main_chat(chat.id):
        await update.message.reply_text("메인 그룹에서만 사용할 수 있는 명령어입니다.")
        return

    conn = get_conn()
    cur = conn.cursor()

    # 이미 발급한 초대링크가 있는지 확인
    cur.execute(
        "SELECT invite_link FROM invite_links WHERE chat_id=? AND inviter_id=? LIMIT 1",
        (chat.id, user.id),
    )
    row = cur.fetchone()

    if row:
        await update.message.reply_text(
            "이미 생성된 나만의 초대 링크가 있습니다.\n"
            "이 링크를 계속 사용해 주세요.\n\n"
            f"{row['invite_link']}"
        )
        conn.close()
        return

    # 새 초대 링크 생성
    try:
        invite: ChatInviteLink = await bot.create_chat_invite_link(
            chat_id=chat.id,
            name=f"referral:{user.id}",
            creates_join_request=False,
        )
    except Exception:
        conn.close()
        await update.message.reply_text("초대 링크를 생성할 수 없습니다. (봇 권한을 확인해 주세요)")
        return

    cur.execute(
        """
        INSERT INTO invite_links (invite_link,chat_id,inviter_id,created_at)
        VALUES (?,?,?,?)
        """,
        (invite.invite_link, chat.id, user.id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "👥 나만의 초대 링크를 생성했습니다!\n"
        "이 링크로 입장한 인원은 모두 내 초대로 집계됩니다.\n\n"
        f"{invite.invite_link}"
    )


async def cmd_myinvites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /myinvites:
    - 그룹/DM 모두 사용 가능
    - MAIN_CHAT_ID 기준으로 초대 인원 집계
    """
    user = update.effective_user
    msg = update.message
    count = get_invite_count_for_user(user.id)

    await msg.reply_text(f"👥 현재까지 내 초대 링크로 들어온 인원은 총 {count}명입니다.")


async def cmd_invites_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /invites_ranking:
    - 초대 랭킹 TOP 10 (메인 그룹 기준)
    """
    chat = update.effective_chat

    if not is_main_chat(chat.id):
        await update.message.reply_text("초대 랭킹은 메인 그룹에서만 확인할 수 있습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username,first_name,last_name,invites_count
        FROM user_stats
        WHERE chat_id=? AND invites_count>0
        ORDER BY invites_count DESC
        LIMIT 10
        """,
        (chat.id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("아직 초대 기록이 없습니다.")
        return

    lines = ["👥 초대 랭킹 TOP 10\n"]
    for i, row in enumerate(rows, start=1):
        if row["username"]:
            name = f"@{row['username']}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or "이름없음"
        lines.append(f"{i}. {name} - {row['invites_count']}명")

    await update.message.reply_text("\n".join(lines))


# -----------------------
# 초대 tracking (멤버 입장 감지) + 초대 XP
# -----------------------


async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or not is_main_chat(chat.id):
        return

    cm: ChatMemberUpdated = update.chat_member
    new = cm.new_chat_member
    old = cm.old_chat_member

    if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
        user = new.user
        invite_link = cm.invite_link
        if not invite_link:
            return

        link_url = invite_link.invite_link

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            "SELECT inviter_id,joined_count FROM invite_links WHERE invite_link=? AND chat_id=?",
            (link_url, chat.id),
        )
        row = cur.fetchone()

        if not row:
            conn.close()
            return

        inviter = row["inviter_id"]
        new_count = row["joined_count"] + 1

        cur.execute(
            "UPDATE invite_links SET joined_count=? WHERE invite_link=? AND chat_id=?",
            (new_count, link_url, chat.id),
        )

        cur.execute(
            "SELECT invites_count FROM user_stats WHERE chat_id=? AND user_id=?",
            (chat.id, inviter),
        )
        inv_row = cur.fetchone()

        if not inv_row:
            cur.execute(
                """
                INSERT INTO user_stats
                (chat_id,user_id,xp,level,messages_count,last_daily,invites_count)
                VALUES (?,?,?,?,?,?,?)
                """,
                (chat.id, inviter, 0, 1, 0, None, 1),
            )
        else:
            cnt = inv_row["invites_count"] + 1
            cur.execute(
                "UPDATE user_stats SET invites_count=? WHERE chat_id=? AND user_id=?",
                (cnt, chat.id, inviter),
            )

        conn.commit()
        conn.close()

        # 초대 XP 부여
        settings = get_settings()
        invite_xp = settings["invite_xp"]
        if invite_xp > 0:
            try:
                inviter_member = await context.bot.get_chat_member(chat.id, inviter)
                inviter_user = inviter_member.user
                # XP 부여
                xp, level, _ = add_xp(chat.id, inviter_user, invite_xp)
                # 로그 기록
                log_xp(chat.id, inviter_user.id, invite_xp, msg_len=0)
            except Exception:
                logger.exception("초대 XP 부여 실패")

        await context.bot.send_message(
            chat_id=chat.id,
            text=f"👋 {user.full_name} 님이 초대 링크를 통해 입장했습니다! (초대자: {inviter})",
        )


# -----------------------
# 관리자 / OWNER 관련 유틸 & 명령어
# -----------------------


async def _resolve_target_user_id(arg: str):
    """@username 또는 숫자 user_id 문자열을 받아 user_id 반환 (없으면 None)"""
    q = arg.strip()
    if q.startswith("@"):
        q = q[1:]

    if q.isdigit():
        return int(q)

    # username 으로 user_stats 에서 찾기 (MAIN_CHAT_ID 우선)
    conn = get_conn()
    cur = conn.cursor()
    if MAIN_CHAT_ID != 0:
        cur.execute(
            "SELECT user_id FROM user_stats WHERE chat_id=? AND username=? LIMIT 1",
            (MAIN_CHAT_ID, q),
        )
    else:
        cur.execute(
            "SELECT user_id FROM user_stats WHERE username=? LIMIT 1",
            (q,),
        )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return int(row["user_id"])


async def cmd_listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return

    lines = ["현재 관리자 목록:"]
    if OWNER_ID:
        lines.append(f"- OWNER: {OWNER_ID}")
    for aid in sorted(ADMIN_USER_IDS):
        lines.append(f"- {aid}")
    await msg.reply_text("\n".join(lines))


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_owner(user.id):
        await msg.reply_text("OWNER만 사용할 수 있습니다.")
        return

    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용할 수 있습니다.")
        return

    if not args:
        await msg.reply_text("사용법: /add_admin <user_id 또는 @username>")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        if args[0].strip().isdigit():
            target_id = int(args[0].strip())
        else:
            await msg.reply_text("해당 유저를 찾을 수 없습니다.")
            return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)",
        (target_id,),
    )
    conn.commit()
    conn.close()

    reload_admins()

    await msg.reply_text(f"✅ 관리자에 user_id {target_id} 를 추가했습니다.")


async def cmd_deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_owner(user.id):
        await msg.reply_text("OWNER만 사용할 수 있습니다.")
        return

    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용할 수 있습니다.")
        return

    if not args:
        await msg.reply_text("사용법: /del_admin <user_id 또는 @username>")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        if args[0].strip().isdigit():
            target_id = int(args[0].strip())
        else:
            await msg.reply_text("해당 유저를 찾을 수 없습니다.")
            return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_users WHERE admin_id=?", (target_id,))
    conn.commit()
    conn.close()

    reload_admins()

    await msg.reply_text(f"✅ 관리자에서 user_id {target_id} 를 제거했습니다.")


async def cmd_refuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not args:
        await msg.reply_text("사용법: /ref_user @username 또는 /ref_user user_id")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        await msg.reply_text("해당 유저를 user_stats 에서 찾을 수 없습니다.")
        return

    count = get_invite_count_for_user(target_id)
    await msg.reply_text(f"해당 유저 초대 인원: {count}명")


async def cmd_userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """관리자용: /user_stats <@handle 또는 user_id> → 유저 스탯 조회 (총/월/캠페인)"""
    admin = update.effective_user
    msg = update.message
    args = context.args

    if not is_admin(admin.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return

    if not args:
        await msg.reply_text("사용법: /user_stats @username 또는 /user_stats user_id")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        await msg.reply_text("해당 유저를 찾을 수 없습니다.")
        return

    chat_id = MAIN_CHAT_ID or msg.chat_id

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, first_name, last_name,
               xp, level, messages_count, invites_count, last_daily
        FROM user_stats
        WHERE chat_id=? AND user_id=?
        """,
        (chat_id, target_id),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await msg.reply_text("해당 유저의 스탯 기록이 없습니다.")
        return

    if row["username"]:
        name = f"@{row['username']}"
    else:
        fn = row["first_name"] or ""
        ln = row["last_name"] or ""
        name = (fn + " " + ln).strip() or f"user_id {target_id}"

    xp = row["xp"]
    level = row["level"]
    msgs = row["messages_count"]
    invites_db = row["invites_count"]
    next_xp = xp_for_next_level(level)

    invites_links = get_invite_count_for_user(target_id)

    last_daily = row["last_daily"]
    if last_daily:
        if len(last_daily) == 10:
            last_daily_str = last_daily
        else:
            try:
                last_daily_str = datetime.fromisoformat(last_daily).strftime("%Y-%m-%d")
            except Exception:
                last_daily_str = "기록 없음"
    else:
        last_daily_str = "기록 없음"

    now_kst = datetime.utcnow() + timedelta(hours=9)
    today = now_kst.date()

    # 이번 달 / 지난 달 XP
    cur_month_start_iso, cur_month_end_iso = _get_month_range_kst(today)
    cur_month_xp = _sum_xp_in_range(chat_id, target_id, cur_month_start_iso, cur_month_end_iso)

    if today.month == 1:
        prev_date = date(today.year - 1, 12, 1)
    else:
        prev_date = date(today.year, today.month - 1, 1)
    prev_month_start_iso, prev_month_end_iso = _get_month_range_kst(prev_date)
    prev_month_xp = _sum_xp_in_range(chat_id, target_id, prev_month_start_iso, prev_month_end_iso)

    # 캠페인 XP
    settings = get_settings()
    campaign_xp = None
    if settings["campaign_start"] and settings["campaign_end"]:
        try:
            cs = date.fromisoformat(settings["campaign_start"])
            ce = date.fromisoformat(settings["campaign_end"])
            cs_kst = datetime.combine(cs, time(0, 0))
            ce_kst = datetime.combine(ce + timedelta(days=1), time(0, 0))
            cs_utc = cs_kst - timedelta(hours=9)
            ce_utc = ce_kst - timedelta(hours=9)
            campaign_xp = _sum_xp_in_range(
                chat_id, target_id, cs_utc.isoformat(), ce_utc.isoformat()
            )
        except Exception:
            campaign_xp = None

    text = (
        f"📊 {name} 님의 스탯\n\n"
        f"🎯 레벨: {level}\n"
        f"⭐ 총 경험치(Total XP): {xp}\n"
        f"📈 다음 레벨까지: {max(0, next_xp - xp)} XP\n"
        f"💬 메시지 수: {msgs}\n"
        f"👥 초대 인원(user_stats.invites_count): {invites_db}명\n"
        f"👥 초대 인원(invite_links 합산): {invites_links}명\n"
        f"🕒 마지막 일일보상 일자(KST 기준): {last_daily_str}\n\n"
        f"📆 이번 달 XP: {cur_month_xp}\n"
        f"📆 지난 달 XP: {prev_month_xp}\n"
    )

    if campaign_xp is not None:
        text += f"🏁 현재 설정된 캠페인 기간 XP: {campaign_xp}\n"

    await msg.reply_text(text)


# -----------------------
# /reset_xp total (백업 + 2단계 확인)
# -----------------------


def backup_db_to_zip() -> str:
    """
    xp_bot.db 를 zip으로 압축해서 파일 경로 반환.
    같은 폴더에 timestamp 붙여서 생성.
    """
    base_dir = os.path.dirname(DB_PATH) or "."
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_name = f"xp_bot_backup_{ts}.zip"
    zip_path = os.path.join(base_dir, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(DB_PATH):
            zf.write(DB_PATH, arcname=os.path.basename(DB_PATH))

    return zip_path


async def cmd_resetxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /reset_xp total
    OWNER 전용.
    - 1단계: '/reset_xp total' → 전체 DB 백업 zip 생성 후, 2단계 안내
    - 2단계: '/reset_xp total 동의합니다.' → 실제 리셋 수행
    """
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_owner(user.id):
        await msg.reply_text("OWNER만 사용할 수 있습니다.")
        return

    if MAIN_CHAT_ID == 0:
        await msg.reply_text("MAIN_CHAT_ID가 설정되어 있지 않아 XP를 리셋할 수 없습니다.")
        return

    if not args:
        await msg.reply_text(
            "사용법:\n"
            "/reset_xp total          → 리셋 전 전체 백업 생성 + 2단계 안내\n"
            "/reset_xp total 동의합니다. → 실제 XP 전체 초기화 실행"
        )
        return

    mode = args[0]

    if mode != "total":
        await msg.reply_text("지원되지 않는 모드입니다. 현재는 '/reset_xp total'만 지원합니다.")
        return

    confirmation_text = "동의합니다."

    # 2단계 확인: /reset_xp total 동의합니다.
    if len(args) >= 2 and " ".join(args[1:]) == confirmation_text:
        # 실제 리셋 수행
        conn = get_conn()
        cur = conn.cursor()

        # 리셋 전 스냅샷 생성
        cur.execute(
            """
            SELECT username, first_name, last_name, xp, level
            FROM user_stats
            WHERE chat_id=?
            ORDER BY xp DESC
            LIMIT 10
            """,
            (MAIN_CHAT_ID,),
        )
        rows = cur.fetchall()

        cur.execute(
            "SELECT COUNT(*) AS c FROM user_stats WHERE chat_id=?",
            (MAIN_CHAT_ID,),
        )
        total_users = cur.fetchone()["c"]

        # 실제 리셋 수행
        cur.execute(
            """
            UPDATE user_stats
            SET xp=0, level=1, messages_count=0,
                last_daily=NULL, invites_count=0,
                last_xp_at=NULL, daily_xp=0, daily_xp_date=NULL
            WHERE chat_id=?
            """,
            (MAIN_CHAT_ID,),
        )
        affected = cur.rowcount
        conn.commit()
        conn.close()

        # 스냅샷 텍스트 구성
        if not rows:
            snapshot_body = "초기화 직전 기록된 데이터가 없습니다."
        else:
            lines = [f"XP 초기화 직전 스냅샷 (MAIN_CHAT_ID={MAIN_CHAT_ID})\n"]
            for i, row in enumerate(rows, start=1):
                if row["username"]:
                    name = f"@{row['username']}"
                else:
                    fn = row["first_name"] or ""
                    ln = row["last_name"] or ""
                    name = (fn + " " + ln).strip() or "이름없음"
                lines.append(f"{i}. {name} - Lv.{row['level']} ({row['xp']} XP)")
            lines.append(f"\n총 기록된 유저 수: {total_users}명")
            snapshot_body = "\n".join(lines)

        # OWNER DM 으로 스냅샷 전송
        try:
            await msg.bot.send_message(chat_id=user.id, text=snapshot_body)
        except Exception:
            logger.exception("resetxp 스냅샷 DM 전송 실패")

        await msg.reply_text(
            f"✅ MAIN_CHAT_ID={MAIN_CHAT_ID} 의 XP/레벨/메시지/초대 기록을 초기화했습니다.\n"
            f"(영향 받은 유저 수: {affected}명)\n"
            "초기화 직전 스냅샷은 OWNER DM으로 전송했습니다.",
        )
        return

    # 여기까지 오면 '/reset_xp total' (백업 + 2단계 안내)
    # 1단계: 전체 DB 백업 zip 생성 후 OWNER에게 전송
    try:
        zip_path = backup_db_to_zip()
        await msg.bot.send_document(
            chat_id=user.id,
            document=open(zip_path, "rb"),
            caption="XP 전체 초기화 전에 생성된 전체 DB 백업입니다.",
        )
    except Exception:
        logger.exception("resetxp 전체 백업 전송 실패")

    await msg.reply_text(
        "⚠️ 이제 XP 전체 초기화를 진행할 수 있습니다.\n\n"
        "정말로 메인 그룹의 XP/레벨/메시지/초대 기록을 모두 초기화하시겠습니까?\n"
        "초기화를 진행하려면 아래와 같이 다시 입력해 주세요.\n\n"
        f"`/reset_xp total {confirmation_text}`",
        parse_mode="Markdown",
    )


# -----------------------
# XP 키워드 관리 (DM, 관리자 전용)
# -----------------------


async def cmd_addxpbonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용할 수 있습니다.")
        return

    if len(args) < 2:
        await msg.reply_text("사용법: /add_xp_bonus <word> <xp>")
        return

    word = args[0].strip()
    try:
        delta = int(args[1])
    except ValueError:
        await msg.reply_text("XP 값은 정수여야 합니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO xp_keywords (word, mode, delta)
        VALUES (?, 'bonus', ?)
        ON CONFLICT(word) DO UPDATE SET mode='bonus', delta=excluded.delta
        """,
        (word, delta),
    )
    conn.commit()
    conn.close()

    await msg.reply_text(f"✅ '{word}' 를 bonus 키워드로 등록했습니다. (XP +{delta})")


async def cmd_addxpblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용할 수 있습니다.")
        return

    if not args:
        await msg.reply_text("사용법: /add_xp_block <word>")
        return

    word = args[0].strip()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO xp_keywords (word, mode, delta)
        VALUES (?, 'block', 0)
        ON CONFLICT(word) DO UPDATE SET mode='block', delta=0
        """,
        (word,),
    )
    conn.commit()
    conn.close()

    await msg.reply_text(f"✅ '{word}' 를 block 키워드로 등록했습니다. (해당 단어 포함 메시지는 XP 0 처리)")


async def cmd_delxpword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용할 수 있습니다.")
        return

    if not args:
        await msg.reply_text("사용법: /del_xp_word <word>")
        return

    word = args[0].strip()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM xp_keywords WHERE word=?", (word,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await msg.reply_text(f"✅ '{word}' 키워드를 삭제했습니다.")
    else:
        await msg.reply_text(f"'{word}' 키워드가 등록되어 있지 않습니다.")


async def cmd_listxpwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용할 수 있습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT word, mode, delta FROM xp_keywords ORDER BY mode, word")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await msg.reply_text("등록된 XP 키워드가 없습니다.")
        return

    bonus_lines = []
    block_lines = []
    for row in rows:
        if row["mode"] == "bonus":
            bonus_lines.append(f"- {row['word']} : +{row['delta']} XP")
        else:
            block_lines.append(f"- {row['word']} : XP 0 처리")

    lines = []
    if bonus_lines:
        lines.append("✨ Bonus 키워드:")
        lines.extend(bonus_lines)
    if block_lines:
        if lines:
            lines.append("")
        lines.append("⛔ Block 키워드:")
        lines.extend(block_lines)

    await msg.reply_text("\n".join(lines))


# -----------------------
# 안티 스팸/초대/캠페인 설정 명령어
# -----------------------


async def cmd_setcooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 DM에서 사용하는 것을 권장합니다.")
        # 계속 진행은 허용

    if not args:
        await msg.reply_text("사용법: /set_cooldown <초>  (예: /set_cooldown 7)")
        return

    try:
        sec = int(args[0])
    except ValueError:
        await msg.reply_text("초 값은 정수여야 합니다.")
        return

    if sec < 0:
        sec = 0

    update_settings(cooldown_seconds=sec)
    await msg.reply_text(f"✅ XP 쿨다운이 {sec}초로 설정되었습니다.")


async def cmd_setdailycap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 DM에서 사용하는 것을 권장합니다.")

    if not args:
        await msg.reply_text("사용법: /set_daily_cap <XP>  (예: /set_daily_cap 500)")
        return

    try:
        cap = int(args[0])
    except ValueError:
        await msg.reply_text("XP 값은 정수여야 합니다.")
        return

    if cap < 0:
        cap = 0

    update_settings(daily_xp_cap=cap)
    await msg.reply_text(f"✅ 일일 XP 상한이 {cap} XP로 설정되었습니다.")


async def cmd_setinvxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 DM에서 사용하는 것을 권장합니다.")

    if not args:
        await msg.reply_text("사용법: /set_inv_xp <XP>  (예: /set_inv_xp 100)")
        return

    try:
        val = int(args[0])
    except ValueError:
        await msg.reply_text("XP 값은 정수여야 합니다.")
        return

    if val < 0:
        val = 0

    update_settings(invite_xp=val)
    await msg.reply_text(f"✅ 초대 1명당 XP가 {val} XP로 설정되었습니다.")


async def cmd_setcampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 DM에서 사용하는 것을 권장합니다.")

    if len(args) != 2:
        await msg.reply_text("사용법: /set_campaign YYYY-MM-DD YYYY-MM-DD")
        return

    try:
        start_date = date.fromisoformat(args[0])
        end_date = date.fromisoformat(args[1])
    except ValueError:
        await msg.reply_text("날짜 형식이 잘못되었습니다. 예: /set_campaign 2025-11-20 2025-11-27")
        return

    if end_date < start_date:
        await msg.reply_text("끝 날짜는 시작 날짜보다 같거나 이후여야 합니다.")
        return

    update_settings(campaign_start=start_date.isoformat(), campaign_end=end_date.isoformat())
    await msg.reply_text(
        f"✅ 캠페인 기간이 {start_date.isoformat()} ~ {end_date.isoformat()} (KST 기준)으로 설정되었습니다."
    )


async def cmd_clearcampaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 DM에서 사용하는 것을 권장합니다.")

    update_settings(campaign_start=None, campaign_end=None)
    await msg.reply_text("✅ 캠페인 기간 설정이 초기화되었습니다.")


async def cmd_add_xp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_xp <@handle 또는 user_id> <XP>
    관리자용 수동 XP 지급
    """
    admin = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(admin.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return

    if not args or len(args) < 2:
        await msg.reply_text("사용법: /add_xp <@handle 또는 user_id> <XP>")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        await msg.reply_text("해당 유저를 찾을 수 없습니다.")
        return

    try:
        delta = int(args[1])
    except ValueError:
        await msg.reply_text("XP 값은 정수여야 합니다.")
        return

    if delta <= 0:
        await msg.reply_text("XP 값은 1 이상이어야 합니다.")
        return

    chat_id = MAIN_CHAT_ID or chat.id

    # 해당 유저의 이름 정보는 user_stats에서 가져오거나, 없으면 placeholder
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, first_name, last_name
        FROM user_stats
        WHERE chat_id=? AND user_id=?
        """,
        (chat_id, target_id),
    )
    row = cur.fetchone()
    conn.close()

    class SimpleUser:
        def __init__(self, uid, username, first_name, last_name):
            self.id = uid
            self.username = username
            self.first_name = first_name
            self.last_name = last_name

    if row:
        u = SimpleUser(
            target_id,
            row["username"],
            row["first_name"] or "",
            row["last_name"] or "",
        )
    else:
        # 기록이 전혀 없다면 이름 정보 없이 추가
        u = SimpleUser(target_id, None, "", "")

    xp, level, _ = add_xp(chat_id, u, delta)
    log_xp(chat_id, target_id, delta, msg_len=0)

    await msg.reply_text(
        f"✅ user_id {target_id} 에게 {delta} XP를 지급했습니다.\n"
        f"현재 총 XP: {xp}, 레벨: {level}"
    )


# -----------------------
# 기간별 요약 (/today, /week, /range)
# -----------------------


def _build_range_summary(start_date_kst: date, end_date_kst: date) -> str:
    """
    KST 기준 start~end 날짜(둘 다 포함)에 대한 메인 그룹 요약 텍스트 생성
    """
    if MAIN_CHAT_ID == 0:
        return "MAIN_CHAT_ID가 설정되어 있지 않아 요약을 생성할 수 없습니다."

    # KST 날짜범위를 UTC ISO 문자열로 변환
    start_kst = datetime.combine(start_date_kst, time(0, 0))
    end_kst = datetime.combine(end_date_kst + timedelta(days=1), time(0, 0))

    start_utc = start_kst - timedelta(hours=9)
    end_utc = end_kst - timedelta(hours=9)

    start_iso = start_utc.isoformat()
    end_iso = end_utc.isoformat()

    conn = get_conn()
    cur = conn.cursor()

    # 총 메시지 수 / 활동 유저 수
    cur.execute(
        """
        SELECT COUNT(*) AS msg_count,
               COUNT(DISTINCT user_id) AS user_count
        FROM xp_log
        WHERE chat_id=? AND created_at >= ? AND created_at < ?
        """,
        (MAIN_CHAT_ID, start_iso, end_iso),
    )
    base_row = cur.fetchone()
    msg_count = base_row["msg_count"] or 0
    user_count = base_row["user_count"] or 0

    # 신규 유저 수 (이 기간에 처음으로 등장한 유저)
    cur.execute(
        """
        SELECT COUNT(*) AS new_users
        FROM (
          SELECT user_id, MIN(created_at) AS first_at
          FROM xp_log
          WHERE chat_id=?
          GROUP BY user_id
          HAVING first_at >= ? AND first_at < ?
        ) t
        """,
        (MAIN_CHAT_ID, start_iso, end_iso),
    )
    new_row = cur.fetchone()
    new_users = new_row["new_users"] or 0

    # XP 기준 TOP 10
    cur.execute(
        """
        SELECT l.user_id,
               u.username, u.first_name, u.last_name,
               SUM(l.xp_delta) AS total_xp,
               COUNT(*) AS msg_cnt
        FROM xp_log l
        LEFT JOIN user_stats u
          ON u.chat_id = l.chat_id AND u.user_id = l.user_id
        WHERE l.chat_id=? AND l.created_at >= ? AND l.created_at < ?
        GROUP BY l.user_id, u.username, u.first_name, u.last_name
        ORDER BY total_xp DESC
        LIMIT 10
        """,
        (MAIN_CHAT_ID, start_iso, end_iso),
    )
    rows = cur.fetchall()

    conn.close()

    header = (
        f"📊 메인 그룹 활동 요약\n"
        f"기간 (KST 기준): {start_date_kst.isoformat()} ~ {end_date_kst.isoformat()}\n\n"
        f"- 총 메시지 수: {msg_count}개\n"
        f"- 활동 유저 수: {user_count}명\n"
        f"- 신규 유저 수: {new_users}명\n"
    )

    if not rows:
        return header + "\n해당 기간에는 활동 기록이 없습니다."

    lines = [header, "\n🏆 XP 기준 TOP 10\n"]
    for i, row in enumerate(rows, start=1):
        if row["username"]:
            name = f"@{row['username']}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or f"user_id {row['user_id']}"

        total_xp = row["total_xp"] or 0
        msg_cnt = row["msg_cnt"] or 0
        lines.append(f"{i}. {name} - {total_xp} XP / {msg_cnt} 메시지")

    return "\n".join(lines)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용할 수 있습니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용해 주세요.")
        return

    now_kst = datetime.utcnow() + timedelta(hours=9)
    today = now_kst.date()

    text = _build_range_summary(today, today)
    await msg.reply_text(text)


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용할 수 있습니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용해 주세요.")
        return

    now_kst = datetime.utcnow() + timedelta(hours=9)
    end_date = now_kst.date()
    start_date = end_date - timedelta(days=6)  # 최근 7일 (오늘 포함)

    text = _build_range_summary(start_date, end_date)
    await msg.reply_text(text)


async def cmd_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용할 수 없습니다.")
        return
    if not is_private_chat(chat):
        await msg.reply_text("이 명령어는 봇과의 1:1 대화(디엠)에서만 사용해 주세요.")
        return

    if len(args) != 2:
        await msg.reply_text("사용법: /range YYYY-MM-DD YYYY-MM-DD")
        return

    try:
        start_date = date.fromisoformat(args[0])
        end_date = date.fromisoformat(args[1])
    except ValueError:
        await msg.reply_text("날짜 형식이 잘못되었습니다. 예: /range 2025-11-01 2025-11-07")
        return

    if end_date < start_date:
        await msg.reply_text("끝 날짜는 시작 날짜보다 같거나 이후여야 합니다.")
        return

    text = _build_range_summary(start_date, end_date)
    await msg.reply_text(text)


# -----------------------
# Daily summary (23:59 KST)
# -----------------------


async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if MAIN_CHAT_ID == 0:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username,first_name,last_name,xp,level
        FROM user_stats
        WHERE chat_id=?
        ORDER BY xp DESC
        LIMIT 10
        """,
        (MAIN_CHAT_ID,),
    )
    rows = cur.fetchall()

    cur.execute(
        "SELECT COUNT(*) AS c FROM user_stats WHERE chat_id=?",
        (MAIN_CHAT_ID,),
    )
    total_users = cur.fetchone()["c"]
    conn.close()

    now_kst = datetime.utcnow() + timedelta(hours=9)

    if not rows:
        body = "오늘 기록된 활동/XP 데이터가 없습니다."
    else:
        lines = ["오늘 기준 메인 그룹 XP 상위 10명:\n"]
        for i, row in enumerate(rows, start=1):
            if row["username"]:
                name = f"@{row['username']}"
            else:
                fn = row["first_name"] or ""
                ln = row["last_name"] or ""
                name = (fn + " " + ln).strip() or "이름없음"
            lines.append(f"{i}. {name} - Lv.{row['level']} ({row['xp']} XP)")
        lines.append(f"\n총 기록된 유저 수: {total_users}명")
        body = "\n".join(lines)

    text = (
        f"📊 Daily XP 요약 (KST 기준)\n"
        f"{now_kst.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"{body}"
    )

    for uid in all_admin_targets():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            logger.exception("daily summary DM 실패 (user_id=%s)", uid)


# -----------------------
# 자동 백업 (매일 23:59 KST)
# -----------------------


async def send_daily_backup(context: ContextTypes.DEFAULT_TYPE):
    """
    매일 23:59 KST 기준 xp_bot.db 를 zip으로 압축하여
    OWNER + 관리자에게 DM으로 전송
    """
    try:
        zip_path = backup_db_to_zip()
    except Exception:
        logger.exception("자동 백업 zip 생성 실패")
        return

    for uid in all_admin_targets():
        try:
            await context.bot.send_document(
                chat_id=uid,
                document=open(zip_path, "rb"),
                caption="📦 Daily 자동 백업 파일입니다.",
            )
        except Exception:
            logger.exception("daily backup DM 실패 (user_id=%s)", uid)


# -----------------------
# 로터리(추첨) 기능
# -----------------------


async def cmd_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /lottery
    /lottery <분>
    /lottery <분> <당첨자수>

    - 그룹 관리자 + DB 관리자 + OWNER만 사용
    - /lottery           : 시간 제한 없이 추첨 시작 → /lottery_end <인원수> 로 종료
    - /lottery 60        : 60분 동안만 /join 받기, 이후 자동으로 종료(당첨자 뽑지는 않음)
    - /lottery 60 3      : 60분 후 자동으로 3명 추첨 및 종료
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    # DB 관리자 목록 최신화
    reload_admins()

    # ✅ 그룹 관리자 or DB 관리자 or OWNER 판정
    if not await is_chat_admin_or_bot_admin(update, context):
        await msg.reply_text("관리자만 사용할 수 있습니다.")
        return

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("이 명령어는 그룹에서만 사용할 수 있습니다.")
        return

    state = LOTTERY_STATE.get(chat.id)
    if state and state.get("active"):
        await msg.reply_text("이미 진행 중인 추첨이 있습니다. 먼저 기존 추첨을 종료해 주세요.")
        return

    duration = None
    winners = None

    if len(args) >= 1:
        try:
            duration = int(args[0])
        except ValueError:
            await msg.reply_text("시간(분)은 정수로 입력해 주세요. 예: /lottery 60 3")
            return
        if duration <= 0:
            duration = None

    if len(args) >= 2:
        try:
            winners = int(args[1])
        except ValueError:
            await msg.reply_text("당첨자 수는 정수로 입력해 주세요. 예: /lottery 60 3")
            return
        if winners <= 0:
            await msg.reply_text("당첨자 수는 1명 이상이어야 합니다.")
            return

    job = None
    if duration is not None:
        job = context.job_queue.run_once(
            auto_end_lottery,
            when=duration * 60,
            data={"chat_id": chat.id, "winners": winners},
            name=f"lottery_{chat.id}",
        )

    LOTTERY_STATE[chat.id] = {
        "active": True,
        "participants": set(),
        "duration": duration,
        "winners": winners,
        "job": job,
    }

    if duration is None and winners is None:
        text = (
            "🎉 추첨을 시작했습니다.\n"
            "참가자는 /join 을 입력해 주세요.\n\n"
            "관리자가 /lottery_end <당첨자수> 명령어로 종료 후 당첨자를 뽑을 수 있습니다.\n"
            "예: /lottery_end 3"
        )
    elif duration is not None and winners is None:
        text = (
            f"⏳ {duration}분 동안 진행되는 추첨을 시작했습니다.\n"
            "참가자는 /join 을 입력해 주세요.\n\n"
            "설정된 시간이 지나면 자동으로 추첨이 종료되며,\n"
            "관리자가 /lottery_end <당첨자수> 로 당첨자를 뽑을 수 있습니다."
        )
    else:
        text = (
            f"⏳ {duration}분 동안 진행되는 추첨을 시작했습니다.\n"
            f"종료 시 자동으로 {winners}명을 추첨합니다.\n"
            "참가자는 /join 을 입력해 주세요."
        )

    await msg.reply_text(text)


async def cmd_join_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /join
    - 유저가 현재 진행 중인 추첨에 참가
    """
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("이 명령어는 그룹에서만 사용할 수 있습니다.")
        return

    state = LOTTERY_STATE.get(chat.id)
    if not state or not state.get("active"):
        await msg.reply_text("현재 진행 중인 추첨이 없습니다.")
        return

    participants = state["participants"]
    if user.id in participants:
        await msg.reply_text("이미 추첨에 참가하셨습니다.")
        return

    participants.add(user.id)
    await msg.reply_text(f"✅ {user.full_name} 님이 추첨에 참가했습니다! (현재 참가 인원: {len(participants)}명)")


async def cmd_lottery_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /lottery_end <당첨자수>

    - 그룹 관리자 + DB 관리자 + OWNER만 사용
    - /lottery 또는 /lottery 60 으로 시작한 경우: 이 명령어로 종료 + 추첨
    - /lottery 60 3 으로 시작했더라도, 시간이 되기 전에 수동으로 종료하고 싶으면 이 명령어 사용 가능
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    args = context.args

    # DB 관리자 목록 최신화
    reload_admins()

    # ✅ 그룹 관리자 or DB 관리자 or OWNER 판정
    if not await is_chat_admin_or_bot_admin(update, context):
        await msg.reply_text("관리자만 사용할 수 있습니다.")
        return

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("이 명령어는 그룹에서만 사용할 수 있습니다.")
        return

    if not args:
        await msg.reply_text("사용법: /lottery_end <당첨자수>\n예: /lottery_end 3")
        return

    try:
        winners = int(args[0])
    except ValueError:
        await msg.reply_text("당첨자 수는 정수로 입력해 주세요. 예: /lottery_end 3")
        return

    if winners <= 0:
        await msg.reply_text("당첨자 수는 1명 이상이어야 합니다.")
        return

    state = LOTTERY_STATE.get(chat.id)
    if not state:
        await msg.reply_text("진행 중인 추첨이 없습니다.")
        return

    participants = list(state.get("participants", set()))
    if not participants:
        await msg.reply_text("추첨 참가자가 없습니다.")
        LOTTERY_STATE.pop(chat.id, None)
        return

    # 예약된 자동 종료 job 이 있으면 취소
    job = state.get("job")
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass

    num = min(len(participants), winners)
    chosen_ids = random.sample(participants, num)

    winners_texts = []
    for uid in chosen_ids:
        try:
            member = await context.bot.get_chat_member(chat.id, uid)
            u = member.user
            if u.username:
                name = f"@{u.username}"
            else:
                name = u.full_name or str(uid)
        except Exception:
            name = str(uid)
        winners_texts.append(f"- {name}")

    LOTTERY_STATE.pop(chat.id, None)

    text = (
        f"🎉 추첨이 종료되었습니다.\n"
        f"총 참가자 수: {len(participants)}명\n"
        f"당첨자 수: {num}명\n\n"
        "🏆 당첨자:\n" + "\n".join(winners_texts)
    )
    await msg.reply_text(text)


async def auto_end_lottery(context: ContextTypes.DEFAULT_TYPE):
    """
    run_once 로 호출되는 자동 종료 콜백
    - winners 가 None 이면: 추첨만 종료하고, 관리자가 /lottery_end 로 수동 추첨
    - winners 가 있으면: 자동으로 winners 명 추첨 후 종료
    """
    job = context.job
    data = job.data or {}
    chat_id = data.get("chat_id")
    winners = data.get("winners")

    if chat_id is None:
        return

    state = LOTTERY_STATE.get(chat_id)
    if not state:
        return

    participants = list(state.get("participants", set()))

    # 더 이상 active 아님
    state["active"] = False
    state["job"] = None

    if not participants:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏰ 추첨 시간이 종료되었지만 참가자가 없습니다.",
        )
        LOTTERY_STATE.pop(chat_id, None)
        return

    # winners 가 설정되지 않은 경우: 추첨은 종료하지만, 실제 당첨자는 /lottery_end 에서 뽑도록
    if winners is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏰ 설정된 시간이 지나 추첨이 종료되었습니다.\n"
                "관리자가 /lottery_end <당첨자수> 로 당첨자를 뽑을 수 있습니다.\n"
                "예: /lottery_end 3"
            ),
        )
        # 참가자 목록은 유지해서 나중에 /lottery_end 에서 사용
        return

    # winners 가 지정된 경우: 자동으로 추첨까지 진행
    num = min(len(participants), winners)
    chosen_ids = random.sample(participants, num)

    winners_texts = []
    for uid in chosen_ids:
        try:
            member = await context.bot.get_chat_member(chat_id, uid)
            u = member.user
            if u.username:
                name = f"@{u.username}"
            else:
                name = u.full_name or str(uid)
        except Exception:
            name = str(uid)
        winners_texts.append(f"- {name}")

    LOTTERY_STATE.pop(chat_id, None)

    text = (
        f"⏰ 설정된 시간이 지나 추첨이 자동 종료되었습니다.\n"
        f"총 참가자 수: {len(participants)}명\n"
        f"당첨자 수: {num}명\n\n"
        "🏆 당첨자:\n" + "\n".join(winners_texts)
    )
    await context.bot.send_message(chat_id=chat_id, text=text)


# -----------------------
# MAIN
# -----------------------


def main():
    init_db()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 일반 메시지 → XP
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_message,
        )
    )

    # 기본 명령어
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chat_id", cmd_chatid))
    app.add_handler(CommandHandler(["stats", "xp"], cmd_stats))
    app.add_handler(CommandHandler(["ranking", "rank"], cmd_ranking))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("mylink", cmd_mylink))
    app.add_handler(CommandHandler("myinvites", cmd_myinvites))
    app.add_handler(CommandHandler("invites_ranking", cmd_invites_ranking))

    # 로터리(추첨)
    app.add_handler(CommandHandler("lottery", cmd_lottery))
    app.add_handler(CommandHandler("join", cmd_join_lottery))
    app.add_handler(CommandHandler("lottery_end", cmd_lottery_end))

    # 관리자 / OWNER 명령어
    app.add_handler(CommandHandler("list_admins", cmd_listadmins))
    app.add_handler(CommandHandler("add_admin", cmd_addadmin))
    app.add_handler(CommandHandler("del_admin", cmd_deladmin))
    app.add_handler(CommandHandler("ref_user", cmd_refuser))
    app.add_handler(CommandHandler("user_stats", cmd_userstats))
    app.add_handler(CommandHandler("reset_xp", cmd_resetxp))

    # XP 키워드 관리
    app.add_handler(CommandHandler("add_xp_bonus", cmd_addxpbonus))
    app.add_handler(CommandHandler("add_xp_block", cmd_addxpblock))
    app.add_handler(CommandHandler("del_xp_word", cmd_delxpword))
    app.add_handler(CommandHandler("list_xp_words", cmd_listxpwords))

    # 안티 스팸/초대/캠페인 설정
    app.add_handler(CommandHandler("set_cooldown", cmd_setcooldown))
    app.add_handler(CommandHandler("set_daily_cap", cmd_setdailycap))
    app.add_handler(CommandHandler("set_inv_xp", cmd_setinvxp))
    app.add_handler(CommandHandler("set_campaign", cmd_setcampaign))
    app.add_handler(CommandHandler("clear_campaign", cmd_clearcampaign))
    app.add_handler(CommandHandler("add_xp", cmd_add_xp))

    # 기간 요약
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("range", cmd_range))

    # 초대 추적
    app.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # 매일 23:59 KST (UTC 14:59) 요약 전송
    app.job_queue.run_daily(
        send_daily_summary,
        time=time(hour=14, minute=59, tzinfo=timezone.utc),
        name="daily_summary",
    )

    # 매일 23:59 KST (UTC 14:59) 자동 백업
    app.job_queue.run_daily(
        send_daily_backup,
        time=time(hour=14, minute=59, tzinfo=timezone.utc),
        name="daily_backup",
    )

    logger.info("XP Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
