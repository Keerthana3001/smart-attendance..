import csv
import io
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

import qrcode
from flask import (Flask, Response, abort, flash, g, redirect,
                   render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = "change-this-secret-key"
DB = "attendance.db"
THRESHOLD = 75  # minimum attendance % required


# ---------- database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS students(
        id INTEGER PRIMARY KEY, roll_no TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL, email TEXT, class_name TEXT);
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, role TEXT NOT NULL, student_id INTEGER);
    CREATE TABLE IF NOT EXISTS courses(
        id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS attendance(
        id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL,
        course_id INTEGER NOT NULL, date TEXT NOT NULL,
        status TEXT NOT NULL, method TEXT DEFAULT 'manual',
        UNIQUE(student_id, course_id, date));
    CREATE TABLE IF NOT EXISTS qr_sessions(
        token TEXT PRIMARY KEY, course_id INTEGER NOT NULL,
        date TEXT NOT NULL, expires_at TEXT NOT NULL);
    """)
    if not db.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                   ("admin", generate_password_hash("admin123"), "teacher"))
    db.commit()
    db.close()


# ---------- auth ----------
def login_required(role=None):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if "user_id" not in session:
                return redirect(url_for("login"))
            if role and session.get("role") != role:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = get_db().execute("SELECT * FROM users WHERE username=?",
                             (request.form["username"].strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear()
            session.update(user_id=u["id"], role=u["role"],
                           student_id=u["student_id"], username=u["username"])
            nxt = request.args.get("next")
            if nxt and nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for("dashboard" if u["role"] == "teacher" else "my_attendance"))
        flash("Invalid username or password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- helpers ----------
def summary_rows(course_id=None):
    """Per student (and course) attendance summary."""
    q = """SELECT s.id sid, s.roll_no, s.name, c.id cid, c.code, c.name cname,
             SUM(a.status='Present') present, COUNT(a.id) total
           FROM attendance a JOIN students s ON s.id=a.student_id
           JOIN courses c ON c.id=a.course_id"""
    args = []
    if course_id:
        q += " WHERE c.id=?"
        args.append(course_id)
    q += " GROUP BY s.id, c.id ORDER BY s.roll_no"
    rows = []
    for r in get_db().execute(q, args):
        d = dict(r)
        d["pct"] = round(100 * d["present"] / d["total"], 1) if d["total"] else 0
        rows.append(d)
    return rows


# ---------- teacher ----------
@app.route("/dashboard")
@login_required("teacher")
def dashboard():
    db = get_db()
    today = date.today().isoformat()
    stats = {
        "students": db.execute("SELECT COUNT(*) FROM students").fetchone()[0],
        "courses": db.execute("SELECT COUNT(*) FROM courses").fetchone()[0],
        "present_today": db.execute(
            "SELECT COUNT(*) FROM attendance WHERE date=? AND status='Present'", (today,)).fetchone()[0],
        "absent_today": db.execute(
            "SELECT COUNT(*) FROM attendance WHERE date=? AND status='Absent'", (today,)).fetchone()[0],
    }
    defaulters = [r for r in summary_rows() if r["pct"] < THRESHOLD][:8]
    return render_template("dashboard.html", stats=stats, defaulters=defaulters,
                           threshold=THRESHOLD)


@app.route("/students", methods=["GET", "POST"])
@login_required("teacher")
def students():
    db = get_db()
    if request.method == "POST":
        roll = request.form["roll_no"].strip()
        try:
            cur = db.execute("INSERT INTO students(roll_no,name,email,class_name) VALUES(?,?,?,?)",
                             (roll, request.form["name"].strip(),
                              request.form["email"].strip(), request.form["class_name"].strip()))
            db.execute("INSERT INTO users(username,password_hash,role,student_id) VALUES(?,?,?,?)",
                       (roll, generate_password_hash(roll), "student", cur.lastrowid))
            db.commit()
            flash(f"Added {roll}. Default login: roll number / roll number.", "ok")
        except sqlite3.IntegrityError:
            flash("Roll number already exists", "error")
        return redirect(url_for("students"))
    rows = db.execute("SELECT * FROM students ORDER BY roll_no").fetchall()
    return render_template("students.html", students=rows)


@app.route("/students/<int:sid>/delete", methods=["POST"])
@login_required("teacher")
def delete_student(sid):
    db = get_db()
    db.execute("DELETE FROM attendance WHERE student_id=?", (sid,))
    db.execute("DELETE FROM users WHERE student_id=?", (sid,))
    db.execute("DELETE FROM students WHERE id=?", (sid,))
    db.commit()
    return redirect(url_for("students"))


@app.route("/courses", methods=["GET", "POST"])
@login_required("teacher")
def courses():
    db = get_db()
    if request.method == "POST":
        try:
            db.execute("INSERT INTO courses(code,name) VALUES(?,?)",
                       (request.form["code"].strip().upper(), request.form["name"].strip()))
            db.commit()
        except sqlite3.IntegrityError:
            flash("Course code already exists", "error")
        return redirect(url_for("courses"))
    return render_template("courses.html",
                           courses=db.execute("SELECT * FROM courses ORDER BY code").fetchall())


@app.route("/mark", methods=["GET", "POST"])
@login_required("teacher")
def mark():
    db = get_db()
    courses_ = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    course_id = request.values.get("course_id", type=int)
    day = request.values.get("date") or date.today().isoformat()
    students_ = []
    existing = {}
    if course_id:
        students_ = db.execute("SELECT * FROM students ORDER BY roll_no").fetchall()
        existing = {r["student_id"]: r["status"] for r in db.execute(
            "SELECT student_id,status FROM attendance WHERE course_id=? AND date=?",
            (course_id, day))}
        if request.method == "POST":
            for s in students_:
                status = request.form.get(f"s_{s['id']}", "Absent")
                db.execute("""INSERT INTO attendance(student_id,course_id,date,status,method)
                              VALUES(?,?,?,?, 'manual')
                              ON CONFLICT(student_id,course_id,date)
                              DO UPDATE SET status=excluded.status, method='manual'""",
                           (s["id"], course_id, day, status))
            db.commit()
            flash("Attendance saved", "ok")
            return redirect(url_for("mark", course_id=course_id, date=day))
    return render_template("mark.html", courses=courses_, students=students_,
                           existing=existing, course_id=course_id, day=day)


@app.route("/qr", methods=["GET", "POST"])
@login_required("teacher")
def qr_session():
    db = get_db()
    courses_ = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    token = course = expires = None
    if request.method == "POST":
        cid = request.form.get("course_id", type=int)
        minutes = request.form.get("minutes", 10, type=int)
        token = secrets.token_urlsafe(12)
        expires = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        db.execute("INSERT INTO qr_sessions VALUES(?,?,?,?)",
                   (token, cid, date.today().isoformat(), expires))
        db.commit()
        course = db.execute("SELECT * FROM courses WHERE id=?", (cid,)).fetchone()
    return render_template("qr.html", courses=courses_, token=token, course=course,
                           expires=expires)


@app.route("/qr/<token>.png")
@login_required("teacher")
def qr_image(token):
    url = url_for("checkin", token=token, _external=True)
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/reports")
@login_required("teacher")
def reports():
    db = get_db()
    course_id = request.args.get("course_id", type=int)
    rows = summary_rows(course_id)
    courses_ = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    return render_template("reports.html", rows=rows, courses=courses_,
                           course_id=course_id, threshold=THRESHOLD)


@app.route("/reports/export")
@login_required("teacher")
def export_csv():
    course_id = request.args.get("course_id", type=int)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Roll No", "Name", "Course", "Present", "Total", "Percentage"])
    for r in summary_rows(course_id):
        w.writerow([r["roll_no"], r["name"], r["code"], r["present"], r["total"], r["pct"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=attendance_report.csv"})


# ---------- student ----------
@app.route("/me")
@login_required("student")
def my_attendance():
    db = get_db()
    sid = session["student_id"]
    student = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    rows = [r for r in summary_rows() if r["sid"] == sid]
    history = db.execute("""SELECT a.date, a.status, a.method, c.code FROM attendance a
                            JOIN courses c ON c.id=a.course_id WHERE a.student_id=?
                            ORDER BY a.date DESC LIMIT 30""", (sid,)).fetchall()
    return render_template("student.html", student=student, rows=rows,
                           history=history, threshold=THRESHOLD)


@app.route("/checkin/<token>")
@login_required("student")
def checkin(token):
    db = get_db()
    s = db.execute("SELECT * FROM qr_sessions WHERE token=?", (token,)).fetchone()
    if not s or datetime.fromisoformat(s["expires_at"]) < datetime.now():
        flash("This QR code is invalid or has expired", "error")
        return redirect(url_for("my_attendance"))
    db.execute("""INSERT INTO attendance(student_id,course_id,date,status,method)
                  VALUES(?,?,?,'Present','qr')
                  ON CONFLICT(student_id,course_id,date)
                  DO UPDATE SET status='Present', method='qr'""",
               (session["student_id"], s["course_id"], s["date"]))
    db.commit()
    flash("Attendance marked. You are present!", "ok")
    return redirect(url_for("my_attendance"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
