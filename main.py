import os
import logging
import sqlite3
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

    # 최초 관리자 등록
    for aid in INITIAL_ADMIN_IDS:
        cur.execute("INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)", (aid,))

    # 기본 차단 키워드 (예시): ㅋㅋ, ㄱㄱ
    cur.execute(
        "INSERT OR IGNORE INTO xp_keywords (word, mode, delta) VALUES (?, 'block', 0)",
        ("ㅋㅋ",),
    )
    cur.execute(
        "INSERT OR IGNORE INTO xp_keywords (word, mode, delta) VALUES (?, 'block', 0)",
        ("ㄱㄱ",),
    )

    conn.commit()
    conn.close()

    reload_admins()


# -----------------------
# XP / 레벨 계산
# -----------------------


def calc_level(xp: int) -> int:
    # xp가 커질수록 레벨업이 점점 어려워지도록
    return int(sqrt(xp / 100)) + 1 if xp > 0 else 1


def xp_for_next_level(level: int) -> int:
    next_level = level + 1
    return int((next_level - 1) ** 2 * 100)


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

    # 기본 XP (메시지 길이 기반)
    base_xp = 3 + len(no_space) // 20

    # 1) 아주 짧은 메시지 → XP 0
    if len(no_space) < 5:
        base_xp = 0

    # 2) 이모지만 있는 메시지 → XP 0
    if _is_emoji_only(text):
        base_xp = 0

    # 3) 키워드 기반 보너스/차단
    keywords = get_xp_keywords()
    lower_text = text.lower()
    blocked = False
    bonus_total = 0

    for row in keywords:
        word = row["word"]
        mode = row["mode"]
        delta = row["delta"] or 0

        if not word:
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

    # XP 반영 + messages_count 증가
    xp, level, _ = add_xp(chat.id, user, xp_delta)

    # XP 로그 기록 (메시지 수/기간 통계용, xp_delta가 0이어도 기록)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO xp_log (chat_id, user_id, xp_delta, msg_len, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat.id,
                user.id,
                xp_delta,
                len(no_space),
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("xp_log insert 실패")

    # 레벨업 알림
    old_xp = xp - xp_delta
    if level > calc_level(old_xp):
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
        "/mylink - 초대 링크 생성 (메인 그룹)\n"
        "/myref - 내 초대 인원\n"
        "/refstats - 초대 랭킹\n"
    )

    text = base_text

    # 관리자/OWNER 추가 메뉴
    if is_admin(user.id):
        text += (
            "\n🔧 관리자 명령어 (DM에서 사용 권장)\n"
            "/chatid - 이 채팅의 ID 확인\n"
            "/listadmins - 관리자 목록\n"
            "/refuser <@handle 또는 user_id> - 특정 유저 초대수\n"
            "/userstats <@handle 또는 user_id> - 특정 유저 스탯\n"
            "/today - 오늘 기준 메인 그룹 요약(KST)\n"
            "/week - 최근 7일 메인 그룹 요약(KST)\n"
            "/range YYYY-MM-DD YYYY-MM-DD - 기간별 요약(KST)\n"
            "/addxpbonus <word> <xp> - 키워드 보너스 XP 등록\n"
            "/addxpblock <word> - 키워드 차단 등록)\n"
            "/delxpword <word> - 키워드 삭제\n"
            "/listxpwords - 키워드 목록\n"
        )

    if is_owner(user.id):
        text += (
            "\n😎 OWNER 전용 명령어 (DM 전용 권장)\n"
            "/addadmin <user_id 또는 @handle> - 관리자 추가\n"
            "/deladmin <user_id 또는 @handle> - 관리자 제거\n"
            "/resetxp - 메인 그룹 XP 초기화 (2단계 확인)\n"
        )

    await message.reply_text(text)


# -----------------------
# 공용 / 유저 명령어
# -----------------------


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용할 수 있습니다.")
        return

    await msg.reply_text(f"이 채팅의 ID는 `{chat.id}` 입니다.", parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT xp, level, messages_count, last_daily, invites_count "
        "FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        await msg.reply_text("아직 경험치 기록이 없습니다.")
        return

    xp = row["xp"]
    level = row["level"]
    msgs = row["messages_count"]
    invites = row["invites_count"]
    next_xp = xp_for_next_level(level)

    text = (
        f"📊 {user.full_name} 님의 통계\n\n"
        f"🎯 레벨: {level}\n"
        f"⭐ 경험치: {xp}\n"
        f"📈 다음 레벨까지: {max(0, next_xp - xp)} XP\n"
        f"💬 메시지 수: {msgs}\n"
        f"👥 초대 인원(유저 통계): {invites}\n"
    )
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
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT xp, level, messages_count, last_daily "
        "FROM user_stats WHERE chat_id=? AND user_id=?",
        (chat.id, user.id),
    )
    row = cur.fetchone()

    now = datetime.utcnow()
    bonus = 50

    if not row:
        cur.execute(
            """
            INSERT INTO user_stats
            (chat_id,user_id,username,first_name,last_name,xp,level,messages_count,last_daily)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                chat.id,
                user.id,
                user.username,
                user.first_name or "",
                user.last_name or "",
                bonus,
                calc_level(bonus),
                0,
                now.isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        await msg.reply_text(f"🎁 첫 일일 보상으로 {bonus} XP를 받았습니다!")
        return

    last = row["last_daily"]
    if last:
        last_dt = datetime.fromisoformat(last)
        if now - last_dt < timedelta(hours=24):
            remain = timedelta(hours=24) - (now - last_dt)
            h = remain.seconds // 3600
            m = (remain.seconds % 3600) // 60
            await msg.reply_text(f"⏰ 이미 오늘 보상을 받았습니다.\n{h}시간 {m}분 후에 다시 시도해 주세요.")
            conn.close()
            return

    xp = row["xp"] + bonus
    level = calc_level(xp)
    cur.execute(
        "UPDATE user_stats SET xp=?,level=?,last_daily=? WHERE chat_id=? AND user_id=?",
        (xp, level, now.isoformat(), chat.id, user.id),
    )
    conn.commit()
    conn.close()

    await msg.reply_text(f"🎁 일일 보상으로 {bonus} XP를 받았습니다!\n현재 XP: {xp}, 레벨: {level}")


# -----------------------
# /mylink & 초대 랭킹
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


async def cmd_myref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message
    count = get_invite_count_for_user(user.id)

    await msg.reply_text(f"👥 현재까지 내 초대 링크로 들어온 인원은 총 {count}명입니다.")


async def cmd_refstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
# 초대 tracking (멤버 입장 감지)
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

        cur.execute(
            """
            INSERT OR REPLACE INTO invited_users
            (chat_id,user_id,inviter_id,invite_link,joined_at)
            VALUES (?,?,?,?,?)
            """,
            (
                chat.id,
                user.id,
                inviter,
                link_url,
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()
        conn.close()

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
        await msg.reply_text("사용법: /addadmin <user_id 또는 @username>")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        # 숫자도 아니고 user_stats에도 없으면 그대로 실패
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
        await msg.reply_text("사용법: /deladmin <user_id 또는 @username>")
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
        await msg.reply_text("사용법: /refuser @username 또는 /refuser user_id")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        await msg.reply_text("해당 유저를 user_stats 에서 찾을 수 없습니다.")
        return

    count = get_invite_count_for_user(target_id)
    await msg.reply_text(f"해당 유저 초대 인원: {count}명")


async def cmd_userstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """관리자용: /userstats <@handle 또는 user_id> → 유저 스탯 조회"""
    admin = update.effective_user
    msg = update.message
    args = context.args

    if not is_admin(admin.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return

    if not args:
        await msg.reply_text("사용법: /userstats @username 또는 /userstats user_id")
        return

    target_id = await _resolve_target_user_id(args[0])
    if target_id is None:
        await msg.reply_text("해당 유저를 찾을 수 없습니다.")
        return

    # 어느 채팅 기준으로 볼지: MAIN_CHAT_ID가 설정돼 있으면 그 기준
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

    # invite_links 기준으로 다시 합산 (참고용)
    invites_links = get_invite_count_for_user(target_id)

    last_daily = row["last_daily"]
    if last_daily:
        last_daily_str = datetime.fromisoformat(last_daily).strftime("%Y-%m-%d %H:%M UTC")
    else:
        last_daily_str = "기록 없음"

    text = (
        f"📊 {name} 님의 스탯 (chat_id={chat_id})\n\n"
        f"🎯 레벨: {level}\n"
        f"⭐ 경험치: {xp}\n"
        f"📈 다음 레벨까지: {max(0, next_xp - xp)} XP\n"
        f"💬 메시지 수: {msgs}\n"
        f"👥 초대 인원(user_stats.invites_count): {invites_db}명\n"
        f"👥 초대 인원(invite_links 합산): {invites_links}명\n"
        f"🕒 마지막 일일보상 시각: {last_daily_str}\n"
    )

    await msg.reply_text(text)


async def cmd_resetxp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /resetxp
    OWNER 전용.
    - 처음 호출: 경고 + 사용법 안내
    - '/resetxp 동의합니다.' 로 다시 호출했을 때만 실제 초기화 수행
    - 초기화 직전 스냅샷을 OWNER DM 으로 먼저 보내고 그 후 리셋
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

    confirmation_text = "동의합니다."

    # 1차 호출: 경고 & 사용법 안내
    if not args or " ".join(args) != confirmation_text:
        await msg.reply_text(
            "⚠️ 이 명령어는 메인 그룹의 모든 XP/레벨/메시지/초대 기록을 초기화합니다.\n"
            "정말로 초기화를 진행하시겠습니까?\n\n"
            f"초기화를 진행하려면 아래와 같이 다시 입력해 주세요.\n"
            f"`/resetxp {confirmation_text}`",
            parse_mode="Markdown",
        )
        return

    # 여기까지 왔으면 '/resetxp 동의합니다.' 로 호출된 것
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
        SET xp=0, level=1, messages_count=0, last_daily=NULL, invites_count=0
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

    # 최종 안내 메시지
    await msg.reply_text(
        f"✅ MAIN_CHAT_ID={MAIN_CHAT_ID} 의 XP/레벨/메시지/초대 기록을 초기화했습니다.\n"
        f"(영향 받은 유저 수: {affected}명)\n"
        "초기화 직전 스냅샷은 DM으로 전송했습니다.",
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
        await msg.reply_text("사용법: /addxpbonus <word> <xp>")
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
        await msg.reply_text("사용법: /addxpblock <word>")
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
        await msg.reply_text("사용법: /delxpword <word>")
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
        await msg.reply_text("관리자만 사용할 수 있습니다.")
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
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler(["stats", "xp"], cmd_stats))
    app.add_handler(CommandHandler(["ranking", "rank"], cmd_ranking))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("mylink", cmd_mylink))
    app.add_handler(CommandHandler(["myref", "myinvites"], cmd_myref))
    app.add_handler(CommandHandler("refstats", cmd_refstats))

    # 관리자 / OWNER 명령어
    app.add_handler(CommandHandler("listadmins", cmd_listadmins))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))
    app.add_handler(CommandHandler("refuser", cmd_refuser))
    app.add_handler(CommandHandler("userstats", cmd_userstats))
    app.add_handler(CommandHandler("resetxp", cmd_resetxp))

    # XP 키워드 관리
    app.add_handler(CommandHandler("addxpbonus", cmd_addxpbonus))
    app.add_handler(CommandHandler("addxpblock", cmd_addxpblock))
    app.add_handler(CommandHandler("delxpword", cmd_delxpword))
    app.add_handler(CommandHandler("listxpwords", cmd_listxpwords))

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

    logger.info("XP Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
