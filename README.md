# Smart College Timetable Maker — V2

This version follows the requested prototype workflow:

1. Landing page: Admin / Teacher / Student
2. Admin signup with college name, location, email, phone, password
3. Email + phone OTP verification flow (prototype OTPs are generated locally)
4. Admin login using college name + password
5. Admin setup: counts, departments, rooms, labs, teachers, sections, subjects, timings
6. Constraint-based timetable generation
7. Timetable table with day, time, section, subject, faculty and room
8. Admin sends today's timetable for teacher review
9. Teacher notification + Approve / Request Change
10. Admin sees approval status and teacher requests
11. Regenerate with teacher constraints
12. Teachers review again
13. Finalize when all teachers approve
14. Publish to students
15. Student notification + timetable

## Run on Windows
```powershell
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

## Important prototype notes
- OTP delivery is simulated. The app generates codes and shows them only in the verification flow.
- Notifications are in-app.
- Passwords are hashed, but production should use a stronger password hashing scheme such as Argon2/bcrypt/Werkzeug.
- The scheduler is a lightweight constraint solver. OR-Tools CP-SAT can replace it for a production-grade optimizer.
