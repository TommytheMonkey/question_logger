import os
import time
import threading
from collections import deque
import re
from datetime import datetime
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from google_utils import (
    get_sheets_service,
    append_to_sheet,
    find_job_in_joblog
)
from monday_utils import (
    find_item_by_job_number,
    get_item_details_by_id
)
import logging
# NEW: thread store helpers

# In-memory relay map (replaces thread_store)
import time
import threading
from collections import deque

class RelayMemory:
    def __init__(self, max_items=30, ttl_seconds=72*3600):
        self.lock = threading.Lock()
        self.max_items = max_items
        self.ttl = ttl_seconds
        # key: client_ts -> (job, internal_channel, internal_ts, created_at)
        self.map = {}
        self.order = deque()

    def _evict(self):
        now = time.time()
        # TTL evict
        while self.order:
            k = self.order[0]
            v = self.map.get(k)
            if not v:
                self.order.popleft()
                continue
            _, _, _, created_at = v
            if now - created_at > self.ttl:
                self.order.popleft()
                self.map.pop(k, None)
            else:
                break
        # Size evict
        while len(self.order) > self.max_items:
            k = self.order.popleft()
            self.map.pop(k, None)

    def save_map(self, job, internal_channel, internal_ts, client_ts):
        with self.lock:
            k = client_ts
            self.map[k] = (job, internal_channel, internal_ts, time.time())
            self.order.append(k)
            self._evict()

    def by_client_thread(self, client_ts):
        with self.lock:
            v = self.map.get(client_ts)
            if not v:
                return None
            job, internal_channel, internal_ts, _ = v
            return (job, internal_channel, internal_ts)

RELAY_MEM = RelayMemory()

def save_map(job, internal_channel, internal_ts, client_channel, client_ts):
    RELAY_MEM.save_map(job, internal_channel, internal_ts, client_ts)

def by_client_thread(client_ts):
    return RELAY_MEM.by_client_thread(client_ts)

load_dotenv()

# ---- Logging config (force console visibility) ----------------------
logging.basicConfig(
    level=logging.DEBUG,  # show everything
    format="%(asctime)s %(levelname)5s [%(name)s] %(message)s",
)
logger = logging.getLogger("question_logger")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

""" Question log column key: 
    A: Job # 
    B: Job name
    C: Request DATE
    D: Request TIME
    E: Requested by
    F: Request BODY
    G: Response DATE
    H: Response TIME
    I: Response FROM
    J: Response BODY
    K: Monday Item ID
    L: Internal TS
    M: Client Root TS
    N: Internal Channel ID
    O: Client Channel ID
"""

# INTERNAL (source) channels where your team asks Qs (from .env)
INTERNAL_CHANNEL_IDS = []
_internal_env = os.getenv("INTERNAL_CHANNEL_IDS", "").strip()
if _internal_env:
    INTERNAL_CHANNEL_IDS = [c.strip() for c in _internal_env.split(",") if c.strip()]
# fallback to previous hardcoded defaults if env not set
if not INTERNAL_CHANNEL_IDS:
    INTERNAL_CHANNEL_IDS = ["C08HUKW7NDU", "C08SLQ1J952"]

# OPTIONAL: explicit client channels; if not set, any non-internal channel is treated as client
CLIENT_CHANNEL_IDS = []
_client_env = os.getenv("CLIENT_CHANNEL_IDS", "").strip()
if _client_env:
    CLIENT_CHANNEL_IDS = [c.strip() for c in _client_env.split(",") if c.strip()]

SPREADSHEET_ID = os.getenv("QUESTIONS_SHEET_ID")
QUESTION_RANGE = "QuestionsLog!A1"

JOBLOG_SHEET_ID = os.getenv("JOBLOG_SHEET_ID")
JOBLOG_RANGE = "Active!A:H"

DIVISION_COLUMN_ID = os.getenv("BRANCH_COL_ID")
PRODUCT_COLUMN_ID = os.getenv("PRODUCT_COL_ID")
DOC_LINK_COLUMN = os.getenv("MONDAY_DOC_LINK_COL_ID")

JOB_TAG_RE = re.compile(r"#(\d{5})\?")

app = App(token=SLACK_BOT_TOKEN)

# Ensure Bolt logger is verbose too
app.logger.setLevel(logging.DEBUG)
logging.getLogger("slack_bolt").setLevel(logging.DEBUG)
logging.getLogger("slack_sdk").setLevel(logging.INFO)

client = WebClient(token=SLACK_BOT_TOKEN)
sheets_service = get_sheets_service()

app.logger.info(f"CLIENT_CHANNEL_IDS: {CLIENT_CHANNEL_IDS}")
app.logger.info(f"INTERNAL_CHANNEL_IDS: {INTERNAL_CHANNEL_IDS}")

def rebuild_from_sheet_cache(logger, max_rows: int = 200):
    try:
        values = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="QuestionsLog!A:O"
        ).execute().get("values", []) or []
        if not values:
            return
        header, rows = values[0], values[1:]
        start = max(0, len(rows) - max_rows)
        for r in rows[start:]:
            # columns by index (0-based)
            job = (r[0].strip() if len(r) > 0 else "")
            internal_ts = (r[11].strip() if len(r) > 11 else "")  # L
            client_ts   = (r[12].strip() if len(r) > 12 else "")  # M
            internal_ch = (r[13].strip() if len(r) > 13 else "")  # N
            client_ch   = (r[14].strip() if len(r) > 14 else "")  # O
            # Rebuild latest internal per job
            if job and internal_ts and internal_ch:
                PENDING_INTERNAL[job] = (internal_ch, internal_ts)
            # Rebuild relay map (we only need client_ts to restore)
            if job and client_ts and internal_ts and internal_ch:
                try:
                    RELAY_MEM.save_map(job, internal_ch, internal_ts, client_ts)
                except Exception:
                    pass
        logger.info(f"Rebuilt cache from sheet: {len(PENDING_INTERNAL)} internal threads, {len(RELAY_MEM.map)} client threads")
    except Exception as e:
        logger.error(f"rebuild_from_sheet_cache failed: {e}")

rebuild_from_sheet_cache(app.logger)

# ─────────────────────────────────────────────────────────────────────
# simple in-memory cache: job_num -> (internal_channel, internal_ts)
# helps us pair a later client "regurgitation" with the most recent internal Q
PENDING_INTERNAL = {}
# ─────────────────────────────────────────────────────────────────────


def _is_client_channel(chan: str) -> bool:
    if chan in INTERNAL_CHANNEL_IDS:
        app.logger.info(f"_is_client_channel FALSE (internal): {chan}")
        return False
    if CLIENT_CHANNEL_IDS:
        app.logger.info(f"_is_client_channel explicit list: {CLIENT_CHANNEL_IDS} | checking {chan}")
        return chan in CLIENT_CHANNEL_IDS
    # fallback: anything not in internal list is considered "client"
    app.logger.info(f"_is_client_channel fallback TRUE for {chan}")
    return True


def _get_user_name_safe(user_id: str, logger) -> str:
    try:
        ui = client.users_info(user=user_id)
        prof = (ui or {}).get("user", {}).get("profile", {})
        return prof.get("real_name") or prof.get("display_name") or (ui or {}).get("user", {}).get("name", "") or user_id
    except Exception as e:
        logger.error(f"users_info failed: {e}")
        return user_id


def _update_sheet_response_for_job(job_num: str, resp_date: str, resp_time: str, resp_from: str, resp_body: str, logger) -> None:
    """
    Find the most recent row for this job that does NOT yet have a response (col G empty),
    then set G:J.
    """
    try:
        # pull full grid A:K
        values = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="QuestionsLog!A:K"
        ).execute().get("values", []) or []

        if not values:
            return

        headers = values[0]
        # locate latest unanswered row for this job
        target_row_idx_1based = None
        for idx in range(len(values) - 1, 0, -1):  # bottom-up, skip header
            row = values[idx]
            a_job = (row[0].strip() if len(row) > 0 else "")
            g_resp_date = (row[6].strip() if len(row) > 6 else "")
            if a_job == str(job_num) and not g_resp_date:
                target_row_idx_1based = idx + 1  # 1-based for A1 notation
                break

        if not target_row_idx_1based:
            logger.warn(f"No unanswered row found for job {job_num}; appending a response-only row.")
            # Fallback: append a new line carrying the job and response
            append_to_sheet(
                sheets_service,
                SPREADSHEET_ID,
                "QuestionsLog!A:K",
                [[job_num, "", "", "", "", "", resp_date, resp_time, resp_from, resp_body, ""]]
            )
            return

        # Build the A1 range for columns G:J on that row
        update_range = f"QuestionsLog!G{target_row_idx_1based}:J{target_row_idx_1based}"
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=update_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[resp_date, resp_time, resp_from, resp_body]]}
        ).execute()

    except Exception as e:
        logger.error(f"Sheet response update failed: {e}")

def _update_client_thread_info(job_num: str, client_ts: str, client_channel: str, logger) -> None:
    try:
        values = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="QuestionsLog!A:O"
        ).execute().get("values", []) or []
        if not values:
            return
        target_row_idx_1based = None
        for idx in range(len(values) - 1, 0, -1):
            row = values[idx]
            a_job = (row[0].strip() if len(row) > 0 else "")
            g_resp_date = (row[6].strip() if len(row) > 6 else "")
            if a_job == str(job_num) and not g_resp_date:
                target_row_idx_1based = idx + 1
                break
        if not target_row_idx_1based:
            logger.warning(f"No unanswered row found for job {job_num}; skipping client thread info write.")
            return
        # Write M (Client Root TS) and O (Client Channel ID)
        update_range = f"QuestionsLog!M{target_row_idx_1based}:O{target_row_idx_1based}"
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=update_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[client_ts, client_channel]]}
        ).execute()
    except Exception as e:
        logger.error(f"Sheet client thread write failed: {e}")

@app.event("message")
def message_router(body, client, logger):
    ev = body.get("event", {}) or {}
    if not ev or ev.get("subtype") or ev.get("bot_id"):
        return

    channel   = ev.get("channel")
    text      = (ev.get("text") or "").strip()
    ts        = ev.get("ts")
    thread_ts = ev.get("thread_ts")

    logger.debug(f"[ROUTER] ch={channel} ts={ts} thread_ts={thread_ts} text={text!r}")

    is_internal = channel in INTERNAL_CHANNEL_IDS
    is_client   = _is_client_channel(channel)

    # Internal question handling
    if is_internal:
        m = JOB_TAG_RE.search(text)
        if m:
            job_num = m.group(1)
            user_id = ev.get("user", "")
            user_name = _get_user_name_safe(user_id, logger)
            now = datetime.now()
            request_date = now.strftime("%Y-%m-%d")
            request_time = now.strftime("%H:%M:%S")

            job_name = ""
            monday_item_id = ""
            try:
                joblog = find_job_in_joblog(sheets_service, JOBLOG_SHEET_ID, JOBLOG_RANGE, job_num)
                if joblog:
                    job_name = joblog.get("job_name", "")
                    monday_item_id = joblog.get("monday_item_id", "")
            except Exception as e:
                logger.error(f"Job Log lookup failed: {e}")

            row = [
                job_num,            # A
                job_name,           # B
                request_date,       # C
                request_time,       # D
                user_name,          # E
                text,               # F
                "",                 # G
                "",                 # H
                "",                 # I
                "",                 # J
                monday_item_id,     # K
                ts,                 # L: Internal TS
                "",                 # M: Client Root TS (blank for now)
                channel,            # N: Internal Channel ID
                ""                  # O: Client Channel ID (blank for now)
            ]
            try:
                append_to_sheet(sheets_service, SPREADSHEET_ID, "QuestionsLog!A:O", [row])
            except Exception as e:
                logger.error(f"Sheet append failed: {e}")

            PENDING_INTERNAL[job_num] = (channel, ts)
            logger.info(f"[INTERNAL] logged job {job_num} and cached thread ({channel},{ts})")
        return

    # Client root regurgitation
    if is_client:
        m = JOB_TAG_RE.search(text)
        is_root = (not thread_ts) or (thread_ts == ts)
        if m and is_root:
            job_num = m.group(1)
            internal_pair = PENDING_INTERNAL.get(job_num)
            if not internal_pair:
                logger.warning(f"[REGURG] no cached internal thread for job {job_num}")
                return
            internal_channel, internal_ts = internal_pair
            try:
                save_map(job_num, internal_channel, internal_ts, channel, ts)
                _update_client_thread_info(job_num, ts, channel, logger)
                logger.info(f"[REGURG] saved map for job {job_num}")
            except Exception as e:
                logger.error(f"[REGURG] save_map failed: {e}")
            return

        # Client reply in thread
        if thread_ts:
            mapping = None
            try:
                mapping = by_client_thread(thread_ts)
            except Exception as e:
                logger.error(f"by_client_thread error: {e}")
                return
            if not mapping:
                logger.debug(f"[CLIENT REPLY] no mapping for ({channel},{thread_ts})")
                return

            job_num, internal_channel, internal_ts = mapping
            user_id = ev.get("user", "")
            user_name = _get_user_name_safe(user_id, logger)
            now = datetime.now()
            resp_date = now.strftime("%Y-%m-%d")
            resp_time = now.strftime("%H:%M:%S")

            _update_sheet_response_for_job(job_num, resp_date, resp_time, user_name, text, logger)

            try:
                client.chat_postMessage(
                    channel=internal_channel,
                    thread_ts=internal_ts,
                    text=f"*Client reply* ({user_name})\n{text}"
                )
                logger.info(f"[CLIENT REPLY] echoed to internal thread for job {job_num}")
            except Exception as e:
                logger.error(f"Failed posting back to internal thread: {e}")
        return

@app.event("app_mention")
def _debug_mentions(body, say, logger):
    ev = body.get("event", {}) or {}
    logger.info(f"[MENTION] ch={ev.get('channel')} ts={ev.get('ts')} text={(ev.get('text') or '')!r}")
    say("👋 I can hear you! (app_mention event received)")

@app.event("message")
def _debug_all_messages(body, logger):
    ev = body.get("event", {}) or {}
    logger.debug(f"[DEBUG message] ch={ev.get('channel')} subtype={ev.get('subtype')} bot_id={ev.get('bot_id')} ts={ev.get('ts')} thread_ts={ev.get('thread_ts')} text={(ev.get('text') or '')!r}")

if __name__ == "__main__":
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
