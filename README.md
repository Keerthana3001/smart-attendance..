# Smart Attendance Management System

Flask + SQLite. Features: teacher/student login, student & course management, manual marking,
time-limited QR check-in, percentage reports with defaulters (<75%), CSV export.

## Run in VS Code
```
python -m venv venv
venv\Scripts\activate        # Windows  (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000
- Teacher: `admin` / `admin123`
- Student: roll number / roll number (created when a teacher adds the student)

Change `app.secret_key` in app.py before deploying.
