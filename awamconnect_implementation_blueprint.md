# AwamConnect: AI Municipal Triage System
## Implementation Blueprint & Architecture Specification

---

### 1. CORE OBJECTIVE
AwamConnect is a multimodal municipal triage web application that ingests citizen grievance reports via photo and bilingual text (Urdu/English), leverages the Gemini API to visually inspect damage, categorize issues, and assign priority tiers (High, Medium, Low) with automated agency routing (e.g., KMC, HMC, Water Board, K-Electric, Solid Waste Management), and provides an integrated, context-restricted Gemini support chatbot to assist citizens strictly with application workflows and ticket resolution.

---

### 2. TECH STACK & DEPLOYMENT CONSTRAINTS

* **Backend & Web Framework:** Python 3.10+, Flask 3.x, Werkzeug.
* **ORM & Database:** Flask-SQLAlchemy 3.x, connecting to a remote serverless-compatible PostgreSQL instance (Neon, Supabase, or Railway PostgreSQL via `psycopg2-binary`).
* **AI & Multimodal SDK:** `google-genai` (official Google GenAI SDK) initialized with `GEMINI_API_KEY`, supporting multimodal image inspection (`gemini-2.5-flash` or `gemini-1.5-flash`) with structured JSON output and system instructions for the support bot.
* **Serverless Deployment Entry Point (`api/index.py`):**
  * Vercel looks for a WSGI handler inside `api/index.py`. The Flask application instance must be exposed as `app` inside this entry point.
* **Serverless Configuration (`vercel.json`):**
  ```json
  {
    "version": 2,
    "builds": [
      {
        "src": "api/index.py",
        "use": "@vercel/python"
      }
    ],
    "routes": [
      {
        "src": "/(.*)",
        "dest": "api/index.py"
      }
    ]
  }
  ```
* **Dependency Management (`requirements.txt`):**
  Must sit at the project root with exact pins:
  ```text
  Flask>=3.0.0
  Flask-SQLAlchemy>=3.1.0
  psycopg2-binary>=2.9.9
  google-genai>=0.1.0
  Pillow>=10.0.0
  python-dotenv>=1.0.0
  ```
* **Statelessness Constraint:** No local file persistence. Uploaded citizen complaint images must be handled in memory via `io.BytesIO`, read as raw bytes for the Gemini Vision call, or piped directly to remote object storage (Cloudinary, AWS S3, or Supabase Storage) to return a persistent image URL.

---

### 3. USER FLOW & FEATURES

1. **Public Intake & Complaint Submission (Citizen View):**
   * The citizen accesses the landing page (no mandatory authentication required per submission checklist).
   * The user selects or captures a photo (pothole, live wire, sewage leak, garbage dump) and inputs a brief grievance description in Urdu or English.
   * The user submits the form. The UI displays an active loading state while Gemini evaluates the visual evidence.
2. **Automated AI Triage & Verification Pipeline:**
   * The backend forwards the image bytes and text to Gemini with a rigid JSON schema instruction.
   * Gemini validates whether genuine civic damage is visible, categorizes the grievance, assigns an urgency score (`HIGH`, `MEDIUM`, `LOW`), determines the target agency, and generates a bilingual justification.
   * A ticket record is written to the remote PostgreSQL database.
3. **Citizen Real-Time Receipt & Triage Summary:**
   * The user is immediately presented with their generated Ticket ID, SLA target (e.g., `< 12 Hours` for High, `24-48 Hours` for Medium, `3-5 Days` for Low), assigned municipal agency, and the AI's visual reasoning tags.
4. **Municipal Dispatcher Review Dashboard:**
   * A clean overview portal displaying incoming tickets in a filterable table (by agency, priority, and date).
   * Includes one-click JSON export for field dispatch and a status toggler (`PENDING`, `DISPATCHED`, `RESOLVED`).
5. **Context-Restricted Gemini Support Assistant (Floating Widget):**
   * Accessible via a persistent floating chat bubble across citizen views.
   * Enables citizens to inquire about ticket statuses, SLAs, municipal agency remits, and how to file complaints.
   * Strictly enforces system instructions: attempts to query off-topic trivia, code, politics, or general knowledge are politely rejected with a canned civic-assistance disclaimer.

---

### 4. DATA MODELS & DATABASE SCHEMA

```python
from datetime import datetime
from enum import Enum
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class PriorityLevel(str, Enum):
    HIGH = "HIGH"        # SLA: < 12 Hours
    MEDIUM = "MEDIUM"    # SLA: 24-48 Hours
    LOW = "LOW"          # SLA: 3-5 Days

class TicketStatus(str, Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RESOLVED = "RESOLVED"

class Complaint(db.Model):
    __tablename__ = "complaints"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ticket_uuid = db.Column(db.String(36), unique=True, nullable=False, index=True)
    
    # Citizen Input
    raw_description = db.Column(db.Text, nullable=False)
    language_detected = db.Column(db.String(10), default="en")  # 'ur' or 'en'
    image_url = db.Column(db.String(512), nullable=True)         # S3/Cloudinary or base64 data URI
    
    # AI Multimodal Triage Outputs
    is_valid_civic_issue = db.Column(db.Boolean, default=True, nullable=False)
    detected_issue = db.Column(db.String(128), nullable=False)   # e.g., "Exposed active electrical cables"
    priority = db.Column(db.Enum(PriorityLevel), nullable=False, default=PriorityLevel.MEDIUM)
    sla_window = db.Column(db.String(64), nullable=False)        # e.g., "< 12 HOURS"
    target_agency = db.Column(db.String(128), nullable=False)    # e.g., "Rapid Response Municipal Cell / K-Electric"
    ai_reasoning = db.Column(db.Text, nullable=False)           # Justification for priority & routing
    
    # Operational Status
    status = db.Column(db.Enum(TicketStatus), nullable=False, default=TicketStatus.PENDING)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "ticket_uuid": self.ticket_uuid,
            "raw_description": self.raw_description,
            "language_detected": self.language_detected,
            "image_url": self.image_url,
            "is_valid_civic_issue": self.is_valid_civic_issue,
            "detected_issue": self.detected_issue,
            "priority": self.priority.value,
            "sla_window": self.sla_window,
            "target_agency": self.target_agency,
            "ai_reasoning": self.ai_reasoning,
            "status": self.status.value,
            "created_at": self.created_at.isoformat()
        }
```

---

### 5. API / BACKEND ENDPOINTS

#### Intake & Core Operations

* **`POST /api/complaints/submit`**
  * **Payload:** `multipart/form-data` containing `image` (file binary) and `description` (string).
  * **Operations:** 
    1. Validate presence of file and text.
    2. Convert image bytes for Gemini Vision.
    3. Invoke Gemini Vision model with a rigid JSON prompt schema implementing the Priority Matrix:
       * **HIGH (< 12 hrs):** Exposed active electrical cables, burst main sewage pipes with flooding, open manholes on active roads.
       * **MEDIUM (24-48 hrs):** Large potholes causing traffic congestion, broken streetlamps, commercial garbage heaps.
       * **LOW (3-5 days):** Faded lane markings, unauthorized wall graffiti, non-hazardous yard debris, broken park benches.
    4. Save parsed record to database via Flask-SQLAlchemy.
  * **Response:** `201 Created` with full JSON complaint details and assigned ticket UUID.

* **`GET /api/complaints`**
  * **Payload:** Query parameters `?priority=HIGH&status=PENDING&agency=KMC`.
  * **Operations:** Queries database for filtered complaints; used by the municipal dispatcher table.
  * **Response:** `200 OK` with array of complaint objects.

* **`GET /api/complaints/<ticket_uuid>`**
  * **Operations:** Fetches individual ticket status and audit log.
  * **Response:** `200 OK` or `404 Not Found`.

* **`PATCH /api/complaints/<ticket_uuid>/status`**
  * **Payload:** `{"status": "DISPATCHED" | "RESOLVED"}`.
  * **Operations:** Updates ticket workflow status in database.
  * **Response:** `200 OK` with updated status.

* **`GET /api/complaints/export`**
  * **Operations:** Dumps active or resolved complaints into formatted JSON for municipal field team dispatch.
  * **Response:** `200 OK` (`Content-Disposition: attachment; filename="triage_export.json"`).

#### Dedicated Gemini Support Chatbot Endpoint

* **`POST /api/chat/support`**
  * **Payload:**
    ```json
    {
      "message": "How long will it take to fix an open manhole?",
      "ticket_uuid": "optional-uuid-context"
    }
    ```
  * **Backend Implementation Logic:**
    1. Ingest user message and optional `ticket_uuid`. If a ticket UUID is supplied, query the database and inject ticket attributes (priority, agency, status) into prompt context.
    2. Construct the Gemini client using `GEMINI_API_KEY`.
    3. Initialize the model with immutable system instructions locking domain context:
       ```text
       You are AwamConnect Support Assistant, an AI dedicated solely to the AwamConnect Municipal Grievance Redressal platform in Sindh (covering KMC, HMC, Water Board, K-Electric, Solid Waste Management, etc.).

       STRICT BOUNDARIES:
       - You answer ONLY questions regarding AwamConnect features, grievance filing, ticket status checks, SLA windows, and municipal agency jurisdictions.
       - SLA Windows: High Priority (< 12 hours), Medium Priority (24-48 hours), Low Priority (3-5 days).
       - NEVER answer general knowledge, coding, homework, political, casual, or off-topic questions.
       - If asked an off-topic question, refuse strictly: "I am only authorized to assist with AwamConnect municipal complaints, ticket status inquiries, and city agency routing."
       ```
    4. Send prompt to Gemini API and return response text.
  * **Response:**
    ```json
    {
      "reply": "According to AwamConnect triage protocols, an open manhole is classified as High Priority (Rapid Response Municipal Cell) with an SLA resolution window of under 12 hours."
    }
    ```

---

### 6. EDGE CASES, TRAPS & VERCEL WARNINGS

* **Vercel Ephemeral Storage (No SQLite):**
  * *Trap:* Defaulting to `sqlite:///app.db` will wipe data on every serverless cold start or spin up isolated instances per execution container.
  * *Mitigation:* A hosted external PostgreSQL database (e.g., Neon or Supabase) must be set via `DATABASE_URL` in Vercel Environment Variables.
* **Vercel Read-Only Filesystem & `/tmp` Limits:**
  * *Trap:* Attempting `image.save("uploads/" + filename)` will trigger a `Read-only file system` error on Vercel.
  * *Mitigation:* Read files directly into memory (`file.read()`) as `io.BytesIO` objects, pass image data directly to the Gemini API via inline base64 or raw bytes, and avoid local disk writes.
* **Serverless Execution Timeouts:**
  * *Trap:* Vercel Hobby tier caps function execution at 10–15 seconds. High-resolution images submitted to multimodal models can breach this limit.
  * *Mitigation:* Downscale or compress client-side images before upload, select `gemini-2.5-flash` or `gemini-1.5-flash` for rapid latency, and set timeout handlers on API calls.
* **API Key Security & Submission Pre-Flight:**
  * *Trap:* Hardcoding `GEMINI_API_KEY` in git repositories triggers disqualification on secret-scanning checks.
  * *Mitigation:* Store keys exclusively in `.env` (included in `.gitignore`) and inject them via the Vercel dashboard. Verify before push.
* **Multimodal Edge Cases (Rubric Hour 4 Verification):**
  * *Night/Low-Light/Blurry Photos:* Gemini must handle ambiguous images gracefully. The JSON schema must require an `is_valid_civic_issue` flag. If a photo is a selfie, clean road, or unidentifiable blur, mark `is_valid_civic_issue: false` and prompt the user to re-upload rather than misrouting.
* **Bilingual Text Processing (Urdu / English):**
  * Explicitly instruct Gemini to parse Nastaliq/Urdu script descriptions and map them to standard English taxonomy while retaining bilingual output for the citizen receipt.