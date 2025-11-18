import os
import logging
import sqlite3
from datetime import datetime, timedelta, time, timezone
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

    # 최초 관리자 등록
    for aid in INITIAL_ADMIN_IDS:
        cur.execute("INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)", (aid,))

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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user

    if not chat or not user or not message:
        return
    if chat.type not in ("group", "supergroup"):
        return

    text = message.text or message.caption or ""
    base_xp = 3 + len(text) // 20

    xp, level, _ = add_xp(chat.id, user, base_xp)

    if level > calc_level(xp - base_xp):
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
        "메시지를 보내면 XP를 얻고 레벨이 올라갑니다.\n\n"
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
            "\n🔧 관리자 명령어\n"
            "/chatid - 이 채팅의 ID 확인\n"
            "/listadmins - 관리자 목록\n"
            "/refuser <@handle 또는 user_id> - 특정 유저 초대수\n"
            "/userstats <@handle 또는 user_id> - 특정 유저 스탯\n"
        )

    if is_owner(user.id):
        text += (
            "\n👑 OWNER 전용 명령어\n"
            "/resetxp - 메인 그룹 XP 초기화\n"
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
    if not is_main_chat(chat.id):
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
# 관리자 명령어 (/listadmins, /refuser, /userstats, /resetxp)
# -----------------------


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


async def _resolve_target_user_id(arg: str) -> int | None:
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

    # invite_links 기준으로 다시 한 번 합산 (참고용)
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
    user = update.effective_user
    msg = update.message

    if not is_owner(user.id):
        await msg.reply_text("OWNER만 사용할 수 있습니다.")
        return

    if MAIN_CHAT_ID == 0:
        await msg.reply_text("MAIN_CHAT_ID가 설정되어 있지 않아 XP를 리셋할 수 없습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
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

    await msg.reply_text(f"✅ MAIN_CHAT_ID={MAIN_CHAT_ID} 의 XP/레벨/메시지/초대 기록을 초기화했습니다.\n"
                         f"(영향 받은 유저 수: {affected}명)")


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
            (filters.TEXT | filters.Caption) & (~filters.COMMAND),
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

    # 관리자 명령어
    app.add_handler(CommandHandler("listadmins", cmd_listadmins))
    app.add_handler(CommandHandler("refuser", cmd_refuser))
    app.add_handler(CommandHandler("userstats", cmd_userstats))
    app.add_handler(CommandHandler("resetxp", cmd_resetxp))

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
