import os
import logging
import sqlite3
from datetime import datetime, timedelta, time, timezone
from math import sqrt

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

# ============================================================
# 환경 변수 / 기본 설정
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "xp_bot.db")

MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))  # 0=모든 그룹 허용
OWNER_ID = int(os.getenv("OWNER_ID", "0"))          # BotFather owner

_admin_env = os.getenv("ADMIN_USER_IDS", "")
INITIAL_ADMIN_IDS = set()
for part in _admin_env.split(","):
    part = part.strip()
    if part:
        try:
            INITIAL_ADMIN_IDS.add(int(part))
        except ValueError:
            pass

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 환경 변수가 설정되지 않았습니다.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 런타임 관리자 목록
ADMIN_USER_IDS: set[int] = set()
KEYWORDS = {}  # {"word": xp}


# ============================================================
# 기본 권한 체크
# ============================================================

def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or (user_id in ADMIN_USER_IDS)


def all_admin_targets() -> set[int]:
    targets = set(ADMIN_USER_IDS)
    if OWNER_ID:
        targets.add(OWNER_ID)
    return targets


def is_main_chat(chat_id: int) -> bool:
    if MAIN_CHAT_ID == 0:
        return True
    return chat_id == MAIN_CHAT_ID


# ============================================================
# DB 유틸
# ============================================================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
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
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS invite_links (
            invite_link TEXT PRIMARY KEY,
            chat_id INTEGER,
            inviter_id INTEGER,
            created_at TEXT,
            joined_count INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS invited_users (
            chat_id INTEGER,
            user_id INTEGER,
            inviter_id INTEGER,
            invite_link TEXT,
            joined_at TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            admin_id INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            word TEXT PRIMARY KEY,
            xp INTEGER
        )
    """)

    for aid in INITIAL_ADMIN_IDS:
        cur.execute("INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)", (aid,))

    conn.commit()
    conn.close()

    reload_admins()
    reload_keywords()


def reload_admins():
    global ADMIN_USER_IDS
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT admin_id FROM admin_users")
    rows = cur.fetchall()
    conn.close()
    ADMIN_USER_IDS = {int(r["admin_id"]) for r in rows}
    logger.info("Loaded admins: %s", ADMIN_USER_IDS)


def reload_keywords():
    global KEYWORDS
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT word, xp FROM keywords")
    rows = cur.fetchall()
    conn.close()
    KEYWORDS = {r["word"]: r["xp"] for r in rows}
    logger.info("Loaded keywords: %s", KEYWORDS)


# ============================================================
# XP 계산
# ============================================================

def calc_level(xp: int) -> int:
    return int(sqrt(xp / 100)) + 1 if xp > 0 else 1


def xp_for_next_level(level: int) -> int:
    next_level = level + 1
    return int((next_level - 1) ** 2 * 100)


def add_xp(chat_id: int, user, base_xp: int):
    user_id = user.id
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT xp, level, messages_count FROM user_stats WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()

    if row is None:
        xp = max(0, base_xp)
        level = calc_level(xp)
        messages_count = 1
        cur.execute("""
            INSERT INTO user_stats
            (chat_id, user_id, username, first_name, last_name, xp, level, messages_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat_id, user.id, user.username, user.first_name or "", user.last_name or "", xp, level, messages_count))
    else:
        xp = row["xp"] + max(0, base_xp)
        level = calc_level(xp)
        messages_count = row["messages_count"] + 1
        cur.execute("""
            UPDATE user_stats
            SET username=?, first_name=?, last_name=?, xp=?, level=?, messages_count=?
            WHERE chat_id=? AND user_id=?
        """, (user.username, user.first_name or "", user.last_name or "", xp, level, messages_count, chat_id, user_id))

    conn.commit()
    conn.close()
    return xp, level, messages_count


# ============================================================
# 메시지 핸들러 → XP 부여 + 키워드 XP
# ============================================================

async def handle_message(update, context):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if chat is None or chat.type not in ("group", "supergroup"):
        return

    text = msg.text or ""
    length_xp = 3 + len(text) // 20
    keyword_xp = 0

    for kw, xp in KEYWORDS.items():
        if kw.lower() in text.lower():
            keyword_xp += xp

    total_xp = length_xp + keyword_xp
    xp, level, _ = add_xp(chat.id, user, total_xp)

    old_level = calc_level(xp - total_xp)
    if level > old_level:
        await msg.reply_text(f"🎉 {user.mention_html()} 님 레벨업! (Lv.{level})", parse_mode="HTML")


# ============================================================
# 공용 명령어
# ============================================================

async def cmd_start(update, context):
    await cmd_help(update, context)


async def cmd_help(update, context):
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if user is None:
        return

    base_text = (
        "안녕하세요! Terminal.Fi XP 봇입니다.\n"
        "메시지를 보내면 XP를 얻고 레벨이 올라갑니다.\n\n"
        "📌 일반 명령어:\n"
        "/stats - 내 XP 확인\n"
        "/ranking - 상위 10명 XP 랭킹\n"
        "/daily - 하루 1회 보너스 XP\n"
        "/mylink - 나만의 초대 링크\n"
        "/myref - 내가 초대한 인원 수\n"
        "/refstats - 초대 랭킹\n"
    )

    if chat.type in ("group", "supergroup"):
        await msg.reply_text(base_text)
        return

    text = base_text
    if is_admin(user.id):
        text += (
            "\n\n🛠 관리자 전용 (DM 전용)\n"
            "/chatid - 현재 대화방 ID 확인\n"
            "/listadmins - 관리자 목록\n"
            "/refuser <user> - 특정 유저 초대 수\n"
            "/resetxp - XP 초기화 (OWNER)\n"
            "/addadmin <id> - 관리자 추가 (OWNER)\n"
            "/deladmin <id> - 관리자 제거 (OWNER)\n"
            "/addkeyword <word> <xp> - 키워드 XP 추가\n"
            "/delkeyword <word> - 키워드 제거\n"
            "/listkeywords - 키워드 목록\n"
        )
    await msg.reply_text(text)


async def cmd_chatid(update, context):
    user = update.effective_user
    msg = update.effective_message
    chat = update.effective_chat

    if not is_admin(user.id):
        await msg.reply_text("관리자만 사용 가능합니다.")
        return

    await msg.reply_text(f"이 채팅의 ID: `{chat.id}`", parse_mode="Markdown")


async def cmd_stats(update, context):
    chat = update.effective_chat
    user = update.effective_user

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_stats WHERE chat_id=? AND user_id=?", (chat.id, user.id))
    row = cur.fetchone()
    conn.close()

    if row is None:
        await update.message.reply_text("아직 XP가 없습니다. 메시지를 보내 보세요!")
        return

    xp = row["xp"]
    level = row["level"]
    nextxp = xp_for_next_level(level)

    msg = (
        f"📊 {user.full_name} 님 통계\n"
        f"🎯 레벨: {level}\n"
        f"⭐ XP: {xp}\n"
        f"📈 다음 레벨까지: {nextxp - xp} XP\n"
        f"💬 메시지 수: {row['messages_count']}\n"
        f"👥 초대 인원: {row['invites_count']}\n"
    )
    await update.message.reply_text(msg)


async def cmd_ranking(update, context):
    chat = update.effective_chat

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, first_name, last_name, xp, level
        FROM user_stats
        WHERE chat_id=?
        ORDER BY xp DESC
        LIMIT 10
    """, (chat.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("아직 XP 기록이 없습니다.")
        return

    msg = ["🏆 경험치 랭킹 TOP 10\n"]
    medals = ["🥇", "🥈", "🥉"]

    for idx, row in enumerate(rows, start=1):
        name = row["username"] and f"@{row['username']}" or (row["first_name"] or "")
        prefix = medals[idx-1] if idx <= 3 else f"{idx}."
        msg.append(f"{prefix} {name} - Lv.{row['level']} ({row['xp']} XP)")

    await update.message.reply_text("\n".join(msg))


# ============================================================
# /daily 보너스
# ============================================================

async def cmd_daily(update, context):
    chat = update.effective_chat
    user = update.effective_user
    now = datetime.utcnow()
    bonus = 50

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT xp, level, last_daily, messages_count FROM user_stats WHERE chat_id=? AND user_id=?", (chat.id, user.id))
    row = cur.fetchone()

    if row is None:
        xp = bonus
        level = calc_level(xp)
        cur.execute("""
            INSERT INTO user_stats (chat_id,user_id,username,first_name,last_name,xp,level,messages_count,last_daily)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chat.id, user.id, user.username, user.first_name or "", user.last_name or "", xp, level, 0, now.isoformat()))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"🎁 첫 보상! {bonus} XP")
        return

    if row["last_daily"]:
        last = datetime.fromisoformat(row["last_daily"])
        if now - last < timedelta(hours=24):
            remain = timedelta(hours=24) - (now - last)
            h = remain.seconds // 3600
            m = (remain.seconds % 3600) // 60
            await update.message.reply_text(f"⏰ 오늘 이미 받았습니다.\n{h}시간 {m}분 후 다시 가능")
            conn.close()
            return

    xp = row["xp"] + bonus
    level = calc_level(xp)

    cur.execute("UPDATE user_stats SET xp=?, level=?, last_daily=? WHERE chat_id=? AND user_id=?",
                (xp, level, now.isoformat(), chat.id, user.id))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"🎁 일일 보상으로 {bonus} XP 지급!\n현재 레벨: {level}")


# ============================================================
# /mylink - 초대 링크
# ============================================================

async def cmd_mylink(update, context):
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("그룹에서만 가능합니다.")
        return

    if not is_main_chat(chat.id):
        await update.message.reply_text("메인 그룹에서만 생성 가능합니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT invite_link FROM invite_links WHERE chat_id=? AND inviter_id=? LIMIT 1", (chat.id, user.id))
    row = cur.fetchone()

    if row:
        await update.message.reply_text(f"이미 생성된 초대 링크:\n{row['invite_link']}")
        conn.close()
        return

    try:
        link: ChatInviteLink = await bot.create_chat_invite_link(
            chat_id=chat.id,
            name=f"referral:{user.id}",
            creates_join_request=False
        )
    except Exception:
        await update.message.reply_text("초대 링크 생성 실패. 봇 관리자 권한을 확인하세요.")
        return

    cur.execute("""
        INSERT INTO invite_links (invite_link, chat_id, inviter_id, created_at)
        VALUES (?, ?, ?, ?)
    """, (link.invite_link, chat.id, user.id, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"새 초대 링크 생성!\n{link.invite_link}")


async def cmd_myref(update, context):
    user = update.effective_user

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT SUM(joined_count) AS c FROM invite_links WHERE inviter_id=?", (user.id,))
    row = cur.fetchone()
    conn.close()

    count = row["c"] or 0
    await update.message.reply_text(f"👥 지금까지 초대한 인원: {count}명")


async def cmd_refstats(update, context):
    chat = update.effective_chat

    if not is_main_chat(chat.id):
        await update.message.reply_text("메인 그룹에서만 확인 가능합니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, first_name, last_name, invites_count
        FROM user_stats
        WHERE chat_id=? AND invites_count>0
        ORDER BY invites_count DESC
        LIMIT 10
    """, (chat.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("초대 기록이 없습니다.")
        return

    msg = ["👥 초대 랭킹 TOP 10\n"]
    for idx, row in enumerate(rows, start=1):
        name = row["username"] and f"@{row['username']}" or (row["first_name"] or "")
        msg.append(f"{idx}. {name} - {row['invites_count']}명")

    await update.message.reply_text("\n".join(msg))


# ============================================================
# chat_member → 초대 추적
# ============================================================

async def handle_chat_member(update, context):
    chat = update.effective_chat
    if not is_main_chat(chat.id):
        return

    cm: ChatMemberUpdated = update.chat_member
    old = cm.old_chat_member
    new = cm.new_chat_member

    if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
        invite = cm.invite_link
        if invite is None:
            return

        link_url = invite.invite_link

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT inviter_id, joined_count FROM invite_links WHERE invite_link=?", (link_url,))
        row = cur.fetchone()
        if row is None:
            conn.close()
            return

        inviter_id = row["inviter_id"]
        new_count = row["joined_count"] + 1

        cur.execute("UPDATE invite_links SET joined_count=? WHERE invite_link=?", (new_count, link_url))

        cur.execute("""
            UPDATE user_stats
            SET invites_count = invites_count + 1
            WHERE chat_id=? AND user_id=?
        """, (chat.id, inviter_id))

        cur.execute("""
            INSERT OR REPLACE INTO invited_users (chat_id,user_id, inviter_id, invite_link, joined_at)
            VALUES (?, ?, ?, ?, ?)
        """, (chat.id, new.user.id, inviter_id, link_url, datetime.utcnow().isoformat()))

        conn.commit()
        conn.close()

        await context.bot.send_message(chat_id=chat.id, text=f"👋 {new.user.full_name} 님이 들어왔습니다!\n초대한 사람: {inviter_id}")


# ============================================================
# 관리자 명령어
# ============================================================

async def cmd_listadmins(update, context):
    user = update.effective_user
    msg = update.message
    if not is_admin(user.id):
        await msg.reply_text("관리자만 가능합니다.")
        return

    lines = ["📋 관리자 목록:"]
    lines.append(f"- OWNER: {OWNER_ID}")
    for a in sorted(ADMIN_USER_IDS):
        lines.append(f"- {a}")

    await msg.reply_text("\n".join(lines))


async def cmd_addadmin(update, context):
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_owner(user.id):
        await msg.reply_text("봇 소유자만 가능합니다.")
        return

    if not args or not args[0].isdigit():
        await msg.reply_text("사용법: /addadmin <user_id>")
        return

    target = int(args[0])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO admin_users (admin_id) VALUES (?)", (target,))
    conn.commit()
    conn.close()

    reload_admins()
    await msg.reply_text(f"관리자 추가 완료: {target}")


async def cmd_deladmin(update, context):
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_owner(user.id):
        await msg.reply_text("봇 소유자만 가능합니다.")
        return

    if not args or not args[0].isdigit():
        await msg.reply_text("사용법: /deladmin <user_id>")
        return

    target = int(args[0])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_users WHERE admin_id=?", (target,))
    conn.commit()
    conn.close()

    reload_admins()
    await msg.reply_text(f"관리자 제거 완료: {target}")


async def cmd_refuser(update, context):
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 가능합니다.")
        return

    if not args:
        await msg.reply_text("사용법: /refuser <@username 또는 user_id>")
        return

    query = args[0]
    if query.startswith("@"):
        query = query[1:]

    target_id = None

    if query.isdigit():
        target_id = int(query)
    else:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM user_stats WHERE username=? LIMIT 1", (query,))
        row = cur.fetchone()
        conn.close()

        if row is None:
            await msg.reply_text("해당 username 이 없습니다.")
            return
        target_id = row["user_id"]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT SUM(joined_count) AS c FROM invite_links WHERE inviter_id=?", (target_id,))
    row = cur.fetchone()
    conn.close()

    count = row["c"] or 0
    await msg.reply_text(f"👥 {target_id} 초대 인원: {count}명")


async def cmd_resetxp(update, context):
    user = update.effective_user
    msg = update.message

    if not is_owner(user.id):
        await msg.reply_text("OWNER만 가능합니다.")
        return

    if MAIN_CHAT_ID == 0:
        await msg.reply_text("MAIN_CHAT_ID 가 설정되지 않았습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE user_stats
        SET xp=0, level=1, messages_count=0, last_daily=NULL, invites_count=0
        WHERE chat_id=?
    """, (MAIN_CHAT_ID,))
    affected = cur.rowcount
    conn.commit()
    conn.close()

    await msg.reply_text(f"XP 전체 초기화 완료 (영향 받은 유저: {affected})")


# ============================================================
# 키워드 XP 설정 (관리자)
# ============================================================

async def cmd_addkeyword(update, context):
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 가능합니다.")
        return

    if len(args) < 2:
        await msg.reply_text("사용법: /addkeyword <word> <xp>")
        return

    word = args[0].lower()
    if not args[1].isdigit():
        await msg.reply_text("XP 값은 숫자여야 합니다.")
        return

    xp = int(args[1])

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO keywords (word,xp) VALUES (?,?)", (word, xp))
    conn.commit()
    conn.close()

    reload_keywords()
    await msg.reply_text(f"키워드 등록 완료: {word} → {xp} XP")


async def cmd_delkeyword(update, context):
    user = update.effective_user
    msg = update.message
    args = context.args

    if not is_admin(user.id):
        await msg.reply_text("관리자만 가능합니다.")
        return

    if not args:
        await msg.reply_text("사용법: /delkeyword <word>")
        return

    word = args[0].lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM keywords WHERE word=?", (word,))
    conn.commit()
    conn.close()

    reload_keywords()
    await msg.reply_text(f"키워드 제거 완료: {word}")


async def cmd_listkeywords(update, context):
    user = update.effective_user
    msg = update.message

    if not is_admin(user.id):
        await msg.reply_text("관리자만 가능합니다.")
        return

    if not KEYWORDS:
        await msg.reply_text("등록된 키워드가 없습니다.")
        return

    lines = ["📚 키워드 XP 목록:"]
    for word, xp in KEYWORDS.items():
        lines.append(f"- {word}: {xp} XP")

    await msg.reply_text("\n".join(lines))


# ============================================================
# 매일 23:59 KST 요약 DM
# ============================================================

async def send_daily_summary(context):
    if MAIN_CHAT_ID == 0:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, first_name, last_name, xp, level
        FROM user_stats
        WHERE chat_id=?
        ORDER BY xp DESC
        LIMIT 10
    """, (MAIN_CHAT_ID,))
    rows = cur.fetchall()

    cur.execute("SELECT COUNT(*) AS c FROM user_stats WHERE chat_id=?", (MAIN_CHAT_ID,))
    total_users = cur.fetchone()["c"]
    conn.close()

    now_kst = datetime.utcnow() + timedelta(hours=9)

    if not rows:
        body = "오늘 XP 활동 기록이 없습니다."
    else:
        lines = ["오늘 XP TOP 10:\n"]
        for idx, row in enumerate(rows, start=1):
            name = row["username"] and f"@{row['username']}" or (row["first_name"] or "")
            lines.append(f"{idx}. {name} - Lv.{row['level']} ({row['xp']} XP)")
        lines.append(f"\n총 유저 수: {total_users}")
        body = "\n".join(lines)

    text = (
        f"📊 Daily XP 요약 (KST 기준)\n"
        f"{now_kst.strftime('%Y-%m-%d %H:%M')}\n"
        f"MAIN_CHAT_ID = {MAIN_CHAT_ID}\n\n"
        f"{body}"
    )

    for uid in all_admin_targets():
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except:
            pass


# ============================================================
# main 함수
# ============================================================

async def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 일반 메시지 → XP
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND), handle_message))

    # 공용 명령어
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("ranking", cmd_ranking))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("mylink", cmd_mylink))
    app.add_handler(CommandHandler(["myref", "myinvites"], cmd_myref))
    app.add_handler(CommandHandler("refstats", cmd_refstats))
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    # 관리자 명령어
    app.add_handler(CommandHandler("listadmins", cmd_listadmins))
    app.add_handler(CommandHandler("refuser", cmd_refuser))
    app.add_handler(CommandHandler("resetxp", cmd_resetxp))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))

    # 키워드 XP 설정
    app.add_handler(CommandHandler("addkeyword", cmd_addkeyword))
    app.add_handler(CommandHandler("delkeyword", cmd_delkeyword))
    app.add_handler(CommandHandler("listkeywords", cmd_listkeywords))

    # 초대 추적
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # 매일 23:59 KST → UTC 14:59
    kst_2359_utc = time(hour=14, minute=59, tzinfo=timezone.utc)
    app.job_queue.run_daily(send_daily_summary, kst_2359_utc)

    logger.info("XP Bot started")
    await app.run_polling(close_loop=False)


# ============================================================

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
