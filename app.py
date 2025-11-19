# app.py
import os
import json
import datetime
import re
import tempfile
from email.message import EmailMessage
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------- Config ----------
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "webp", "gif"}
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "manjunathmeti@agastya.org")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", None)  # Gmail app password
SHEET_ID = os.environ.get("SHEET_ID", None)  # required
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", None)  # optional: upload folder in Drive

# The Google Service Account JSON should be stored in env var GOOGLE_SERVICE_ACCOUNT_JSON
# as the literal JSON text (Render secrets recommended).
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", None)

if not SERVICE_ACCOUNT_JSON:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable not set.")

# ---------- Flask init ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
UPLOAD_TMP_DIR = tempfile.gettempdir()

# ---------- Google Auth & clients ----------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
]

def get_credentials():
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return creds

def get_gspread_sheet():
    creds = get_credentials()
    gc = gspread.authorize(creds)
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID env var not set.")
    sh = gc.open_by_key(SHEET_ID)
    # Use first worksheet
    ws = sh.sheet1
    return ws

def get_drive_service():
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return drive

# ---------- Utilities ----------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def is_valid_email(email: str) -> bool:
    if not email:
        return False
    return re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email) is not None

def send_confirmation_email(recipient: str, subject: str, body: str):
    if not EMAIL_PASSWORD:
        app.logger.warning("EMAIL_PASSWORD not set — skipping email.")
        return False
    try:
        import smtplib
        msg = EmailMessage()
        msg["From"] = EMAIL_SENDER
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)

        smtp = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
        smtp.ehlo()
        smtp.starttls()
        smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
        smtp.send_message(msg)
        smtp.quit()
        return True
    except Exception as e:
        app.logger.error("Email send failed: %s", e)
        return False

def datetime_filename_prefix():
    return datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def email_body_from_doc(d, saved=True):
    op = "saved" if saved else "updated"
    return (
        f"Dear recipient,\n\n"
        f"A ProgramTeam entry has been {op}.\n\n"
        f"Details:\n"
        f"Region: {d.get('region')}\n"
        f"Version: {d.get('version')}\n"
        f"Language: {d.get('language')}\n"
        f"Quantity: {d.get('quantity')}\n"
        f"Grade: {d.get('grade')}\n"
        f"Total: {d.get('total')}\n"
        f"LR file: {d.get('lr')}\n\n"
        f"Regards,\nAgastya International Foundation\n"
    )

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/records", methods=["GET"])
def api_records():
    ws = get_gspread_sheet()
    # get all records as list of dicts (requires header row)
    try:
        records = ws.get_all_records()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed reading sheet: {e}"}), 500
    # ensure created_at is ISO formatted if present; otherwise keep as-is
    for r in records:
        if isinstance(r.get("created_at"), datetime.datetime):
            r["created_at"] = r["created_at"].isoformat()
    return jsonify({"ok": True, "records": records})

@app.route("/api/save", methods=["POST"])
def api_save():
    ws = get_gspread_sheet()
    data = request.form.to_dict()

    required = ["region", "version", "language", "quantity", "grade", "total", "poc"]
    for r in required:
        if not data.get(r):
            return jsonify({"ok": False, "error": f"{r} is required"}), 400
    if not data["quantity"].isdigit() or not data["grade"].isdigit() or not data["total"].isdigit():
        return jsonify({"ok": False, "error": "Quantity/Grade/Total must be numeric"}), 400
    if not is_valid_email(data["poc"]):
        return jsonify({"ok": False, "error": "POC must be a valid email"}), 400

    lr_link = data.get("lr", "")

    # handle LR file upload and push to Drive
    if "lrfile" in request.files:
        f = request.files["lrfile"]
        if f and allowed_file(f.filename):
            filename = secure_filename(f"{datetime_filename_prefix()}_{f.filename}")
            tmp_path = os.path.join(UPLOAD_TMP_DIR, filename)
            f.save(tmp_path)
            try:
                drive = get_drive_service()
                file_metadata = {"name": filename}
                if DRIVE_FOLDER_ID:
                    file_metadata["parents"] = [DRIVE_FOLDER_ID]
                media = MediaFileUpload(tmp_path, resumable=False)
                created = drive.files().create(body=file_metadata, media_body=media, fields="id").execute()
                file_id = created.get("id")
                # make it viewable by anyone with link
                drive.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()
                lr_link = f"https://drive.google.com/uc?id={file_id}"
            except Exception as e:
                app.logger.error("Drive upload failed: %s", e)
                # keep lr_link empty or fallback
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    # create a unique id for the row (timestamp-based)
    row_id = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    created_at = datetime.datetime.utcnow().isoformat()

    # ensure sheet has headers. Required columns:
    # id, region, version, language, quantity, grade, total, poc, lr, created_at
    headers = ["id", "region", "version", "language", "quantity", "grade", "total", "poc", "lr", "created_at"]
    # read header row (row 1)
    existing_headers = ws.row_values(1)
    if not existing_headers or set(headers).issubset(set(headers)) and existing_headers != headers:
        # if first row is empty or not matching, set headers (safe to set)
        try:
            ws.update("A1:J1", [headers])
        except Exception:
            pass

    row = [
        row_id,
        data["region"],
        data["version"],
        data["language"],
        data["quantity"],
        data["grade"],
        data["total"],
        data["poc"],
        lr_link,
        created_at,
    ]
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to append row: {e}"}), 500

    # send confirmation email
    subject = "Program Team – Data Saved Successfully"
    doc = dict(zip(headers, row))
    send_confirmation_email(doc["poc"], subject, email_body_from_doc(doc, saved=True))

    return jsonify({"ok": True, "id": row_id})

@app.route("/api/update/<row_id>", methods=["POST"])
def api_update(row_id):
    ws = get_gspread_sheet()
    data = request.form.to_dict()

    if not data.get("quantity", "").isdigit() or not data.get("grade", "").isdigit() or not data.get("total", "").isdigit():
        return jsonify({"ok": False, "error": "Quantity/Grade/Total must be numeric"}), 400
    if not is_valid_email(data.get("poc", "")):
        return jsonify({"ok": False, "error": "POC must be a valid email"}), 400

    lr_link = data.get("lr", "")
    # possible new file
    if "lrfile" in request.files:
        f = request.files["lrfile"]
        if f and allowed_file(f.filename):
            filename = secure_filename(f"{datetime_filename_prefix()}_{f.filename}")
            tmp_path = os.path.join(UPLOAD_TMP_DIR, filename)
            f.save(tmp_path)
            try:
                drive = get_drive_service()
                file_metadata = {"name": filename}
                if DRIVE_FOLDER_ID:
                    file_metadata["parents"] = [DRIVE_FOLDER_ID]
                media = MediaFileUpload(tmp_path, resumable=False)
                created = drive.files().create(body=file_metadata, media_body=media, fields="id").execute()
                file_id = created.get("id")
                drive.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()
                lr_link = f"https://drive.google.com/uc?id={file_id}"
            except Exception as e:
                app.logger.error("Drive upload failed: %s", e)
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    # find the row containing id == row_id (search column A)
    try:
        cell = ws.find(row_id)
    except Exception:
        return jsonify({"ok": False, "error": "Record not found"}), 404

    # prepare update row values in order of headers
    headers = ws.row_values(1)
    # map provided fields to header indices
    updated = {
        "region": data.get("region"),
        "version": data.get("version"),
        "language": data.get("language"),
        "quantity": data.get("quantity"),
        "grade": data.get("grade"),
        "total": data.get("total"),
        "poc": data.get("poc"),
        "lr": lr_link,
        "updated_at": datetime.datetime.utcnow().isoformat()
    }

    # write each field in correct column
    base_row_num = cell.row
    for col_idx, header in enumerate(headers, start=1):
        if header in updated:
            try:
                ws.update_cell(base_row_num, col_idx, updated[header])
            except Exception as e:
                app.logger.error("Failed updating cell %s:%s -> %s", base_row_num, col_idx, e)

    # send update email
    subject = "Program Team – Data Updated Successfully"
    send_confirmation_email(updated["poc"], subject, email_body_from_doc(updated, saved=False))

    return jsonify({"ok": True})

# ---------- Static uploads route (not used for Drive but left for compatibility) ----------
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    # fallback if you want to serve any temporarily saved files (not recommended)
    return send_from_directory(UPLOAD_TMP_DIR, filename)

# ---------- Run ----------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
