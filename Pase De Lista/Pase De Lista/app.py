import csv
import base64
from contextlib import contextmanager
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import qrcode
from qrcode.image.svg import SvgPathImage


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("PASELISTA_DB", ROOT / "pase_lista.sqlite3"))
HOST = os.getenv("PASELISTA_HOST", "127.0.0.1")
PORT = int(os.getenv("PASELISTA_PORT", "8000"))
DEMO_MODE = os.getenv("PASELISTA_DEMO_MODE", "1").lower() in {"1", "true", "yes"}
DEMO_ACCESS_CODE = os.getenv("PASELISTA_DEMO_ACCESS_CODE") or secrets.token_urlsafe(9)
ADMIN_EMAIL = os.getenv("PASELISTA_ADMIN_EMAIL", "haressanchez506@gmail.com").strip().lower()
TOKEN_TTL_SECONDS = int(os.getenv("PASELISTA_TOKEN_TTL_SECONDS", "60"))
MAX_TOKEN_ATTEMPTS = 5
SESSION_LIFETIME_HOURS = 12
COOKIE_SECURE = os.getenv("PASELISTA_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
DEFAULT_TIMEZONE = os.getenv("PASELISTA_TIMEZONE", "America/Mexico_City")
TWILIO_ACCOUNT_SID = os.getenv("PASELISTA_TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("PASELISTA_TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("PASELISTA_TWILIO_FROM_NUMBER", "")
DEMO_ACCOUNTS = {
    "admin@demo.pasadelista.local": ("Administración de demostración", "SUPER_ADMINISTRADOR"),
    "docente@demo.pasadelista.local": ("Docente de demostración", "PROFESOR"),
    "alumno@demo.pasadelista.local": ("Estudiante de demostración", "ESTUDIANTE"),
}
TOKEN_LOCK = threading.Lock()
PIN_PEPPER = secrets.token_bytes(32)


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat(timespec="seconds")


def valid_timezone_name(value):
    return value == "UTC" or bool(
        re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+", value)
    )


def migrate_legacy_attendance(connection, school_id, teacher_id):
    if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 1").fetchone():
        return
    legacy_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'attendance'"
    ).fetchone()
    if legacy_table:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(attendance)").fetchall()
        }
        required = {"user_id", "group_name", "career", "attendance_date", "arrived_at"}
        if not required.issubset(columns):
            raise RuntimeError("La tabla de asistencia anterior tiene un formato no reconocido")
        groups = {}
        sessions = {}
        rows = connection.execute(
            """
            SELECT user_id, group_name, career, attendance_date, arrived_at
            FROM attendance ORDER BY attendance_date, arrived_at
            """
        ).fetchall()
        for row in rows:
            original_name = str(row["group_name"]).strip()
            career = str(row["career"]).strip()
            if not original_name or not career:
                raise RuntimeError("Hay una asistencia anterior sin grupo o carrera")
            key = (original_name, career)
            if key not in groups:
                group = connection.execute(
                    "SELECT id, career FROM class_groups WHERE school_id = ? AND name = ?",
                    (school_id, original_name),
                ).fetchone()
                group_name = original_name
                if group and group["career"] != career:
                    group_name = f"{original_name} ({career})"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO class_groups (school_id, name, career)
                    VALUES (?, ?, ?)
                    """,
                    (school_id, group_name, career),
                )
                group_id = connection.execute(
                    "SELECT id FROM class_groups WHERE school_id = ? AND name = ?",
                    (school_id, group_name),
                ).fetchone()["id"]
                groups[key] = group_id
                connection.execute(
                    "INSERT OR IGNORE INTO group_teachers (group_id, user_id) VALUES (?, ?)",
                    (group_id, teacher_id),
                )
            group_id = groups[key]
            attendance_date = date.fromisoformat(str(row["attendance_date"])[:10])
            session_key = (group_id, attendance_date)
            start = datetime.combine(attendance_date, time.min, timezone.utc)
            if session_key not in sessions:
                cursor = connection.execute(
                    """
                    INSERT INTO class_sessions
                        (group_id, teacher_id, started_at, closes_at, active)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (
                        group_id,
                        teacher_id,
                        start.isoformat(timespec="seconds"),
                        (start + timedelta(days=1)).isoformat(timespec="seconds"),
                    ),
                )
                sessions[session_key] = cursor.lastrowid
            registered_at = str(row["arrived_at"])
            try:
                registered = datetime.fromisoformat(registered_at.replace(" ", "T"))
            except ValueError:
                registered = start
            if registered.tzinfo is None:
                registered = registered.replace(tzinfo=timezone.utc)
            registered_at = registered.astimezone(timezone.utc).isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT OR IGNORE INTO attendance_records (session_id, user_id, registered_at)
                VALUES (?, ?, ?)
                """,
                (sessions[session_key], row["user_id"], registered_at),
            )
            if connection.execute(
                """
                SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id
                WHERE ur.user_id = ? AND r.name = 'ESTUDIANTE'
                """,
                (row["user_id"],),
            ).fetchone():
                connection.execute(
                    "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
                    (group_id, row["user_id"]),
                )
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
        (iso_now(),),
    )


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    global PIN_PEPPER
    if not valid_timezone_name(DEFAULT_TIMEZONE):
        raise ValueError("PASELISTA_TIMEZONE debe ser un identificador de zona horaria válido")
    if os.getenv("PASELISTA_SECRET") and len(os.environ["PASELISTA_SECRET"].encode("utf-8")) < 32:
        raise ValueError("PASELISTA_SECRET debe contener al menos 32 bytes")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        if not connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 2"
        ).fetchone():
            school_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(schools)").fetchall()
            }
            if {"latitude", "longitude"}.issubset(school_columns):
                connection.execute("UPDATE schools SET latitude = NULL, longitude = NULL")
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (2, ?)",
                (iso_now(),),
            )
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "matricula" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN matricula TEXT")
        if "guardian_phone" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN guardian_phone TEXT")
        if "qr_token_hash" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN qr_token_hash TEXT")
        if "password_hash" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_matricula "
            "ON users(matricula) WHERE matricula IS NOT NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_qr_token "
            "ON users(qr_token_hash) WHERE qr_token_hash IS NOT NULL"
        )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                client_key TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                window_started_at TEXT NOT NULL,
                blocked_until TEXT
            );
            """
        )
        connection.executemany(
            "INSERT OR IGNORE INTO roles (name, description) VALUES (?, ?)",
            [
                ("SUPER_ADMINISTRADOR", "Administra escuelas y configuración"),
                ("PROFESOR", "Gestiona sesiones y consulta asistencia"),
                ("ESTUDIANTE", "Registra su asistencia"),
            ],
        )
        for email, (name, role) in DEMO_ACCOUNTS.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO users (email, full_name, provider, external_id, active)
                VALUES (?, ?, 'demo', ?, 1)
                """,
                (email, name, "demo:" + email),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO user_roles (user_id, role_id)
                SELECT u.id, r.id FROM users u JOIN roles r ON r.name = ?
                WHERE u.email = ?
                """,
                (role, email),
            )
        if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}", ADMIN_EMAIL):
            raise ValueError("PASELISTA_ADMIN_EMAIL debe ser un correo válido")
        connection.execute(
            """
            INSERT OR IGNORE INTO users (email, full_name, provider, external_id, active)
            VALUES (?, 'Administrador principal', 'password', ?, 1)
            """,
            (ADMIN_EMAIL, "password:" + ADMIN_EMAIL),
        )
        admin_user = connection.execute(
            "SELECT id, password_hash FROM users WHERE email = ?", (ADMIN_EMAIL,)
        ).fetchone()
        connection.execute(
            """
            INSERT OR IGNORE INTO user_roles (user_id, role_id)
            SELECT ?, id FROM roles WHERE name = 'SUPER_ADMINISTRADOR'
            """,
            (admin_user["id"],),
        )
        initial_admin_password = os.getenv("PASELISTA_ADMIN_PASSWORD", "")
        if initial_admin_password and not admin_user["password_hash"]:
            validate_password(initial_admin_password)
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_digest(initial_admin_password), admin_user["id"]),
            )

        connection.execute(
            """
            INSERT OR IGNORE INTO schools (name, timezone)
            VALUES ('Plantel de demostración', ?)
            """,
            (DEFAULT_TIMEZONE,),
        )
        school_id = connection.execute(
            "SELECT id FROM schools WHERE name = 'Plantel de demostración'"
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT OR IGNORE INTO class_groups (school_id, name, career)
            VALUES (?, '401', 'Programación')
            """,
            (school_id,),
        )
        group_id = connection.execute(
            "SELECT id FROM class_groups WHERE school_id = ? AND name = '401'",
            (school_id,),
        ).fetchone()["id"]
        teacher_id = connection.execute(
            "SELECT id FROM users WHERE email = 'docente@demo.pasadelista.local'"
        ).fetchone()["id"]
        student_id = connection.execute(
            "SELECT id FROM users WHERE email = 'alumno@demo.pasadelista.local'"
        ).fetchone()["id"]
        connection.execute(
            "INSERT OR IGNORE INTO group_teachers (group_id, user_id) VALUES (?, ?)",
            (group_id, teacher_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
            (group_id, student_id),
        )
        migrate_legacy_attendance(connection, school_id, teacher_id)
        if not os.getenv("PASELISTA_SECRET"):
            setting = connection.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = 'pin_pepper'"
            ).fetchone()
            if setting:
                PIN_PEPPER = bytes.fromhex(setting["setting_value"])
            else:
                connection.execute(
                    "INSERT INTO app_settings (setting_key, setting_value) VALUES ('pin_pepper', ?)",
                    (PIN_PEPPER.hex(),),
                )
        else:
            PIN_PEPPER = os.environ["PASELISTA_SECRET"].encode("utf-8")
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (iso_now(),))
        # Without an externally managed secret, PINs are intentionally invalidated at restart.
        if not os.getenv("PASELISTA_SECRET"):
            connection.execute(
                "UPDATE class_sessions SET pin_hash = NULL, pin_expires_at = NULL WHERE active = 1"
            )


def audit(connection, actor_id, action, object_type, object_id=None):
    connection.execute(
        """
        INSERT INTO audit_events (actor_id, action, object_type, object_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (actor_id, action, object_type, object_id, iso_now()),
    )


def pin_digest(session_id, pin):
    message = f"{session_id}:{pin}".encode("utf-8")
    return hmac.new(PIN_PEPPER, message, hashlib.sha256).hexdigest()


def normalize_guardian_phone(value):
    phone = re.sub(r"[\s()-]", "", str(value))
    if re.fullmatch(r"\d{10}", phone):
        phone = "+52" + phone
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise ValueError("El número del tutor debe incluir 10 dígitos o estar en formato internacional")
    return phone


def send_guardian_sms(phone, matricula):
    configuration = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER)
    if not all(configuration):
        return {"sent": False, "message": "Aviso al tutor no enviado: configura el servicio SMS."}
    message = (
        f"¡Hola! Nos da gusto avisarte que tu hij@, con matrícula {matricula}, "
        "ya llegó al plantel y completó su pase de lista. ¡Que tenga un excelente día! 🌟"
    )
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    payload = urlencode({"To": phone, "From": TWILIO_FROM_NUMBER, "Body": message}).encode("utf-8")
    request = Request(url, data=payload, method="POST")
    credentials = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")
    ).decode("ascii")
    request.add_header("Authorization", f"Basic {credentials}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urlopen(request, timeout=8) as response:
            if response.status not in {200, 201}:
                return {"sent": False, "message": "El registro se guardó, pero el SMS no pudo enviarse."}
    except (HTTPError, URLError, TimeoutError):
        return {"sent": False, "message": "El registro se guardó, pero el SMS no pudo enviarse."}
    return {"sent": True, "message": "Aviso enviado al tutor."}


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)


def request_json(handler):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as error:
        raise ValueError("Longitud de solicitud inválida") from error
    if length < 0 or length > 16_384:
        raise ValueError("La solicitud supera el tamaño permitido")
    try:
        value = json.loads(handler.rfile.read(length) or b"{}")
    except json.JSONDecodeError as error:
        raise ValueError("El cuerpo debe contener JSON válido") from error
    if not isinstance(value, dict):
        raise ValueError("El cuerpo JSON debe ser un objeto")
    return value


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


PASSWORD_ITERATIONS = 310_000


def validate_password(password):
    if (
        not isinstance(password, str)
        or len(password) < 12
        or len(password) > 128
        or any(ord(char) < 32 for char in password)
    ):
        raise ValueError("La contraseña debe tener entre 12 y 128 caracteres")
    return password


def password_digest(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password, encoded):
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def reset_code_hash(user_id, code):
    return hmac.new(PIN_PEPPER, f"password-reset:{user_id}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def send_password_reset_email(email, code):
    smtp_user = os.getenv("PASELISTA_SMTP_USER", "")
    smtp_password = os.getenv("PASELISTA_SMTP_PASSWORD", "")
    if not smtp_user or not smtp_password:
        raise RuntimeError("Configura PASELISTA_SMTP_USER y PASELISTA_SMTP_PASSWORD")
    message = EmailMessage()
    message["Subject"] = "Código para recuperar tu cuenta de Pase de Lista"
    message["From"] = os.getenv("PASELISTA_SMTP_FROM", smtp_user)
    message["To"] = email
    message.set_content(
        f"Tu código de recuperación es {code}. Vence en 10 minutos. "
        "Si no solicitaste este código, ignora este correo."
    )
    host = os.getenv("PASELISTA_SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("PASELISTA_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(message)


def new_qr_credential():
    token = secrets.token_urlsafe(32)
    svg = qrcode.make(f"PL1:{token}", image_factory=SvgPathImage).to_string().decode("utf-8")
    return token, svg


def authenticated_user(handler):
    parsed = cookies.SimpleCookie(handler.headers.get("Cookie", ""))
    session_cookie = parsed.get("session")
    if not session_cookie:
        return None
    with db() as connection:
        now = iso_now()
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
        session = connection.execute(
            """
            SELECT s.csrf_token, u.id, u.email, u.full_name, u.active,
                   GROUP_CONCAT(r.name) AS roles
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN user_roles ur ON ur.user_id = u.id
            LEFT JOIN roles r ON r.id = ur.role_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1
            GROUP BY s.token_hash
            """,
            (token_hash(session_cookie.value), now),
        ).fetchone()
    if not session:
        return None
    return dict(session)


def is_admin(user):
    return user and "SUPER_ADMINISTRADOR" in (user["roles"] or "").split(",")


def has_role(user, role):
    return user and role in (user["roles"] or "").split(",")


def clean_text(data, key, label, maximum=120):
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{label} es obligatorio")
    value = value.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} no es válido")
    return value


def csrf_valid(handler, user):
    return hmac.compare_digest(
        handler.headers.get("X-CSRF-Token", ""),
        user.get("csrf_token", ""),
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "PaseListaDemo/2"

    def parse_request(self):
        if self.raw_requestline.startswith(b"\x16\x03"):
            self.log_error(
                "Se recibió HTTPS en un servidor HTTP. Abre http://IP-DE-LA-PC:%s en el celular.",
                self.server.server_port,
            )
            return False
        return super().parse_request()

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args))

    def send_html(self):
        html = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(html)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_html()
            return
        static_files = {
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
            "/service-worker.js": ("service-worker.js", "text/javascript"),
            "/app-icon.svg": ("app-icon.svg", "image/svg+xml"),
            "/vendor/jsQR.js": ("vendor/jsQR.js", "text/javascript"),
        }
        if path in static_files:
            file_name, content_type = static_files[path]
            try:
                body = (ROOT / file_name).read_bytes()
            except OSError:
                json_response(self, 404, {"error": "Recurso no encontrado"})
                return
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        user = authenticated_user(self)
        if path == "/api/config":
            json_response(
                self,
                200,
                {
                    "demo_mode": DEMO_MODE,
                    "timezone": DEFAULT_TIMEZONE,
                    "authentication": "password",
                    "password_recovery": bool(
                        os.getenv("PASELISTA_SMTP_USER") and os.getenv("PASELISTA_SMTP_PASSWORD")
                    ),
                },
            )
        elif path == "/api/me":
            json_response(
                self,
                200,
                {"authenticated": bool(user), "user": user, "csrf_token": user["csrf_token"] if user else None},
            )
        elif path == "/api/dashboard":
            if not user:
                json_response(self, 401, {"error": "Inicia sesión para continuar"})
            else:
                self.dashboard(user)
        elif path == "/api/attendance":
            self.attendance_list(user)
        elif path == "/api/attendance.csv":
            self.attendance_csv(user)
        else:
            json_response(self, 404, {"error": "Ruta no encontrada"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/password-reset/request":
                self.request_password_reset()
                return
            if path == "/api/password-reset/confirm":
                self.confirm_password_reset()
                return
            if path == "/api/login":
                self.login()
                return
            user = authenticated_user(self)
            if not user:
                json_response(self, 401, {"error": "Inicia sesión para continuar"})
                return
            if not csrf_valid(self, user):
                json_response(self, 403, {"error": "Token de seguridad inválido; actualiza la página"})
                return
            routes = {
                "/api/logout": self.logout,
                "/api/schools": lambda: self.create_school(user),
                "/api/users": lambda: self.create_user(user),
                "/api/groups": lambda: self.create_group(user),
                "/api/enrollments": lambda: self.enroll_student(user),
                "/api/sessions/start": lambda: self.start_session(user),
                "/api/attendance": lambda: self.register_attendance(user),
            }
            if path in routes:
                routes[path]()
                return
            segments = path.strip("/").split("/")
            if len(segments) == 4 and segments[:2] == ["api", "sessions"] and segments[3] == "pin":
                self.rotate_pin(user, segments[2])
            elif len(segments) == 4 and segments[:2] == ["api", "sessions"] and segments[3] == "close":
                self.close_session(user, segments[2])
            elif len(segments) == 4 and segments[:2] == ["api", "sessions"] and segments[3] == "scan":
                self.scan_student_qr(user, segments[2])
            elif len(segments) == 4 and segments[:2] == ["api", "users"] and segments[3] == "qr":
                self.issue_student_qr(user, segments[2])
            else:
                json_response(self, 404, {"error": "Ruta no encontrada"})
        except ValueError as error:
            json_response(self, 400, {"error": str(error)})
        except sqlite3.IntegrityError as error:
            message = str(error)
            if "attendance_records.session_id, attendance_records.user_id" in message:
                json_response(self, 409, {"error": "Tu asistencia ya está registrada para esta clase"})
            elif "UNIQUE constraint failed: class_groups.school_id, class_groups.name" in message:
                json_response(self, 409, {"error": "Ya existe ese grupo en el plantel"})
            elif "UNIQUE constraint failed: schools.name" in message:
                json_response(self, 409, {"error": "Ya existe un plantel con ese nombre"})
            else:
                json_response(self, 409, {"error": "La operación entra en conflicto con los datos existentes"})

    def login(self):
        data = request_json(self)
        email = str(data.get("email", "")).strip().lower()
        access_code = data.get("access_code", "")
        password = data.get("password", "")
        client_key = self.client_address[0]
        with db() as connection:
            now = utc_now()
            now_text = now.isoformat(timespec="seconds")
            connection.execute(
                """
                DELETE FROM login_attempts
                WHERE (blocked_until IS NOT NULL AND blocked_until <= ?)
                   OR (blocked_until IS NULL AND window_started_at <= ?)
                """,
                (
                    now_text,
                    (now - timedelta(minutes=15)).isoformat(timespec="seconds"),
                ),
            )
            attempt = connection.execute(
                "SELECT * FROM login_attempts WHERE client_key = ?", (client_key,)
            ).fetchone()
            if attempt and attempt["blocked_until"] and datetime.fromisoformat(
                attempt["blocked_until"]
            ) > now:
                json_response(self, 429, {"error": "Demasiados intentos; espera 15 minutos"})
                return
            account = connection.execute(
                "SELECT password_hash FROM users WHERE email = ? AND active = 1",
                (email,),
            ).fetchone()
            valid_password = password_matches(
                password, account["password_hash"] if account else None
            )
            valid_demo_code = (
                DEMO_MODE
                and (
                    email in DEMO_ACCOUNTS
                    or (account and account["password_hash"] is None and email != ADMIN_EMAIL)
                )
                and isinstance(access_code, str)
                and hmac.compare_digest(access_code, DEMO_ACCESS_CODE)
            )
            if not email or not (valid_password or valid_demo_code):
                in_window = bool(
                    attempt
                    and now - datetime.fromisoformat(attempt["window_started_at"])
                    < timedelta(minutes=15)
                )
                attempts = attempt["attempts"] + 1 if in_window else 1
                window_started = attempt["window_started_at"] if in_window else now.isoformat(timespec="seconds")
                blocked_until = (
                    (now + timedelta(minutes=15)).isoformat(timespec="seconds")
                    if attempts >= 5
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO login_attempts (client_key, attempts, window_started_at, blocked_until)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(client_key) DO UPDATE SET
                      attempts = excluded.attempts,
                      window_started_at = excluded.window_started_at,
                      blocked_until = excluded.blocked_until
                    """,
                    (client_key, attempts, window_started, blocked_until),
                )
                json_response(self, 403, {"error": "Correo o contraseña incorrectos"})
                return
            connection.execute("DELETE FROM login_attempts WHERE client_key = ?", (client_key,))
            user = connection.execute(
                """
                SELECT u.id, u.email, u.full_name, u.active, GROUP_CONCAT(r.name) AS roles
                FROM users u
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id
                WHERE u.email = ? AND u.active = 1
                GROUP BY u.id
                """,
                (email,),
            ).fetchone()
            if not user:
                json_response(self, 403, {"error": "La cuenta no existe, está desactivada o no tiene un rol asignado"})
                return
            if not (
                is_admin(user)
                or has_role(user, "PROFESOR")
                or has_role(user, "ESTUDIANTE")
            ):
                json_response(self, 403, {"error": "La cuenta no tiene un rol habilitado"})
                return
            session_token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(24)
            created_at = now.isoformat(timespec="seconds")
            expires_at = (now + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT INTO auth_sessions (token_hash, csrf_token, user_id, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash(session_token), csrf_token, user["id"], expires_at, created_at),
            )
            audit(connection, user["id"], "login", "user", user["id"])
        payload = {
            "ok": True,
            "message": "Sesión iniciada correctamente",
            "user": {"email": user["email"], "name": user["full_name"], "roles": user["roles"]},
            "csrf_token": csrf_token,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        cookie = f"session={session_token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_LIFETIME_HOURS * 3600}"
        if COOKIE_SECURE:
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def request_password_reset(self):
        data = request_json(self)
        email = str(data.get("email", "")).strip().lower()
        if not os.getenv("PASELISTA_SMTP_USER") or not os.getenv("PASELISTA_SMTP_PASSWORD"):
            json_response(self, 503, {"error": "La recuperación por correo aún no está configurada"})
            return
        generic_response = {
            "ok": True,
            "message": "Si la cuenta existe, recibirás un código por correo.",
        }
        if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}", email):
            json_response(self, 200, generic_response)
            return

        now = utc_now()
        code = f"{secrets.randbelow(1_000_000):06d}"
        with db() as connection:
            account = connection.execute(
                """
                SELECT id FROM users
                WHERE email = ? AND active = 1 AND password_hash IS NOT NULL
                """,
                (email,),
            ).fetchone()
            if not account:
                json_response(self, 200, generic_response)
                return
            existing = connection.execute(
                "SELECT created_at FROM password_reset_codes WHERE user_id = ?",
                (account["id"],),
            ).fetchone()
            if existing and now - datetime.fromisoformat(existing["created_at"]) < timedelta(minutes=1):
                json_response(self, 200, generic_response)
                return
            connection.execute(
                """
                INSERT INTO password_reset_codes (user_id, code_hash, expires_at, created_at, attempts)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    code_hash = excluded.code_hash,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at,
                    attempts = 0
                """,
                (
                    account["id"],
                    reset_code_hash(account["id"], code),
                    (now + timedelta(minutes=10)).isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            user_id = account["id"]
        try:
            send_password_reset_email(email, code)
        except (OSError, RuntimeError, smtplib.SMTPException) as error:
            with db() as connection:
                connection.execute(
                    "DELETE FROM password_reset_codes WHERE user_id = ?", (user_id,)
                )
            print(f"No se pudo enviar el código de recuperación: {error}")
            json_response(self, 503, {"error": "No se pudo enviar el correo de recuperación"})
            return
        json_response(self, 200, generic_response)

    def confirm_password_reset(self):
        data = request_json(self)
        email = str(data.get("email", "")).strip().lower()
        code = data.get("code", "")
        password = validate_password(data.get("password"))
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
            raise ValueError("El código debe tener seis dígitos")
        with db() as connection:
            account = connection.execute(
                """
                SELECT id FROM users
                WHERE email = ? AND active = 1 AND password_hash IS NOT NULL
                """,
                (email,),
            ).fetchone()
            reset = (
                connection.execute(
                    "SELECT * FROM password_reset_codes WHERE user_id = ?",
                    (account["id"],),
                ).fetchone()
                if account
                else None
            )
            now = utc_now()
            if (
                not account
                or not reset
                or reset["attempts"] >= 5
                or datetime.fromisoformat(reset["expires_at"]) <= now
                or not hmac.compare_digest(
                    reset["code_hash"], reset_code_hash(account["id"], code)
                )
            ):
                if reset and reset["attempts"] < 5:
                    connection.execute(
                        "UPDATE password_reset_codes SET attempts = attempts + 1 WHERE user_id = ?",
                        (account["id"],),
                    )
                json_response(self, 403, {"error": "El código es incorrecto o venció"})
                return
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (password_digest(password), account["id"]),
            )
            connection.execute(
                "DELETE FROM password_reset_codes WHERE user_id = ?", (account["id"],)
            )
            connection.execute("DELETE FROM auth_sessions WHERE user_id = ?", (account["id"],))
            audit(connection, account["id"], "password_reset", "user", account["id"])
        json_response(
            self,
            200,
            {"ok": True, "message": "Contraseña actualizada. Ya puedes iniciar sesión."},
        )

    def logout(self):
        parsed = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        session = parsed.get("session")
        if session:
            with db() as connection:
                connection.execute(
                    "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash(session.value),)
                )
        self.send_response(204)
        self.send_header("Set-Cookie", "session=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def dashboard(self, user):
        with db() as connection:
            if is_admin(user):
                groups = connection.execute(
                    """
                    SELECT g.id, g.name, g.career, g.school_id, s.name AS school_name
                    FROM class_groups g JOIN schools s ON s.id = g.school_id
                    WHERE g.active = 1 ORDER BY s.name, g.name
                    """
                ).fetchall()
                schools = connection.execute(
                    "SELECT id, name, timezone FROM schools WHERE active = 1 ORDER BY name"
                ).fetchall()
                students = connection.execute(
                    """
                    SELECT u.id, u.email, u.full_name, u.matricula,
                           CASE WHEN u.qr_token_hash IS NULL THEN 0 ELSE 1 END AS has_qr
                    FROM users u
                    JOIN user_roles ur ON ur.user_id = u.id JOIN roles r ON r.id = ur.role_id
                    WHERE r.name = 'ESTUDIANTE' AND u.active = 1 ORDER BY u.full_name
                    """
                ).fetchall()
                teachers = connection.execute(
                    """
                    SELECT u.id, u.email, u.full_name FROM users u
                    JOIN user_roles ur ON ur.user_id = u.id JOIN roles r ON r.id = ur.role_id
                    WHERE r.name = 'PROFESOR' AND u.active = 1 ORDER BY u.full_name
                    """
                ).fetchall()
            elif has_role(user, "PROFESOR"):
                groups = connection.execute(
                    """
                    SELECT g.id, g.name, g.career, g.school_id, s.name AS school_name
                    FROM class_groups g JOIN schools s ON s.id = g.school_id
                    JOIN group_teachers gt ON gt.group_id = g.id
                    WHERE gt.user_id = ? AND g.active = 1 ORDER BY g.name
                    """,
                    (user["id"],),
                ).fetchall()
                schools, students, teachers = [], [], []
            else:
                groups = connection.execute(
                    """
                    SELECT g.id, g.name, g.career, g.school_id, s.name AS school_name
                    FROM class_groups g JOIN schools s ON s.id = g.school_id
                    JOIN group_members gm ON gm.group_id = g.id
                    WHERE gm.user_id = ? AND g.active = 1 ORDER BY g.name
                    """,
                    (user["id"],),
                ).fetchall()
                schools, students, teachers = [], [], []
            group_ids = [group["id"] for group in groups]
            if group_ids:
                placeholders = ",".join("?" for _ in group_ids)
                sessions = connection.execute(
                    f"""
                    SELECT cs.id, cs.group_id, cs.started_at, cs.closes_at, cs.active,
                           cs.pin_expires_at, g.name AS group_name, g.career, s.name AS school_name,
                           (SELECT COUNT(*) FROM attendance_records ar
                            WHERE ar.session_id = cs.id) AS attendance_count
                    FROM class_sessions cs
                    JOIN class_groups g ON g.id = cs.group_id
                    JOIN schools s ON s.id = g.school_id
                    WHERE cs.group_id IN ({placeholders}) AND cs.active = 1
                      AND cs.closes_at > ?
                    ORDER BY cs.started_at DESC
                    """,
                    (*group_ids, iso_now()),
                ).fetchall()
            else:
                sessions = []
            today = utc_now().date().isoformat()
            counts = connection.execute(
                """
                SELECT (SELECT COUNT(*) FROM schools WHERE active = 1) AS schools,
                       (SELECT COUNT(*) FROM class_groups WHERE active = 1) AS groups,
                       (SELECT COUNT(*) FROM class_sessions WHERE active = 1 AND closes_at > ?) AS active_sessions,
                       (SELECT COUNT(*) FROM attendance_records WHERE substr(registered_at, 1, 10) = ?) AS attendance_today
                """,
                (iso_now(), today),
            ).fetchone()
        json_response(
            self,
            200,
            {
                "groups": [dict(row) for row in groups],
                "schools": [dict(row) for row in schools],
                "students": [dict(row) for row in students],
                "teachers": [dict(row) for row in teachers],
                "sessions": [dict(row) for row in sessions],
                "stats": dict(counts),
                "can_admin": bool(is_admin(user)),
                "can_teach": bool(is_admin(user) or has_role(user, "PROFESOR")),
            },
        )

    def create_school(self, user):
        if not is_admin(user):
            json_response(self, 403, {"error": "Sólo administración puede crear planteles"})
            return
        data = request_json(self)
        name = clean_text(data, "name", "Nombre del plantel")
        timezone_name = clean_text(data, "timezone", "Zona horaria", 60) if data.get("timezone") else DEFAULT_TIMEZONE
        if not valid_timezone_name(timezone_name):
            raise ValueError("Usa una zona horaria IANA como America/Mexico_City o UTC")
        with db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO schools (name, timezone)
                VALUES (?, ?)
                """,
                (name, timezone_name),
            )
            audit(connection, user["id"], "create", "school", cursor.lastrowid)
        json_response(self, 201, {"ok": True, "school_id": cursor.lastrowid})

    def create_user(self, user):
        if not is_admin(user):
            json_response(self, 403, {"error": "Sólo administración puede crear cuentas"})
            return
        data = request_json(self)
        email = clean_text(data, "email", "Correo electrónico", 254).lower()
        role = data.get("role")
        if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,190}", email):
            raise ValueError("El correo electrónico no tiene un formato válido")
        if role not in {"PROFESOR", "ESTUDIANTE"}:
            raise ValueError("Selecciona el rol de profesor o estudiante")
        matricula = None
        guardian_phone = None
        password_hash = None
        name = clean_text(data, "full_name", "Nombre", 120) if role == "PROFESOR" else ""
        if role == "ESTUDIANTE":
            matricula = clean_text(data, "matricula", "Matrícula", 20)
            if not re.fullmatch(r"\d{6,12}-\d{1,2}", matricula):
                raise ValueError("La matrícula debe tener el formato 241440424-6")
            guardian_phone = normalize_guardian_phone(data.get("guardian_phone", ""))
            name = f"Estudiante {matricula}"
            qr_token, qr_svg = new_qr_credential()
            qr_token_hash = token_hash(qr_token)
        else:
            password = validate_password(data.get("password"))
            password_hash = password_digest(password)
            qr_svg = None
            qr_token_hash = None
        with db() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users
                    (email, full_name, matricula, guardian_phone, qr_token_hash, password_hash,
                     provider, external_id, active)
                VALUES (?, ?, ?, ?, ?, ?, 'admin_provisioned_demo', ?, 1)
                """,
                (
                    email,
                    name,
                    matricula,
                    guardian_phone,
                    qr_token_hash,
                    password_hash,
                    "provisioned:" + secrets.token_urlsafe(24),
                ),
            )
            user_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO user_roles (user_id, role_id)
                SELECT ?, id FROM roles WHERE name = ?
                """,
                (user_id, role),
            )
            audit(connection, user["id"], "create", "user", user_id)
        json_response(self, 201, {"ok": True, "user_id": user_id, "qr_svg": qr_svg})

    def issue_student_qr(self, user, student_id_text):
        if not is_admin(user):
            json_response(self, 403, {"error": "Sólo administración puede emitir credenciales QR"})
            return
        try:
            student_id = int(student_id_text)
        except ValueError as error:
            raise ValueError("Identificador de estudiante inválido") from error
        with db() as connection:
            student = connection.execute(
                """
                SELECT u.id, u.matricula FROM users u
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id
                WHERE u.id = ? AND u.active = 1 AND r.name = 'ESTUDIANTE'
                """,
                (student_id,),
            ).fetchone()
            if not student:
                json_response(self, 404, {"error": "Estudiante no encontrado"})
                return
            credential, svg = new_qr_credential()
            connection.execute(
                "UPDATE users SET qr_token_hash = ? WHERE id = ?",
                (token_hash(credential), student_id),
            )
            audit(connection, user["id"], "issue_qr", "user", student_id)
        json_response(
            self,
            200,
            {"ok": True, "matricula": student["matricula"], "qr_svg": svg},
        )

    def create_group(self, user):
        if not is_admin(user):
            json_response(self, 403, {"error": "Sólo administración puede crear grupos"})
            return
        data = request_json(self)
        name = clean_text(data, "name", "Nombre del grupo", 60)
        career = clean_text(data, "career", "Carrera", 100)
        try:
            school_id = int(data.get("school_id"))
        except (TypeError, ValueError) as error:
            raise ValueError("Selecciona un plantel válido") from error
        with db() as connection:
            school = connection.execute("SELECT id FROM schools WHERE id = ? AND active = 1", (school_id,)).fetchone()
            if not school:
                json_response(self, 404, {"error": "Plantel no encontrado"})
                return
            cursor = connection.execute(
                "INSERT INTO class_groups (school_id, name, career) VALUES (?, ?, ?)",
                (school_id, name, career),
            )
            group_id = cursor.lastrowid
            teacher_email = data.get("teacher_email")
            if teacher_email:
                teacher = connection.execute(
                    """
                    SELECT u.id FROM users u JOIN user_roles ur ON ur.user_id = u.id
                    JOIN roles r ON r.id = ur.role_id
                    WHERE u.email = ? AND r.name = 'PROFESOR' AND u.active = 1
                    """,
                    (str(teacher_email).strip().lower(),),
                ).fetchone()
                if not teacher:
                    raise ValueError("Selecciona una cuenta docente válida")
                connection.execute(
                    "INSERT INTO group_teachers (group_id, user_id) VALUES (?, ?)",
                    (group_id, teacher["id"]),
                )
            audit(connection, user["id"], "create", "group", group_id)
        json_response(self, 201, {"ok": True, "group_id": group_id})

    def enroll_student(self, user):
        if not is_admin(user):
            json_response(self, 403, {"error": "Sólo administración puede inscribir estudiantes"})
            return
        data = request_json(self)
        try:
            group_id = int(data.get("group_id"))
            student_id = int(data.get("student_id"))
        except (TypeError, ValueError) as error:
            raise ValueError("Selecciona un grupo y estudiante válidos") from error
        with db() as connection:
            student = connection.execute(
                """
                SELECT u.id FROM users u JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id
                WHERE u.id = ? AND r.name = 'ESTUDIANTE' AND u.active = 1
                """,
                (student_id,),
            ).fetchone()
            group = connection.execute(
                "SELECT id FROM class_groups WHERE id = ? AND active = 1", (group_id,)
            ).fetchone()
            if not student or not group:
                json_response(self, 404, {"error": "Grupo o estudiante no encontrado"})
                return
            connection.execute(
                "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, student_id),
            )
            audit(connection, user["id"], "enroll", "group", group_id)
        json_response(self, 200, {"ok": True})

    def start_session(self, user):
        if not (is_admin(user) or has_role(user, "PROFESOR")):
            json_response(self, 403, {"error": "Sólo profesores pueden iniciar una clase"})
            return
        data = request_json(self)
        try:
            group_id = int(data.get("group_id"))
            duration = int(data.get("duration_minutes", 90))
        except (TypeError, ValueError) as error:
            raise ValueError("Selecciona un grupo y duración válidos") from error
        if not 1 <= duration <= 360:
            raise ValueError("La duración debe ser de 1 a 360 minutos")
        with db() as connection:
            group = connection.execute(
                "SELECT id FROM class_groups WHERE id = ? AND active = 1", (group_id,)
            ).fetchone()
            if not group:
                json_response(self, 404, {"error": "Grupo no encontrado"})
                return
            if not is_admin(user) and not connection.execute(
                "SELECT 1 FROM group_teachers WHERE group_id = ? AND user_id = ?",
                (group_id, user["id"]),
            ).fetchone():
                json_response(self, 403, {"error": "No tienes asignado ese grupo"})
                return
            now = utc_now()
            connection.execute(
                "UPDATE class_sessions SET active = 0, pin_hash = NULL, pin_expires_at = NULL WHERE group_id = ? AND active = 1",
                (group_id,),
            )
            cursor = connection.execute(
                """
                INSERT INTO class_sessions (group_id, teacher_id, started_at, closes_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    group_id,
                    user["id"],
                    now.isoformat(timespec="seconds"),
                    (now + timedelta(minutes=duration)).isoformat(timespec="seconds"),
                ),
            )
            session_id = cursor.lastrowid
            pin = self.set_pin(connection, session_id)
            audit(connection, user["id"], "start", "class_session", session_id)
        json_response(
            self,
            201,
            {"ok": True, "session_id": session_id, "pin": pin, "valid_for_seconds": TOKEN_TTL_SECONDS},
        )

    def set_pin(self, connection, session_id):
        pin = f"{secrets.randbelow(1_000_000):06d}"
        expires = (utc_now() + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(timespec="seconds")
        connection.execute(
            "UPDATE class_sessions SET pin_hash = ?, pin_expires_at = ? WHERE id = ? AND active = 1",
            (pin_digest(session_id, pin), expires, session_id),
        )
        connection.execute("DELETE FROM pin_attempts WHERE session_id = ?", (session_id,))
        return pin

    def rotate_pin(self, user, session_id_text):
        if not (is_admin(user) or has_role(user, "PROFESOR")):
            json_response(self, 403, {"error": "Sólo profesores pueden generar un PIN"})
            return
        try:
            session_id = int(session_id_text)
        except ValueError as error:
            raise ValueError("Sesión inválida") from error
        with db() as connection:
            session = connection.execute(
                """
                SELECT cs.*, gt.user_id AS assigned_teacher
                FROM class_sessions cs LEFT JOIN group_teachers gt
                  ON gt.group_id = cs.group_id AND gt.user_id = ?
                WHERE cs.id = ? AND cs.active = 1 AND cs.closes_at > ?
                """,
                (user["id"], session_id, iso_now()),
            ).fetchone()
            if not session:
                json_response(self, 404, {"error": "La clase ya terminó o no existe"})
                return
            if not is_admin(user) and not session["assigned_teacher"]:
                json_response(self, 403, {"error": "No tienes asignada esta clase"})
                return
            pin = self.set_pin(connection, session_id)
            audit(connection, user["id"], "rotate_pin", "class_session", session_id)
        json_response(self, 200, {"ok": True, "session_id": session_id, "pin": pin, "valid_for_seconds": TOKEN_TTL_SECONDS})

    def close_session(self, user, session_id_text):
        if not (is_admin(user) or has_role(user, "PROFESOR")):
            json_response(self, 403, {"error": "Sólo profesores pueden cerrar una clase"})
            return
        try:
            session_id = int(session_id_text)
        except ValueError as error:
            raise ValueError("Sesión inválida") from error
        with db() as connection:
            session = connection.execute(
                """
                SELECT cs.id, gt.user_id AS assigned_teacher
                FROM class_sessions cs LEFT JOIN group_teachers gt
                  ON gt.group_id = cs.group_id AND gt.user_id = ?
                WHERE cs.id = ? AND cs.active = 1
                """,
                (user["id"], session_id),
            ).fetchone()
            if not session:
                json_response(self, 404, {"error": "Clase activa no encontrada"})
                return
            if not is_admin(user) and not session["assigned_teacher"]:
                json_response(self, 403, {"error": "No tienes asignada esta clase"})
                return
            connection.execute(
                "UPDATE class_sessions SET active = 0, closes_at = ?, pin_hash = NULL, pin_expires_at = NULL WHERE id = ?",
                (iso_now(), session_id),
            )
            audit(connection, user["id"], "close", "class_session", session_id)
        json_response(self, 200, {"ok": True})

    def scan_student_qr(self, user, session_id_text):
        if not (is_admin(user) or has_role(user, "PROFESOR")):
            json_response(self, 403, {"error": "Sólo profesores pueden registrar asistencia por QR"})
            return
        try:
            session_id = int(session_id_text)
        except ValueError as error:
            raise ValueError("Sesión inválida") from error
        data = request_json(self)
        credential = data.get("credential")
        if not isinstance(credential, str) or not re.fullmatch(r"PL1:[A-Za-z0-9_-]{43}", credential):
            json_response(self, 400, {"error": "El QR no tiene un formato de Pase de Lista válido"})
            return

        with TOKEN_LOCK, db() as connection:
            session = connection.execute(
                """
                SELECT cs.id, gt.user_id AS assigned_teacher
                FROM class_sessions cs LEFT JOIN group_teachers gt
                  ON gt.group_id = cs.group_id AND gt.user_id = ?
                WHERE cs.id = ? AND cs.active = 1 AND cs.closes_at > ?
                """,
                (user["id"], session_id, iso_now()),
            ).fetchone()
            if not session:
                json_response(self, 404, {"error": "El pase ya terminó o no está abierto"})
                return
            if not is_admin(user) and not session["assigned_teacher"]:
                json_response(self, 403, {"error": "No tienes asignado este grupo"})
                return
            student = connection.execute(
                """
                SELECT u.id, u.email, u.matricula, u.guardian_phone
                FROM users u
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id AND r.name = 'ESTUDIANTE'
                JOIN group_members gm ON gm.user_id = u.id
                JOIN class_sessions cs ON cs.group_id = gm.group_id AND cs.id = ?
                WHERE u.qr_token_hash = ? AND u.active = 1
                """,
                (session_id, token_hash(credential[4:])),
            ).fetchone()
            if not student:
                json_response(self, 404, {"error": "QR inválido, revocado o de un estudiante no inscrito en este grupo"})
                return
            try:
                connection.execute(
                    "INSERT INTO attendance_records (session_id, user_id, registered_at) VALUES (?, ?, ?)",
                    (session_id, student["id"], iso_now()),
                )
            except sqlite3.IntegrityError as error:
                if "attendance_records.session_id, attendance_records.user_id" in str(error):
                    json_response(self, 409, {"error": "La asistencia de este estudiante ya está registrada"})
                    return
                raise
            audit(connection, user["id"], "qr_attendance", "class_session", session_id)

        notification = (
            send_guardian_sms(student["guardian_phone"], student["matricula"])
            if student["guardian_phone"] and student["matricula"]
            else {"sent": False, "message": "Aviso al tutor no disponible para esta cuenta."}
        )
        json_response(
            self,
            201,
            {
                "ok": True,
                "student": {"matricula": student["matricula"], "email": student["email"]},
                "registered_at": iso_now(),
                "notification": notification,
            },
        )

    def register_attendance(self, user):
        if not has_role(user, "ESTUDIANTE"):
            json_response(self, 403, {"error": "Sólo un estudiante puede registrar asistencia"})
            return
        data = request_json(self)
        try:
            session_id = int(data.get("session_id"))
        except (TypeError, ValueError) as error:
            raise ValueError("Selecciona una clase activa") from error
        pin = data.get("presence_token", "")
        if not isinstance(pin, str) or len(pin) > 12:
            json_response(self, 403, {"error": "PIN incorrecto"})
            return
        with TOKEN_LOCK, db() as connection:
            session = connection.execute(
                """
                SELECT cs.id, cs.pin_hash, cs.pin_expires_at, cs.closes_at
                FROM class_sessions cs
                JOIN group_members gm ON gm.group_id = cs.group_id AND gm.user_id = ?
                WHERE cs.id = ? AND cs.active = 1 AND cs.closes_at > ?
                """,
                (user["id"], session_id, iso_now()),
            ).fetchone()
            if not session:
                json_response(self, 403, {"error": "No estás inscrito en una clase activa"})
                return
            attempt = connection.execute(
                "SELECT attempts FROM pin_attempts WHERE session_id = ? AND user_id = ?",
                (session_id, user["id"]),
            ).fetchone()
            if attempt and attempt["attempts"] >= MAX_TOKEN_ATTEMPTS:
                json_response(self, 429, {"error": "Se agotaron los intentos para este PIN"})
                return
            expires = (
                datetime.fromisoformat(session["pin_expires_at"])
                if session["pin_expires_at"]
                else None
            )
            if not expires or expires <= utc_now() or not session["pin_hash"]:
                json_response(self, 403, {"error": "El PIN expiró; solicita al profesor que genere uno nuevo"})
                return
            if not hmac.compare_digest(pin_digest(session_id, pin), session["pin_hash"]):
                connection.execute(
                    """
                    INSERT INTO pin_attempts (session_id, user_id, attempts) VALUES (?, ?, 1)
                    ON CONFLICT(session_id, user_id) DO UPDATE SET attempts = attempts + 1
                    """,
                    (session_id, user["id"]),
                )
                json_response(self, 403, {"error": "PIN incorrecto"})
                return
            connection.execute(
                "INSERT INTO attendance_records (session_id, user_id, registered_at) VALUES (?, ?, ?)",
                (session_id, user["id"], iso_now()),
            )
            audit(connection, user["id"], "attendance", "class_session", session_id)
            student = connection.execute(
                "SELECT matricula, guardian_phone FROM users WHERE id = ?",
                (user["id"],),
            ).fetchone()
        notification = (
            send_guardian_sms(student["guardian_phone"], student["matricula"])
            if student["guardian_phone"] and student["matricula"]
            else {"sent": False, "message": "Aviso al tutor no disponible para esta cuenta."}
        )
        json_response(
            self,
            201,
            {
                "ok": True,
                "message": "Asistencia registrada para esta clase.",
                "notification": notification,
            },
        )

    def attendance_rows(self, user):
        if not (is_admin(user) or has_role(user, "PROFESOR")):
            return None
        with db() as connection:
            if is_admin(user):
                return connection.execute(
                    """
                    SELECT ar.registered_at, u.full_name, u.email, g.name AS group_name,
                           g.career, s.name AS school_name
                    FROM attendance_records ar JOIN users u ON u.id = ar.user_id
                    JOIN class_sessions cs ON cs.id = ar.session_id
                    JOIN class_groups g ON g.id = cs.group_id
                    JOIN schools s ON s.id = g.school_id
                    ORDER BY ar.registered_at DESC LIMIT 1000
                    """
                ).fetchall()
            return connection.execute(
                """
                SELECT ar.registered_at, u.full_name, u.email, g.name AS group_name,
                       g.career, s.name AS school_name
                FROM attendance_records ar JOIN users u ON u.id = ar.user_id
                JOIN class_sessions cs ON cs.id = ar.session_id
                JOIN class_groups g ON g.id = cs.group_id
                JOIN schools s ON s.id = g.school_id
                JOIN group_teachers gt ON gt.group_id = g.id
                WHERE gt.user_id = ?
                ORDER BY ar.registered_at DESC LIMIT 1000
                """,
                (user["id"],),
            ).fetchall()

    def attendance_list(self, user):
        rows = self.attendance_rows(user) if user else None
        if rows is None:
            json_response(self, 403, {"error": "No tienes permiso para consultar asistencias"})
            return
        json_response(self, 200, {"items": [dict(row) for row in rows]})

    def attendance_csv(self, user):
        rows = self.attendance_rows(user) if user else None
        if rows is None:
            json_response(self, 403, {"error": "No tienes permiso para exportar asistencias"})
            return
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["Plantel", "Grupo", "Carrera", "Estudiante", "Correo", "Fecha y hora"])

        def safe_cell(value):
            text = str(value)
            if text.startswith(("=", "+", "-", "@", "\t", "\r")):
                return "'" + text
            return text

        def format_local_time(value):
            registered = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if registered.tzinfo is None:
                registered = registered.replace(tzinfo=timezone.utc)
            veracruz_time = registered.astimezone(timezone(timedelta(hours=-6)))
            return veracruz_time.strftime("%d/%m/%Y %H:%M:%S")

        for row in rows:
            writer.writerow(
                [
                    safe_cell(row["school_name"]),
                    safe_cell(row["group_name"]),
                    safe_cell(row["career"]),
                    safe_cell(row["full_name"]),
                    safe_cell(row["email"]),
                    format_local_time(row["registered_at"]),
                ]
            )
        body = ("\ufeff" + stream.getvalue()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="asistencias.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if TOKEN_TTL_SECONDS <= 0:
        raise ValueError("PASELISTA_TOKEN_TTL_SECONDS debe ser mayor que cero")
    initialize_database()
    print(f"Pase de Lista ejecutándose en http://{HOST}:{PORT}")
    print("Este servidor sólo acepta HTTP; no abras la dirección con https://.")
    print(f"Base de datos: {DB_PATH}")
    print("Autenticación de cuentas: correo y contraseña")
    if COOKIE_SECURE:
        print("Cookies seguras: activas")
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
