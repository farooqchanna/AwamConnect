import os
from datetime import datetime
from enum import Enum
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

db = SQLAlchemy()

def get_database_uri() -> str:
    """
    Returns a normalized, ready-to-use database URI for Flask-SQLAlchemy.
    Handles Supabase PgBouncer strings by stripping '?pgbouncer=true' (which libpq rejects)
    and ensuring proper driver and sslmode settings.
    """
    uri = os.getenv("SQLALCHEMY_DATABASE_URI") or os.getenv("DATABASE_URL")
    if not uri:
        raise ValueError("Neither SQLALCHEMY_DATABASE_URI nor DATABASE_URL is set in environment.")

    # Remove pgbouncer query parameter (Prisma-specific; libpq/psycopg rejects it)
    uri = uri.replace("?pgbouncer=true&", "?")
    uri = uri.replace("?pgbouncer=true", "")
    uri = uri.replace("&pgbouncer=true", "")

    # Ensure postgresql+psycopg2 driver dialect is explicit when using psycopg2
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql+psycopg2://", 1)
    elif uri.startswith("postgresql://") and not uri.startswith("postgresql+"):
        uri = uri.replace("postgresql://", "postgresql+psycopg2://", 1)

    # Ensure sslmode=require for remote cloud databases (Supabase, Neon, AWS)
    if "sslmode=" not in uri and ("supabase.com" in uri or "neon.tech" in uri):
        sep = "&" if "?" in uri else "?"
        uri = f"{uri}{sep}sslmode=require"

    return uri


class PriorityLevel(str, Enum):
    HIGH = "HIGH"        # SLA: < 12 Hours
    MEDIUM = "MEDIUM"    # SLA: 24-48 Hours
    LOW = "LOW"          # SLA: 3-5 Days


class TicketStatus(str, Enum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RESOLVED = "RESOLVED"


class User(db.Model):
    """Citizen and Municipal Staff user entity."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(128), nullable=False)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(128), unique=True, nullable=False, index=True)
    cnic = db.Column(db.String(25), nullable=False)
    phone = db.Column(db.String(25), nullable=False)
    address = db.Column(db.Text, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default="citizen", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship to Complaints
    complaints = db.relationship("Complaint", backref="user", lazy=True)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "username": self.username,
            "email": self.email,
            "cnic": self.cnic,
            "phone": self.phone,
            "address": self.address,
            "role": self.role,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class Complaint(db.Model):
    """Citizen civic hazard complaint entity."""
    __tablename__ = "complaints"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ticket_uuid = db.Column(db.String(36), unique=True, nullable=False, index=True)

    # Optional Link to Registered Citizen
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    citizen_name = db.Column(db.String(128), nullable=True)
    citizen_cnic = db.Column(db.String(25), nullable=True)
    citizen_phone = db.Column(db.String(25), nullable=True)
    citizen_address = db.Column(db.String(256), nullable=True)

    # Citizen Grievance Input
    raw_description = db.Column(db.Text, nullable=False)
    language_detected = db.Column(db.String(10), default="en")  # 'ur' or 'en'
    image_url = db.Column(db.Text, nullable=True)               # base64 data URI or CDN URL

    # AI Multimodal Triage Outputs
    is_valid_civic_issue = db.Column(db.Boolean, default=True, nullable=False)
    detected_issue = db.Column(db.String(128), nullable=False)   # e.g., "Exposed active electrical cables"
    priority = db.Column(db.Enum(PriorityLevel), nullable=False, default=PriorityLevel.MEDIUM)
    sla_window = db.Column(db.String(64), nullable=False)        # e.g., "< 12 HOURS"
    target_agency = db.Column(db.String(128), nullable=False)    # e.g., "Rapid Response Municipal Cell / K-Electric"
    ai_reasoning = db.Column(db.Text, nullable=False)           # Justification for priority & routing

    # Assigned Municipal Worker
    assigned_worker_name = db.Column(db.String(128), nullable=True)
    assigned_worker_role = db.Column(db.String(64), nullable=True)
    assigned_worker_email = db.Column(db.String(128), nullable=True)
    assigned_worker_phone = db.Column(db.String(32), nullable=True)

    # Operational Status
    status = db.Column(db.Enum(TicketStatus), nullable=False, default=TicketStatus.PENDING)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "ticket_uuid": self.ticket_uuid,
            "user_id": self.user_id,
            "citizen_name": self.citizen_name,
            "citizen_cnic": self.citizen_cnic,
            "citizen_phone": self.citizen_phone,
            "citizen_address": self.citizen_address,
            "raw_description": self.raw_description,
            "language_detected": self.language_detected,
            "image_url": self.image_url,
            "is_valid_civic_issue": self.is_valid_civic_issue,
            "detected_issue": self.detected_issue,
            "priority": self.priority.value if hasattr(self.priority, "value") else str(self.priority),
            "sla_window": self.sla_window,
            "target_agency": self.target_agency,
            "ai_reasoning": self.ai_reasoning,
            "assigned_worker_name": self.assigned_worker_name,
            "assigned_worker_role": self.assigned_worker_role,
            "assigned_worker_email": self.assigned_worker_email,
            "assigned_worker_phone": self.assigned_worker_phone,
            "status": self.status.value if hasattr(self.status, "value") else str(self.status),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }


class Worker(db.Model):
    """Field Municipal Worker entity."""
    __tablename__ = "workers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    role = db.Column(db.String(64), nullable=False, index=True)
    name = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(128), nullable=False)
    phone = db.Column(db.String(32), nullable=False)
    is_available = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "role": self.role,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "is_available": self.is_available
        }


WORKERS_DATASET = [
    {"role": "Sewerage Worker", "name": "Muhammad Aslam", "email": "m.aslam.wasap@gmail.com", "phone": "+92 300 4581290"},
    {"role": "Sewerage Worker", "name": "Ramesh Kumar", "email": "ramesh.kumar92@yahoo.com", "phone": "+92 321 8723411"},
    {"role": "Sewerage Worker", "name": "Tariq Mehmood", "email": "tariq.mehmood.wasa@gmail.com", "phone": "+92 333 5190284"},
    {"role": "Road Builder", "name": "Abdul Rehman", "email": "abdul.rehman.nha@gmail.com", "phone": "+92 345 6289104"},
    {"role": "Road Builder", "name": "Ghulam Mustafa", "email": "g.mustafa.infra@yahoo.com", "phone": "+92 312 7741953"},
    {"role": "Road Builder", "name": "Noman Bashir", "email": "noman.bashir.cw@outlook.com", "phone": "+92 301 3904512"},
    {"role": "Electrician Worker", "name": "Muhammad Usman", "email": "usman.electrician99@gmail.com", "phone": "+92 302 8129471"},
    {"role": "Electrician Worker", "name": "Bilal Ahmed", "email": "bilal.ahmed.lesco@gmail.com", "phone": "+92 322 4618392"},
    {"role": "Electrician Worker", "name": "Farhan Akhtar", "email": "farhan.akhtar.power@outlook.com", "phone": "+92 334 9081275"},
    {"role": "Labour", "name": "Imran Ali", "email": "imran.ali.mazdoor@gmail.com", "phone": "+92 305 6742918"},
    {"role": "Labour", "name": "Zahid Hussain", "email": "zahid.hussain.site@yahoo.com", "phone": "+92 346 5123984"},
    {"role": "Labour", "name": "Kashif Mehmood", "email": "kashif.mehmood.ops@gmail.com", "phone": "+92 313 8847120"},
    {"role": "Garbage Collector", "name": "Nadeem Masih", "email": "nadeem.masih.lwmc@gmail.com", "phone": "+92 303 9182746"},
    {"role": "Garbage Collector", "name": "Babu Lal", "email": "babu.lal.sanitation@yahoo.com", "phone": "+92 324 7391058"},
    {"role": "Garbage Collector", "name": "Sajid Masih", "email": "sajid.masih.cdg@gmail.com", "phone": "+92 335 6201943"}
]


def init_db_schema(app):
    """
    Initializes database tables, performs safe migrations for new columns,
    and seeds workers dataset.
    """
    from sqlalchemy import text, inspect
    with app.app_context():
        # Create all tables (e.g. users, complaints, workers) if they do not exist
        db.create_all()

        # Migrate complaints table for user_id, citizen fields, and assigned workers
        try:
            with db.engine.connect() as conn:
                inspector = inspect(db.engine)
                cols = [c["name"] for c in inspector.get_columns("complaints")]
                
                alter_statements = []
                if "user_id" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
                if "citizen_name" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS citizen_name VARCHAR(128)")
                if "citizen_cnic" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS citizen_cnic VARCHAR(25)")
                if "citizen_phone" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS citizen_phone VARCHAR(25)")
                if "citizen_address" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS citizen_address VARCHAR(256)")
                
                # Assigned Worker columns
                if "assigned_worker_name" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS assigned_worker_name VARCHAR(128)")
                if "assigned_worker_role" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS assigned_worker_role VARCHAR(64)")
                if "assigned_worker_email" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS assigned_worker_email VARCHAR(128)")
                if "assigned_worker_phone" not in cols:
                    alter_statements.append("ALTER TABLE complaints ADD COLUMN IF NOT EXISTS assigned_worker_phone VARCHAR(32)")

                # Ensure image_url column is TEXT so base64 images of any size fit
                alter_statements.append("ALTER TABLE complaints ALTER COLUMN image_url TYPE TEXT")

                for stmt in alter_statements:
                    conn.execute(text(stmt))
                if alter_statements:
                    conn.commit()

            # Seed Workers table if empty
            if Worker.query.count() == 0:
                for w in WORKERS_DATASET:
                    db.session.add(Worker(
                        role=w["role"],
                        name=w["name"],
                        email=w["email"],
                        phone=w["phone"],
                        is_available=True
                    ))
                db.session.commit()
                app.logger.info("Successfully seeded 15 field workers from dataset.")

        except Exception as e:
            app.logger.warning(f"Database schema migration check notice: {e}")
