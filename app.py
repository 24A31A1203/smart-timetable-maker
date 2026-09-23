
from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3, hashlib, secrets, re, json
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = "smart-timetable-prototype-change-this"
DB = "timetable.db"

DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def hp(p): return hashlib.sha256(p.encode()).hexdigest()

def init_db():
    c=db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS colleges(
      id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, location TEXT,
      email TEXT, phone TEXT, password_hash TEXT,
      email_verified INTEGER DEFAULT 0, phone_verified INTEGER DEFAULT 0,
      student_otp TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS otp_codes(
      id INTEGER PRIMARY KEY, college_id INTEGER, email_code TEXT, phone_code TEXT,
      created_at TEXT, verified_email INTEGER DEFAULT 0, verified_phone INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS requirements(
      college_id INTEGER PRIMARY KEY, rooms_count INTEGER DEFAULT 0,
      departments_count INTEGER DEFAULT 0, teachers_count INTEGER DEFAULT 0,
      labs_count INTEGER DEFAULT 0, start_time TEXT DEFAULT '09:00',
      end_time TEXT DEFAULT '16:00', period_minutes INTEGER DEFAULT 50,
      lunch_start TEXT DEFAULT '13:00', lunch_end TEXT DEFAULT '14:00',
      working_days TEXT DEFAULT '["Monday","Tuesday","Wednesday","Thursday","Friday"]'
    );
    CREATE TABLE IF NOT EXISTS departments(
      id INTEGER PRIMARY KEY, college_id INTEGER, name TEXT
    );
    CREATE TABLE IF NOT EXISTS teachers(
      id INTEGER PRIMARY KEY, college_id INTEGER, name TEXT, email TEXT,
      password_hash TEXT
    );
    CREATE TABLE IF NOT EXISTS rooms(
      id INTEGER PRIMARY KEY, college_id INTEGER, name TEXT, is_lab INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS sections(
      id INTEGER PRIMARY KEY, college_id INTEGER, name TEXT, department_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS students(
      id INTEGER PRIMARY KEY, college_id INTEGER, roll TEXT, name TEXT,
      section TEXT, password_hash TEXT
    );
    CREATE TABLE IF NOT EXISTS subjects(
      id INTEGER PRIMARY KEY, college_id INTEGER, name TEXT, teacher_id INTEGER,
      section TEXT, weekly_hours INTEGER, kind TEXT
    );
    CREATE TABLE IF NOT EXISTS timetable(
      id INTEGER PRIMARY KEY, college_id INTEGER, day TEXT, slot TEXT,
      section TEXT, subject TEXT, teacher TEXT, room TEXT, kind TEXT,
      status TEXT DEFAULT 'DRAFT', round_no INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS teacher_reviews(
      id INTEGER PRIMARY KEY, college_id INTEGER, teacher TEXT, round_no INTEGER,
      status TEXT DEFAULT 'PENDING', message TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS teacher_requests(
      id INTEGER PRIMARY KEY, college_id INTEGER, teacher TEXT, message TEXT,
      parsed_rule TEXT, unavailable_day TEXT, unavailable_slot TEXT,
      status TEXT DEFAULT 'PENDING', created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS notifications(
      id INTEGER PRIMARY KEY, college_id INTEGER, role TEXT, username TEXT,
      message TEXT, created_at TEXT, is_read INTEGER DEFAULT 0
    );
    """)
    c.commit(); c.close()

def college():
    if not session.get("college_id"): return None
    c=db(); x=c.execute("SELECT * FROM colleges WHERE id=?", (session["college_id"],)).fetchone(); c.close()
    return x

def require(role):
    return session.get("role")==role

def minutes(s):
    h,m=map(int,s.split(":")); return h*60+m

def clock(n): return f"{n//60:02d}:{n%60:02d}"

def periods(req):
    cur=minutes(req["start_time"]); end=minutes(req["end_time"])
    ls=minutes(req["lunch_start"]); le=minutes(req["lunch_end"])
    out=[]
    while cur+req["period_minutes"]<=end:
        nxt=cur+req["period_minutes"]
        if not (cur < le and nxt > ls):
            out.append(f"{clock(cur)}-{clock(nxt)}")
        cur=nxt
    return out

def send_note(c, role, username, message):
    c.execute("INSERT INTO notifications(college_id,role,username,message,created_at) VALUES(?,?,?,?,?)",
              (session["college_id"],role,username,message,datetime.now().isoformat()))

def parse_request(msg):
    day=None; slot=None
    for d in DAYS:
        if d.lower() in msg.lower(): day=d; break
    m=re.search(r'(\d{1,2}(?::\d{2})?)\s*(?:-|to)\s*(\d{1,2}(?::\d{2})?)\s*(?:am|pm)?', msg.lower())
    if m:
        a,b=m.group(1),m.group(2)
        def norm(x):
            if ":" not in x: x += ":00"
            h,mi=map(int,x.split(":"))
            # Interpret 9/10 as morning; 1-6 as afternoon
            if h<7: h+=12
            return f"{h:02d}:{mi:02d}"
        slot=f"{norm(a)}-{norm(b)}"
    rule="Teacher availability constraint"
    if day and slot: rule += f": avoid {day} {slot}"
    elif "morning" in msg.lower(): rule += ": avoid morning slots"
    elif "afternoon" in msg.lower(): rule += ": avoid afternoon slots"
    elif "half-day" in msg.lower() or "leave" in msg.lower(): rule += ": teacher unavailable for stated period/day"
    return day,slot,rule

def generate(college_id, round_no):
    c=db()
    req=c.execute("SELECT * FROM requirements WHERE college_id=?", (college_id,)).fetchone()
    if not req: c.close(); return False,"Complete timing requirements first."
    teachers=list(c.execute("SELECT * FROM teachers WHERE college_id=?", (college_id,)))
    rooms=list(c.execute("SELECT * FROM rooms WHERE college_id=?", (college_id,)))
    subs=list(c.execute("SELECT * FROM subjects WHERE college_id=?", (college_id,)))
    if not teachers or not rooms or not subs:
        c.close(); return False,"Add teachers, rooms/labs and subjects before generating."
    days=json.loads(req["working_days"]); slots=periods(req)
    if not slots: c.close(); return False,"No usable periods. Check college timing/lunch."
    c.execute("DELETE FROM timetable WHERE college_id=?", (college_id,))
    # Active teacher constraints from pending/processed requests
    blocks=set()
    for r in c.execute("""SELECT * FROM teacher_requests WHERE college_id=? AND status IN ('PENDING','APPLIED')""",(college_id,)):
        if r["unavailable_day"] and r["unavailable_slot"]: blocks.add((r["teacher"],r["unavailable_day"],r["unavailable_slot"]))
    jobs=[]
    for s in subs:
        for _ in range(max(1,int(s["weekly_hours"]))): jobs.append(s)
    jobs.sort(key=lambda s: 0 if s["kind"].lower()=="lab" else 1)
    used_t=set(); used_r=set()
    placed=0
    for s in jobs:
        teacher=c.execute("SELECT name FROM teachers WHERE id=?", (s["teacher_id"],)).fetchone()
        teacher=teacher["name"] if teacher else "Unassigned"
        ok=False
        for d in days:
            for sl in slots:
                if (teacher,d,sl) in blocks or (teacher,d,sl) in used_t: continue
                if c.execute("SELECT 1 FROM timetable WHERE college_id=? AND day=? AND slot=? AND section=?",
                             (college_id,d,sl,s["section"])).fetchone(): continue
                candidates=[r for r in rooms if (not s["kind"].lower()=="lab" or r["is_lab"]) and (d,sl,r["name"]) not in used_r]
                if not candidates: continue
                room=candidates[0]["name"]
                c.execute("""INSERT INTO timetable(college_id,day,slot,section,subject,teacher,room,kind,status,round_no)
                             VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (college_id,d,sl,s["section"],s["name"],teacher,room,s["kind"],"DRAFT",round_no))
                used_t.add((teacher,d,sl)); used_r.add((d,sl,room)); placed+=1; ok=True; break
            if ok: break
        if not ok:
            c.close(); return False,f"Could not place {s['name']}. Add rooms/periods or reduce weekly hours."
    c.commit(); c.close()
    return True,f"Timetable generated successfully ({placed} periods)."

@app.route("/")
def home(): return render_template("home.html", college=college())

@app.route("/admin/signup", methods=["GET","POST"])
def admin_signup():
    if request.method=="POST":
        f=request.form
        if f["password"] != f["confirm_password"]:
            flash("Passwords do not match."); return render_template("admin_signup.html")
        c=db()
        if c.execute("SELECT 1 FROM colleges WHERE name=?", (f["college_name"].strip(),)).fetchone():
            c.close(); flash("College already registered."); return render_template("admin_signup.html")
        c.execute("""INSERT INTO colleges(name,location,email,phone,password_hash,created_at)
                     VALUES(?,?,?,?,?,?)""",
                  (f["college_name"].strip(),f["location"].strip(),f["email"].strip(),f["phone"].strip(),hp(f["password"]),datetime.now().isoformat()))
        cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        email_code=f"{secrets.randbelow(900000)+100000}"; phone_code=f"{secrets.randbelow(900000)+100000}"
        c.execute("INSERT INTO otp_codes(college_id,email_code,phone_code,created_at) VALUES(?,?,?,?)",
                  (cid,email_code,phone_code,datetime.now().isoformat()))
        c.commit(); c.close()
        session["verify_college_id"]=cid
        session["demo_email_otp"]=email_code; session["demo_phone_otp"]=phone_code
        flash("Prototype OTPs generated. Enter the codes shown on the verification screen.")
        return redirect(url_for("verify"))
    return render_template("admin_signup.html")

@app.route("/admin/verify", methods=["GET","POST"])
def verify():
    cid=session.get("verify_college_id")
    if not cid: return redirect(url_for("admin_signup"))
    if request.method=="POST":
        c=db(); row=c.execute("SELECT * FROM otp_codes WHERE college_id=? ORDER BY id DESC LIMIT 1",(cid,)).fetchone()
        if row and request.form["email_otp"]==row["email_code"] and request.form["phone_otp"]==row["phone_code"]:
            c.execute("UPDATE colleges SET email_verified=1,phone_verified=1 WHERE id=?",(cid,))
            c.execute("UPDATE otp_codes SET verified_email=1,verified_phone=1 WHERE id=?",(row["id"],))
            c.commit(); c.close()
            session.pop("verify_college_id",None); session.pop("demo_email_otp",None); session.pop("demo_phone_otp",None)
            flash("Email and phone verified. Your college account is created. Please sign in.")
            return redirect(url_for("admin_login"))
        c.close(); flash("Invalid OTP. Please check both codes.")
    return render_template("verify.html", email_otp=session.get("demo_email_otp"), phone_otp=session.get("demo_phone_otp"))

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method=="POST":
        c=db(); row=c.execute("SELECT * FROM colleges WHERE name=?", (request.form["college_name"].strip(),)).fetchone(); c.close()
        if row and row["password_hash"]==hp(request.form["password"]) and row["email_verified"] and row["phone_verified"]:
            session.clear(); session["role"]="admin"; session["college_id"]=row["id"]; session["username"]=row["name"]
            return redirect(url_for("admin_dashboard"))
        flash("Invalid college name/password or account not verified.")
    return render_template("admin_login.html")

@app.route("/teacher/login", methods=["GET","POST"])
def teacher_login():
    if request.method=="POST":
        c=db(); col=c.execute("SELECT * FROM colleges WHERE name=?", (request.form["college_name"].strip(),)).fetchone()
        row=None
        if col: row=c.execute("SELECT * FROM teachers WHERE college_id=? AND name=?", (col["id"],request.form["name"].strip())).fetchone()
        c.close()
        if row and row["password_hash"]==hp(request.form["password"]):
            session.clear(); session["role"]="teacher"; session["college_id"]=col["id"]; session["username"]=row["name"]
            return redirect(url_for("teacher_dashboard"))
        flash("Invalid teacher login details.")
    return render_template("teacher_login.html")

@app.route("/student/login", methods=["GET","POST"])
def student_login():
    if request.method=="POST":
        c=db(); col=c.execute("SELECT * FROM colleges WHERE name=?", (request.form["college_name"].strip(),)).fetchone()
        row=None
        if col:
            row=c.execute("SELECT * FROM students WHERE college_id=? AND roll=?", (col["id"],request.form["roll"].strip())).fetchone()
        if col and row and row["password_hash"]==hp(request.form["password"]) and request.form["access_otp"]==col["student_otp"]:
            session.clear(); session["role"]="student"; session["college_id"]=col["id"]; session["username"]=row["roll"]; session["section"]=row["section"]
            c.close(); return redirect(url_for("student_dashboard"))
        c.close(); flash("Invalid student login details or access OTP.")
    return render_template("student_login.html")

@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("home"))

@app.route("/admin", methods=["GET","POST"])
def admin_dashboard():
    if not require("admin"): return redirect(url_for("admin_login"))
    cid=session["college_id"]; c=db()
    if request.method=="POST":
        f=request.form; action=f["action"]
        try:
            if action=="requirements":
                days=[d.strip() for d in f["days"].split(",") if d.strip()]
                c.execute("""INSERT INTO requirements(college_id,rooms_count,departments_count,teachers_count,labs_count,start_time,end_time,period_minutes,lunch_start,lunch_end,working_days)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(college_id) DO UPDATE SET rooms_count=excluded.rooms_count,departments_count=excluded.departments_count,teachers_count=excluded.teachers_count,labs_count=excluded.labs_count,start_time=excluded.start_time,end_time=excluded.end_time,period_minutes=excluded.period_minutes,lunch_start=excluded.lunch_start,lunch_end=excluded.lunch_end,working_days=excluded.working_days""",
                          (cid,int(f["rooms_count"]),int(f["departments_count"]),int(f["teachers_count"]),int(f["labs_count"]),f["start_time"],f["end_time"],int(f["period_minutes"]),f["lunch_start"],f["lunch_end"],json.dumps(days)))
                c.commit(); flash("College requirements saved.")
            elif action=="department":
                c.execute("INSERT INTO departments(college_id,name) VALUES(?,?)",(cid,f["name"].strip())); c.commit(); flash("Department added.")
            elif action=="teacher":
                c.execute("INSERT INTO teachers(college_id,name,email,password_hash) VALUES(?,?,?,?)",(cid,f["name"].strip(),f["email"].strip(),hp(f["password"]))); c.commit(); flash("Teacher added.")
            elif action=="room":
                c.execute("INSERT INTO rooms(college_id,name,is_lab) VALUES(?,?,?)",(cid,f["name"].strip(),1 if f.get("is_lab") else 0)); c.commit(); flash("Room/lab added.")
            elif action=="section":
                c.execute("INSERT INTO sections(college_id,name,department_id) VALUES(?,?,?)",(cid,f["name"].strip(),int(f["department_id"]))); c.commit(); flash("Section added.")
            elif action=="student":
                c.execute("INSERT INTO students(college_id,roll,name,section,password_hash) VALUES(?,?,?,?,?)",(cid,f["roll"].strip(),f["name"].strip(),f["section"].strip(),hp(f["password"]))); c.commit(); flash("Student added.")
            elif action=="subject":
                c.execute("INSERT INTO subjects(college_id,name,teacher_id,section,weekly_hours,kind) VALUES(?,?,?,?,?,?)",(cid,f["name"].strip(),int(f["teacher_id"]),f["section"].strip(),int(f["weekly_hours"]),f["kind"])); c.commit(); flash("Subject added.")
            elif action=="access_otp":
                otp=f["student_otp"].strip()
                if len(otp)<4: raise ValueError()
                c.execute("UPDATE colleges SET student_otp=? WHERE id=?",(otp,cid)); c.commit(); flash("Student access OTP saved.")
            elif action=="generate":
                c.commit(); c.close(); success,msg=generate(cid,1); flash(msg); return redirect(url_for("admin_dashboard"))
        except Exception as e:
            c.rollback(); flash("Could not save this item. Check the values and avoid duplicate names.")
    req=c.execute("SELECT * FROM requirements WHERE college_id=?",(cid,)).fetchone()
    data={
      "req":req,
      "departments":list(c.execute("SELECT * FROM departments WHERE college_id=?",(cid,))),
      "teachers":list(c.execute("SELECT * FROM teachers WHERE college_id=?",(cid,))),
      "rooms":list(c.execute("SELECT * FROM rooms WHERE college_id=?",(cid,))),
      "sections":list(c.execute("SELECT sections.*,departments.name department FROM sections LEFT JOIN departments ON departments.id=sections.department_id WHERE sections.college_id=?",(cid,))),
      "students":list(c.execute("SELECT * FROM students WHERE college_id=?",(cid,))),
      "subjects":list(c.execute("SELECT subjects.*,teachers.name teacher FROM subjects LEFT JOIN teachers ON teachers.id=subjects.teacher_id WHERE subjects.college_id=?",(cid,))),
      "tt":list(c.execute("SELECT * FROM timetable WHERE college_id=? ORDER BY CASE day WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2 WHEN 'Wednesday' THEN 3 WHEN 'Thursday' THEN 4 WHEN 'Friday' THEN 5 WHEN 'Saturday' THEN 6 END,slot,section",(cid,))),
      "reviews":list(c.execute("SELECT teacher,status,message,round_no FROM teacher_reviews WHERE college_id=? GROUP BY teacher ORDER BY teacher",(cid,))),
      "requests":list(c.execute("SELECT * FROM teacher_requests WHERE college_id=? ORDER BY id DESC",(cid,))),
      "college":c.execute("SELECT * FROM colleges WHERE id=?",(cid,)).fetchone()
    }
    c.close(); return render_template("admin.html",**data)

@app.route("/admin/send")
def admin_send():
    if not require("admin"): return redirect(url_for("admin_login"))
    c=db(); cid=session["college_id"]
    round_no=c.execute("SELECT COALESCE(MAX(round_no),1) FROM timetable WHERE college_id=?",(cid,)).fetchone()[0]
    c.execute("UPDATE timetable SET status='UNDER_REVIEW' WHERE college_id=?",(cid,))
    c.execute("DELETE FROM teacher_reviews WHERE college_id=? AND round_no=?",(cid,round_no))
    teachers=list(c.execute("SELECT name FROM teachers WHERE college_id=?",(cid,)))
    for t in teachers:
        c.execute("INSERT INTO teacher_reviews(college_id,teacher,round_no,status,created_at) VALUES(?,?,?,?,?)",(cid,t["name"],round_no,"PENDING",datetime.now().isoformat()))
        send_note(c,"teacher",t["name"],"Your admin has sent you today's timetable for verification.")
    c.commit(); c.close(); flash("Today's timetable was sent to all teachers."); return redirect(url_for("admin_dashboard"))

@app.route("/teacher")
def teacher_dashboard():
    if not require("teacher"): return redirect(url_for("teacher_login"))
    c=db(); cid=session["college_id"]; teacher=session["username"]
    rows=list(c.execute("SELECT * FROM timetable WHERE college_id=? AND teacher=? ORDER BY day,slot",(cid,teacher)))
    notes=list(c.execute("SELECT * FROM notifications WHERE college_id=? AND role='teacher' AND username=? ORDER BY id DESC",(cid,teacher)))
    review=c.execute("SELECT * FROM teacher_reviews WHERE college_id=? AND teacher=? ORDER BY id DESC LIMIT 1",(cid,teacher)).fetchone()
    c.close(); return render_template("teacher.html",rows=rows,notes=notes,review=review,teacher=teacher)

@app.route("/teacher/review", methods=["POST"])
def teacher_review():
    if not require("teacher"): return redirect(url_for("teacher_login"))
    cid=session["college_id"]; teacher=session["username"]; decision=request.form["decision"]; msg=request.form.get("message","").strip()
    c=db(); rev=c.execute("SELECT * FROM teacher_reviews WHERE college_id=? AND teacher=? ORDER BY id DESC LIMIT 1",(cid,teacher)).fetchone()
    if not rev: c.close(); flash("No timetable is waiting for your review."); return redirect(url_for("teacher_dashboard"))
    status="APPROVED" if decision=="approve" else "REQUESTED"
    c.execute("UPDATE teacher_reviews SET status=?,message=? WHERE id=?",(status,msg,rev["id"]))
    if decision=="request":
        day,slot,rule=parse_request(msg)
        c.execute("""INSERT INTO teacher_requests(college_id,teacher,message,parsed_rule,unavailable_day,unavailable_slot,status,created_at)
                     VALUES(?,?,?,?,?,?,?,?)""",(cid,teacher,msg,rule,day,slot,"PENDING",datetime.now().isoformat()))
        send_note(c,"admin","admin",f"{teacher} requested a timetable change: {msg}")
    else:
        send_note(c,"admin","admin",f"{teacher} approved the timetable.")
    c.commit(); c.close()
    flash("Your response was submitted."); return redirect(url_for("teacher_dashboard"))

@app.route("/admin/regenerate")
def admin_regenerate():
    if not require("admin"): return redirect(url_for("admin_login"))
    cid=session["college_id"]; c=db()
    reqs=list(c.execute("SELECT id,teacher FROM teacher_requests WHERE college_id=? AND status='PENDING'",(cid,)))
    for r in reqs: c.execute("UPDATE teacher_requests SET status='APPLIED' WHERE id=?",(r["id"],))
    c.execute("UPDATE teacher_reviews SET status='PENDING' WHERE college_id=?",(cid,))
    c.commit(); c.close()
    # determine round
    c=db(); old=c.execute("SELECT COALESCE(MAX(round_no),1) FROM timetable WHERE college_id=?",(cid,)).fetchone()[0]; c.close()
    ok,msg=generate(cid,old+1); flash(("Regenerated with teacher requests. " if ok else "")+msg); return redirect(url_for("admin_dashboard"))

@app.route("/admin/finalize")
def admin_finalize():
    if not require("admin"): return redirect(url_for("admin_login"))
    c=db(); cid=session["college_id"]
    total=c.execute("SELECT COUNT(*) n FROM teachers WHERE college_id=?",(cid,)).fetchone()["n"]
    approved=c.execute("""SELECT COUNT(*) n FROM teacher_reviews WHERE college_id=? AND status='APPROVED'
                          AND round_no=(SELECT COALESCE(MAX(round_no),1) FROM timetable WHERE college_id=?)""",(cid,cid)).fetchone()["n"]
    if total==0 or approved!=total:
        c.close(); flash("All teachers must approve the current timetable before finalizing."); return redirect(url_for("admin_dashboard"))
    c.execute("UPDATE timetable SET status='FINAL' WHERE college_id=?",(cid,))
    c.commit(); c.close(); flash("All teachers approved. Timetable finalized."); return redirect(url_for("admin_dashboard"))

@app.route("/admin/publish")
def admin_publish():
    if not require("admin"): return redirect(url_for("admin_login"))
    c=db(); cid=session["college_id"]
    c.execute("UPDATE timetable SET status='PUBLISHED' WHERE college_id=?",(cid,))
    students=list(c.execute("SELECT roll FROM students WHERE college_id=?",(cid,)))
    for s in students: send_note(c,"student",s["roll"],"Today's timetable was created and published by your admin.")
    c.commit(); c.close(); flash("Timetable access published to students."); return redirect(url_for("admin_dashboard"))

@app.route("/student")
def student_dashboard():
    if not require("student"): return redirect(url_for("student_login"))
    c=db(); cid=session["college_id"]; roll=session["username"]
    st=c.execute("SELECT * FROM students WHERE college_id=? AND roll=?",(cid,roll)).fetchone()
    rows=list(c.execute("SELECT * FROM timetable WHERE college_id=? AND section=? AND status='PUBLISHED' ORDER BY day,slot",(cid,st["section"])))
    notes=list(c.execute("SELECT * FROM notifications WHERE college_id=? AND role='student' AND username=? ORDER BY id DESC",(cid,roll)))
    c.close(); return render_template("student.html",rows=rows,notes=notes,student=st)

@app.route("/reset", methods=["POST"])
def reset():
    # Prototype convenience for local demos only.
    c=db(); c.executescript("""DELETE FROM notifications; DELETE FROM teacher_requests; DELETE FROM teacher_reviews; DELETE FROM timetable; DELETE FROM subjects; DELETE FROM students; DELETE FROM sections; DELETE FROM rooms; DELETE FROM teachers; DELETE FROM departments; DELETE FROM requirements; DELETE FROM otp_codes; DELETE FROM colleges;"""); c.commit(); c.close(); session.clear(); flash("Local prototype data reset."); return redirect(url_for("home"))

if __name__=="__main__":
    init_db()
    print("Smart College Timetable Maker V2 starting...")
    app.run(debug=True)
