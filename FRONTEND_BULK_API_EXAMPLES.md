# Frontend bulk API mapping

## AddTask.jsx final Save

Current UI state:

```js
sel = { type, source, project }
validRows = [
  { name, date, start, end, priority, status, assigned }
]
```

Send this payload:

```js
await api.post('/tasks/bulk-create/', {
  type: sel.type,
  source: sel.source,
  project: sel.project,
  manual_source_name: sel.type === 'manual' ? srcName : '',
  tasks: validRows.map(row => ({
    name: row.name,
    date: row.date || undefined,
    start: row.start || undefined,
    end: row.end || undefined,
    priority: row.priority || 'normal',
    status: row.status || 'todo',
    assigned: row.assigned || '',
  })),
});
```

## AddMeeting.jsx final Save

Current meeting wizard is single record, but you can still call bulk API with one item:

```js
await api.post('/meetings/bulk-create/', {
  type: sel.type,
  source: sel.source,
  project: sel.project,
  manual_source_name: ['manual', 'general'].includes(sel.type) ? 'Manual' : '',
  meetings: [{
    title: form.title,
    date: form.date,
    start: form.start,
    end: form.end || undefined,
    location: form.location,
    link: form.link,
    agenda: form.agenda,
    status: form.status || 'scheduled',
    mom: mom.mom,
    conclusion: mom.conclusion,
    next_action: mom.next_action,
    members,
  }],
});
```

When meeting roles are stored in React state, replace `members` with:

```js
member_roles: members.map(m => ({ member: m.id, role: m.role, attended: m.attended }))
```
