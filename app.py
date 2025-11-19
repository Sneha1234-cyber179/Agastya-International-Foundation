# app.py
import os
import datetime
import re
from bson import ObjectId
from flask import Flask, render_template, request, jsonify, send_from_directory
from pymongo import MongoClient
import smtplib
from email.message import EmailMessage
from werkzeug.utils import secure_filename

# ---------- Configuration (use env vars in production) ----------
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/vendor_db")
DB_NAME = os.environ.get("DB_NAME", "vendor_db")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "program_team")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")  # for LR files - on Render use S3 or similar
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "webp", "gif"}

EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "manjunathmeti@agastya.org")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", None)  # set as env var on Render

# ---------- App init ----------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------- MongoDB init ----------
client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

# ---------- Utilities ----------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def is_valid_email(email: str) -> bool:
    if not email:
        return False
    return re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email) is not None

def send_confirmation_email(recipient: str, subject: str, body: str):
    """Send via Gmail SMTP. EMAIL_PASSWORD must be set as env var."""
    if not EMAIL_PASSWORD:
        app.logger.warning("EMAIL_PASSWORD not set — skipping email.")
        return False
    try:
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

# ---------- Routes ----------
@app.route("/")
def index():
    # main page (serves HTML + JS which fetches records via API)
    return render_template("index.html")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

# API: list all records
@app.route("/api/records", methods=["GET"])
def api_records():
    docs = list(collection.find().sort("_id", -1))  # latest first
    # convert ObjectId to string and keep fields in predictable order
    out = []
    for d in docs:
        out.append({
            "id": str(d.get("_id")),
            "region": d.get("region", ""),
            "version": d.get("version", ""),
            "language": d.get("language", ""),
            "quantity": d.get("quantity", ""),
            "grade": d.get("grade", ""),
            "total": d.get("total", ""),
            "poc": d.get("poc", ""),
            "lr": d.get("lr", ""),
            "created_at": d.get("created_at").isoformat() if d.get("created_at") else ""
        })
    return jsonify({"ok": True, "records": out})

# API: save new record
@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.form.to_dict()
    # validation
    required = ["region", "version", "language", "quantity", "grade", "total", "poc"]
    for r in required:
        if not data.get(r):
            return jsonify({"ok": False, "error": f"{r} is required"}), 400
    if not data["quantity"].isdigit() or not data["grade"].isdigit() or not data["total"].isdigit():
        return jsonify({"ok": False, "error": "Quantity/Grade/Total must be numeric"}), 400
    if not is_valid_email(data["poc"]):
        return jsonify({"ok": False, "error": "POC must be a valid email"}), 400

    # handle LR file (optional)
    lr_link = data.get("lr", "")
    if "lrfile" in request.files:
        f = request.files["lrfile"]
        if f and allowed_file(f.filename):
            filename = secure_filename(f"{datetime_filename_prefix()}_{f.filename}")
            path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            f.save(path)
            # accessible URL
            lr_link = "/uploads/" + filename

    doc = {
        "region": data["region"],
        "version": data["version"],
        "language": data["language"],
        "quantity": data["quantity"],
        "grade": data["grade"],
        "total": data["total"],
        "poc": data["poc"],
        "lr": lr_link,
        "created_at": datetime.datetime.utcnow()
    }
    res = collection.insert_one(doc)

    # send confirmation email (non-blocking would be better; we keep simple)
    subject = "Program Team – Data Saved Successfully"
    body = email_body_from_doc(doc, saved=True)
    send_confirmation_email(doc["poc"], subject, body)

    return jsonify({"ok": True, "id": str(res.inserted_id)})

# API: update record
@app.route("/api/update/<id>", methods=["POST"])
def api_update(id):
    # validation and update
    data = request.form.to_dict()
    if not ObjectId.is_valid(id):
        return jsonify({"ok": False, "error": "Invalid id"}), 400
    # simple validation
    if not data.get("quantity", "").isdigit() or not data.get("grade", "").isdigit() or not data.get("total", "").isdigit():
        return jsonify({"ok": False, "error": "Quantity/Grade/Total must be numeric"}), 400
    if not is_valid_email(data.get("poc", "")):
        return jsonify({"ok": False, "error": "POC must be a valid email"}), 400

    # potential LR upload
    lr_link = data.get("lr", "")
    if "lrfile" in request.files:
        f = request.files["lrfile"]
        if f and allowed_file(f.filename):
            filename = secure_filename(f"{datetime_filename_prefix()}_{f.filename}")
            path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            f.save(path)
            lr_link = "/uploads/" + filename

    update_doc = {
        "region": data.get("region"),
        "version": data.get("version"),
        "language": data.get("language"),
        "quantity": data.get("quantity"),
        "grade": data.get("grade"),
        "total": data.get("total"),
        "poc": data.get("poc"),
        "lr": lr_link,
        "updated_at": datetime.datetime.utcnow()
    }
    collection.update_one({"_id": ObjectId(id)}, {"$set": update_doc})

    subject = "Program Team – Data Updated Successfully"
    body = email_body_from_doc(update_doc, saved=False)
    send_confirmation_email(update_doc["poc"], subject, body)

    return jsonify({"ok": True})

# helper functions used above
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

# ---------- Run ----------
if __name__ == "__main__":
    # debug mode only for local testing
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
