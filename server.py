#!/usr/bin/env python3
"""
Resume store — Postgres (Neon/Supabase) or SQLite, with Google-Docs-style
version history.

    python3 server.py                  serve on http://127.0.0.1:8000
    python3 server.py --migrate        copy the local resume.db into DATABASE_URL

Which engine is used depends on DATABASE_URL in .env — see db.py.

A "document" is one named resume variant ("AI CV", "Full Stack", ...). Each one
keeps its current HTML plus a trail of versions: autosaves the server takes on
its own, and named versions you ask for explicitly. Named versions are never
pruned; autosaves are capped per document.
"""
from __future__ import annotations

import html as htmllib
import os
import re
import secrets
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from db import backend, db, init_db

ROOT = Path(__file__).parent
INDEX = ROOT / "resume.html"

# a new autosave version is cut at most this often; edits in between just
# overwrite the document's current HTML
AUTOSAVE_VERSION_EVERY = 180  # seconds
MAX_AUTOSAVES_PER_DOC = 50

def now() -> int:
    return int(time.time())


def unique_name(con, wanted: str, profile_id: int) -> str:
    """'AI CV' -> 'AI CV 2' if the name is taken within this profile."""
    wanted = (wanted or "Untitled").strip()[:120] or "Untitled"
    taken = {r["name"] for r in con.execute(
        "SELECT name FROM document WHERE profile_id = ? AND deleted_at IS NULL", (profile_id,))}
    if wanted not in taken:
        return wanted
    base = re.sub(r"\s+\d+$", "", wanted)
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def doc_row(con, doc_id: int):
    row = con.execute(
        "SELECT * FROM document WHERE id = ? AND deleted_at IS NULL", (doc_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "No such document")
    return row


def cut_version(con, doc_id: int, html: str, label: str | None) -> int:
    vid = con.execute(
        "INSERT INTO version (document_id, html, label, created_at) VALUES (?,?,?,?) RETURNING id",
        (doc_id, html, label, now()),
    ).fetchone()["id"]
    if label is None:
        # keep named versions forever, trim the autosave trail
        # Pruning hides the old autosaves rather than removing them; nothing
        # in this file issues a DELETE against document or version.
        con.execute(
            """UPDATE version SET deleted_at = ?
                 WHERE label IS NULL AND deleted_at IS NULL AND document_id = ?
                   AND id NOT IN (
                       SELECT id FROM version
                        WHERE label IS NULL AND deleted_at IS NULL AND document_id = ?
                        ORDER BY created_at DESC LIMIT ?)""",
            (now(), doc_id, doc_id, MAX_AUTOSAVES_PER_DOC),
        )
    return vid


# --------------------------------------------------------------------------- #

class ProfileIn(BaseModel):
    name: str = Field(max_length=120)
    description: str = ""


class DocIn(BaseModel):
    profile_id: int
    name: str = Field(default="Untitled", max_length=120)
    html: str = ""
    copy_of: int | None = None      # clone another document's current HTML


class ShareIn(BaseModel):
    rotate: bool = False


class CommentIn(BaseModel):
    name: str = Field(max_length=80)
    body: str = Field(max_length=2000)


class HtmlIn(BaseModel):
    html: str


class NameIn(BaseModel):
    name: str = Field(max_length=120)


class LabelIn(BaseModel):
    label: str | None = Field(default=None, max_length=120)


def prepare_storage() -> None:
    """Create the schema and bring it up to date. Idempotent, and called from
    both entry points — `python3 server.py` and a host running
    `uvicorn server:app`, where __main__ never runs."""
    init_db()
    if not os.environ.get("SKIP_PROFILE_MIGRATION"):
        migrate_to_profiles()
    migrate_soft_delete()
    migrate_sharing()


app = FastAPI(title="Resume store", docs_url="/api/docs")


@app.on_event("startup")
def _startup() -> None:
    # On a serverless host this runs on every cold start, and each check costs a
    # round trip to a database that may itself be waking. The schema only ever
    # needs it once — set SKIP_MIGRATIONS=1 after the first successful deploy.
    if os.environ.get("SKIP_MIGRATIONS"):
        return
    prepare_storage()


# Contact details are the one part of the resume worth keeping out of a public
# repo — a phone number in git history is permanent and scrapeable. They live in
# .env and are substituted on the way out, so the tracked file carries only a
# placeholder. Opened straight off disk with no server, the placeholder shows
# through; that copy is a fallback seed, and the real resume lives in the
# database anyway.
CONTACT_FIELDS = ("RESUME_LOCATION", "RESUME_PHONE", "RESUME_EMAIL", "RESUME_LINKEDIN")


def contact_line() -> str:
    parts = [os.environ.get(k, "").strip() for k in CONTACT_FIELDS]
    parts = [p for p in parts if p]
    return " · ".join(parts) if parts else "Set RESUME_* in .env to fill this in"


@app.get("/")
def index():
    html = INDEX.read_text(encoding="utf-8").replace("{{CONTACT}}", contact_line())
    return HTMLResponse(html)


@app.get("/api/health")
def health():
    return {"ok": True, **backend()}


@app.get("/api/profiles")
def list_profiles():
    with db() as con:
        rows = con.execute(
            "SELECT id, name, description, created_at, share_token, views "
            "FROM profile WHERE deleted_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/profiles/deleted")
def list_deleted_profiles():
    """Soft-deleted profiles, newest first. Nothing is ever removed, so this is
    always the full undo history."""
    with db() as con:
        rows = con.execute(
            "SELECT id, name, description, created_at, deleted_at FROM profile "
            "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/profiles", status_code=201)
def create_profile(body: ProfileIn):
    with db() as con:
        ts = now()
        try:
            profile_id = con.execute(
                "INSERT INTO profile (name, description, created_at) VALUES (?,?,?) RETURNING id",
                (body.name.strip()[:120], body.description.strip()[:500], ts),
            ).fetchone()["id"]
            return {"id": profile_id, "name": body.name, "description": body.description, "created_at": ts}
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(400, "Profile name already exists")
            raise


@app.patch("/api/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileIn):
    with db() as con:
        if con.execute("SELECT 1 FROM profile WHERE id = ?", (profile_id,)).fetchone() is None:
            raise HTTPException(404, "No such profile")
        try:
            con.execute(
                "UPDATE profile SET name = ?, description = ? WHERE id = ?",
                (body.name.strip()[:120], body.description.strip()[:500], profile_id),
            )
            return {"id": profile_id, "name": body.name, "description": body.description}
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(400, "Profile name already exists")
            raise


@app.delete("/api/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: int):
    """Soft delete. The row stays, its documents and their version history stay,
    and the profile simply stops being listed. Nothing here ever issues a DELETE
    against profile, document or version — recovering a profile is an UPDATE."""
    with db() as con:
        row = con.execute(
            "SELECT deleted_at FROM profile WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No such profile")
        if row["deleted_at"] is not None:
            return                      # already deleted; deleting twice is a no-op
        live = con.execute(
            "SELECT COUNT(*) c FROM profile WHERE deleted_at IS NULL"
        ).fetchone()["c"]
        if live <= 1:
            raise HTTPException(400, "That is the only profile left")
        con.execute(
            "UPDATE profile SET deleted_at = ? WHERE id = ?", (now(), profile_id)
        )


@app.post("/api/profiles/{profile_id}/share")
def share_profile(profile_id: int, body: ShareIn | None = None):
    """Mint the profile's share token, or rotate it. Rotating invalidates any
    link already sent out, which is the only way to take one back."""
    with db() as con:
        row = con.execute(
            "SELECT share_token FROM profile WHERE id = ? AND deleted_at IS NULL",
            (profile_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No such profile")
        token = row["share_token"]
        if token is None or (body and body.rotate):
            token = secrets.token_urlsafe(9)
            con.execute("UPDATE profile SET share_token = ? WHERE id = ?",
                        (token, profile_id))
        return {"id": profile_id, "token": token, "path": f"/r/{token}"}


@app.delete("/api/profiles/{profile_id}/share", status_code=204)
def unshare_profile(profile_id: int):
    """Revoke. The link stops resolving immediately."""
    with db() as con:
        con.execute("UPDATE profile SET share_token = NULL WHERE id = ?", (profile_id,))


# --------------------------------------------------------------- comments ---
def _comments(con, profile_id: int):
    rows = con.execute(
        """SELECT id, name, body, created_at FROM comment
            WHERE profile_id = ? AND deleted_at IS NULL
            ORDER BY created_at DESC""",
        (profile_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/profiles/{profile_id}/comments")
def list_comments(profile_id: int):
    with db() as con:
        return _comments(con, profile_id)


@app.delete("/api/comments/{comment_id}", status_code=204)
def delete_comment(comment_id: int):
    """Soft, like everything else here."""
    with db() as con:
        con.execute("UPDATE comment SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                    (now(), comment_id))


# ----------------------------------------------------------------- public ---
def _by_token(con, token: str):
    row = con.execute(
        "SELECT * FROM profile WHERE share_token = ? AND deleted_at IS NULL", (token,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "No such page")
    return row


@app.get("/api/public/{token}")
def public_json(token: str):
    with db() as con:
        p = _by_token(con, token)
        doc = con.execute(
            """SELECT id, name, html FROM document
                WHERE profile_id = ? AND deleted_at IS NULL
                ORDER BY updated_at DESC LIMIT 1""",
            (p["id"],),
        ).fetchone()
        return {
            "name": p["name"],
            "description": p["description"],
            "views": p["views"],
            "html": doc["html"] if doc else "",
            "comments": _comments(con, p["id"]),
        }


@app.post("/api/public/{token}/comments", status_code=201)
def post_comment(token: str, body: CommentIn):
    name = body.name.strip()[:80] or "Anonymous"
    text = body.body.strip()[:2000]
    if not text:
        raise HTTPException(400, "Comment is empty")
    with db() as con:
        p = _by_token(con, token)
        cid = con.execute(
            "INSERT INTO comment (profile_id, name, body, created_at) "
            "VALUES (?,?,?,?) RETURNING id",
            (p["id"], name, text, now()),
        ).fetchone()["id"]
        return {"id": cid, "name": name, "body": text}


PUBLIC_CSS = """
  body{background:#f1f3f5;display:block}
  .pub-bar{
    position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;
    padding:12px 20px;background:#fff;border-bottom:1px solid #e5e7eb;
    font:500 14px/1 'Segoe UI',Helvetica,Arial,sans-serif;color:#1a1a1a;
  }
  .pub-bar .nm{font-weight:700;color:#4f46e5}
  .pub-bar .views{color:#6b7280;font-size:13px}
  .pub-bar .sp{flex:1}
  .pub-bar button{
    font:600 13px/1 inherit;color:#fff;background:#4f46e5;border:none;
    border-radius:6px;padding:9px 16px;cursor:pointer;
  }
  .pub-bar button:hover{background:#4338ca}
  #doc{padding:24px 12px 40px;overflow-x:auto}
  .wrap{max-width:210mm;margin:0 auto 60px;padding:0 12px;
        font-family:'Segoe UI',Helvetica,Arial,sans-serif}
  .wrap h3{font-size:16px;margin:0 0 14px;color:#1a1a1a}
  .cmt{background:#fff;border:1px solid #e5e7eb;border-radius:10px;
       padding:14px 16px;margin-bottom:10px}
  .cmt .who{font-weight:600;font-size:13px;color:#1a1a1a}
  .cmt .when{font-size:11px;color:#9ca3af;margin-left:8px;font-weight:400}
  .cmt .txt{font-size:14px;line-height:1.5;color:#374151;margin-top:6px;
            white-space:pre-wrap;word-wrap:break-word}
  .cmt-form{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px}
  .cmt-form input,.cmt-form textarea{
    width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:6px;
    font-family:inherit;font-size:14px;margin-bottom:10px;box-sizing:border-box;
  }
  .cmt-form textarea{height:84px;resize:vertical}
  .cmt-form button{
    font:600 13px/1 inherit;color:#fff;background:#4f46e5;border:none;
    border-radius:6px;padding:10px 18px;cursor:pointer;
  }
  .empty{color:#9ca3af;font-size:14px;padding:10px 0}
  @media print{
    .pub-bar,.wrap{display:none !important}
    body{background:#fff}
    #doc{padding:0}
    .page{width:auto;min-height:0;margin:0;padding:0;box-shadow:none}
  }
"""


def _resume_css() -> str:
    """The editor's own stylesheet, so a shared page renders exactly as the PDF
    does. Its read-only rules (body without .editing) already hide the ctl
    buttons, adders and page guides, so nothing needs stripping."""
    page = INDEX.read_text(encoding="utf-8")
    return page[page.index("<style>") + 7 : page.index("</style>")]


@app.get("/r/{token}")
def public_page(token: str, mode: str = "all"):
    """Read-only resume for sharing. `?mode=resume` drops the bar and comments,
    leaving only the sheet."""
    with db() as con:
        p = _by_token(con, token)
        doc = con.execute(
            """SELECT html FROM document
                WHERE profile_id = ? AND deleted_at IS NULL
                ORDER BY updated_at DESC LIMIT 1""",
            (p["id"],),
        ).fetchone()
        con.execute("UPDATE profile SET views = views + 1 WHERE id = ?", (p["id"],))
        views = (p["views"] or 0) + 1
        comments = _comments(con, p["id"])

    esc = htmllib.escape
    body = doc["html"] if doc else "<p>This resume is empty.</p>"
    bare = mode == "resume"

    bar = "" if bare else (
        '<div class="pub-bar">'
        f'<span class="nm">{esc(p["name"])}</span>'
        f'<span class="views">{views} view{"" if views == 1 else "s"}</span>'
        '<span class="sp"></span>'
        '<button onclick="window.print()">Download PDF</button>'
        "</div>"
    )

    if comments:
        items = "".join(
            '<div class="cmt"><div><span class="who">' + esc(c["name"]) + "</span>"
            '<span class="when">' + time.strftime("%d %b %Y", time.localtime(c["created_at"]))
            + "</span></div>"
            '<div class="txt">' + esc(c["body"]) + "</div></div>"
            for c in comments
        )
    else:
        items = '<p class="empty">No comments yet.</p>'

    talk = "" if bare else (
        '<div class="wrap">'
        f"<h3>Comments ({len(comments)})</h3>"
        f"{items}"
        '<form class="cmt-form" onsubmit="return send(event)">'
        '<input id="nm" placeholder="Your name" maxlength="80" required>'
        '<textarea id="bd" placeholder="Leave a comment" maxlength="2000" required></textarea>'
        '<button type="submit">Post comment</button>'
        "</form></div>"
        "<script>"
        "async function send(e){e.preventDefault();"
        "const n=document.getElementById('nm').value.trim();"
        "const b=document.getElementById('bd').value.trim();"
        "if(!n||!b)return false;"
        f"const r=await fetch('/api/public/{token}/comments',"
        "{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({name:n,body:b})});"
        "if(r.ok)location.reload(); else alert('Could not post that comment.');"
        "return false;}"
        "</script>"
    )

    return HTMLResponse(
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(p['name'])} — Saurabh Saha</title>"
        '<meta name="robots" content="noindex, nofollow">'
        f"<style>{_resume_css()}</style><style>{PUBLIC_CSS}</style></head><body>"
        f'{bar}<div id="doc"><div class="page">{body}</div></div>{talk}'
        "</body></html>"
    )

@app.get("/api/activity")
def activity():
    """Everything currently soft-deleted, newest first — the undo history.
    Nothing is ever removed, so this is complete."""
    out = []
    with db() as con:
        for r in con.execute(
            """SELECT id, name, deleted_at,
                      (SELECT COUNT(*) FROM document d WHERE d.profile_id = p.id) AS docs
                 FROM profile p WHERE deleted_at IS NOT NULL"""
        ).fetchall():
            out.append({"kind": "profile", "id": r["id"], "name": r["name"],
                        "deleted_at": r["deleted_at"],
                        "detail": f'{r["docs"]} resume' + ("" if r["docs"] == 1 else "s")})
        for r in con.execute(
            """SELECT d.id, d.name, d.deleted_at, p.name AS profile
                 FROM document d JOIN profile p ON p.id = d.profile_id
                WHERE d.deleted_at IS NOT NULL"""
        ).fetchall():
            out.append({"kind": "document", "id": r["id"], "name": r["name"],
                        "deleted_at": r["deleted_at"], "detail": f'in {r["profile"]}'})
        for r in con.execute(
            """SELECT v.id, v.label, v.deleted_at, d.name AS doc
                 FROM version v JOIN document d ON d.id = v.document_id
                WHERE v.deleted_at IS NOT NULL"""
        ).fetchall():
            out.append({"kind": "version", "id": r["id"],
                        "name": r["label"] or "Autosave",
                        "deleted_at": r["deleted_at"], "detail": f'of {r["doc"]}'})
    out.sort(key=lambda x: x["deleted_at"], reverse=True)
    return out


@app.post("/api/profiles/{profile_id}/restore")
def restore_profile(profile_id: int):
    """Undo a soft delete. Fails if a live profile has taken the name since."""
    with db() as con:
        row = con.execute(
            "SELECT name, deleted_at FROM profile WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No such profile")
        if row["deleted_at"] is None:
            return {"id": profile_id, "restored": False}
        clash = con.execute(
            "SELECT 1 FROM profile WHERE name = ? AND deleted_at IS NULL", (row["name"],)
        ).fetchone()
        if clash:
            raise HTTPException(400, f'A profile called "{row["name"]}" already exists')
        con.execute("UPDATE profile SET deleted_at = NULL WHERE id = ?", (profile_id,))
        return {"id": profile_id, "restored": True}


@app.get("/api/documents")
def list_documents(profile_id: int | None = None):
    with db() as con:
        if profile_id is None:
            rows = con.execute(
                """SELECT d.id, d.profile_id, d.name, d.created_at, d.updated_at,
                          (SELECT COUNT(*) FROM version v
                            WHERE v.document_id = d.id AND v.deleted_at IS NULL) AS versions
                     FROM document d JOIN profile p ON p.id = d.profile_id
                    WHERE p.deleted_at IS NULL AND d.deleted_at IS NULL
                    ORDER BY d.updated_at DESC"""
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT d.id, d.profile_id, d.name, d.created_at, d.updated_at,
                          (SELECT COUNT(*) FROM version v
                            WHERE v.document_id = d.id AND v.deleted_at IS NULL) AS versions
                     FROM document d
                    WHERE d.profile_id = ? AND d.deleted_at IS NULL
                    ORDER BY d.updated_at DESC""",
                (profile_id,),
            ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/documents", status_code=201)
def create_document(body: DocIn):
    with db() as con:
        if con.execute("SELECT 1 FROM profile WHERE id = ?", (body.profile_id,)).fetchone() is None:
            raise HTTPException(404, "No such profile")
        html = body.html
        if body.copy_of is not None:
            html = doc_row(con, body.copy_of)["html"]
        if not html.strip():
            raise HTTPException(400, "Refusing to create an empty document")
        ts = now()
        name = unique_name(con, body.name, body.profile_id)
        doc_id = con.execute(
            "INSERT INTO document (profile_id, name, html, created_at, updated_at) VALUES (?,?,?,?,?) RETURNING id",
            (body.profile_id, name, html, ts, ts),
        ).fetchone()["id"]
        cut_version(con, doc_id, html, "Created")
        return {"id": doc_id, "profile_id": body.profile_id, "name": name, "created_at": ts, "updated_at": ts, "versions": 1}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int):
    with db() as con:
        return dict(doc_row(con, doc_id))


@app.put("/api/documents/{doc_id}")
def save_document(doc_id: int, body: HtmlIn):
    """Autosave. Overwrites current HTML; cuts a version on a throttle."""
    with db() as con:
        row = doc_row(con, doc_id)
        if body.html == row["html"]:
            return {"saved": False, "versioned": False, "updated_at": row["updated_at"]}
        ts = now()
        con.execute("UPDATE document SET html = ?, updated_at = ? WHERE id = ?",
                    (body.html, ts, doc_id))
        last = con.execute(
            "SELECT created_at FROM version WHERE document_id = ? ORDER BY created_at DESC LIMIT 1",
            (doc_id,),
        ).fetchone()
        versioned = last is None or ts - last["created_at"] >= AUTOSAVE_VERSION_EVERY
        if versioned:
            cut_version(con, doc_id, body.html, None)
        return {"saved": True, "versioned": versioned, "updated_at": ts}


@app.patch("/api/documents/{doc_id}")
def rename_document(doc_id: int, body: NameIn):
    with db() as con:
        row = doc_row(con, doc_id)
        name = unique_name(con, body.name, row["profile_id"])
        con.execute("UPDATE document SET name = ?, updated_at = ? WHERE id = ?",
                    (name, now(), doc_id))
        return {"id": doc_id, "name": name}


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int):
    """Soft. The row and its versions stay; undo with /undelete."""
    with db() as con:
        row = doc_row(con, doc_id)
        live = con.execute(
            "SELECT COUNT(*) c FROM document WHERE profile_id = ? AND deleted_at IS NULL",
            (row["profile_id"],),
        ).fetchone()["c"]
        if live <= 1:
            raise HTTPException(400, "That is the only document left in this profile")
        con.execute("UPDATE document SET deleted_at = ? WHERE id = ?", (now(), doc_id))


@app.post("/api/documents/{doc_id}/undelete")
def undelete_document(doc_id: int):
    """Undo a soft delete. Named undelete, not restore: on a document, restore
    already means rolling its content back to a version."""
    with db() as con:
        row = con.execute(
            "SELECT profile_id, name, deleted_at FROM document WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No such document")
        if row["deleted_at"] is None:
            return {"id": doc_id, "undeleted": False}
        clash = con.execute(
            "SELECT 1 FROM document WHERE profile_id = ? AND name = ? AND deleted_at IS NULL",
            (row["profile_id"], row["name"]),
        ).fetchone()
        if clash:
            raise HTTPException(400, f'A document called "{row["name"]}" already exists here')
        con.execute("UPDATE document SET deleted_at = NULL WHERE id = ?", (doc_id,))
        return {"id": doc_id, "undeleted": True}


@app.get("/api/documents/{doc_id}/versions")
def list_versions(doc_id: int, deleted: bool = False):
    """Live versions by default; `?deleted=1` for the ones hidden by a delete or
    by autosave pruning, which are still all here."""
    with db() as con:
        doc_row(con, doc_id)
        clause = "IS NOT NULL" if deleted else "IS NULL"
        rows = con.execute(
            f"""SELECT id, label, created_at, deleted_at, LENGTH(html) AS bytes
                  FROM version WHERE document_id = ? AND deleted_at {clause}
                 ORDER BY created_at DESC, id DESC""",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/documents/{doc_id}/versions", status_code=201)
def create_version(doc_id: int, body: LabelIn):
    """Explicitly name the current state — 'Sent to Stripe', 'Pre-rewrite'."""
    with db() as con:
        row = doc_row(con, doc_id)
        vid = cut_version(con, doc_id, row["html"], (body.label or "Named version").strip())
        return {"id": vid}


@app.get("/api/versions/{version_id}")
def get_version(version_id: int):
    with db() as con:
        row = con.execute("SELECT * FROM version WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such version")
        return dict(row)


@app.patch("/api/versions/{version_id}")
def label_version(version_id: int, body: LabelIn):
    with db() as con:
        if con.execute("SELECT 1 FROM version WHERE id = ?", (version_id,)).fetchone() is None:
            raise HTTPException(404, "No such version")
        con.execute("UPDATE version SET label = ? WHERE id = ?",
                    ((body.label or "").strip() or None, version_id))
        return {"id": version_id, "label": body.label}


@app.delete("/api/versions/{version_id}", status_code=204)
def delete_version(version_id: int):
    """Soft. The snapshot stays and can be brought back with /undelete."""
    with db() as con:
        row = con.execute(
            "SELECT deleted_at FROM version WHERE id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No such version")
        if row["deleted_at"] is not None:
            return
        con.execute("UPDATE version SET deleted_at = ? WHERE id = ?", (now(), version_id))


@app.post("/api/versions/{version_id}/undelete")
def undelete_version(version_id: int):
    with db() as con:
        if con.execute("SELECT 1 FROM version WHERE id = ?", (version_id,)).fetchone() is None:
            raise HTTPException(404, "No such version")
        con.execute("UPDATE version SET deleted_at = NULL WHERE id = ?", (version_id,))
        return {"id": version_id, "undeleted": True}


@app.post("/api/documents/{doc_id}/restore/{version_id}")
def restore_version(doc_id: int, version_id: int):
    """Snapshot what is current, then roll the document back to `version_id`."""
    with db() as con:
        row = doc_row(con, doc_id)
        ver = con.execute(
            "SELECT * FROM version WHERE id = ? AND document_id = ? AND deleted_at IS NULL",
            (version_id, doc_id),
        ).fetchone()
        if ver is None:
            raise HTTPException(404, "No such version for this document")
        stamp = time.strftime("%d %b %H:%M", time.localtime())
        cut_version(con, doc_id, row["html"], f"Before restore · {stamp}")
        ts = now()
        con.execute("UPDATE document SET html = ?, updated_at = ? WHERE id = ?",
                    (ver["html"], ts, doc_id))
        return {"id": doc_id, "html": ver["html"], "updated_at": ts}


def migrate_from_sqlite() -> None:
    """Copy documents and their full version history out of the local
    resume.db and into whatever DATABASE_URL points at. Ids are preserved,
    names that already exist on the target are suffixed rather than clobbered."""
    import sqlite3

    import db as dbmod

    src_path = ROOT / "resume.db"
    if not dbmod.IS_PG:
        sys.exit("DATABASE_URL is not a Postgres URL — nothing to migrate into.")
    if not src_path.exists():
        sys.exit(f"No {src_path.name} to migrate from.")

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    docs = src.execute("SELECT * FROM document ORDER BY id").fetchall()
    vers = src.execute("SELECT * FROM version ORDER BY id").fetchall()

    init_db()
    moved_docs = moved_vers = 0
    with db() as con:
        # Create a default profile for migrated documents
        default_profile = con.execute(
            "INSERT INTO profile (name, description, created_at) VALUES (?,?,?) RETURNING id",
            ("Primary", "Main resume profile", now()),
        ).fetchone()["id"]

        existing = {r["name"] for r in con.execute("SELECT name FROM document WHERE profile_id = ?", (default_profile,)).fetchall()}
        taken_ids = {r["id"] for r in con.execute("SELECT id FROM document").fetchall()}
        remap: dict[int, int] = {}
        for d in docs:
            name = d["name"]
            if name in existing:
                n = 2
                while f"{name} {n}" in existing:
                    n += 1
                name = f"{name} {n}"
            existing.add(name)
            if d["id"] in taken_ids:
                new_id = con.execute(
                    "INSERT INTO document (profile_id, name, html, created_at, updated_at) "
                    "VALUES (?,?,?,?,?) RETURNING id",
                    (default_profile, name, d["html"], d["created_at"], d["updated_at"]),
                ).fetchone()["id"]
            else:
                new_id = con.execute(
                    "INSERT INTO document (id, profile_id, name, html, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?) RETURNING id",
                    (d["id"], default_profile, name, d["html"], d["created_at"], d["updated_at"]),
                ).fetchone()["id"]
            remap[d["id"]] = new_id
            moved_docs += 1
        for v in vers:
            if v["document_id"] not in remap:
                continue
            con.execute(
                "INSERT INTO version (document_id, html, label, created_at) VALUES (?,?,?,?)",
                (remap[v["document_id"]], v["html"], v["label"], v["created_at"]),
            )
            moved_vers += 1
    dbmod.fix_pg_sequences()
    src.close()
    print(f"Migrated {moved_docs} document(s) and {moved_vers} version(s) "
          f"into {backend()['target']}")


def migrate_soft_delete() -> None:
    """Add profile.deleted_at, and move name uniqueness onto live rows only.

    Idempotent: safe to run on every start. The old schema declared
    `name TEXT NOT NULL UNIQUE`, which would keep a deleted profile's name
    reserved forever, so that constraint is replaced by a partial unique index.
    """
    import db as dbmod

    with db() as con:
        wide = "BIGINT" if dbmod.IS_PG else "INTEGER"
        for table in ("profile", "document", "version"):
            if dbmod.IS_PG:
                has_col = con.execute(
                    """SELECT 1 FROM information_schema.columns
                        WHERE table_name = ? AND column_name = 'deleted_at'""",
                    (table,),
                ).fetchone()
            else:
                has_col = "deleted_at" in {
                    r["name"] for r in con.execute(f"PRAGMA table_info({table})")
                }
            if not has_col:
                con.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at {wide}")
                print(f"  · added {table}.deleted_at")

        if dbmod.IS_PG:
            # Old schema put unconditional UNIQUE on profile.name and on
            # (document.profile_id, name); both would keep a deleted row's name
            # reserved forever. Replace with partial indexes over live rows.
            for table in ("profile", "document"):
                for row in con.execute(
                    """SELECT c.conname FROM pg_constraint c
                         JOIN pg_class t ON t.oid = c.conrelid
                        WHERE t.relname = ? AND c.contype = 'u'""",
                    (table,),
                ).fetchall():
                    con.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{row["conname"]}"')
                    print(f"  · dropped unconditional unique constraint {row['conname']}")

        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS profile_live_name "
            "ON profile(name) WHERE deleted_at IS NULL"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS document_live_name "
            "ON document(profile_id, name) WHERE deleted_at IS NULL"
        )


def migrate_sharing() -> None:
    """Add profile.share_token / profile.views and the comment table.

    Idempotent, same as migrate_soft_delete: safe on every start.
    """
    import db as dbmod

    with db() as con:
        wide = "BIGINT" if dbmod.IS_PG else "INTEGER"
        if dbmod.IS_PG:
            have = {r["column_name"] for r in con.execute(
                """SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'profile'""").fetchall()}
        else:
            have = {r["name"] for r in con.execute("PRAGMA table_info(profile)")}
        if "share_token" not in have:
            con.execute("ALTER TABLE profile ADD COLUMN share_token TEXT")
            print("  · added profile.share_token")
        if "views" not in have:
            con.execute(f"ALTER TABLE profile ADD COLUMN views {wide} NOT NULL DEFAULT 0")
            print("  · added profile.views")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS profile_share_token "
            "ON profile(share_token) WHERE share_token IS NOT NULL"
        )
        pk = ("BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if dbmod.IS_PG
              else "INTEGER PRIMARY KEY AUTOINCREMENT")
        con.execute(f"""CREATE TABLE IF NOT EXISTS comment (
            id          {pk},
            profile_id  {wide} NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
            name        TEXT NOT NULL,
            body        TEXT NOT NULL,
            created_at  {wide} NOT NULL,
            deleted_at  {wide}
        )""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS comment_profile_idx "
            "ON comment(profile_id, created_at DESC)"
        )


def migrate_to_profiles() -> None:
    """Migrate existing database to profile-based schema."""
    import db as dbmod

    if not dbmod.IS_PG:
        print("Profile migration is for Postgres only (SQLite handles it automatically on init)")
        return

    with db() as con:
        # Check if profile table already exists
        result = con.execute(
            """SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'profile'"""
        ).fetchone()

        if result:
            print("Profile table already exists. Checking if document has profile_id...")
            # Check if document already has profile_id
            col_result = con.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_name = 'document' AND column_name = 'profile_id'"""
            ).fetchone()
            if col_result:
                print("Migration already applied.")
                return

        print("Creating profile table...")
        con.execute("""
            CREATE TABLE IF NOT EXISTS profile (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at BIGINT NOT NULL
            )
        """)

        print("Adding profile_id to document table...")
        con.execute("""
            ALTER TABLE document
            ADD COLUMN profile_id BIGINT,
            ADD CONSTRAINT fk_document_profile FOREIGN KEY (profile_id) REFERENCES profile(id) ON DELETE CASCADE
        """)

        # Create a default profile
        default_profile_id = con.execute(
            "INSERT INTO profile (name, description, created_at) VALUES (?,?,?) RETURNING id",
            ("Primary", "Main resume profile", now()),
        ).fetchone()["id"]

        print(f"Assigning all documents to default profile (id={default_profile_id})...")
        con.execute(
            "UPDATE document SET profile_id = ? WHERE profile_id IS NULL",
            (default_profile_id,)
        )

        # Now make profile_id NOT NULL and add unique constraint
        con.execute("""
            ALTER TABLE document
            ALTER COLUMN profile_id SET NOT NULL,
            DROP CONSTRAINT IF EXISTS document_name_key,
            ADD CONSTRAINT document_profile_name_key UNIQUE (profile_id, name)
        """)

        # Create index
        con.execute("""
            CREATE INDEX IF NOT EXISTS document_profile_idx
            ON document(profile_id, updated_at DESC)
        """)

        print("Migration completed successfully.")


if __name__ == "__main__":
    if "--migrate" in sys.argv:
        migrate_from_sqlite()
        sys.exit(0)
    if "--migrate-profiles" in sys.argv:
        migrate_to_profiles()
        sys.exit(0)
    # A host supplies PORT and needs 0.0.0.0; locally both default to the old
    # behaviour, so `python3 server.py` is unchanged.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    info = backend()
    print(f"Resume store  ·  {info['backend']} → {info['target']}  ·  http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
