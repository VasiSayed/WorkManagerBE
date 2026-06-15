# Personal Work Manager — Backend Feature Matrix

This backend follows the UI/UX blueprint and the React frontend route map.

## User-wise data isolation
Every business model inherits `OwnedModel` and stores `owner`. Every ViewSet filters by `request.user`, and every create saves `owner=request.user`. Related fields use owner-scoped primary-key fields, so one user cannot attach another user's job/project/member records.

## Implemented modules and APIs

### Auth
- Register: `POST /api/auth/register/`
- Login JWT: `POST /api/auth/login/`
- Refresh JWT: `POST /api/auth/refresh/`
- Current user/profile: `GET/PATCH /api/auth/me/`
- Change password: `POST /api/auth/change-password/`
- Forgot/reset placeholders: `POST /api/auth/forgot-password/`, `POST /api/auth/reset-password/`

### Dashboard
- Daily summary cards: `GET /api/dashboard/`
- Returns today's tasks, upcoming meetings, and pending salaries.

### Tasks
- CRUD: `/api/tasks/`
- Detail/edit/delete: `/api/tasks/{id}/`
- Bulk task create for FE table/notepad import: `POST /api/tasks/bulk-create/`
- Supports type/source/project + many task rows.

### Meetings / MOM
- CRUD: `/api/meetings/`
- Bulk meeting create: `POST /api/meetings/bulk-create/`
- MOM update: `PATCH /api/meetings/{id}/mom/`
- Supports projects, members, roles, attendance, agenda, MOM, conclusion, next action.

### Leave / Holiday / Half Day
- CRUD: `/api/leave/`
- Supports company/job, leave type, paid/unpaid, deduction, email template and recipients.

### Salary / Payments
- CRUD: `/api/salary/`
- Job salary calculation: `POST /api/salary/calculate/`
- Supports pro-rata calculation from joining date and salary change history.
- Nested calculation lines are stored with each salary record.

### Emails
- CRUD/log: `/api/emails/`
- Send existing email log: `POST /api/emails/{id}/send/`
- Supports email types, templates, recipients, CC/BCC, status, SMTP failure log.

### Reports
- Hub: `GET /api/reports/`
- Categories: `/api/reports/tasks/`, `/api/reports/meetings/`, `/api/reports/salary/`, `/api/reports/leave/`, `/api/reports/emails/`, `/api/reports/employees/`, `/api/reports/designations/`

### Management
- Jobs: `/api/management/jobs/`
- Freelancing sources: `/api/management/sources/` and `/api/management/freelancing/`
- Projects: `/api/management/projects/`
- Employees/members: `/api/management/employees/`
- Email types: `/api/management/email-types/`
- Email templates: `/api/management/email-templates/`
- SMTP config: `/api/management/smtp/`
- Salary rules: `/api/management/salary-rules/`
- Designation history: `/api/management/designations/`
- Salary changes: `/api/management/salary-changes/`
- Attachments: `/api/management/attachments/`

## Frontend integration payloads
The integrated React zip calls these endpoints through `src/api/client.js`. Set this in frontend `.env`:

```env
VITE_API_BASE_URL=http://127.0.0.1:8000/api
```

