# Personal Work Manager Backend

Django + Django REST Framework backend for the Personal Work Manager React app.

## Setup

```bash
cd pwm_backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env  # Windows
# cp .env.example .env  # macOS/Linux

python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

Seed default email types after registering a user:

```bash
python manage.py seed_defaults --email your@email.com
```

## Auth APIs

| Page | API | Method |
|---|---|---|
| Register | `/api/auth/register/` | POST |
| Login | `/api/auth/login/` | POST |
| Refresh token | `/api/auth/refresh/` | POST |
| Profile/settings | `/api/auth/me/` | GET/PATCH |
| Change password | `/api/auth/change-password/` | POST |
| Forgot password | `/api/auth/forgot-password/` | POST |
| Reset password | `/api/auth/reset-password/` | POST |

Login body:

```json
{
  "email": "anil@example.com",
  "password": "your-password"
}
```

Use the token in React:

```txt
Authorization: Bearer <access_token>
```

## Daily Page APIs

| React Page | API |
|---|---|
| Dashboard | `/api/dashboard/` |
| Tasks list/new/detail | `/api/tasks/`, `/api/tasks/{id}/` |
| Meetings/MOM | `/api/meetings/`, `/api/meetings/{id}/mom/` |
| Leave | `/api/leave/`, `/api/leave/{id}/` |
| Salary | `/api/salary/`, `/api/salary/{id}/`, `/api/salary/calculate/` |
| Emails | `/api/emails/`, `/api/emails/{id}/send/` |
| Reports hub | `/api/reports/` |
| Task report | `/api/reports/tasks/` |
| Meeting report | `/api/reports/meetings/` |
| Salary report | `/api/reports/salary/` |
| Leave report | `/api/reports/leave/` |
| Email report | `/api/reports/emails/` |
| Employee report | `/api/reports/employees/` |

## Management APIs

| React Page | API |
|---|---|
| Jobs | `/api/management/jobs/` |
| Job detail | `/api/management/jobs/{id}/full-detail/` |
| Freelancing/Sources | `/api/management/sources/` or `/api/management/freelancing/` |
| Projects | `/api/management/projects/` |
| Employees/Members | `/api/management/employees/` |
| Email Types | `/api/management/email-types/` |
| Email Templates | `/api/management/email-templates/` |
| SMTP Config | `/api/management/smtp/`, `/api/management/smtp/{id}/test/` |
| Salary Rules | `/api/management/salary-rules/` |
| Designation History | `/api/management/designations/` |
| Salary Changes | `/api/management/salary-changes/` |
| Attachments | `/api/management/attachments/` |

## Useful filters

```txt
/api/tasks/?task_type=job&status=todo&today=1
/api/tasks/?this_week=1
/api/meetings/?status=scheduled&date_from=2026-06-01&date_to=2026-06-30
/api/leave/?job=1&leave_type=half_day
/api/salary/?salary_type=job&status=partial&month=2026-06
/api/management/projects/?source_type=freelancing
/api/management/employees/?member_type=client
```

## Salary calculation API

This handles onboarding/pro-rata and salary changes.

```http
POST /api/salary/calculate/
```

```json
{
  "job": 1,
  "month": "2026-02-01"
}
```

Response:

```json
{
  "expected_amount": "9500.00",
  "calculation_lines": [
    {
      "from_date": "2026-02-10",
      "to_date": "2026-02-28",
      "daily_rate": "500.00",
      "days": "19",
      "amount": "9500.00",
      "notes": "Salary 15000 pro-rata"
    }
  ]
}
```

## React integration order

1. Auth pages: replace login/register mock flow with `/api/auth/*`.
2. Management pages: jobs, sources, projects, employees, email types/templates, SMTP.
3. Daily pages: tasks, meetings, leave, salary, emails.
4. Dashboard/reports: connect aggregate endpoints.


## Bulk create APIs for current React FE

These two endpoints were added for the current React wizard screens.

### Bulk Tasks

Use this from `src/pages/AddTask.jsx` on the final Save button. The FE already has `validRows`, `sel.type`, `sel.source`, and `sel.project`, so the payload can stay close to the UI state.

```http
POST /api/tasks/bulk-create/
Authorization: Bearer <access_token>
```

```json
{
  "type": "job",
  "source": 1,
  "project": 1,
  "tasks": [
    {
      "name": "Fix login bug",
      "date": "2026-06-14",
      "start": "10:00",
      "end": "12:00",
      "priority": "high",
      "status": "todo",
      "assigned": "Rahul Sharma"
    },
    {
      "name": "Write unit tests",
      "date": "2026-06-15",
      "start": "09:00",
      "end": "11:00",
      "priority": "normal",
      "status": "in_progress",
      "assigned": "Priya Singh"
    }
  ]
}
```

Notes:
- `type=job` maps `source` to `job`.
- `type=freelancing` maps `source` to `freelance_source`.
- `project` is converted into `projects: [project]`.
- `assigned` is resolved against the user's employees/members by name/email. If not found, a lightweight member is created for the logged-in user only.
- The request is all-or-nothing. If one row is invalid, no task rows are saved.

### Bulk Meetings

The current React meeting wizard creates one meeting, but this endpoint supports future multiple meeting rows or notepad/table import.

```http
POST /api/meetings/bulk-create/
Authorization: Bearer <access_token>
```

```json
{
  "type": "job",
  "source": 1,
  "project": 1,
  "meetings": [
    {
      "title": "Sprint Planning Q3",
      "date": "2026-06-14",
      "start": "10:00",
      "end": "11:00",
      "location": "Google Meet",
      "link": "https://meet.google.com/xxx",
      "agenda": "Plan sprint tasks",
      "mom": "Discussed sprint goals and blockers.",
      "conclusion": "Start backend work first.",
      "next_action": "Prepare API integration.",
      "status": "scheduled",
      "members": [1, 2, 3]
    }
  ]
}
```

Notes:
- `members: [1,2]` is converted into meeting member role rows with default role `attendee`.
- You can also send `member_roles` directly when the FE starts storing roles:

```json
{
  "member_roles": [
    { "member": 1, "role": "host", "attended": true },
    { "member": 2, "role": "client", "attended": false }
  ]
}
```

Response for both endpoints:

```json
{
  "count": 2,
  "ids": [10, 11],
  "results": []
}
```

## Integrated frontend
Use the matching integrated frontend zip. Create frontend `.env` from `.env.example`:

```env
VITE_API_BASE_URL=http://127.0.0.1:8000/api
```

Run backend first, then frontend:

```bash
python manage.py runserver 127.0.0.1:8000
npm install
npm run dev
```

## Feature verification
- Python syntax checked with `python -m compileall`.
- Frontend build checked with `npm run build` in the integrated frontend package.
