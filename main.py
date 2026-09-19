from jose import jwt, JWTError
from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, Query
from fastapi import Request
import os
import time
import sqlite3
import json
import smtplib
import stripe
from email.mime.text import MIMEText
from openai import OpenAI
from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/upload")
async def upload(description: str = Form(...), software: str = Form(...), customer_email: str = Form(...)):
    return JSONResponse({"status": "ok", "description": description, "software": software, "customer_email": customer_email})

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = FastAPI()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"

REQUEST_LOG = []
MAX_REQUESTS_PER_MINUTE = 5

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
MODELS_DIR = os.path.join(os.path.dirname(__file__), "uploads", "models")
FINAL_MODELS_DIR = os.path.join(os.path.dirname(__file__), "uploads", "final_models")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MODELS_DIR, exist_ok=True)
os.makedirs(FINAL_MODELS_DIR, exist_ok=True)

CAD_KEYWORDS = {
    "solidworks": ["extrude", "cut", "fillet", "chamfer", "loft", "sweep", "shell"],
    "nx": ["sketch", "extrude", "revolve", "pattern", "draft", "blend"],
    "autocad": ["line", "circle", "arc", "trim", "extend", "offset", "fillet", "dimensions", "polyline"]
}

GEOMETRY_KEYWORDS = {
    "cylinder": ["cylinder", "round", "pipe", "tube", "shaft"],
    "block": ["block", "cube", "rectangular", "plate", "housing"],
    "bracket": ["bracket", "mount", "support", "arm"],
    "hole": ["hole", "bore", "drill", "opening"],
    "slot": ["slot", "channel", "groove", "cutout"],
    "thread": ["thread", "screw", "bolt", "nut", "fastener"]
}

TUTORIAL_TEMPLATES = {
    "solidworks": {
        "cylinder": [
            "Start a new sketch on the Front plane.",
            "Draw a circle with the specified diameter.",
            "Exit the sketch and use the extrude feature to create a cylinder with the specified height.",
            "Apply fillets or chamfers if specified."
        ],
        "block": [
            "Start a new sketch on the Top plane.",
            "Draw a rectangle using the given width and length.",
            "Exit the sketch and Extrude to the specified height.",
            "Add holes or slots using the Cut-Extrude feature."
        ],
        "bracket": [
            "Sketch the base profile on the Top Plane.",
            "Extrude the base.",
            "Sketch the vertical arm on the side face of the base.",
            "Extrude the arm.",
            "Add mounting holes using the Cut-Extrude feature as needed."
        ]
    },
    "nx": {
        "cylinder": [
            "Create a new sketch on the XY plane.",
            "Draw a circle with the required diameter.",
            "Finish the sketch and use the extrude command.",
            "Set the extrusion height to the specified value.",
            "Add blends or chamfers if needed."
        ],
        "block": [
            "Sketch a rectangle on the XY plane.",
            "Finish the sketch.",
            "Use the Extrude to create a block.",
            "Add holes or slots using the cut feature as specified.",
        ]
    }
}


def get_tutorial_template(software: str, geometry_type: str):
    software = software.lower()
    if software in TUTORIAL_TEMPLATES:
        if geometry_type in TUTORIAL_TEMPLATES[software]:
            return TUTORIAL_TEMPLATES[software][geometry_type]
    return None


def is_description_specific(text: str) -> bool:
    if len(text.strip()) < 20:
        return False

    keywords = [
        "mm", "cm", "inches", "dimensions", "size", "length", "width", "height",
        "diameter", "radius", "hole", "slot", "bracket", "mount", "tolerance",
        "fit", "clearance", "thread", "angle"
    ]
    matches = sum(1 for k in keywords if k in text.lower())
    return matches >= 2


def classify_geometry(description: str) -> str:
    text = description.lower()
    for shape, keywords in GEOMETRY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return shape
    return "unknown"


def geometry_feasibility(description: str) -> bool:
    shape = classify_geometry(description)
    if shape == "unknown":
        return False
    return is_description_specific(description)


def run_ai_interpretation(description, geometry_type, software):
    try:
        ai_raw = call_ai_model(description, geometry_type, software)
        ai_json = json.loads(ai_raw)
        return ai_json
    except:
        return {
            "geometry_plan": build_ai_geometry_plan(geometry_type, description),
            "feature_plan": list(build_ai_feature_plan(geometry_type)),
            "tutorial_plan": list(build_ai_tutorial_plan(software, geometry_type))
        }


def call_ai_model(description, geometry_type, software):
    prompt = f"""
You are an expert CAD engineer specializing in SolidWorks, Siemens NX, and AutoCAD.
Interpret the following part description and produce:

1. A geometry plan
2. A feature plan
3. A tutorial plan

Description: {description}
Geometry Type: {geometry_type}
Software: {software}

Respond in JSON with keys:
- geometry_plan
- feature_plan
- tutorial_plan
    """

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )

    return response.choices[0].message.content


def build_ai_geometry_plan(geometry_type: str, description: str):
    return {
        "geometry_type": geometry_type,
        "primary_dimensions": description.lower(),  # placeholder
        "key_features": [],
        "complexity": "unknown",
        "confidence": 0.0
    }


def build_ai_feature_plan(geometry_type: str):
    feature_map = {
        "cylinder": ["sketch circle", "extrude", "fillet"],
        "block": ["sketch rectangle", "extrude", "cut-extrude"],
        "bracket": ["sketch base", "extrude", "sketch arm", "extrude", "cut holes"],
        "hole": ["sketch point", "cut-extrude"],
        "slot": ["sketch slot", "cut-extrude"],
        "thread": ["sketch circle", "extrude", "thread tool"]
    }
    return feature_map.get(geometry_type, [])


def build_ai_tutorial_plan(software: str, geometry_type: str):
    steps = get_tutorial_template(software, geometry_type)
    return steps if steps else []


DB_PATH = "orders.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            description_raw TEXT,
            description_clean TEXT,
            dimensions_raw TEXT,
            software_raw TEXT,
            software_normalized TEXT,
            geometry_type TEXT,
            feasible INTEGER,
            file_uploaded TEXT,
            ai_geometry_plan TEXT,
            ai_feature_plan TEXT,
            ai_tutorial_plan TEXT,
            customer_email TEXT,
            confirmation_message TEXT,
            ai_model_file TEXT,
            final_model_file TEXT,
            order_status TEXT,
            admin_notes TEXT,
            rough_model_paid INTEGER,
            final_model_paid INTEGER,
            payment_intent_id TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def save_order_to_db(order):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO orders (
            timestamp,
            description_raw,
            description_clean,
            dimensions_raw,
            software_raw,
            software_normalized,
            geometry_type,
            feasible,
            file_uploaded,
            ai_geometry_plan,
            ai_feature_plan,
            ai_tutorial_plan,
            customer_email,
            confirmation_message,
            ai_model_file,
            final_model_file,
            order_status,
            admin_notes,
            rough_model_paid,
            final_model_paid,
            payment_intent_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order["timestamp"],
        order["description_raw"],
        order["description_clean"],
        order["dimensions_raw"],
        order["software_raw"],
        order["software_normalized"],
        order["geometry_type"],
        int(order["feasible"]),
        order["file_uploaded"],
        json.dumps(order["ai_geometry_plan"]),
        json.dumps(order["ai_feature_plan"]),
        json.dumps(order["ai_tutorial_plan"]),
        order["customer_email"],
        order["confirmation_message"],
        order["ai_model_file"],
        order["final_model_file"],
        order["order_status"],
        order["admin_notes"],
        order["rough_model_paid"],
        order["final_model_paid"],
        order["payment_intent_id"]
    ))
    conn.commit()
    conn.close()

from pathlib import Path

DB_PATH = Path(__file__).parent / "tokens.db"

def init_token_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NUL,
                created_at TIMESTAMPT DEFAULT CURENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

init_token_db()

import secrets

def generate_download_token(order_id: int, hours_valid: int=24):
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + (hours_valid * 3600)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO download_tokens (token, order_id, expires_at, used)
        VALUES (?, ?, ?, 0)
    """, (token, order_id, expires_at))
    conn.commit()
    conn.close()

    return token

def process_order(description, dimensions, software, file, geometry_type, feasible, customer_email):
    ai_output = run_ai_interpretation(description, geometry_type, software)

    order = {
        "description_raw": description,
        "description_clean": description.lower().strip(),
        "dimensions_raw": dimensions,
        "software_raw": software,
        "software_normalized": software.lower().replace(" ", "_"),
        "geometry_type": geometry_type,
        "feasible": feasible,
        "file_uploaded": file.filename if file else None,
        "ai_geometry_plan": ai_output["geometry_plan"],
        "ai_feature_plan": ai_output["feature_plan"],
        "ai_tutorial_plan": ai_output["tutorial_plan"],
        "order_id": None,
        "timestamp": time.time(),
        "customer_email": customer_email,
        "confirmation_message": None,
        "ai_model_file": None,
        "final_model_file": None,
        "order_status": "pending",
        "admin_notes": None,
        "rough_model_paid": 0,
        "final_model_paid": 0,
        "payment_intent_id": None
    }

    return order


def send_email(to_email: str, subject: str, body: str):
    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    sender_email = "cad.workshop.tutorials@gmail.com"
    sender_password = os.getenv("EMAIL_APP_PASSWORD")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_email

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print("Email error:", e)
        return False


def get_order(order_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    row = cursor.fetchone()

    conn.close()
    return row


def list_orders():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM orders ORDER BY order_id DESC")
    rows = cursor.fetchall()

    conn.close()
    return rows


@app.post("/upload")
async def upload_request(
    description: str = Form(...),
    dimensions: str = Form(...),
    software: str = Form(...),
    customer_email: str = Form(None),
    file: UploadFile = File(None)
):
    current_time = time.time()
    REQUEST_LOG.append(current_time)
    REQUEST_LOG[:] = [t for t in REQUEST_LOG if current_time - t < 60]

    if len(REQUEST_LOG) > MAX_REQUESTS_PER_MINUTE:
        return {
            "status": "error",
            "message": "Too many requests. Please wait a moment before submitting another order."
        }

    if not is_description_specific(description):
        if not (file and file.filename):
            return {
                "status": "error",
                "message": "Retry order form. Make sure the description is specific or upload an image."
            }

    shape = classify_geometry(description)
    feasible = geometry_feasibility(description)
    tutorial_steps = get_tutorial_template(software, shape)

    order_packet = process_order(
        description,
        dimensions,
        software,
        file,
        shape,
        feasible,
        customer_email
    )

    saved_filename = None
    if file and file.filename:
        file_path = os.path.join(UPLOAD_DIR, file.filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())

        saved_filename = file.filename
        order_packet["file_uploaded"] = saved_filename

    save_order_to_db(order_packet)

    if order_packet["customer_email"]:
        send_email(
            order_packet["customer_email"],
            "Your CAD Order Confirmation",
            "Thank you for your order. Processing request."
        )

    send_email(
        "cad.workshop.tutorials@gmail.com",
        "New CAD Order Received",
        json.dumps(order_packet, indent=2)
    )

    return {
        "status": "success",
        "message": "Order saved. Thank you.",
        "saved_file": saved_filename,
        "geometry_type": shape,
        "ai_ready": bool(feasible),
        "tutorial_steps": tutorial_steps,
        "order": order_packet,
        "ai_geometry_plan": order_packet["ai_geometry_plan"],
        "ai_feature_plan": order_packet["ai_feature_plan"],
        "ai_tutorial_plan": order_packet["ai_tutorial_plan"],
        "note": "BE SPECIFIC: include dimensions, features, and shape details."
    }


def admin_required(token: str = Query(...)):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Not authorized.")
    except JWTError:
        raise HTTPException(status_code=403, detail="Invalid token.")

def validate_download_token(order_id: int, token: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT token, expires_at, used FROM download_tokens
        WHERE token = ? AND order_id = ?
    """, (token, order_id))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return False

    token_value, expires_at, used = row

    if used:
        return False

    if time.time() > expires_at:
        return False

    return True

def mark_token_used(token: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE download_tokens SET used = 1 WHERE token = ?
    """, (token,))
    conn.commit()
    conn.close()

@app.get("/admin/orders")
def admin_list_orders(token: str = Depends(admin_required)):
    rows = list_orders()
    return [format_order(r) for r in rows]


@app.get("/admin/order/{order_id}")
def admin_get_order(order_id: int, token: str = Depends(admin_required)):
    row = get_order(order_id)
    return format_order(row)

from geometry_generator import generate_cylinder, generate_block, generate_bracket

@app.post("/admin/generate_model/{order_id}")
def admin_generate_model(order_id: int, token: str = Depends(admin_required)):
    order = get_order(order_id)
    if not order:
        return {"status": "error", "message": "Order not found."}

    geometry_type = order[7]
    dims = order[4]

    dims = dims.lower(). replace("mm", "").replace(" ", "")
    numbers= [float(x) for x in dims.split(", ")]

    if geometry_type == "cylinder":
        mesh = generate_cylinder(numbers[0], numbers[1])
    elif geometry_type == "block":
        mesh = generate_block(numbers[0], numbers[1], numbers[2])
    elif geometry_type == "braket":
        mesh = generate_bracket(numbers[0], numbers[1], numbers[2])
    else: 
        return {"status": "error", "message": "Unsupported geometry type."}

    filename = f"order_{order_id}_rough.stl"
    file_path = os.path.join(MODELS_DIR, filename)
    mesh.export(file_path)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders
        SET ai_model_file = ?, order_status = ?
        WHERE order_id = ?
    """, (filename, "ai_generated", order_id))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "message": "AI rough model generated (placeholder).",
        "ai_model_file": filename
    }


@app.post("/admin/upload_final_model/{order_id}")
async def admin_upload_final_model(
    order_id: int,
    file: UploadFile = File(...),
    token: str = Depends(admin_required)
):
    order = get_order(order_id)
    if not order:
        return {"status": "error", "message": "Order not found."}

    file_path = os.path.join(FINAL_MODELS_DIR, file.filename)
    with open(file_path, "wb") as f:
        f.write(await file.read())

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders
        SET final_model_file = ?, order_status = ?
        WHERE order_id = ?
    """, (file.filename, "final_complete", order_id))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "message": "Final model uploaded.",
        "final_model_file": file.filename
    }


@app.post("/admin/update_status/{order_id}")
def admin_update_status(
    order_id: int,
    new_status: str = Form(...),
    token: str = Depends(admin_required)
):
    valid_statuses = [
        "pending",
        "ai_generated",
        "final_complete",
        "rejected",
        "needs_revision",
        "awaiting_payment"
    ]

    if new_status not in valid_statuses:
        return {"status": "error", "message": "Invalid status."}

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders
        SET order_status = ?
        WHERE order_id = ?
    """, (new_status, order_id))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "message": f"Order {order_id} updated to '{new_status}'."
    }


@app.post("/admin/login")
def admin_login(password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        return {"status": "error", "message": "Invalid admin password."}

    token = jwt.encode({"role": "admin"}, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"status": "success", "token": token}


@app.post("/admin/add_note/{order_id}")
def admin_add_note(
    order_id: int,
    note: str = Form(...),
    token: str = Depends(admin_required)
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders
        SET admin_notes = ?
        WHERE order_id = ?
    """, (note, order_id))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "message": "Note added.",
        "note": note
    }


@app.get("/admin/orders_by_status/{status}")
def admin_orders_by_status(
    status: str,
    token: str = Depends(admin_required)
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM orders WHERE order_status = ?
        ORDER BY order_id DESC
    """, (status,))
    rows = cursor.fetchall()
    conn.close()

    return [format_order(r) for r in rows]


def format_order(row):
    if not row:
        return None

    return {
        "order_id": row[0],
        "timestamp": row[1],
        "description_raw": row[2],
        "description_clean": row[3],
        "dimensions_raw": row[4],
        "software_raw": row[5],
        "software_normalized": row[6],
        "geometry_type": row[7],
        "feasible": bool(row[8]),
        "file_uploaded": row[9],
        "ai_geometry_plan": json.loads(row[10]),
        "ai_feature_plan": json.loads(row[11]),
        "ai_tutorial_plan": json.loads(row[12]),
        "customer_email": row[13],
        "confirmation_message": row[14],
        "ai_model_file": row[15],
        "final_model_file": row[16],
        "order_status": row[17],
        "admin_notes": row[18],
        "rough_model_paid": row[19],
        "final_model_paid": row[20],
        "payment_intent_id": row[21]
    }

@app.post("/payment/create_intent/{order_id}")
def create_payment_intent(order_id: int, model_type: str = Form(...)):
    """
    model_type = "rough" or "final"
    """

    row = get_order(order_id)
    if not row:
        return {"status": "error", "message": "Order not found."}

    if model_type == "rough":
        amount = 500
    elif model_type == "final":
        amount = 1500
    else:
        return {"status": "error", "message": "Invalid model type."}

    intent = stripe.PaymentIntent.create(
        amount=amount,
        currency="usd",
        metadata={"order_id": order_id, "model_type": model_type}
    )

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE order SET payment_intent_id = ? WHERE order_id = ?
    """, (intent.id, order_id))
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id
    }

@app.post("/payment/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}

    if event ["type"] == "payment_intent.succeeded":
        intent = event["data"]["object"]
        order_id = intent["metadata"]["order_id"]
        model_type = intent["metadata"]["model_type"]

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        if model_type == "rough":
            cursor.execute("""
                UPDATE orders SET rough_model_paid = 1 WHERE order_id = ?
            """, (order_id,))
        elif model_type == "final":
            cursor.execute("""
                UPDATE orders SET final_model_paid = 1 WHERE order_id = ?
            """, (order_id,))

        conn.commet()
        conn.close()

        token = generate_download_token(order_id)

        row = get_order(order_id)
        customer_email = row[13]

        if customer_email:
            send_email(
                customer_email,
                "Your CAD Model is Ready",
                f"Download link: https://cad.workshop.tutorials.com/download/final_model/{order_id}?token={token}"
            )

    return {"status": "success"}

@app.post("/admin?generate_download_token/{order_id}")
def admin_generate_download_token(order_id: int, token: str = Depends(admin_required)):
    row = get_order(order_id)
    if not row:
        return {"status": "error", "message": "Order not found."}

    token = generate_download_token(order_id)

    return {
        "status": "success",
        "download_token": token,
        "download_link": f"https://cad.workshop.tutorials.com/download/final_model/{order_id}?token={token}"

    }

from fastapi.responses import FileResponse

@app.get("/download/uploaded/{order_id}")
def download_uploaded(order_id: int):
    row = get_order(order_id)
    if not row:
        return {"status": "error", "message": "Order not found."}

    filename = row[9]  # file_uploaded
    if not filename:
        return {"status": "error", "message": "No uploaded file for this order."}

    file_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(file_path):
        return {"status": "error", "message": "File missing on server."}

    return FileResponse(file_path, filename=filename)

@app.get("/download/ai_model/{order_id}")
def download_ai_model(order_id: int):
    row = get_order(order_id)
    if not row:
        return {"status": "error", "message": "Order not found."}

    filename = row[15]
    if not filename:
        return {"status": "error", "message": "No AI model generated for this order."}

    file_path = os.path.join(MODELS_DIR, filename)
    if not os.path.exists(file_path):
        return {"status": "error", "message": "AI model file missing on server."}

    return FileResponse(file_path, filename=filename)

@app.get("/download/final_model/{order_id}")
def download_final_model(order_id: int, token: str):
    if not validate_download_token(order_id, token):
        return {"status": "error", "message": "Invalid or expired token."}
    
    row = get_order(order_id)
    filename = row[16]
    file_path = os.path.join(FINAL_MODELS_DIR, filename)

    mark_token_used(token)

    return FileResponse(file_path, filename=filename)

print("OpenAI key loaded:", os.getenv("OPENAI_API_KEY") is not None)
print("Email password loaded:", os.getenv("EMAIL_APP_PASSWORD") is not None)
