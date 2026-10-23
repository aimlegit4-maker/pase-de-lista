import http.cookiejar
import csv
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

import app


class AttendanceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        patcher = patch.multiple(
            app,
            DB_PATH=Path(self.temp_dir.name) / "test.sqlite3",
            DEMO_ACCESS_CODE="test-demo-code",
            DEMO_MODE=True,
            TOKEN_TTL_SECONDS=60,
            PIN_PEPPER=b"test-secret-pepper",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        app.initialize_database()
        self.qr_token = "T" * 43
        with app.db() as connection:
            student = connection.execute(
                "SELECT id FROM users WHERE email = 'alumno@demo.pasadelista.local'"
            ).fetchone()
            connection.execute(
                """
                UPDATE users SET matricula = ?, guardian_phone = ?, qr_token_hash = ?
                WHERE id = ?
                """,
                ("241440424-6", "+525512345678", app.token_hash(self.qr_token), student["id"]),
            )
        self.qr_token = "T" * 43
        with app.db() as connection:
            student = connection.execute(
                "SELECT id FROM users WHERE email = 'alumno@demo.pasadelista.local'"
            ).fetchone()
            connection.execute(
                """
                UPDATE users SET matricula = ?, guardian_phone = ?, qr_token_hash = ?
                WHERE id = ?
                """,
                ("241440424-6", "+525512345678", app.token_hash(self.qr_token), student["id"]),
            )
        self.server = app.Server(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.openers = {}
        self.csrf = {}

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_mobile_install_instructions_are_available_before_login(self):
        with urllib.request.urlopen(self.base_url + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
        self.assertIn('id="install-button"', html)
        self.assertIn("Instala Pase de Lista en tu celular", html)
        self.assertIn("Agregar a pantalla de inicio", html)
        self.assertNotIn("Duración de clase", html)
        self.assertIn('const VERACRUZ_TIMEZONE = "America/Mexico_City";', html)
        self.assertIn('<script src="/vendor/jsQR.js"></script>', html)
        with urllib.request.urlopen(self.base_url + "/vendor/jsQR.js", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("root[\"jsQR\"] = factory();", response.read().decode("utf-8"))

    def test_https_request_to_http_port_logs_actionable_message(self):
        tls_handshake = b"\x16\x03\x01\x00\x08\x01\x00\x00\x04\x00\n"
        with patch.object(app.Handler, "log_error") as log_error:
            with socket.create_connection(self.server.server_address, timeout=3) as client:
                client.sendall(tls_handshake)
                self.assertEqual(client.recv(1024), b"")

        log_error.assert_called_once_with(
            "Se recibió HTTPS en un servidor HTTP. Abre http://IP-DE-LA-PC:%s en el celular.",
            self.server.server_port,
        )

    def request(self, path, method="GET", body=None, profile=None, headers=None):
        request_headers = dict(headers or {})
        if profile and profile in self.csrf and method != "GET" and "X-CSRF-Token" not in request_headers:
            request_headers["X-CSRF-Token"] = self.csrf[profile]
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        opener = self.openers.get(profile, urllib.request.build_opener())
        try:
            with opener.open(request, timeout=3) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read()
            error.close()
            return error.code, json.loads(raw) if raw else {}

    def login(self, profile):
        email = {
            "admin": "admin@demo.pasadelista.local",
            "teacher": "docente@demo.pasadelista.local",
            "student": "alumno@demo.pasadelista.local",
        }[profile]
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.openers[profile] = opener
        status, result = self.request(
            "/api/login",
            method="POST",
            body={"email": email, "access_code": "test-demo-code"},
            profile=profile,
        )
        self.assertEqual(status, 200, result)
        self.csrf[profile] = result["csrf_token"]
        return result

    def login_email(self, profile, email):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.openers[profile] = opener
        status, result = self.request(
            "/api/login",
            method="POST",
            body={"email": email, "access_code": "test-demo-code"},
            profile=profile,
        )
        self.assertEqual(status, 200, result)
        self.csrf[profile] = result["csrf_token"]
        return result

    def login_password(self, profile, email, password):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.openers[profile] = opener
        return self.request(
            "/api/login",
            method="POST",
            body={"email": email, "password": password},
            profile=profile,
        )

    def configure_default_school(self):
        self.login("admin")
        _, dashboard = self.request("/api/dashboard", profile="admin")
        return dashboard["schools"][0]["id"]

    def test_end_to_end_teacher_scans_student_qr_and_records_attendance(self):
        self.configure_default_school()
        self.login("teacher")
        _, dashboard = self.request("/api/dashboard", profile="teacher")
        group_id = dashboard["groups"][0]["id"]

        status, class_session = self.request(
            "/api/sessions/start",
            method="POST",
            body={"group_id": group_id},
            profile="teacher",
        )
        self.assertEqual(status, 201, class_session)
        with app.db() as connection:
            session = connection.execute(
                "SELECT closes_at FROM class_sessions WHERE id = ?",
                (class_session["session_id"],),
            ).fetchone()
        self.assertEqual(session["closes_at"], app.OPEN_SESSION_CLOSES_AT)
        with patch.object(
            app,
            "send_guardian_sms",
            return_value={"sent": True, "message": "Aviso enviado al tutor."},
        ) as send_sms:
            status, attendance = self.request(
            f"/api/sessions/{class_session['session_id']}/scan",
            method="POST",
            profile="teacher",
            body={
                "credential": f"PL1:{self.qr_token}",
            },
        )
        self.assertEqual(status, 201, attendance)
        self.assertEqual(attendance["student"]["matricula"], "241440424-6")
        send_sms.assert_called_once_with("+525512345678", "241440424-6")
        self.assertEqual(status, 201, attendance)
        status, duplicate = self.request(
            f"/api/sessions/{class_session['session_id']}/scan",
            method="POST",
            profile="teacher",
            body={"credential": f"PL1:{self.qr_token}"},
        )
        self.assertEqual(status, 409, duplicate)

        status, rows = self.request("/api/attendance", profile="teacher")
        self.assertEqual(status, 200)
        self.assertEqual(len(rows["items"]), 1)
        self.assertEqual(rows["items"][0]["group_name"], "401")
        self.assertEqual(rows["items"][0]["matricula"], "241440424-6")
        self.assertNotIn("full_name", rows["items"][0])
        self.assertNotIn("session_id", rows["items"][0])

        request = urllib.request.Request(
            self.base_url + "/api/attendance.csv",
            headers={"X-CSRF-Token": self.csrf["teacher"]},
        )
        with self.openers["teacher"].open(request, timeout=3) as response:
            exported = list(csv.reader(response.read().decode("utf-8-sig").splitlines()))
        self.assertEqual(
            exported[0],
            ["Plantel", "Grupo", "Carrera", "Matrícula", "Correo", "Fecha y hora"],
        )
        self.assertEqual(len(exported[1]), 6)
        self.assertRegex(exported[1][-1], r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$")

    def test_attendance_list_is_sorted_by_matricula(self):
        self.configure_default_school()
        self.login("teacher")
        _, dashboard = self.request("/api/dashboard", profile="teacher")
        status, session = self.request(
            "/api/sessions/start",
            method="POST",
            body={"group_id": dashboard["groups"][0]["id"]},
            profile="teacher",
        )
        self.assertEqual(status, 201, session)

        with app.db() as connection:
            demo_student = connection.execute(
                "SELECT id FROM users WHERE email = 'alumno@demo.pasadelista.local'"
            ).fetchone()
            connection.execute(
                """
                INSERT INTO attendance_records (session_id, user_id, registered_at)
                VALUES (?, ?, ?)
                """,
                (session["session_id"], demo_student["id"], app.iso_now()),
            )
            for email, full_name, matricula in (
                ("zeta@escuela.test", "Zeta", "241440425-6"),
                ("alfa@escuela.test", "Alfa", "241440420-6"),
            ):
                cursor = connection.execute(
                    """
                    INSERT INTO users (email, full_name, matricula, provider, external_id)
                    VALUES (?, ?, ?, 'password', ?)
                    """,
                    (email, full_name, matricula, "password:" + email),
                )
                connection.execute(
                    """
                    INSERT INTO attendance_records (session_id, user_id, registered_at)
                    VALUES (?, ?, ?)
                    """,
                    (session["session_id"], cursor.lastrowid, app.iso_now()),
                )

        status, records = self.request("/api/attendance", profile="teacher")
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["matricula"] for item in records["items"]],
            ["241440420-6", "241440424-6", "241440425-6"],
        )

    def test_veracruz_time_conversion_uses_mexico_timezone(self):
        self.assertEqual(
            app.format_veracruz_time("2024-01-15T12:30:00+00:00"),
            "15/01/2024 06:30:00",
        )

    def test_qr_scan_rejects_invalid_or_revoked_credentials(self):
        self.configure_default_school()
        self.login("teacher")
        _, dashboard = self.request("/api/dashboard", profile="teacher")
        _, class_session = self.request(
            "/api/sessions/start",
            method="POST",
            body={"group_id": dashboard["groups"][0]["id"]},
            profile="teacher",
        )
        status, result = self.request(
            f"/api/sessions/{class_session['session_id']}/scan",
            method="POST",
            profile="teacher",
            body={"credential": "PL1:" + "X" * 43},
        )
        self.assertEqual(status, 404)
        with app.db() as connection:
            connection.execute(
                "UPDATE users SET qr_token_hash = NULL WHERE email = 'alumno@demo.pasadelista.local'"
            )
        status, result = self.request(
            f"/api/sessions/{class_session['session_id']}/scan",
            method="POST",
            profile="teacher",
            body={"credential": f"PL1:{self.qr_token}"},
        )
        self.assertEqual(status, 404)

    def test_wrong_access_code_is_rate_limited(self):
        for attempt in range(5):
            status, _ = self.request(
                "/api/login",
                method="POST",
                body={
                    "email": "alumno@demo.pasadelista.local",
                    "access_code": "incorrecto",
                },
            )
            self.assertEqual(status, 403, f"attempt {attempt + 1}")
        status, result = self.request(
            "/api/login",
            method="POST",
            body={
                "email": "alumno@demo.pasadelista.local",
                "access_code": "test-demo-code",
            },
        )
        self.assertEqual(status, 429)
        self.assertIn("15 minutos", result["error"])

    def test_demo_access_code_is_rejected_when_demo_is_disabled(self):
        with patch.object(app, "DEMO_MODE", False):
            status, result = self.request(
                "/api/login",
                method="POST",
                body={
                    "email": "admin@demo.pasadelista.local",
                    "access_code": "test-demo-code",
                },
            )
        self.assertEqual(status, 403)
        self.assertIn("contraseña", result["error"])

    def test_closed_session_rejects_qr_scans(self):
        self.configure_default_school()
        self.login("teacher")
        _, dashboard = self.request("/api/dashboard", profile="teacher")
        _, class_session = self.request(
            "/api/sessions/start",
            method="POST",
            body={"group_id": dashboard["groups"][0]["id"]},
            profile="teacher",
        )
        with app.db() as connection:
            connection.execute(
                "UPDATE class_sessions SET closes_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", class_session["session_id"]),
            )
        status, result = self.request(
            f"/api/sessions/{class_session['session_id']}/scan",
            method="POST",
            profile="teacher",
            body={"credential": f"PL1:{self.qr_token}"},
        )
        self.assertEqual(status, 404)
        self.assertIn("no está abierto", result["error"])

    def test_admin_can_provision_accounts_and_multiple_school_groups(self):
        self.login("admin")
        status, result = self.request(
            "/api/users",
            method="POST",
            profile="admin",
            body={
                "full_name": "Docente nuevo",
                "email": "docente.nuevo@escuela.test",
                "role": "PROFESOR",
                "password": "DocenteSeguro2026!",
            },
        )
        self.assertEqual(status, 201, result)

        status, result = self.login_password(
            "new-teacher", "docente.nuevo@escuela.test", "DocenteSeguro2026!"
        )
        self.assertEqual(status, 200, result)

        status, result = self.request(
            "/api/schools",
            method="POST",
            profile="admin",
            body={"name": "Plantel de prueba", "timezone": "America/Mexico_City"},
        )
        self.assertEqual(status, 201, result)
        _, dashboard = self.request("/api/dashboard", profile="admin")
        school_id = next(s["id"] for s in dashboard["schools"] if s["name"] == "Plantel de prueba")
        teacher = next(t for t in dashboard["teachers"] if t["email"] == "docente.nuevo@escuela.test")
        status, group = self.request(
            "/api/groups",
            method="POST",
            profile="admin",
            body={
                "school_id": school_id,
                "name": "A-101",
                "career": "Ciencias",
                "teacher_email": teacher["email"],
            },
        )
        self.assertEqual(status, 201, group)

        _, teacher_dashboard = self.request("/api/dashboard", profile="new-teacher")
        self.assertEqual([item["name"] for item in teacher_dashboard["groups"]], ["A-101"])

        status, _ = self.request(
            "/api/users",
            method="POST",
            profile="admin",
            body={
                "email": "estudiante.nuevo@escuela.test",
                "role": "ESTUDIANTE",
                "matricula": "241440425-6",
                "guardian_phone": "5512345678",
            },
        )
        self.assertEqual(status, 201)
        _, dashboard = self.request("/api/dashboard", profile="admin")
        student = next(item for item in dashboard["students"] if item["email"] == "estudiante.nuevo@escuela.test")
        status, result = self.request(
            "/api/enrollments",
            method="POST",
            profile="admin",
            body={"group_id": group["group_id"], "student_id": student["id"]},
        )
        self.assertEqual(status, 200, result)

    def test_admin_account_is_seeded_for_configured_email(self):
        with app.db() as connection:
            account = connection.execute(
                """
                SELECT u.email, GROUP_CONCAT(r.name) AS roles
                FROM users u
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id
                WHERE u.email = ?
                GROUP BY u.id
                """,
                (app.ADMIN_EMAIL,),
            ).fetchone()
        self.assertIsNotNone(account)
        self.assertIn("SUPER_ADMINISTRADOR", account["roles"])

    def test_teacher_can_reset_password_with_email_code(self):
        self.login("admin")
        status, _ = self.request(
            "/api/users",
            method="POST",
            profile="admin",
            body={
                "full_name": "Docente con recuperación",
                "email": "recuperacion@escuela.test",
                "role": "PROFESOR",
                "password": "ClaveInicialSegura2026!",
            },
        )
        self.assertEqual(status, 201)
        status, _ = self.login_password(
            "reset-teacher", "recuperacion@escuela.test", "ClaveInicialSegura2026!"
        )
        self.assertEqual(status, 200)

        sent_codes = {}
        with patch.dict(
            os.environ,
            {"PASELISTA_SMTP_USER": "mailer@example.test", "PASELISTA_SMTP_PASSWORD": "app-password"},
        ), patch.object(
            app,
            "send_password_reset_email",
            side_effect=lambda email, code: sent_codes.update(email=email, code=code),
        ):
            status, result = self.request(
                "/api/password-reset/request",
                method="POST",
                body={"email": "recuperacion@escuela.test"},
            )
        self.assertEqual(status, 200, result)
        self.assertEqual(sent_codes["email"], "recuperacion@escuela.test")

        status, result = self.request(
            "/api/password-reset/confirm",
            method="POST",
            body={
                "email": "recuperacion@escuela.test",
                "code": sent_codes["code"],
                "password": "ClaveNuevaSegura2026!",
            },
        )
        self.assertEqual(status, 200, result)
        status, _ = self.login_password(
            "old-password", "recuperacion@escuela.test", "ClaveInicialSegura2026!"
        )
        self.assertEqual(status, 403)
        status, result = self.login_password(
            "new-password", "recuperacion@escuela.test", "ClaveNuevaSegura2026!"
        )
        self.assertEqual(status, 200, result)

    def test_student_attendance_sends_guardian_sms_without_location(self):
        self.configure_default_school()
        self.login("admin")
        status, created = self.request(
            "/api/users",
            method="POST",
            profile="admin",
            body={
                "email": "estudiante.aviso@escuela.test",
                "role": "ESTUDIANTE",
                "matricula": "241440426-6",
                "guardian_phone": "5512345678",
            },
        )
        self.assertEqual(status, 201, created)
        _, dashboard = self.request("/api/dashboard", profile="admin")
        student = next(item for item in dashboard["students"] if item["email"] == "estudiante.aviso@escuela.test")
        group_id = dashboard["groups"][0]["id"]
        status, _ = self.request(
            "/api/enrollments",
            method="POST",
            profile="admin",
            body={"group_id": group_id, "student_id": student["id"]},
        )
        self.assertEqual(status, 200)

        self.login("teacher")
        status, class_session = self.request(
            "/api/sessions/start",
            method="POST",
            body={"group_id": group_id},
            profile="teacher",
        )
        self.assertEqual(status, 201, class_session)
        self.login_email("new_student", "estudiante.aviso@escuela.test")
        with patch.object(
            app,
            "send_guardian_sms",
            return_value={"sent": True, "message": "Aviso enviado al tutor."},
        ) as send_sms:
            status, result = self.request(
                "/api/attendance",
                method="POST",
                profile="new_student",
                body={
                    "session_id": class_session["session_id"],
                    "presence_token": class_session["pin"],
                },
            )
        self.assertEqual(status, 201, result)
        self.assertTrue(result["notification"]["sent"])
        send_sms.assert_called_once_with("+525512345678", "241440426-6")

    def test_twilio_sms_uses_configured_sender_and_guardian_number(self):
        with (
            patch.multiple(
                app,
                TWILIO_ACCOUNT_SID="ACdemo",
                TWILIO_AUTH_TOKEN="test-token",
                TWILIO_FROM_NUMBER="+15551234567",
            ),
            patch("app.urlopen") as open_url,
        ):
            open_url.return_value.__enter__.return_value.status = 201
            result = app.send_guardian_sms("+525512345678", "241440424-6")

        self.assertTrue(result["sent"])
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.twilio.com/2010-04-01/Accounts/ACdemo/Messages.json")
        payload = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(payload["To"], ["+525512345678"])
        self.assertEqual(payload["From"], ["+15551234567"])
        self.assertIn("tu hij@", payload["Body"][0])
        self.assertIn("241440424-6", payload["Body"][0])

    def test_csrf_and_role_permissions_are_enforced(self):
        self.login("student")
        status, result = self.request(
            "/api/sessions/start",
            method="POST",
            body={"group_id": 1},
            profile="student",
            headers={"X-CSRF-Token": "incorrecto"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Token de seguridad", result["error"])
        status, result = self.request(
            "/api/sessions/start",
            method="POST",
            body={"group_id": 1},
            profile="student",
        )
        self.assertEqual(status, 403)
        self.assertIn("profesores", result["error"])

    def test_school_creation_does_not_use_location_configuration(self):
        self.login("admin")
        status, result = self.request(
            "/api/schools",
            method="POST",
            profile="admin",
            body={"name": "Plantel sin geolocalización"},
        )
        self.assertEqual(status, 201, result)
        _, dashboard = self.request("/api/dashboard", profile="admin")
        school = next(item for item in dashboard["schools"] if item["name"] == "Plantel sin geolocalización")
        self.assertNotIn("latitude", school)
        self.assertNotIn("longitude", school)
        status, result = self.request(
            f"/api/schools/{school['id']}/geofence",
            method="POST",
            profile="admin",
            body={"latitude": 19.21209, "longitude": -96.16463},
        )
        self.assertEqual(status, 404, result)

    def test_legacy_school_coordinates_are_cleared_on_upgrade(self):
        with app.db() as connection:
            school_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(schools)").fetchall()
            }
            for column in ("latitude", "longitude"):
                if column not in school_columns:
                    connection.execute(f"ALTER TABLE schools ADD COLUMN {column} REAL")
            connection.execute(
                "UPDATE schools SET latitude = 19.21209, longitude = -96.16463"
            )
            connection.execute("DELETE FROM schema_migrations WHERE version = 2")

        app.initialize_database()

        with app.db() as connection:
            school = connection.execute(
                "SELECT latitude, longitude FROM schools LIMIT 1"
            ).fetchone()
            migration = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 2"
            ).fetchone()
        self.assertIsNone(school["latitude"])
        self.assertIsNone(school["longitude"])
        self.assertIsNotNone(migration)

    def test_legacy_attendance_is_migrated_once(self):
        with app.db() as connection:
            student_id = connection.execute(
                "SELECT id FROM users WHERE email = 'alumno@demo.pasadelista.local'"
            ).fetchone()["id"]
            connection.execute("DELETE FROM schema_migrations WHERE version = 1")
            connection.execute(
                """
                CREATE TABLE attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    group_name TEXT NOT NULL,
                    career TEXT NOT NULL,
                    attendance_date TEXT NOT NULL,
                    arrived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, attendance_date)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO attendance
                    (user_id, group_name, career, attendance_date, arrived_at)
                VALUES (?, 'Grupo histórico', 'Ciencias', '2024-05-01', '2024-05-01 08:10:00')
                """,
                (student_id,),
            )

        app.initialize_database()
        app.initialize_database()
        with app.db() as connection:
            migrated = connection.execute(
                """
                SELECT ar.registered_at, g.name, g.career
                FROM attendance_records ar
                JOIN class_sessions cs ON cs.id = ar.session_id
                JOIN class_groups g ON g.id = cs.group_id
                WHERE g.name = 'Grupo histórico'
                """
            ).fetchall()
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0]["registered_at"], "2024-05-01T08:10:00+00:00")
        self.assertEqual(migrated[0]["career"], "Ciencias")


if __name__ == "__main__":
    unittest.main()
