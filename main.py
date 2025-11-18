import os
import logging
import sqlite3
from datetime import datetime, timedelta, time as dtime
from math import sqrt
from zoneinfo import ZoneInfo

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
# 환경 변수 / 설정
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "xp_bot.db")

# 레퍼럴 / 통계를 적용할 메인 그룹 ID (없으면 0)
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0"))

# 봇 오너 (BotFather로 봇 만든 계정의 user id)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# 초기 관리자 (콤마 구분 리스트, 선택)
ADMIN_USER_IDS_ENV = os.getenv("ADMIN_USER_IDS", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 환경 변수가 설정되어 있지 않습니다.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# -----------------------
# DB 유틸
# -----------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # 유저별 + 채팅방별 XP 정보
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

    # 초대 링크
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

    # 초대한 유저 목록 (어떤 링크로 들어왔는지)
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
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
        """
    )

    # 보너스 XP 키워드
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bonus_keywords (
            word TEXT PRIMARY KEY,
            xp INTEGER NOT NULL
        )
        """
    )

    # XP 제외 키워드
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS blocked_keywords (
            word TEXT PRIMARY KEY
        )
        """
    )

    # 오너를 기본 관리자에 포함
    if OWNER_ID > 0:
        cur.execute(
            "INSERT OR IGNORE INTO admins (user_id) VALUES (?)",
            (OWNER_ID,),
        )

    # 환경 변수로 넘어온 초기 관리자 추가
    if ADMIN_USER_IDS_ENV:
        for s in ADMIN_USER_IDS_ENV.split(","):
            s = s.strip()
            if not s:
                continue
            try:
                uid = int(s)
            except ValueError:
                continue
            cur.execute(
                "INSERT OR IGNORE INTO admins (user_id) VALUES (?)",
                (uid,),
            )

    conn.commit()
    conn.close()


# -----------------------
# 권한 유틸
# -----------------------
def is_owner(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return OWNER_ID > 0 and user_id == OWNER_ID


def is_admin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if is_owner(user_id):
        return True
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def get_all_admin_ids() -> list[int]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM admins")
    rows = cur.fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def is_main_chat(chat_id: int) -> bool:
    """레퍼럴/초대 관련 기능을 사용할 수 있는 채팅인지 확인."""
    if MAIN_CHAT_ID == 0:
        return True
    return chat_id == MAIN_CHAT_ID


# -----------------------
# 키워드 유틸
# -----------------------
def get_keywords():
    """보너스/차단 키워드 목록 조회."""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT word, xp FROM bonus_keywords")
    bonus = [(row["word"], row["xp"]) for row in cur.fetchall()]

    cur.execute("SELECT word FROM blocked_keywords")
    blocked = [row["word"] for row in cur.fetchall()]

    conn.close()
    return bonus, blocked


# -----------------------
# XP / 레벨 계산 로직
# -----------------------
def calc_level(xp: int) -> int:
    # 간단한 레벨 공식: xp가 커질수록 레벨업이 점점 어려워짐
    return int(sqrt(xp / 100)) + 1 if xp > 0 else 1


def xp_for_next_level(level: int) -> int:
    # 다음 레벨에 필요한 누적 XP
    next_level = level + 1
    return int((next_level - 1) ** 2 * 100)


def add_xp(chat_id: int, user, base_xp: int) -> tuple[int, int, int]:
    """
    XP를 추가하고 (xp, level, messages_count)를 반환.
    """
    user_id = user.id
    username = user.username
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT xp, level, messages_count FROM user_stats
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat_id, user_id),
    )
    row = cur.fetchone()

    if row is None:
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
            SET username = ?, first_name = ?, last_name = ?,
                xp = ?, level = ?, messages_count = ?
            WHERE chat_id = ? AND user_id = ?
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
# 메시지 핸들러
# -----------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 그룹 / 수퍼그룹 메시지에만 반응
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat is None or user is None or message is None:
        return

    if chat.type not in ("group", "supergroup"):
        return

    text = message.text or message.caption or ""
    if not text:
        return

    # 키워드 로직 적용
    bonus_kw, blocked_kw = get_keywords()
    lower_text = text.lower()

    # 차단 키워드가 하나라도 포함되어 있으면 XP 부여 안 함
    for w in blocked_kw:
        if w.lower() in lower_text:
            return

    # 기본 XP (메시지 길이 기반)
    length = len(text)
    base_xp = 3 + length // 20

    # 보너스 키워드 XP 추가
    bonus_xp = 0
    for w, xp in bonus_kw:
        if w.lower() in lower_text:
            try:
                bonus_xp += int(xp)
            except Exception:
                continue

    total_xp = base_xp + bonus_xp

    xp, level, messages_count = add_xp(chat.id, user, total_xp)

    # 레벨업 알림
    old_xp = xp - total_xp
    old_level = calc_level(old_xp)
    if level > old_level:
        await message.reply_text(
            f"🎉 {user.mention_html()} 님이 레벨업 했습니다!\n"
            f"➡️ 현재 레벨: {level}",
            parse_mode="HTML",
        )


# -----------------------
# 일반 명령어 핸들러
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    is_dm = chat.type == "private" if chat else False
    admin_flag = is_admin(user.id) if user else False

    user_help = (
        "안녕하세요! 저는 Terminal.Fi XP 봇입니다.\n"
        "이 채팅방에서 메시지를 보내면 XP를 얻고 레벨이 올라갑니다.\n\n"
        "일반 명령어:\n"
        "/stats - 내 레벨/XP 확인\n"
        "/ranking - 상위 10명 랭킹\n"
        "/daily - 하루 한 번 보너스 XP\n"
        "/mylink - 나만의 초대 링크 생성 (메인 그룹 전용)\n"
        "/refstats - 초대 랭킹 보기 (메인 그룹 전용)\n"
        "/help - 이 도움말 보기\n"
    )

    if is_dm and admin_flag:
        admin_help = (
            "\n------\n"
            "🔐 관리자/오너 전용 명령어 (DM에서 사용 권장)\n"
            "/chatid - (그룹에서 실행) 해당 채팅의 ID 확인\n"
            "/addadmin <user_id> - 관리자 추가 (오너 전용)\n"
            "/removeadmin <user_id> - 관리자 제거 (오너 전용)\n"
            "/listadmins - 관리자 목록 보기\n"
            "/resetxpall - 메인 그룹 전체 XP 초기화 (오너 전용)\n"
            "\n[키워드 기반 XP 설정]\n"
            "/addbonus <단어> <xp> - 단어 포함 시 XP 추가\n"
            "/delbonus <단어> - 보너스 단어 삭제\n"
            "/listbonus - 보너스 단어 목록\n"
            "/addblock <단어> - 단어 포함 시 XP 미부여\n"
            "/delblock <단어> - 차단 단어 삭제\n"
            "/listblock - 차단 단어 목록\n"
        )
        await update.message.reply_text(user_help + admin_help)
    else:
        await update.message.reply_text(user_help)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    if not is_admin(user.id):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    await update.message.reply_text(
        f"이 채팅의 ID는 `{chat.id}` 입니다.", parse_mode="Markdown"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT xp, level, messages_count, last_daily, invites_count
        FROM user_stats
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat.id, user.id),
    )
    row = cur.fetchone()
    conn.close()

    if row is None:
        await update.message.reply_text(
            "아직 기록된 경험치가 없습니다.\n메시지를 보내면 XP가 쌓입니다!"
        )
        return

    xp = row["xp"]
    level = row["level"]
    messages_count = row["messages_count"]
    invites_count = row["invites_count"]
    next_xp = xp_for_next_level(level)
    remain = max(0, next_xp - xp)

    text = (
        f"📊 {user.full_name} 님의 통계\n\n"
        f"🎯 레벨: {level}\n"
        f"⭐ 경험치: {xp} XP\n"
        f"📈 다음 레벨까지: {remain} XP\n"
        f"💬 총 메시지 수: {messages_count}\n"
        f"👥 초대 인원 수: {invites_count}"
    )

    await update.message.reply_text(text)


async def cmd_ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, first_name, last_name, xp, level
        FROM user_stats
        WHERE chat_id = ?
        ORDER BY xp DESC
        LIMIT 10
        """,
        (chat.id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("아직 이 채팅방에는 경험치 기록이 없습니다.")
        return

    lines = ["🏆 경험치 랭킹 TOP 10\n"]
    medals = ["🥇", "🥈", "🥉"]
    for idx, row in enumerate(rows, start=1):
        username = row["username"]
        if username:
            name = f"@{username}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or "이름없음"

        xp = row["xp"]
        level = row["level"]

        prefix = medals[idx - 1] if idx <= len(medals) else f"{idx}."
        lines.append(f"{prefix} {name} - Lv.{level} ({xp} XP)")

    await update.message.reply_text("\n".join(lines))


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT xp, level, messages_count, last_daily
        FROM user_stats
        WHERE chat_id = ? AND user_id = ?
        """,
        (chat.id, user.id),
    )
    row = cur.fetchone()

    now = datetime.utcnow()
    bonus_xp = 50

    if row is None:
        # 처음 사용하는 유저
        xp = bonus_xp
        level = calc_level(xp)
        messages_count = 0
        last_daily_str = now.isoformat()
        cur.execute(
            """
            INSERT INTO user_stats
            (chat_id, user_id, username, first_name, last_name, xp, level, messages_count, last_daily)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat.id,
                user.id,
                user.username,
                user.first_name or "",
                user.last_name or "",
                xp,
                level,
                messages_count,
                last_daily_str,
            ),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"🎁 첫 일일 보상으로 {bonus_xp} XP를 받았습니다!\n"
            f"현재 레벨: {level}, 총 XP: {xp}"
        )
        return

    last_daily = row["last_daily"]
    if last_daily:
        last_dt = datetime.fromisoformat(last_daily)
        if now - last_dt < timedelta(hours=24):
            remain = timedelta(hours=24) - (now - last_dt)
            hours = remain.seconds // 3600
            minutes = (remain.seconds % 3600) // 60
            await update.message.reply_text(
                f"⏰ 이미 오늘의 보상을 받았습니다.\n"
                f"{hours}시간 {minutes}분 후에 다시 시도해 주세요."
            )
            conn.close()
            return

    xp = row["xp"] + bonus_xp
    level = calc_level(xp)
    messages_count = row["messages_count"]
    last_daily_str = now.isoformat()

    cur.execute(
        """
        UPDATE user_stats
        SET xp = ?, level = ?, last_daily = ?
        WHERE chat_id = ? AND user_id = ?
        """,
        (xp, level, last_daily_str, chat.id, user.id),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🎁 일일 보상으로 {bonus_xp} XP를 받았습니다!\n"
        f"현재 레벨: {level}, 총 XP: {xp}"
    )


# -----------------------
# 레퍼럴 / 초대 링크
# -----------------------
async def cmd_mylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    bot = context.bot

    if chat is None or user is None:
        return

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("이 명령어는 그룹 채팅에서만 사용할 수 있습니다.")
        return

    if not is_main_chat(chat.id):
        await update.message.reply_text(
            "이 봇의 레퍼럴 시스템은 지정된 메인 그룹에서만 사용할 수 있습니다."
        )
        return

    try:
        invite: ChatInviteLink = await bot.create_chat_invite_link(
            chat_id=chat.id,
            name=f"referral:{user.id}",
            creates_join_request=False,
        )
    except Exception:
        logger.exception("초대 링크 생성 실패")
        await update.message.reply_text(
            "초대 링크를 생성할 수 없습니다.\n"
            "봇이 관리자이며 초대 링크 생성 권한이 있는지 확인해 주세요."
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO invite_links
        (invite_link, chat_id, inviter_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (invite.invite_link, chat.id, user.id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "👥 나만의 초대 링크를 생성했습니다!\n"
        "이 링크로 들어온 인원은 모두 내 초대로 집계됩니다.\n\n"
        f"{invite.invite_link}"
    )


async def cmd_refstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return

    if not is_main_chat(chat.id):
        await update.message.reply_text(
            "초대 랭킹은 메인 그룹에서만 확인할 수 있습니다."
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, first_name, last_name, invites_count
        FROM user_stats
        WHERE chat_id = ? AND invites_count > 0
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
    for idx, row in enumerate(rows, start=1):
        username = row["username"]
        if username:
            name = f"@{username}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or "이름없음"

        count = row["invites_count"]
        lines.append(f"{idx}. {name} - {count}명 초대")

    await update.message.reply_text("\n".join(lines))


async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None:
        return

    if not is_main_chat(chat.id):
        return

    chat_member: ChatMemberUpdated = update.chat_member
    new = chat_member.new_chat_member
    old = chat_member.old_chat_member

    if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
        user = new.user
        invite_link = chat_member.invite_link
        if invite_link is None:
            return

        link_url = invite_link.invite_link

        conn = get_conn()
        cur = conn.cursor()

        # 이미 초대 기록이 있는 유저인지 확인
        cur.execute(
            """
            SELECT inviter_id FROM invited_users
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat.id, user.id),
        )
        if cur.fetchone() is not None:
            conn.close()
            return

        # 초대 링크 테이블 업데이트
        cur.execute(
            """
            SELECT inviter_id, joined_count FROM invite_links
            WHERE invite_link = ? AND chat_id = ?
            """,
            (link_url, chat.id),
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            return

        inviter_id = row["inviter_id"]
        joined_count = row["joined_count"] + 1

        cur.execute(
            """
            UPDATE invite_links
            SET joined_count = ?
            WHERE invite_link = ? AND chat_id = ?
            """,
            (joined_count, link_url, chat.id),
        )

        # 초대한 사람의 invites_count +1
        cur.execute(
            """
            SELECT invites_count, username, first_name, last_name FROM user_stats
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat.id, inviter_id),
        )
        inviter_row = cur.fetchone()
        if inviter_row is None:
            cur.execute(
                """
                INSERT INTO user_stats
                (chat_id, user_id, username, first_name, last_name, xp, level, messages_count, last_daily, invites_count)
                VALUES (?, ?, ?, ?, ?, 0, 1, 0, NULL, 1)
                """,
                (
                    chat.id,
                    inviter_id,
                    None,
                    "",
                    "",
                ),
            )
        else:
            invites_count = inviter_row["invites_count"] + 1
            cur.execute(
                """
                UPDATE user_stats
                SET invites_count = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (invites_count, chat.id, inviter_id),
            )

        # 어떤 링크로 들어왔는지 저장
        cur.execute(
            """
            INSERT OR REPLACE INTO invited_users
            (chat_id, user_id, inviter_id, invite_link, joined_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat.id,
                user.id,
                inviter_id,
                link_url,
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()
        conn.close()

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"👋 {user.full_name} 님이 초대 링크를 통해 입장했습니다!\n"
                    f"초대한 유저 ID: {inviter_id}"
                ),
            )
        except Exception:
            logger.exception("welcome 메시지 전송 실패")


# -----------------------
# 관리자 / 오너 명령어
# -----------------------
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return

    if not is_owner(user.id):
        await update.message.reply_text("이 명령어는 오너만 사용할 수 있습니다.")
        return

    if not context.args:
        await update.message.reply_text("사용법: /addadmin <user_id>")
        return

    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 는 숫자여야 합니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO admins (user_id) VALUES (?)",
        (new_id,),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"관리자 {new_id} 이(가) 추가되었습니다.")


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return

    if not is_owner(user.id):
        await update.message.reply_text("이 명령어는 오너만 사용할 수 있습니다.")
        return

    if not context.args:
        await update.message.reply_text("사용법: /removeadmin <user_id>")
        return

    try:
        rm_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 는 숫자여야 합니다.")
        return

    # 오너 자신은 삭제 불가
    if rm_id == OWNER_ID:
        await update.message.reply_text("오너는 관리자 목록에서 제거할 수 없습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM admins WHERE user_id = ?", (rm_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"관리자 {rm_id} 이(가) 제거되었습니다.")


async def cmd_listadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return

    if not is_admin(user.id):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    admin_ids = get_all_admin_ids()
    text_lines = ["현재 관리자 목록:\n"]
    for uid in admin_ids:
        marker = " (오너)" if is_owner(uid) else ""
        text_lines.append(f"- {uid}{marker}")
    await update.message.reply_text("\n".join(text_lines))


async def cmd_resetxpall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return

    if not is_owner(user.id):
        await update.message.reply_text("이 명령어는 오너만 사용할 수 있습니다.")
        return

    if MAIN_CHAT_ID == 0:
        await update.message.reply_text(
            "MAIN_CHAT_ID 가 설정되어 있지 않아 전체 리셋을 할 수 없습니다."
        )
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE user_stats
        SET xp = 0, level = 1, messages_count = 0,
            last_daily = NULL, invites_count = 0
        WHERE chat_id = ?
        """,
        (MAIN_CHAT_ID,),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"메인 그룹({MAIN_CHAT_ID})의 모든 XP/레벨/메시지/초대 수가 초기화되었습니다."
    )


# -----------------------
# 키워드 설정 명령어
# -----------------------
async def cmd_addbonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("사용법: /addbonus <단어> <xp>")
        return

    word = context.args[0].strip().lower()
    try:
        xp = int(context.args[1])
    except ValueError:
        await update.message.reply_text("xp 는 숫자여야 합니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO bonus_keywords (word, xp)
        VALUES (?, ?)
        """,
        (word, xp),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(f"보너스 단어 '{word}' 가 {xp} XP 로 설정되었습니다.")


async def cmd_delbonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not context.args:
        await update.message.reply_text("사용법: /delbonus <단어>")
        return

    word = context.args[0].strip().lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM bonus_keywords WHERE word = ?", (word,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"보너스 단어 '{word}' 가 삭제되었습니다.")


async def cmd_listbonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT word, xp FROM bonus_keywords")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("보너스 단어가 아직 없습니다.")
        return

    lines = ["보너스 단어 목록:\n"]
    for row in rows:
        lines.append(f"- {row['word']} (+{row['xp']} XP)")
    await update.message.reply_text("\n".join(lines))


async def cmd_addblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not context.args:
        await update.message.reply_text("사용법: /addblock <단어>")
        return

    word = context.args[0].strip().lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO blocked_keywords (word) VALUES (?)",
        (word,),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"차단 단어 '{word}' 가 추가되었습니다. 이 단어가 포함된 메시지는 XP가 부여되지 않습니다."
    )


async def cmd_delblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    if not context.args:
        await update.message.reply_text("사용법: /delblock <단어>")
        return

    word = context.args[0].strip().lower()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM blocked_keywords WHERE word = ?", (word,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"차단 단어 '{word}' 가 삭제되었습니다.")


async def cmd_listblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await update.message.reply_text("이 명령어는 관리자만 사용할 수 있습니다.")
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT word FROM blocked_keywords")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("차단 단어가 아직 없습니다.")
        return

    lines = ["차단 단어 목록:\n"]
    for row in rows:
        lines.append(f"- {row['word']}")
    await update.message.reply_text("\n".join(lines))


# -----------------------
# 매일 23:59 KST 통계 DM
# -----------------------
async def job_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if MAIN_CHAT_ID == 0:
        return

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT username, first_name, last_name, xp, level, messages_count, invites_count
        FROM user_stats
        WHERE chat_id = ?
        ORDER BY xp DESC
        LIMIT 10
        """,
        (MAIN_CHAT_ID,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    lines = [
        f"📊 {now_kst.strftime('%Y-%m-%d')} 기준 메인 그룹({MAIN_CHAT_ID}) TOP 10 통계\n"
    ]

    for idx, row in enumerate(rows, start=1):
        username = row["username"]
        if username:
            name = f"@{username}"
        else:
            fn = row["first_name"] or ""
            ln = row["last_name"] or ""
            name = (fn + " " + ln).strip() or "이름없음"

        xp = row["xp"]
        level = row["level"]
        msgs = row["messages_count"]
        invites = row["invites_count"]
        lines.append(
            f"{idx}. {name} - Lv.{level}, XP {xp}, 메시지 {msgs}, 초대 {invites}"
        )

    summary = "\n".join(lines)

    bot = context.bot
    admin_ids = get_all_admin_ids()
    for uid in admin_ids:
        try:
            await bot.send_message(chat_id=uid, text=summary)
        except Exception:
            logger.exception("일일 통계 전송 실패")


# -----------------------
# 메인
# -----------------------
def main():
    init_db()

    application: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 메시지 핸들러
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Caption) & (~filters.COMMAND),
            handle_message,
        )
    )

    # 일반 명령어
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler(["stats", "xp"], cmd_stats))
    application.add_handler(CommandHandler(["ranking", "rank"], cmd_ranking))
    application.add_handler(CommandHandler("daily", cmd_daily))
    application.add_handler(CommandHandler("mylink", cmd_mylink))
    application.add_handler(CommandHandler("refstats", cmd_refstats))

    # 관리자 / 오너 명령어
    application.add_handler(CommandHandler("addadmin", cmd_addadmin))
    application.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    application.add_handler(CommandHandler("listadmins", cmd_listadmins))
    application.add_handler(CommandHandler("resetxpall", cmd_resetxpall))

    # 키워드 관련 명령어
    application.add_handler(CommandHandler("addbonus", cmd_addbonus))
    application.add_handler(CommandHandler("delbonus", cmd_delbonus))
    application.add_handler(CommandHandler("listbonus", cmd_listbonus))
    application.add_handler(CommandHandler("addblock", cmd_addblock))
    application.add_handler(CommandHandler("delblock", cmd_delblock))
    application.add_handler(CommandHandler("listblock", cmd_listblock))

    # chat_member 업데이트 (초대 링크 추적)
    application.add_handler(
        ChatMemberHandler(
            handle_chat_member,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # 매일 23:59 KST 통계 Job 등록
    kst = ZoneInfo("Asia/Seoul")
    application.job_queue.run_daily(
        job_daily_summary,
        dtime(hour=23, minute=59, tzinfo=kst),
        name="daily_summary",
    )

    logger.info("XP Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()
