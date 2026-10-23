import os
import sys
import uuid
import base64
import json
import re
import threading
from io import BytesIO
from datetime import datetime
from flask import (
    Flask, request, jsonify, render_template, send_file, Response,
    session, redirect, url_for, flash
)
from dotenv import load_dotenv
from sqlalchemy import or_, func

# Ensure root and current directory are in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
for p in [BASE_DIR, CURRENT_DIR, os.getcwd()]:
    if p and p not in sys.path:
        sys.path.insert(0, p)

load_dotenv(os.path.join(BASE_DIR, ".env"))

from models import (
    db, get_database_uri, User, Complaint, PriorityLevel, TicketStatus, init_db_schema,
    Worker, WORKERS_DATASET
)

# Robust template & static directory resolution for local & Vercel serverless
template_dir = os.path.join(BASE_DIR, "templates")
if not os.path.isdir(template_dir):
    template_dir = os.path.join(CURRENT_DIR, "templates")
if not os.path.isdir(template_dir):
    template_dir = os.path.join(os.getcwd(), "templates")

static_dir = os.path.join(BASE_DIR, "static")
if not os.path.isdir(static_dir):
    static_dir = os.path.join(CURRENT_DIR, "static")
if not os.path.isdir(static_dir):
    static_dir = os.path.join(os.getcwd(), "static")

app = Flask(
    __name__,
    template_folder=template_dir,
    static_folder=static_dir
)

# App Configuration
app.config["SQLALCHEMY_DATABASE_URI"] = get_database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": 10,
    "max_overflow": 20
}
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "awamconnect-secret-key-prod-2026")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB max upload

db.init_app(app)

# Ensure database tables and columns exist on startup
init_db_schema(app)


# Context Processor to make current_user available to all Jinja templates
@app.context_processor
def inject_user():
    user = None
    if "user_id" in session:
        try:
            user = db.session.get(User, session["user_id"])
        except Exception:
            user = None
    return dict(current_user=user)


# Try importing google-genai SDK
GENAI_AVAILABLE = False
try:
    from google import genai
    from google.genai import types
    from PIL import Image
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


def is_urdu_text(text: str) -> bool:
    """Checks if string contains Arabic/Urdu unicode characters."""
    return bool(re.search(r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]', text))


def run_with_timeout(func, args=(), kwargs=None, timeout=3.5):
    """Executes a function in a daemon thread with a strict timeout to prevent hangs."""
    if kwargs is None:
        kwargs = {}
    result = [None]
    error = [None]

    def worker():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return None, TimeoutError(f"Operation timed out after {timeout} seconds")
    return result[0], error[0]


def select_worker_by_issue(description: str, detected_issue: str = "", target_agency: str = "") -> dict:
    """
    Selects the most suitable worker from the dataset according to the complaint type.
    """
    desc_lower = (description or "").lower()
    issue_lower = (detected_issue or "").lower()
    agency_lower = (target_agency or "").lower()
    full_text = f"{desc_lower} {issue_lower} {agency_lower}"

    # Keyword sets
    sewage_keys = ["sewage", "sewer", "gutter", "manhole", "drain", "water burst", "flood", "leak", "گٹر", "سیوریج", "پائپ", "پانی"]
    electric_keys = ["electric", "wire", "cable", "spark", "current", "shock", "voltage", "transformer", "pole", "streetlamp", "streetlight", "بجلی", "تار", "شارٹ سرکٹ"]
    road_keys = ["pothole", "road", "cavity", "asphalt", "sidewalk", "street", "sinkhole", "crater", "سڑک", "کھڈا", "روڈ"]
    garbage_keys = ["garbage", "trash", "waste", "kuda", "dump", "debris", "refuse", "کچرا", "کوڑا", "غلاظت"]

    # 1. Primary check strictly on citizen's complaint words
    if any(k in desc_lower for k in sewage_keys):
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Sewerage Worker"]
        return candidates[0] if candidates else WORKERS_DATASET[0]

    if any(k in desc_lower for k in electric_keys):
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Electrician Worker"]
        return candidates[0] if candidates else WORKERS_DATASET[6]

    if any(k in desc_lower for k in road_keys):
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Road Builder"]
        return candidates[0] if candidates else WORKERS_DATASET[3]

    if any(k in desc_lower for k in garbage_keys):
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Garbage Collector"]
        return candidates[0] if candidates else WORKERS_DATASET[12]

    # 2. Secondary check across detected issue and target agency
    if any(k in full_text for k in sewage_keys) or "kw&sc" in full_text:
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Sewerage Worker"]
        return candidates[0] if candidates else WORKERS_DATASET[0]

    if any(k in full_text for k in electric_keys) or "k-electric" in full_text:
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Electrician Worker"]
        return candidates[0] if candidates else WORKERS_DATASET[6]

    if any(k in full_text for k in road_keys) or "builder" in full_text:
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Road Builder"]
        return candidates[0] if candidates else WORKERS_DATASET[3]

    if any(k in full_text for k in garbage_keys) or "sswmb" in full_text:
        candidates = [w for w in WORKERS_DATASET if w["role"] == "Garbage Collector"]
        return candidates[0] if candidates else WORKERS_DATASET[12]

    # Default fallback to general Labour
    candidates = [w for w in WORKERS_DATASET if w["role"] == "Labour"]
    return candidates[0] if candidates else WORKERS_DATASET[9]


def fallback_triage_analyzer(description: str, has_image: bool) -> dict:
    """
    Heuristic triage engine adhering to the blueprint Priority Matrix
    when Gemini API key is unset or external call is unavailable.
    """
    desc_lower = description.lower()
    is_urdu = is_urdu_text(description)
    
    # Keyword categories
    electric_keys = ["wire", "cable", "electric", "current", "spark", "shock", "voltage", "short circuit", "transformer", "تار", "بجلی", "شارٹ سرکٹ"]
    sewage_high_keys = ["sewage pipe", "gutter", "manhole", "open hole", "burst sewage", "water burst", "sewer line", "گٹر", "سیوریج", "پائپ پھٹ", "کھلا مین ہول", "سیوریج لائن"]
    sewage_med_keys = ["sewage", "sewer", "drain", "leak", "water pipe", "پائپ", "پانی"]
    road_high_keys = ["sinkhole", "road collapse", "bridge collapse", "deep crater"]
    road_keys = ["pothole", "road", "cavity", "asphalt", "cracked road", "sidewalk", "کھڈا", "سڑک", "روڈ"]
    garbage_keys = ["garbage", "trash", "waste", "kuda", "debris", "dump", "refuse", "کچرا", "کوڑا", "غلاظت"]
    streetlight_keys = ["streetlamp", "streetlight", "broken light", "dark street", "اسٹریٹ لائٹ"]

    # Rule evaluation
    if any(k in desc_lower for k in electric_keys):
        detected_issue = "Exposed active electrical cables or sparking wire hazard" if not is_urdu else "بجلی کی ننگی تاریں یا شارٹ سرکٹ کا خطرہ"
        priority = PriorityLevel.HIGH
        sla = "< 12 HOURS"
        agency = "K-Electric / Rapid Emergency Response Cell"
        reasoning = (
            "CRITICAL: Live electrical hazard detected posing immediate life-safety risk. Requires emergency intervention within 12 hours."
            if not is_urdu else
            "انتہائی سنگین: بجلی کی ننگی تاروں کا فوری خطرہ۔ 12 گھنٹے کے اندر ہنگامی کارروائی درکار ہے۔"
        )
    elif any(k in desc_lower for k in sewage_high_keys):
        detected_issue = "Burst sewage pipeline, collapsed gutter, or open manhole hazard" if not is_urdu else "سیوریج پائپ لائن پھٹنا، گٹر یا کھلا مین ہول"
        priority = PriorityLevel.HIGH
        sla = "< 12 HOURS"
        agency = "Karachi Water & Sewerage Corporation (KW&SC)"
        reasoning = (
            "CRITICAL: Uncovered manhole or major sewage pipeline burst endangering pedestrian and vehicular safety. Immediate 12-hour dispatch."
            if not is_urdu else
            "انتہائی سنگین: کھلا مین ہول یا سیوریج پائپ لائن پھٹ جانا جس سے شہریوں کی جان کو خطرہ ہے۔ 12 گھنٹے کے اندر فوری مرمت۔"
        )
    elif any(k in desc_lower for k in road_high_keys):
        detected_issue = "Severe road collapse or dangerous sinkhole" if not is_urdu else "سڑک کا دھنس جانا یا خطرناک گڑھا"
        priority = PriorityLevel.HIGH
        sla = "< 12 HOURS"
        agency = "Karachi Metropolitan Corporation (KMC) / DMC"
        reasoning = (
            "CRITICAL: Structural road collapse posing serious collision hazard. Immediate emergency repair within 12 hours."
            if not is_urdu else
            "انتہائی سنگین: سڑک کا بیٹھ جانا یا گرنا جس سے سنگین حادثے کا اندیشہ ہے۔"
        )
    elif any(k in desc_lower for k in road_keys):
        detected_issue = "Road cavity, damaged asphalt surface, or pothole hazard" if not is_urdu else "سڑک پر کھڈا یا ٹوٹی پھوٹی سڑک"
        priority = PriorityLevel.MEDIUM
        sla = "24-48 HOURS"
        agency = "Karachi Metropolitan Corporation (KMC) / District Municipal Corporation"
        reasoning = (
            "MODERATE: Severe civic impediment affecting traffic flow or vehicle safety. Dispatched within 24-48 hour window."
            if not is_urdu else
            "درمیانی ترجیح: سڑک پر کھڈوں کی وجہ سے ٹریفک روانی متاثر ہو رہی ہے۔ 24 سے 48 گھنٹے کے اندر مرمت۔"
        )
    elif any(k in desc_lower for k in garbage_keys):
        detected_issue = "Accumulated garbage heap or solid waste dump" if not is_urdu else "کچرے کا بڑا ڈھیر یا غلاظت"
        priority = PriorityLevel.MEDIUM
        sla = "24-48 HOURS"
        agency = "Sindh Solid Waste Management Board (SSWMB)"
        reasoning = (
            "MODERATE: Uncollected solid waste causing sanitary and environmental concern. Dispatched for clearing within 24-48 hours."
            if not is_urdu else
            "درمیانی ترجیح: کچرے کا ڈھیر تعفن اور بیماریاں پھیلانے کا سبب بن رہا ہے۔ 24 سے 48 گھنٹے میں صفائی۔"
        )
    elif any(k in desc_lower for k in streetlight_keys):
        detected_issue = "Malfunctioning streetlight or illumination failure" if not is_urdu else "خراب اسٹریٹ لائٹ یا اندھیرا"
        priority = PriorityLevel.MEDIUM
        sla = "24-48 HOURS"
        agency = "District Municipal Corporation (DMC) Electrical Department"
        reasoning = (
            "MODERATE: Street illumination failure compromising nighttime safety. Dispatched within 24-48 hours."
            if not is_urdu else
            "درمیانی ترجیح: اسٹریٹ لائٹ خراب ہونے سے رات کو اندھیرا رہتا ہے۔"
        )
    elif any(k in desc_lower for k in sewage_med_keys):
        detected_issue = "Municipal water pipe leakage or minor drainage blockage" if not is_urdu else "پانی کی پائپ لائن میں لیکیج یا نکاسی کا مسئلہ"
        priority = PriorityLevel.MEDIUM
        sla = "24-48 HOURS"
        agency = "Karachi Water & Sewerage Corporation (KW&SC)"
        reasoning = (
            "MODERATE: Water leakage or drainage issue scheduled for crew inspection within 24-48 hours."
            if not is_urdu else
            "درمیانی ترجیح: پانی کے ضیاع یا نکاسی آب کا مسئلہ۔ 24 سے 48 گھنٹے میں معائنہ۔"
        )
    else:
        detected_issue = "Minor municipal maintenance or civic upkeep grievance" if not is_urdu else "معمولی بلدیاتی مسئلہ"
        priority = PriorityLevel.LOW
        sla = "3-5 DAYS"
        agency = "District Municipal Corporation (DMC) Parks & Maintenance"
        reasoning = (
            "ROUTINE: Non-hazardous civic repair or municipal maintenance ticket scheduled for routine resolution."
            if not is_urdu else
            "معمول کی ترجیح: بغیر فوری خطرے کے بلدیاتی مرمت جس کو معمول کے مطابق حل کیا جائے گا۔"
        )

    assigned_worker = select_worker_by_issue(description, detected_issue, agency)
        
    return {
        "is_valid_civic_issue": True,
        "detected_issue": detected_issue,
        "priority": priority,
        "sla_window": sla,
        "target_agency": agency,
        "ai_reasoning": reasoning,
        "language_detected": "ur" if is_urdu else "en",
        "assigned_worker": assigned_worker
    }


def call_gemini_vision(image_bytes: bytes, description: str, mime_type: str = "image/jpeg") -> dict:
    """Invokes Gemini Multimodal Vision with online models and worker allotment."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("Gemini_API_Key")
    if not api_key or not GENAI_AVAILABLE:
        return fallback_triage_analyzer(description, bool(image_bytes))

    def _invoke():
        client = genai.Client(api_key=api_key)
        prompt = f"""
        You are the automated AI Triage Inspector for AwamConnect, an official municipal grievance redressal platform in Sindh, Pakistan.
        Analyze this citizen grievance submission:
        Citizen Complaint Words / Description: "{description}"
        
        INSTRUCTIONS FOR MULTIMODAL ANALYSIS:
        1. Review the citizen's complaint words AND analyze the attached photo evidence (if provided).
        2. Set the severity priority strictly to one of: HIGH, MEDIUM, LOW based on visual evidence and reported impact:
           - HIGH (< 12 Hours): Imminent hazard to life, safety, or primary transport. E.g., active sparking or fallen live electrical wires, uncovered/open manholes on active streets or sidewalks, severe raw sewage flooding entire streets, collapsed road sections or deep sinkholes, burst main water pipeline flooding.
           - MEDIUM (24-48 Hours): Moderate hazard, significant traffic obstruction, or environmental health concern. E.g., large potholes, malfunctioning/broken streetlights leaving roads dark at night, large overflowing garbage piles/refuse dumps, non-flooding water leakage.
           - LOW (3-5 Days): Minor non-hazardous civic maintenance. E.g., faded lane markings, unauthorized wall graffiti, broken park benches, small road cracks, dry leaf debris.
        3. Determine the targeted municipal authority in Sindh:
           - Karachi Metropolitan Corporation (KMC) (major arteries, bridges, large stormwater drains)
           - Karachi Water & Sewerage Corporation (KW&SC) (water supply, sewage pipelines, manhole covers)
           - K-Electric (KE) (electrical wiring, poles, transformers, power hazards)
           - Sindh Solid Waste Management Board (SSWMB) (garbage heaps, solid waste collection, dumpsters)
           - District Municipal Corporation (DMC) / Town Municipal Administration (TMA) (local neighborhood streets, streetlights, local parks)
           - Hyderabad Municipal Corporation (HMC) (civic issues in Hyderabad)

        4. ALLOT FIELD WORKER FROM ROSTER:
        Select ONE exact field worker from this available roster based on complaint type:
        - Sewerage Worker (for sewage, drains, gutters, water mains, open manholes):
          * Name: "Muhammad Aslam", Email: "m.aslam.wasap@gmail.com", Phone: "+92 300 4581290"
          * Name: "Ramesh Kumar", Email: "ramesh.kumar92@yahoo.com", Phone: "+92 321 8723411"
          * Name: "Tariq Mehmood", Email: "tariq.mehmood.wasa@gmail.com", Phone: "+92 333 5190284"
        - Road Builder (for potholes, damaged road surface, broken asphalt, sidewalks):
          * Name: "Abdul Rehman", Email: "abdul.rehman.nha@gmail.com", Phone: "+92 345 6289104"
          * Name: "Ghulam Mustafa", Email: "g.mustafa.infra@yahoo.com", Phone: "+92 312 7741953"
          * Name: "Noman Bashir", Email: "noman.bashir.cw@outlook.com", Phone: "+92 301 3904512"
        - Electrician Worker (for active sparking wires, power poles, transformers, broken streetlights):
          * Name: "Muhammad Usman", Email: "usman.electrician99@gmail.com", Phone: "+92 302 8129471"
          * Name: "Bilal Ahmed", Email: "bilal.ahmed.lesco@gmail.com", Phone: "+92 322 4618392"
          * Name: "Farhan Akhtar", Email: "farhan.akhtar.power@outlook.com", Phone: "+92 334 9081275"
        - Labour (for general manual physical tasks, park maintenance, bench repair, clearing tree limbs):
          * Name: "Imran Ali", Email: "imran.ali.mazdoor@gmail.com", Phone: "+92 305 6742918"
          * Name: "Zahid Hussain", Email: "zahid.hussain.site@yahoo.com", Phone: "+92 346 5123984"
          * Name: "Kashif Mehmood", Email: "kashif.mehmood.ops@gmail.com", Phone: "+92 313 8847120"
        - Garbage Collector (for uncollected trash piles, solid waste dumps, commercial garbage heaps):
          * Name: "Nadeem Masih", Email: "nadeem.masih.lwmc@gmail.com", Phone: "+92 303 9182746"
          * Name: "Babu Lal", Email: "babu.lal.sanitation@yahoo.com", Phone: "+92 324 7391058"
          * Name: "Sajid Masih", Email: "sajid.masih.cdg@gmail.com", Phone: "+92 335 6201943"

        5. Validate if genuine civic damage is visible/described. If completely unrelated (e.g., selfie, pet, indoor bedroom, clear sky), mark is_valid_civic_issue as false.
        6. Detect whether description language is Urdu ('ur') or English ('en').

        Return ONLY a rigid JSON object with exactly these keys:
        {{
            "is_valid_civic_issue": true/false,
            "detected_issue": "concise description of issue based on image analysis and citizen complaint words",
            "priority": "HIGH" or "MEDIUM" or "LOW",
            "sla_window": "< 12 HOURS" or "24-48 HOURS" or "3-5 DAYS",
            "target_agency": "Specific targeted municipal body name",
            "ai_reasoning": "Clear justification explaining how the photo and citizen words were analyzed to assign this priority and agency, in both English and Urdu",
            "language_detected": "en" or "ur",
            "assigned_worker": {{
                "name": "Exact worker name from roster",
                "role": "Sewerage Worker" or "Road Builder" or "Electrician Worker" or "Labour" or "Garbage Collector",
                "email": "Exact worker email from roster",
                "phone": "Exact worker phone from roster"
            }}
        }}
        """

        contents = [prompt]
        if image_bytes:
            contents.append(
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type or "image/jpeg"
                )
            )

        response = None
        for m_name in ["gemini-flash-lite-latest", "gemini-3-flash-preview"]:
            try:
                response = client.models.generate_content(
                    model=m_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                if response and response.text:
                    break
            except Exception as me:
                app.logger.warning(f"Model {m_name} failed: {me}")

        if not response or not response.text:
            return None

        result_text = response.text.strip()
        data = json.loads(result_text)
        p_str = data.get("priority", "MEDIUM").upper()
        if p_str not in ["HIGH", "MEDIUM", "LOW"]:
            p_str = "MEDIUM"
        data["priority"] = PriorityLevel(p_str)

        # Ensure worker exists in dataset
        worker_info = data.get("assigned_worker") or {}
        matched_worker = None
        for w in WORKERS_DATASET:
            if w["name"].lower() == worker_info.get("name", "").strip().lower():
                matched_worker = w
                break
        if not matched_worker:
            matched_worker = select_worker_by_issue(description, data.get("detected_issue", ""), data.get("target_agency", ""))
        data["assigned_worker"] = matched_worker

        return data

    res, err = run_with_timeout(_invoke, timeout=18.0)
    if res:
        return res
    if err:
        app.logger.warning(f"Gemini Vision call aborted or timed out: {err}. Using local heuristic triage.")
    return fallback_triage_analyzer(description, bool(image_bytes))


# ==========================================
# WEB APPLICATION VIEWS
# ==========================================

@app.route("/")
def index():
    """Serves Citizen Grievance Intake Portal."""
    return render_template("index.html")


@app.route("/dispatcher")
def dispatcher():
    """Serves Municipal Dispatcher Review Dashboard."""
    return render_template("dispatcher.html")


@app.route("/signup", methods=["GET", "POST"])
def signup_view():
    """Serves Citizen Sign Up Registration Page."""
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        # Supports standard form submission
        name = request.form.get("name", "").strip()
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        cnic = request.form.get("cnic", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        password = request.form.get("password", "").strip()

        if not all([name, username, email, cnic, phone, address, password]):
            flash("All fields are required. Please fill in the complete form.", "danger")
            return render_template("signup.html", form_data=request.form)

        if User.query.filter(func.lower(User.username) == username.lower()).first():
            flash("This username is already taken. Please choose another.", "danger")
            return render_template("signup.html", form_data=request.form)

        if User.query.filter(func.lower(User.email) == email.lower()).first():
            flash("This email address is already registered. Please log in.", "danger")
            return render_template("signup.html", form_data=request.form)

        try:
            new_user = User(
                name=name,
                username=username,
                email=email.lower(),
                cnic=cnic,
                phone=phone,
                address=address,
                role="citizen"
            )
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()

            session["user_id"] = new_user.id
            flash(f"Welcome, {new_user.name}! Your account has been registered successfully.", "success")
            return redirect(url_for("index"))
        except Exception as e:
            db.session.rollback()
            flash(f"Registration failed: {str(e)}", "danger")
            return render_template("signup.html", form_data=request.form)

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login_view():
    """Serves Citizen Login Page (Authenticates via Username OR Email)."""
    if "user_id" in session:
        return redirect(url_for("index"))

    next_page = request.args.get("next")

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "").strip()

        if not identifier or not password:
            flash("Please enter your username/email and password.", "danger")
            return render_template("login.html", identifier=identifier)

        # Authenticate via either username OR email (case-insensitive)
        user = User.query.filter(
            or_(
                func.lower(User.username) == identifier.lower(),
                func.lower(User.email) == identifier.lower()
            )
        ).first()

        if user and user.check_password(password):
            session["user_id"] = user.id
            flash(f"Welcome back, {user.name}!", "success")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)
            return redirect(url_for("index"))
        else:
            flash("Invalid username/email or password. Please verify and try again.", "danger")
            return render_template("login.html", identifier=identifier)

    return render_template("login.html")


@app.route("/logout")
def logout_view():
    """Logs the user out of the session."""
    session.clear()
    flash("You have been signed out successfully.", "info")
    return redirect(url_for("login_view"))


@app.route("/my-complaints")
def my_complaints_view():
    """Serves Citizen's Personal Complaint History and Status Tracking."""
    if "user_id" not in session:
        flash("Please log in to view your filed complaints.", "warning")
        return redirect(url_for("login_view", next="/my-complaints"))

    user = db.session.get(User, session["user_id"])
    if not user:
        session.clear()
        return redirect(url_for("login_view"))

    user_complaints = Complaint.query.filter_by(user_id=user.id).order_by(Complaint.created_at.desc()).all()
    return render_template("my_complaints.html", user=user, complaints=user_complaints)


# ==========================================
# REST API: AUTHENTICATION ENDPOINTS
# ==========================================

@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    """
    POST /api/auth/signup
    Registers a new citizen user.
    Required fields: name, address, cnic, phone, email, password, username
    """
    data = request.get_json() if request.is_json else request.form.to_dict()

    name = data.get("name", "").strip()
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()
    cnic = data.get("cnic", "").strip()
    phone = data.get("phone", "").strip()
    address = data.get("address", "").strip()
    password = data.get("password", "").strip()

    missing = []
    for field_name, val in [
        ("name", name), ("username", username), ("email", email),
        ("cnic", cnic), ("phone", phone), ("address", address), ("password", password)
    ]:
        if not val:
            missing.append(field_name)

    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    if User.query.filter(func.lower(User.username) == username.lower()).first():
        return jsonify({"error": "Username is already taken"}), 409

    if User.query.filter(func.lower(User.email) == email.lower()).first():
        return jsonify({"error": "Email is already registered"}), 409

    try:
        user = User(
            name=name,
            username=username,
            email=email.lower(),
            cnic=cnic,
            phone=phone,
            address=address,
            role="citizen"
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        session["user_id"] = user.id
        return jsonify({
            "success": True,
            "message": "User registered successfully",
            "user": user.to_dict()
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to register user: {str(e)}"}), 500


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    """
    POST /api/auth/login
    Authenticates user via Username OR Email + Password.
    """
    data = request.get_json() if request.is_json else request.form.to_dict()
    identifier = data.get("identifier", "").strip() or data.get("username", "").strip() or data.get("email", "").strip()
    password = data.get("password", "").strip()

    if not identifier or not password:
        return jsonify({"error": "Username/email and password are required"}), 400

    user = User.query.filter(
        or_(
            func.lower(User.username) == identifier.lower(),
            func.lower(User.email) == identifier.lower()
        )
    ).first()

    if not user or not user.check_password(password):
        return jsonify({"error": "Invalid username/email or password"}), 401

    session["user_id"] = user.id
    return jsonify({
        "success": True,
        "message": "Login successful",
        "user": user.to_dict()
    }), 200


@app.route("/api/auth/logout", methods=["POST", "GET"])
def api_logout():
    """POST /api/auth/logout - Clears current session."""
    session.clear()
    return jsonify({"success": True, "message": "Logged out successfully"}), 200


@app.route("/api/auth/me", methods=["GET"])
def api_me():
    """GET /api/auth/me - Returns currently logged-in user profile."""
    if "user_id" not in session:
        return jsonify({"authenticated": False, "user": None}), 200

    user = db.session.get(User, session["user_id"])
    if not user:
        session.clear()
        return jsonify({"authenticated": False, "user": None}), 200

    return jsonify({"authenticated": True, "user": user.to_dict()}), 200


# ==========================================
# REST API: COMPLAINT ENDPOINTS
# ==========================================

@app.route("/api/complaints/submit", methods=["POST"])
def submit_complaint():
    """
    POST /api/complaints/submit
    Ingests citizen complaint with image and description, executes Gemini triage,
    and records ticket to PostgreSQL complaints table linked to citizen.
    """
    description = request.form.get("description", "").strip()
    if not description:
        return jsonify({"error": "Description is required"}), 400

    # Retrieve citizen association if logged in or passed
    user_id = session.get("user_id")
    logged_user = db.session.get(User, user_id) if user_id else None

    citizen_name = request.form.get("citizen_name", "").strip()
    citizen_cnic = request.form.get("citizen_cnic", "").strip()
    citizen_phone = request.form.get("citizen_phone", "").strip()
    citizen_address = request.form.get("citizen_address", "").strip()

    if logged_user:
        citizen_name = citizen_name or logged_user.name
        citizen_cnic = citizen_cnic or logged_user.cnic
        citizen_phone = citizen_phone or logged_user.phone
        citizen_address = citizen_address or logged_user.address

    # Image handling (stateless, in-memory)
    image_file = request.files.get("image")
    image_bytes = None
    image_data_uri = None
    mimetype = "image/jpeg"

    if image_file and image_file.filename != "":
        image_bytes = image_file.read()
        if len(image_bytes) > 0:
            mimetype = image_file.mimetype or "image/jpeg"
            b64_data = base64.b64encode(image_bytes).decode("utf-8")
            image_data_uri = f"data:{mimetype};base64,{b64_data}"

    # Perform Multimodal AI Triage with Gemini (photo + complaint words)
    triage = call_gemini_vision(image_bytes, description, mime_type=mimetype)

    ticket_id = f"AWAM-{uuid.uuid4().hex[:8].upper()}"

    # Worker allotment
    worker_info = triage.get("assigned_worker") or select_worker_by_issue(description, triage.get("detected_issue", ""), triage.get("target_agency", ""))

    complaint = Complaint(
        ticket_uuid=ticket_id,
        user_id=logged_user.id if logged_user else None,
        citizen_name=citizen_name or None,
        citizen_cnic=citizen_cnic or None,
        citizen_phone=citizen_phone or None,
        citizen_address=citizen_address or None,
        raw_description=description,
        language_detected=triage.get("language_detected", "en"),
        image_url=image_data_uri,
        is_valid_civic_issue=triage.get("is_valid_civic_issue", True),
        detected_issue=triage.get("detected_issue", "Municipal report"),
        priority=triage.get("priority", PriorityLevel.MEDIUM),
        sla_window=triage.get("sla_window", "24-48 HOURS"),
        target_agency=triage.get("target_agency", "Karachi Metropolitan Corporation (KMC)"),
        ai_reasoning=triage.get("ai_reasoning", "Triage assigned according to municipal urgency parameters."),
        assigned_worker_name=worker_info.get("name"),
        assigned_worker_role=worker_info.get("role"),
        assigned_worker_email=worker_info.get("email"),
        assigned_worker_phone=worker_info.get("phone"),
        status=TicketStatus.PENDING
    )

    try:
        db.session.add(complaint)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        try:
            db.session.add(complaint)
            db.session.commit()
        except Exception as retry_e:
            db.session.rollback()
            app.logger.error(f"Database write failure: {retry_e}")
            return jsonify({"error": f"Failed to record ticket: {str(retry_e)}"}), 500

    resp = complaint.to_dict()
    resp["success"] = True
    resp["message"] = f"Your complaint submitted successfully! Allotted Complaint Number: #{complaint.id}"
    resp["complaint_number"] = complaint.id
    resp["complaint_id"] = complaint.id
    resp["assigned_worker"] = {
        "name": complaint.assigned_worker_name,
        "role": complaint.assigned_worker_role,
        "email": complaint.assigned_worker_email,
        "phone": complaint.assigned_worker_phone
    }
    return jsonify(resp), 201


@app.route("/api/workers", methods=["GET"])
def list_workers():
    """GET /api/workers - Returns list of municipal field workers from dataset."""
    workers = Worker.query.all()
    if not workers:
        return jsonify(WORKERS_DATASET), 200
    return jsonify([w.to_dict() for w in workers]), 200


@app.route("/api/complaints/search", methods=["GET"])
def search_complaints():
    """
    GET /api/complaints/search?q=<query>
    Searches complaints by:
    - Database Complaint ID (e.g. '1', '#1', '15')
    - Ticket UUID (e.g. 'AWAM-A1B2C3D4' or 'A1B2C3D4')
    - Keywords in description or detected issue
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Search query parameter 'q' is required"}), 400

    clean_q = q.lstrip("#").strip()
    complaint = None

    # 1. Search by numeric Complaint ID
    if clean_q.isdigit():
        complaint = db.session.get(Complaint, int(clean_q))

    # 2. Search by Ticket UUID exact or partial
    if not complaint:
        complaint = Complaint.query.filter(
            or_(
                Complaint.ticket_uuid.ilike(clean_q),
                Complaint.ticket_uuid.ilike(f"AWAM-{clean_q}"),
                Complaint.ticket_uuid.ilike(f"%{clean_q}%")
            )
        ).order_by(Complaint.created_at.desc()).first()

    # 3. Fallback: Search in detected_issue or raw_description
    if not complaint:
        complaint = Complaint.query.filter(
            or_(
                Complaint.detected_issue.ilike(f"%{clean_q}%"),
                Complaint.raw_description.ilike(f"%{clean_q}%")
            )
        ).order_by(Complaint.created_at.desc()).first()

    if not complaint:
        return jsonify({"found": False, "error": f"No complaint found matching ID or Ticket '{q}'"}), 404

    data = complaint.to_dict()
    data["complaint_number"] = complaint.id
    return jsonify({
        "found": True,
        "complaint": data,
        "complaint_number": complaint.id,
        "ticket_uuid": complaint.ticket_uuid
    }), 200


@app.route("/api/complaints", methods=["GET"])
def list_complaints():
    """
    GET /api/complaints
    Queries database for complaints with filter parameters: ?priority, ?status, ?agency, ?user_id
    """
    query = Complaint.query

    priority_param = request.args.get("priority")
    status_param = request.args.get("status")
    agency_param = request.args.get("agency")
    user_id_param = request.args.get("user_id")

    if priority_param and priority_param.upper() in PriorityLevel.__members__:
        query = query.filter(Complaint.priority == PriorityLevel(priority_param.upper()))

    if status_param and status_param.upper() in TicketStatus.__members__:
        query = query.filter(Complaint.status == TicketStatus(status_param.upper()))

    if agency_param:
        query = query.filter(Complaint.target_agency.ilike(f"%{agency_param}%"))

    if user_id_param:
        query = query.filter(Complaint.user_id == int(user_id_param))

    complaints = query.order_by(Complaint.created_at.desc()).all()
    return jsonify([c.to_dict() for c in complaints]), 200


@app.route("/api/my-complaints", methods=["GET"])
def list_my_complaints():
    """GET /api/my-complaints - Returns all complaints lodged by the current logged-in citizen."""
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized. Please log in."}), 401

    complaints = Complaint.query.filter_by(user_id=session["user_id"]).order_by(Complaint.created_at.desc()).all()
    return jsonify([c.to_dict() for c in complaints]), 200


@app.route("/api/complaints/<identifier>", methods=["GET"])
def get_complaint(identifier):
    """
    GET /api/complaints/<identifier>
    Fetches individual complaint status by Ticket UUID OR Database Complaint ID.
    """
    clean_id = identifier.strip().lstrip("#")
    complaint = None
    if clean_id.isdigit():
        complaint = db.session.get(Complaint, int(clean_id))
    if not complaint:
        complaint = Complaint.query.filter(
            or_(
                Complaint.ticket_uuid.ilike(clean_id),
                Complaint.ticket_uuid.ilike(f"AWAM-{clean_id}"),
                Complaint.ticket_uuid.ilike(f"%{clean_id}%")
            )
        ).first()

    if not complaint:
        return jsonify({"error": f"Complaint not found for ID or Ticket '{identifier}'"}), 404

    data = complaint.to_dict()
    data["complaint_number"] = complaint.id
    return jsonify(data), 200


@app.route("/api/complaints/<ticket_uuid>/status", methods=["PATCH"])
def update_status(ticket_uuid):
    """
    PATCH /api/complaints/<ticket_uuid>/status
    Updates ticket status (PENDING, DISPATCHED, RESOLVED).
    """
    complaint = Complaint.query.filter_by(ticket_uuid=ticket_uuid).first()
    if not complaint:
        return jsonify({"error": "Ticket not found"}), 404

    data = request.get_json() or {}
    new_status = data.get("status", "").upper()

    if new_status not in TicketStatus.__members__:
        return jsonify({"error": f"Invalid status. Must be one of: {[s.value for s in TicketStatus]}"}), 400

    complaint.status = TicketStatus(new_status)
    complaint.updated_at = datetime.utcnow()

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Failed to update status: {str(e)}"}), 500

    return jsonify(complaint.to_dict()), 200


@app.route("/api/complaints/export", methods=["GET"])
def export_complaints():
    """
    GET /api/complaints/export
    Dumps complaints into formatted JSON attachment for municipal field team dispatch.
    """
    complaints = Complaint.query.order_by(Complaint.created_at.desc()).all()
    data = [c.to_dict() for c in complaints]
    
    json_bytes = json.dumps(data, indent=2).encode("utf-8")
    return Response(
        json_bytes,
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="triage_export.json"'}
    )


@app.route("/api/chat/support", methods=["POST"])
def support_chat():
    """
    POST /api/chat/support
    Context-restricted Gemini support assistant with live database read access
    and empathetic frustration de-escalation for AwamConnect civic complaints.
    """
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    ticket_uuid = data.get("ticket_uuid", "").strip()

    if not message:
        return jsonify({"error": "Message is required"}), 400

    # 1. Citizen Authentication & Database Profile
    user_id = session.get("user_id")
    logged_user = db.session.get(User, user_id) if user_id else None
    
    user_complaints = []
    if logged_user:
        user_complaints = Complaint.query.filter_by(user_id=logged_user.id).order_by(Complaint.created_at.desc()).all()

    # 2. Extract potential ticket/complaint references from message or payload
    referenced_complaints = []
    if ticket_uuid:
        t = Complaint.query.filter(
            or_(Complaint.ticket_uuid == ticket_uuid, Complaint.ticket_uuid.ilike(f"%{ticket_uuid}%"))
        ).first()
        if t and t not in referenced_complaints:
            referenced_complaints.append(t)

    # Search for digits/complaint IDs (e.g. #19, complaint 19, 19)
    found_nums = re.findall(r'#?(\b\d+\b)', message)
    for num_str in found_nums:
        try:
            c = db.session.get(Complaint, int(num_str))
            if c and c not in referenced_complaints:
                referenced_complaints.append(c)
        except Exception:
            pass

    # Search for AWAM-XXXXXXXX
    found_uuids = re.findall(r'AWAM-[A-Za-z0-9]+', message, re.IGNORECASE)
    for uid in found_uuids:
        c = Complaint.query.filter(Complaint.ticket_uuid.ilike(f"%{uid}%")).first()
        if c and c not in referenced_complaints:
            referenced_complaints.append(c)

    # Search for CNIC format
    found_cnics = re.findall(r'\d{5}-\d{7}-\d', message)
    for cnic in found_cnics:
        cnic_complaints = Complaint.query.filter_by(citizen_cnic=cnic).all()
        for c in cnic_complaints:
            if c not in referenced_complaints:
                referenced_complaints.append(c)

    # Platform global metrics
    total_platform_tickets = Complaint.query.count()
    resolved_platform_tickets = Complaint.query.filter_by(status=TicketStatus.RESOLVED).count()

    # 3. Build rich Database Context for Gemini
    db_context_lines = []
    pending_c = []
    dispatched_c = []
    resolved_c = []

    if logged_user:
        pending_c = [c for c in user_complaints if c.status == TicketStatus.PENDING]
        dispatched_c = [c for c in user_complaints if c.status == TicketStatus.DISPATCHED]
        resolved_c = [c for c in user_complaints if c.status == TicketStatus.RESOLVED]

        db_context_lines.append("[AUTHENTICATED CITIZEN PROFILE (FROM DATABASE)]")
        db_context_lines.append(f"- Citizen Name: {logged_user.name}")
        db_context_lines.append(f"- Username: {logged_user.username}")
        db_context_lines.append(f"- CNIC: {logged_user.cnic}")
        db_context_lines.append(f"- Phone: {logged_user.phone}")
        db_context_lines.append(f"- Address: {logged_user.address}")
        db_context_lines.append(f"- Email: {logged_user.email}")
        db_context_lines.append(f"- TOTAL COMPLAINTS LODGED BY THIS USER: {len(user_complaints)}")
        db_context_lines.append(f"  * PENDING: {len(pending_c)}")
        db_context_lines.append(f"  * DISPATCHED: {len(dispatched_c)}")
        db_context_lines.append(f"  * RESOLVED: {len(resolved_c)}")
        
        if user_complaints:
            db_context_lines.append("\n[CITIZEN'S RECORDED COMPLAINTS IN DATABASE (Latest First)]:")
            for c in user_complaints[:10]:
                w_str = f"{c.assigned_worker_name} ({c.assigned_worker_role}, Phone: {c.assigned_worker_phone})" if c.assigned_worker_name else "Unassigned"
                created_str = c.created_at.strftime("%Y-%m-%d %H:%M UTC") if c.created_at else "Recent"
                db_context_lines.append(
                    f"• Complaint #{c.id} (Ticket ID: {c.ticket_uuid}):\n"
                    f"  - Detected Issue: {c.detected_issue}\n"
                    f"  - Priority: {c.priority.value} | Target SLA Window: {c.sla_window}\n"
                    f"  - Target Agency: {c.target_agency}\n"
                    f"  - Current Status: {c.status.value}\n"
                    f"  - Assigned Field Worker: {w_str}\n"
                    f"  - Lodged At: {created_str}\n"
                    f"  - Citizen Description: \"{c.raw_description}\""
                )
    else:
        db_context_lines.append("[CITIZEN AUTH STATUS]: Guest (Not logged into an account).")
        if not referenced_complaints:
            db_context_lines.append("- Note: Citizen is browsing as a guest. If they ask how many complaints they lodged, let them know they are currently browsing as a guest, and invite them to log in or provide their Complaint #, Ticket ID, or CNIC so you can search the database for them.")

    if referenced_complaints:
        db_context_lines.append("\n[SPECIFIC COMPLAINTS LOOKED UP FROM DATABASE]:")
        for c in referenced_complaints:
            w_str = f"{c.assigned_worker_name} ({c.assigned_worker_role}, Phone: {c.assigned_worker_phone})" if c.assigned_worker_name else "Unassigned"
            created_str = c.created_at.strftime("%Y-%m-%d %H:%M UTC") if c.created_at else "Recent"
            db_context_lines.append(
                f"• Complaint #{c.id} (Ticket ID: {c.ticket_uuid}):\n"
                f"  - Complainant: {c.citizen_name or 'Anonymous'} ({c.citizen_phone or 'No phone'})\n"
                f"  - Detected Issue: {c.detected_issue}\n"
                f"  - Priority: {c.priority.value} | Target SLA Window: {c.sla_window}\n"
                f"  - Agency: {c.target_agency} | Status: {c.status.value}\n"
                f"  - Assigned Field Worker: {w_str}\n"
                f"  - Lodged At: {created_str}\n"
                f"  - Complainant Words: \"{c.raw_description}\""
            )

    db_context_lines.append(f"\n[GLOBAL PLATFORM STATS]: Total Complaints in Database: {total_platform_tickets}, Resolved: {resolved_platform_tickets}")
    db_context_str = "\n".join(db_context_lines)

    system_instruction = (
        "You are the official AwamConnect AI Support Assistant for the AwamConnect Municipal Grievance Redressal platform in Sindh, Pakistan.\n"
        "You have DIRECT REAL-TIME DATABASE PERMISSION to read the live records provided in [DATABASE CONTEXT] below.\n\n"
        "DATABASE READING & COMPLAINT COUNT INSTRUCTIONS:\n"
        "1. When the user asks 'How many complaints have I submitted?', 'What are my complaints?', 'Show my grievance status', or similar questions:\n"
        "   - If they are logged in: Cite the EXACT count from their profile (e.g. 'You have submitted X complaints: Y Pending, Z Resolved'). Give a concise, neat breakdown of their complaints (Complaint #, Issue, Priority, Status, Assigned Worker).\n"
        "   - If they are a guest without complaints attached: Cordially let them know: 'You are currently browsing as a guest. Please log in to your account, or tell me your Complaint Number (e.g. #19), Ticket ID, or CNIC, and I will search our database for you immediately.'\n"
        "2. When a user asks about a specific complaint by ID or ticket (e.g., 'What is happening with complaint #19?'): Pull the exact details from the database records and state its issue, priority, SLA target, current status, and assigned field worker name and phone number.\n\n"
        "CITIZEN FRUSTRATION & PENDING WORK PROTOCOL (DE-ESCALATION):\n"
        "3. When a user expresses frustration, anger, or asks why their complaint is still pending / taking so long (e.g., 'Why is my complaint still pending?!', 'Nobody came to fix it!', 'You guys are useless!', 'Why is it taking so long?!', 'It has been days!', 'اب تک ٹھیک کیوں نہیں ہوا؟', 'کتنا وقت لگے گا؟'):\n"
        "   - SINCERE EMPATHY: Validate their concern and inconvenience with utmost respect and warmth (e.g., 'I completely understand how frustrating it is to wait while dealing with this issue in your neighborhood, and I sincerely apologize for the delay.'). In Urdu: 'ہمیں آپ کی پریشانی اور زحمت کا پورا احساس ہے اور تاخیر کے لیے دل سے معذرت خواہ ہیں...'\n"
        "   - TRANSPARENT TICKET DATA: Cite their exact Complaint #, detected issue, priority tier, and SLA window (HIGH: < 12 Hours, MEDIUM: 24-48 Hours, LOW: 3-5 Days).\n"
        "   - EXPLAIN REALISTIC PENDING CAUSES:\n"
        "     * Field Prioritization: Severe life-safety hazards (active high-voltage wires, open manholes) receive immediate emergency dispatch first.\n"
        "     * Ground Logistics: Specialized municipal equipment (suction/jetting trucks, asphalt rollers), procurement of replacement pipelines/cables, or coordination with traffic police / utility shutdowns.\n"
        "     * Active Crew: The field crew has been assigned and is processing workloads across the sector.\n"
        "   - ACTIONABLE ESCALATION:\n"
        "     * Direct Field Worker: Share their assigned worker's name, role, and direct telephone number from the database record (e.g. 'Your allotted worker is Muhammad Aslam (Sewerage Worker) at +92 300 4581290').\n"
        "     * Emergency Helplines: Provide official helpline numbers:\n"
        "       - Karachi Water & Sewerage Corporation (KW&SC): 1339 or (021) 9924-4632\n"
        "       - K-Electric 24/7 Rapid Emergency: 118 or (021) 111-537-211\n"
        "       - Sindh Solid Waste Management Board (SSWMB): 1128\n"
        "       - KMC Central Control: 1339\n"
        "   - REASSURANCE: Reassure the user that their ticket is permanently tracked in the provincial database and cannot be closed without verification.\n"
        "   - TONE: Calm, polite, professional, caring, and solution-oriented. Never dismissive or defensive.\n\n"
        "STRICT BOUNDARIES (ABSOLUTE ENFORCEMENT):\n"
        "4. You MUST ONLY assist with AwamConnect municipal grievances, ticket tracking, SLA timelines, Sindh civic agencies (KMC, KW&SC, K-Electric, SSWMB, DMC), and citizen accounts.\n"
        "5. If the user asks about ANY completely unrelated topic (e.g., coding, math, general knowledge, movies, cooking, politics, casual chit-chat, who made you):\n"
        "   Politely refuse: 'I am only authorized to assist with AwamConnect municipal complaints, database ticket tracking, SLA windows, and Sindh civic agencies.' (or in Urdu if asked in Urdu).\n"
        "6. BILINGUAL SUPPORT: Reply in Urdu (اردو) if asked in Urdu, and English if asked in English."
    )

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("Gemini_API_Key")
    if api_key and GENAI_AVAILABLE:
        def _invoke_chat():
            client = genai.Client(api_key=api_key)
            full_prompt = f"{db_context_str}\n\nUser Question: {message}"
            for m_name in ["gemini-flash-lite-latest", "gemini-3-flash-preview"]:
                try:
                    response = client.models.generate_content(
                        model=m_name,
                        contents=full_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.3
                        )
                    )
                    if response and response.text:
                        return response.text.strip()
                except Exception as me:
                    app.logger.warning(f"Support model {m_name} failed: {me}")
            return None

        reply, err = run_with_timeout(_invoke_chat, timeout=9.0)
        if reply:
            return jsonify({"reply": reply})
        if err:
            app.logger.warning(f"Gemini Chat call aborted or timed out: {err}. Using local assistant.")

    # Fallback response engine enforcing boundaries and database context
    msg_lower = message.lower()
    is_urdu = is_urdu_text(message)

    # Off-topic detector
    off_topic = [
        "weather", "cricket", "football", "fifa", "cup", "game", "code", "python",
        "java", "c++", "algorithm", "president", "prime minister", "recipe", "math",
        "joke", "movie", "song", "who are you", "who made you"
    ]
    if any(ot in msg_lower for ot in off_topic) and not any(k in msg_lower for k in ["ticket", "awam", "complaint", "kmc", "water", "wire", "sla", "register", "login", "cnic", "worker"]):
        return jsonify({
            "reply": "I am only authorized to assist with AwamConnect municipal complaints, database ticket status, and Sindh civic agencies."
        })

    # 1. Check if user asks about complaint count
    if any(k in msg_lower for k in ["how many", "count", "my complaint", "meri shikayat", "کتنی شکایت", "شکوے", "complaints submitted", "my ticket"]):
        if logged_user:
            summary_items = []
            for c in user_complaints[:3]:
                w_info = f" • Worker: {c.assigned_worker_name}" if c.assigned_worker_name else ""
                summary_items.append(f"- Complaint #{c.id} ({c.detected_issue}) • Status: {c.status.value}{w_info}")
            items_str = "\n".join(summary_items)
            return jsonify({
                "reply": f"You have submitted {len(user_complaints)} complaint(s) in total ({len(pending_c)} Pending, {len(dispatched_c)} Dispatched, {len(resolved_c)} Resolved).\n\nRecent Complaints:\n{items_str}\n\nYou can track all your tickets in 'My Complaints'."
            })
        else:
            return jsonify({
                "reply": "You are currently browsing as a guest. Please log in to view your complete grievance history, or provide your Complaint Number (e.g. #19) so I can search the database for you."
            })

    # 2. Check if user expresses frustration or asks why pending
    if any(k in msg_lower for k in ["why pending", "still pending", "frustrat", "angry", "delay", "late", "useless", "slow", "ab tak", "kab hoga", "kyun pending", "wait", "کیوں نہیں ہوا", "تاخیر", "کتنا وقت"]):
        active_c = None
        if referenced_complaints:
            active_c = referenced_complaints[0]
        elif pending_c:
            active_c = pending_c[0]
        elif user_complaints:
            active_c = user_complaints[0]

        if active_c:
            w_msg = f"\nYour allotted field worker is {active_c.assigned_worker_name} ({active_c.assigned_worker_role}). You can reach them directly at {active_c.assigned_worker_phone}." if active_c.assigned_worker_name else ""
            agency_hl = "1339 (KW&SC)" if "Water" in active_c.target_agency else ("118 (K-Electric)" if "Electric" in active_c.target_agency else "1128 (SSWMB)")
            return jsonify({
                "reply": (
                    f"I completely understand your frustration regarding the wait on Complaint #{active_c.id} ({active_c.detected_issue}), and we sincerely apologize for the delay. "
                    f"Your ticket is classified as {active_c.priority.value} Priority with an SLA target of {active_c.sla_window} assigned to {active_c.target_agency}.\n\n"
                    f"Pending work is typically due to emergency field queue prioritization (life-threatening hazards handled first) or specialized equipment deployment.{w_msg}\n\n"
                    f"You may also contact the direct agency helpline at {agency_hl}. Your ticket remains active in our system and will not be closed until resolved."
                )
            })
        else:
            return jsonify({
                "reply": (
                    "I completely understand your frustration and apologize for any delay. "
                    "Municipal repairs operate under strict priority SLAs: High Priority (< 12 Hours), Medium Priority (24-48 Hours), and Low Priority (3-5 Days). "
                    "Delays can occur due to emergency prioritization or equipment procurement. Please provide your Complaint Number (e.g. #19) so I can check your exact assigned field team and escalate for you."
                )
            })

    # 3. Check referenced complaint lookup
    if referenced_complaints:
        c = referenced_complaints[0]
        w_msg = f" Assigned worker: {c.assigned_worker_name} ({c.assigned_worker_role}, Tel: {c.assigned_worker_phone})." if c.assigned_worker_name else ""
        return jsonify({
            "reply": f"Complaint #{c.id} ({c.ticket_uuid}) is currently {c.status.value}. Detected Issue: '{c.detected_issue}', Priority: {c.priority.value}, SLA: {c.sla_window}, Assigned Agency: {c.target_agency}.{w_msg}"
        })

    if "sla" in msg_lower or "how long" in msg_lower or "time" in msg_lower or "kitna time" in msg_lower or "وقت" in msg_lower:
        return jsonify({
            "reply": "According to AwamConnect triage protocols: High Priority emergency hazards are resolved in under 12 hours; Medium Priority municipal issues in 24-48 hours; and Low Priority routine repairs in 3-5 days. / ہنگامی مسائل (< 12 گھنٹے)، درمیانے مسائل (24-48 گھنٹے)، اور معمول کی مرمت (3-5 دن) میں حل کی جاتی ہے۔"
        })
    elif "register" in msg_lower or "sign up" in msg_lower or "account" in msg_lower or "اکاؤنٹ" in msg_lower or "رجسٹر" in msg_lower:
        return jsonify({
            "reply": "You can sign up on AwamConnect by clicking 'Sign Up' in the header. We require your Name, CNIC, Phone Number, Residential Address, and Email to attach your grievance history to your citizen profile."
        })
    elif "electric" in msg_lower or "wire" in msg_lower or "bijli" in msg_lower or "current" in msg_lower or "تار" in msg_lower or "بجلی" in msg_lower:
        return jsonify({
            "reply": "Electrical hazards, fallen cables, and sparking wires are classified as HIGH PRIORITY (< 12 Hours SLA) and routed directly to K-Electric Rapid Response Cell."
        })
    elif "sewage" in msg_lower or "gutter" in msg_lower or "pani" in msg_lower or "water" in msg_lower or "پانی" in msg_lower or "گٹر" in msg_lower or "سیوریج" in msg_lower:
        return jsonify({
            "reply": "Sewage bursts, open manholes, and pipeline leaks are routed to Karachi Water & Sewerage Corporation (KW&SC). Street flooding and open manholes on roads are prioritized under < 12 Hours SLA."
        })
    elif "garbage" in msg_lower or "trash" in msg_lower or "kuda" in msg_lower or "safai" in msg_lower or "کوڑا" in msg_lower or "کچرا" in msg_lower:
        return jsonify({
            "reply": "Solid waste piles and unattended garbage heaps are assigned to the Sindh Solid Waste Management Board (SSWMB) with an SLA of 24-48 hours."
        })
    elif "submit" in msg_lower or "file" in msg_lower or "complaint" in msg_lower or "shikayat" in msg_lower or "شکایت" in msg_lower:
        return jsonify({
            "reply": "To lodge a complaint on AwamConnect, upload a clear photo of the civic hazard, write a short description in Urdu or English, and click 'Submit Grievance'. Our AI will automatically verify, assign a qualified worker from our roster, and dispatch your ticket."
        })
    else:
        return jsonify({
            "reply": "Welcome to AwamConnect Support. I can read your database records to check how many complaints you have filed, track ticket progress, explain pending work reasons, and provide assigned field worker contacts."
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
