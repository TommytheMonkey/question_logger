from __future__ import annotations
import re
import os
import json
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from datetime import datetime
import base64

base64_creds = os.getenv("GOOGLE_CREDS_BASE64")
if base64_creds and not os.path.exists("google_creds.json"):
    with open("google_creds.json", "wb") as f:
        f.write(base64.b64decode(base64_creds))

_DOC_URL_RE = re.compile(r"(https?://docs\.google\.com/document/d/([a-zA-Z0-9_-]+))")


def get_google_creds():
    creds_file = os.getenv("GOOGLE_CREDS_FILE", "google_creds.json")
    return Credentials.from_service_account_file(
        creds_file,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive",
        ],
    )


def extract_google_doc_link(text: str) -> tuple[str | None, str | None]:
    """
    Find the first Google Doc URL inside arbitrary text from a Monday column.
    Returns (full_url, doc_id) or (None, None).
    """
    if not text:
        return None, None
    m = _DOC_URL_RE.search(text)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def get_drive_service():
    creds = get_google_creds()
    return build("drive", "v3", credentials=creds)


def get_docs_service():
    creds = get_google_creds()
    return build("docs", "v1", credentials=creds)


def get_sheets_service():
    creds = get_google_creds()
    return build("sheets", "v4", credentials=creds)


def doc_id_from_url(url: str) -> str | None:
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def append_to_sheet(sheets_service, spreadsheet_id, sheet_range, values):
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()




def log_revision_to_sheet(sheet_service, job_num, job_name, division, doc_link, monday_id):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = [[job_num, job_name, now, "", "Active", doc_link, monday_id]]
    sheet_service.spreadsheets().values().append(
        spreadsheetId="1Q9FeNSiqfJBY51aidWW5yviYPg855oIEQuGrnoBH8c0",
        range="Active!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def read_range(sheets_service, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
    if not spreadsheet_id:
        raise ValueError("read_range: spreadsheet_id is missing/empty")
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,   # <-- REQUIRED (was missing)
        range=a1_range
    ).execute()
    return resp.get("values", []) or []


def find_job_in_joblog(sheets_service, spreadsheet_id: str, a1_range: str, job_num: str) -> dict | None:
    rows = read_range(sheets_service, spreadsheet_id, a1_range)
    if not rows:
        return None

    headers = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(headers)}

    job_idx     = idx.get("Job #")
    name_idx    = idx.get("Job Name")
    monday_idx  = idx.get("Ewing Board Item ID") or idx.get("Monday ID")
    wl_idx      = idx.get("WLIII Item ID")

    for r in rows[1:]:
        if job_idx is None or job_idx >= len(r):
            continue
        if str(r[job_idx]).strip() == str(job_num):
            return {
                "job_num": r[job_idx].strip(),
                "job_name": (r[name_idx].strip() if name_idx is not None and name_idx < len(r) else ""),
                "monday_item_id": (r[monday_idx].strip() if monday_idx is not None and monday_idx < len(r) else ""),
                "wl_item_id": (r[wl_idx].strip() if wl_idx is not None and wl_idx < len(r) else ""),
                "raw_row": r,
                "headers": headers,
            }
    return None


def append_to_google_doc(docs_service, doc_id, content):
    """Appends plain text content to the end of a Google Doc."""
    try:
        # Fetch the full document
        doc = docs_service.documents().get(documentId=doc_id).execute()
        content_list = doc.get("body", {}).get("content", [])

        # Determine where to insert the new text
        end_index = content_list[-1]["endIndex"] - 1 if content_list else 1

        # Prepare the insert request
        requests = [
            {
                "insertText": {
                    "location": {"index": end_index},
                    "text": content.strip() + "\n\n"  # ensures clean text
                }
            }
        ]

        # Execute the request
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests}
        ).execute()

    except Exception as e:
        print(f"Error appending to Google Doc {doc_id}: {e}")


def append_revision_divider(docs_service, doc_id, triggering_text, rev_num):
    """
    • greys-out + italicises everything that already existed in the doc
    • inserts a bold black ====== divider
    • writes a bold-italic header “REVISION #N – MM/DD/YYYY”
    • appends the triggering Slack message in normal body style
    """
    # ── figure out where to start inserting ──────────────────────────
    doc          = docs_service.documents().get(documentId=doc_id).execute()
    body_content = doc["body"]["content"]
    end_index    = body_content[-1]["endIndex"] - 1 if body_content else 1

    today        = datetime.now().strftime("%m/%d/%Y")
    divider_line = "=" * 72                    # 80 chars wide – looks good on A4/Ltr
    header_text  = f"REVISION #{rev_num} – {today}"

    # we’ll assemble the exact text we’ll drop in and remember the offsets
    text_to_insert  = (
        "\n\n"                      # blank spacer
        + divider_line + "\n\n"     # ======
        + header_text + "\n\n"      # header
        + triggering_text + "\n\n"  # Slack message
    )

    # offsets **inside** the chunk we’re inserting
    divider_rel_start = 2                          # after the “\n\n”
    divider_rel_end   = divider_rel_start + len(divider_line)
    header_rel_start  = divider_rel_end + 2        # skip “\n\n”
    header_rel_end    = header_rel_start + len(header_text)
    msg_rel_start     = header_rel_end + 2         # skip “\n\n”
    msg_rel_end       = msg_rel_start + len(triggering_text)

    # ── build Requests ───────────────────────────────────────────────
    requests = []

    # 1. grey-out everything that’s already there
    if end_index > 1:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": end_index},
                "textStyle": {
                    "italic": True,
                    "foregroundColor": {
                        "color": {"rgbColor": {"red": .5, "green": .5, "blue": .5}}
                    },
                },
                "fields": "italic,foregroundColor",
            }
        })

    # 2. insert our new chunk
    requests.append({
        "insertText": {
            "location": {"index": end_index},
            "text": text_to_insert,
        }
    })

    # absolute offsets (after the insert)
    abs_divider_start = end_index + divider_rel_start
    abs_divider_end   = end_index + divider_rel_end
    abs_header_start  = end_index + header_rel_start
    abs_header_end    = end_index + header_rel_end
    abs_msg_start     = end_index + msg_rel_start
    abs_msg_end       = end_index + msg_rel_end

    # 3. style the divider (bold / black)
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": abs_divider_start, "endIndex": abs_divider_end},
            "textStyle": {"bold": True},
            "fields": "bold",
        }
    })

    # 4. style the header (bold-italic / black / NON-italic)
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": abs_header_start, "endIndex": abs_header_end},
            "textStyle": {
                "bold": True,
                "italic": False,
                "foregroundColor": {
                    "color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}
                }
            },
            "fields": "bold,italic,foregroundColor",
        }
    })

    # 5. ensure the newly added Slack message is **normal** style
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": abs_msg_start, "endIndex": abs_msg_end},
            "textStyle": {
                "italic": False,
                "foregroundColor": {
                    "color": {"rgbColor": {"red": 0, "green": 0, "blue": 0}}
                },
            },
            "fields": "italic,foregroundColor",
        }
    })

    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()
