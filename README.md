# Resume editor

An editable HTML rebuild of the original PDF, backed by Postgres (Neon) for
named variants and version history.

```bash
python3 server.py     # http://127.0.0.1:8000
```

Requires `fastapi`, `uvicorn` and `psycopg[binary,pool]`. On first run the
schema is created and the resume markup from `resume.html` is seeded as a
document called **Master**.

## Database

The engine is chosen by `DATABASE_URL` in `.env` (gitignored — see
`.env.example`):

- **set to a Postgres URL** → Neon, Supabase, or any Postgres
- **unset** → the local `resume.db` SQLite file, so the app still runs offline

Currently pointed at Neon (`ap-southeast-1`). The startup line names the engine.
The browser does not display it — which store you are on is not a decision you
make while editing — but it is named in the prompts that matter, e.g. "Save them
to neon before switching?", and in the tooltip on the sidebar's save line.

Two details that make the hosted path stable:

- the pool validates a connection on checkout (`check_connection`), so a
  connection dropped while Neon's compute was suspended is replaced rather than
  handed to a request
- prepared statements are disabled (`prepare_threshold=None`), which is required
  against Neon's `-pooler` endpoint — PgBouncer in transaction mode cannot carry
  server-side prepared statements between transactions

To move an existing SQLite database into Postgres:

```bash
python3 server.py --migrate
```

Documents and their full version history are copied, ids preserved where free,
and identity sequences advanced past them.

## Files

| file | what it is |
| --- | --- |
| `resume.html` | the resume itself, the editor UI, and the client-side store |
| `server.py` | FastAPI app, API, and the SQLite→Postgres migration |
| `db.py` | engine selection, schema, connection pool, `.env` loading |
| `.env` | your `DATABASE_URL` (gitignored, chmod 600) |
| `resume.db` | the old local SQLite store, kept as the migration source |

## URLs

The URL says where you are, so a profile is a link you can bookmark or share:

```
#/                 the home page — every profile as a card
#/p/3              that profile's resume, open in the editor
#/p/3/history      that resume's version history
```

Hash routing rather than paths, so the same file still routes when opened off
disk with no server. Back and forward work; the tab title follows the profile.
The 🏠 icon and the wordmark both return home.

## Profiles

Profiles organize your resume by persona — "me as SDE", "me as DS". Each profile
holds its own documents with independent version history, and the picker in the
top bar switches between them.

The home page owns everything profile-level: the cards under **Profiles** open,
**Edit** (name and description) and **Delete** a profile, the **New profile**
card creates one, and **Activity** restores anything deleted. There is no
profile menu in the top bar — only the picker, for switching while you edit.

Nothing is seeded for you: a fresh database gets a single **Primary** profile and
nothing else. Creating asks what the new profile should **start from** — a copy
of any existing resume, or the one baked into `resume.html`. Copying is usually
what you want; the built-in is a snapshot from when the file was written and
drifts out of date as you edit.

Switching to a profile that somehow has no documents creates a `Master` for it
rather than leaving the previous profile's resume on screen.

## Sharing

**Share link** in the sidebar mints an unguessable token and gives you a
read-only page at `/r/<token>`:

```
/r/k7m2x9qp4n                  the resume, view count, and comments
/r/k7m2x9qp4n?mode=resume      the sheet alone — no bar, no comments
```

It is not enumerable, not listed, and carries `noindex, nofollow`. **New link**
rotates the token and **Revoke** clears it; either stops every link already sent
out, immediately.

The page reuses the editor's own stylesheet, so it renders exactly as the PDF
does — its read-only rules already hide the block controls, adders and page
guides, so nothing has to be stripped. **Download PDF** opens the print dialog,
the same path as the editor's Download.

Each load increments `profile.views`, shown on the page and in the share dialog.
Bot and preview traffic counts, so read it as traffic rather than readers.

### Comments

Anyone with the link can leave a name and a comment, and it appears at once —
there is no approval step. You delete what you do not want, from the share
dialog. Deletion is soft, like everything else here.

Names and bodies are escaped on render; a comment containing markup shows as
text rather than running.

```
POST   /api/profiles/{id}/share      mint, or {rotate:true} for a new token
DELETE /api/profiles/{id}/share      revoke
GET    /api/profiles/{id}/comments   all live comments, for the owner
DELETE /api/comments/{id}            soft delete one
GET    /r/{token}                    the public page (?mode=resume for bare)
GET    /api/public/{token}           the same thing as JSON
POST   /api/public/{token}/comments  leave one {name, body}
```

## Deleting

**Nothing is ever removed.** `server.py` contains no `DELETE FROM` at all.
Profiles, documents and versions each carry a `deleted_at` stamp; deleting sets
it, and the row simply stops being listed. That includes autosave pruning past
`MAX_AUTOSAVES_PER_DOC`, which now hides old snapshots rather than dropping
them.

The **Activity** tab on the home page is the undo log: every deleted profile,
resume and snapshot, newest first, each with a Restore. `GET /api/activity`
returns it in one call.

- the last live profile cannot be deleted, and nor can a profile's last resume
- deleting twice is a no-op rather than an error
- a deleted profile's documents drop out of the "start from" picker
- rolling back to a deleted snapshot is refused rather than silently working
- name uniqueness is a partial unique index over live rows (`profile_live_name`,
  `document_live_name`), so a deleted row does not keep its name reserved.
  Restoring refuses if a live row has taken the name since — rename that one
  first.

`migrate_soft_delete()` runs on every start and is idempotent: it adds the
column to all three tables, drops the old unconditional `UNIQUE` constraints,
and creates the partial indexes.

This exists because an earlier hard delete destroyed a profile, its resume and
its entire version history in one call, with the cascade doing the rest.

## Naming

A profile holds one resume, and the profile's name is that resume's name — so
renaming happens in the profile dialog (👤 → **Rename** on a row), which edits
the name and the description shown on the landing card.

The schema still models several documents per profile and the API still exposes
them (`POST /api/documents` with `copy_of`), but no UI creates or switches them:
personas are profiles now, so the sidebar carries no variant picker.

## Page breaks and fitting

Breaks are yours to place. **Page break** inserts a forced break just above the
block the cursor is in; each break marker carries ↑ ↓ ✕ controls, so it can be
slid past any neighbouring block or removed with one click.

Dashed red guides across the sheet show where each printed page actually ends,
recomputed as you type — the same blocks the print engine keeps whole (roles,
list items, paragraphs) are walked to find the boundary. **Page count** under
*Design* reads the number of sheets, with the room left on the last one in its
tooltip; overflow is urgent rather than informational, so that goes inline and
turns red — `Page count: 3 · 12mm over`.

**Compact** tightens type and spacing (body 9pt → 8.5pt, line-height 1.27 →
1.22, smaller headings and gaps) and is remembered between sessions. It cannot
change the page margin — `@page` cannot be driven by a body class — so screen
and PDF stay identical.

## Small screens

Under 900px the sidebar stops taking 280px of the width and becomes a drawer
over the page, opened by ☰ in the top bar; the scrim and any tool you press
close it again. The landing page drops to a single column.

The sheet itself does not shrink. It stays a true 210mm and pans sideways,
because `measure()` reads real geometry off it to place the page guides — a
scaled sheet would make the page count lie about what prints. So a phone is
good for reading, small edits and checking the page count, and a laptop is
where you lay a resume out.

## Drafts vs saving

The database is remote, so editing does not touch the network. Two tiers:

- **draft** — every edit is written to this browser (debounced 300ms). Instant,
  offline-safe, and it survives a reload. The line under *Versions* reads
  `● Draft — not saved` and the Save button turns amber.
- otherwise that line reads how long ago this resume last reached the database
  — `Saved 12 hours ago` — refreshed every minute, with the exact timestamp on
  hover. Note it tracks the document, not the version trail: saves update the
  document every time but cut a version at most every 3 minutes, so the
  document is often newer than its latest snapshot.
- **save** — pressing **Save** (or ⌘S) pushes the draft to the database. That is
  the only thing that writes to Postgres, and the only thing that cuts versions.

Reopening a variant with an unsaved draft restores the draft, not the stored
copy, and says so in a banner with two buttons: **Save them**, or **Discard and
load the saved copy**. A **Discard** button also appears in the toolbar whenever
there is a draft, which throws the local copy away and reloads from the
database.

What counts as a change is the resume markup alone. `contenteditable`
attributes and the injected `+` buttons are editor chrome, stripped before
anything is stored, drafted or compared — otherwise merely loading the page, or
toggling *Editing*, would register as an edit. Switching variants, creating one from a copy,
naming a version, or restoring one all prompt to save first — declining keeps
the draft for later rather than discarding it. If the database is unreachable
when you press Save, the draft stays in the browser and the editor falls back to
local storage rather than losing the edit.

## Versions

**History** opens `#/p/<id>/history` — its own page, not a panel over the
resume. Versions run down the left with the full date, how long ago, and size;
the selected one renders in full on the right, with how much longer or shorter
it is than what you have open. So you read a version before it replaces
anything.

Versions are cut on save, not on every keystroke:

- a **save version** is cut at most once every 3 minutes (`AUTOSAVE_VERSION_EVERY`),
  and the last 50 per document are kept (`MAX_AUTOSAVES_PER_DOC`) — saving twice
  in quick succession updates the document without spamming the history
- a **named version** is cut whenever you ask for one, and is never pruned
- **Restore** snapshots the current state first, so rolling back is itself reversible

## Offline

Opening `resume.html` straight off disk (no server) still works — the editor
falls back to this browser's `localStorage` and shows a **browser only** badge.
Same UI, same variants and versions, just stored locally. If the server or the
database goes away mid-session the editor fails over to local storage and
carries the open document across, so the edit in flight is not lost.
**Download** is the only export: it opens the print dialog, where you choose
Save as PDF.

## API

23 endpoints, no authentication. Base URL is wherever it is running:

```
https://resume-jade-sigma-97.vercel.app     deployed
http://127.0.0.1:8000                       local
```

`GET /api/docs` gives the generated OpenAPI page, `GET /openapi.json` the spec.

### Profiles

```
GET    /api/profiles                      live profiles
POST   /api/profiles                      create {name, description}
PATCH  /api/profiles/{id}                 update {name, description}
DELETE /api/profiles/{id}                 soft delete — hides it; refuses the last live one
GET    /api/profiles/deleted              soft-deleted profiles
POST   /api/profiles/{id}/restore         undo a soft delete
```

### Documents

A document is one resume. Every profile holds one.

```
GET    /api/documents                     all documents (optionally ?profile_id=N)
POST   /api/documents                     create {profile_id, name, html | copy_of}
GET    /api/documents/{id}                the document, including its html
PUT    /api/documents/{id}                replace the html {html} — this is how you edit
PATCH  /api/documents/{id}                rename {name}
DELETE /api/documents/{id}                soft delete; refuses a profile's last one
POST   /api/documents/{id}/undelete       undo
```

Editing is read–modify–write: `GET` the document, change its `html`, `PUT` it back.
There is no section-level endpoint — the html is one blob.

### Versions

```
GET    /api/documents/{id}/versions       history (?deleted=1 for hidden ones)
POST   /api/documents/{id}/versions       name the current state {label}
POST   /api/documents/{id}/restore/{vid}  roll back — snapshots first, so reversible
GET    /api/versions/{vid}                one version's html
PATCH  /api/versions/{vid}                (re)label {label}
DELETE /api/versions/{vid}                soft delete
POST   /api/versions/{vid}/undelete       undo
```

### Other

```
GET    /api/health                        {ok, backend, target}
GET    /api/activity                      everything soft-deleted, newest first
```

### Notes for anyone calling this

- **No auth.** Every endpoint above is open to anyone with the URL, including
  the writes and deletes.
- `PUT /api/documents/{id}` overwrites the whole document. Two clients editing
  at once will clobber each other — there is no locking or conflict check.
- Saves cut a version at most once every 3 minutes, so rapid writes update the
  document without filling the history.
- Nothing is ever removed from the database. `DELETE` sets a `deleted_at` stamp
  and the matching restore/undelete endpoint reverses it.

## Printing

**Download** opens the print dialog. Leave **Margins** on *Default* — the
16mm margins live in the stylesheet's `@page` rule and Chrome honours them;
choosing *None* overrides them to zero and pushes the text to the paper edge.
Untick **Headers and footers**, or Chrome prints the date, page title, URL and
page number onto the PDF.

Layout metrics (A4, 16mm margins, Helvetica, 9pt body, the accent `#0f4c81`)
were measured off the source PDF.
