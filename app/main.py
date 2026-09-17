import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
SENTENCES_CSV = os.path.join(DATA, "sentences.csv")
TOKENS_CSV = os.path.join(DATA, "tokens.csv")

# Writable output location. Set GSL_DATA_DIR to a persistent volume in production
# (a /data mount is picked up automatically if it exists).
WRITE_DIR = os.environ.get("GSL_DATA_DIR") or ("/data" if os.path.isdir("/data") else DATA)
os.makedirs(WRITE_DIR, exist_ok=True)
DB_PATH = os.path.join(WRITE_DIR, "gsl.sqlite3")
# Legacy flat files, imported into the database once if present.
LEGACY_ANNOTATIONS_CSV = os.path.join(WRITE_DIR, "annotations.csv")
LEGACY_USERS_JSON = os.path.join(WRITE_DIR, "users.json")
LEGACY_ASSOCIATIONS_JSON = os.path.join(WRITE_DIR, "associations.json")

# Every Nth sentence (by position) is annotated by 3 different annotators.
MULTI_ANNOTATION_EVERY = int(os.environ.get("GSL_MULTI_EVERY", "4"))  # 1000/4 = 250 sentences
MULTI_ANNOTATION_COUNT = 3

DEFAULT_ASSOCIATIONS = [
    "Demonstration School for the Deaf Teacher",
    "Presbyterian College of Education",
]

ANNOTATION_COLUMNS = [
    "id", "english_sentence", "category", "sentence_type", "word_count", "length_band",
    "gsl_gloss", "annotator_id", "annotator_role", "timestamp", "confidence",
    "non_manual_markers", "flagged_missing_sign", "notes",
]

app = FastAPI(title="GSL Annotation Tool")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    association TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS users_name_ci ON users (lower(name));
CREATE TABLE IF NOT EXISTS associations (
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS annotations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    english_sentence TEXT NOT NULL,
    category TEXT NOT NULL,
    sentence_type TEXT NOT NULL,
    word_count TEXT NOT NULL,
    length_band TEXT NOT NULL,
    gsl_gloss TEXT NOT NULL,
    annotator_id TEXT NOT NULL,
    annotator_role TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    non_manual_markers TEXT NOT NULL DEFAULT '',
    flagged_missing_sign TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE (id, annotator_id)
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def import_legacy_files(conn):
    if os.path.exists(LEGACY_ASSOCIATIONS_JSON):
        with open(LEGACY_ASSOCIATIONS_JSON, encoding="utf-8") as f:
            conn.executemany("INSERT OR IGNORE INTO associations (name) VALUES (?)", [(a,) for a in json.load(f)])
    if os.path.exists(LEGACY_USERS_JSON):
        with open(LEGACY_USERS_JSON, encoding="utf-8") as f:
            conn.executemany(
                "INSERT OR IGNORE INTO users (id, name, association, created_at) VALUES (?, ?, ?, ?)",
                [(u["id"], u["name"], u["association"], u["created_at"]) for u in json.load(f)],
            )
    if os.path.exists(LEGACY_ANNOTATIONS_CSV):
        with open(LEGACY_ANNOTATIONS_CSV, newline="", encoding="utf-8") as f:
            rows = [{k: r.get(k, "") for k in ANNOTATION_COLUMNS} for r in csv.DictReader(f)]
        conn.executemany(
            f"INSERT OR IGNORE INTO annotations ({', '.join(ANNOTATION_COLUMNS)}) "
            f"VALUES ({', '.join(':' + c for c in ANNOTATION_COLUMNS)})",
            rows,
        )


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0 and \
                conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.execute("BEGIN IMMEDIATE")
            import_legacy_files(conn)
            conn.execute("COMMIT")
    finally:
        conn.close()


def load_sentences():
    with open(SENTENCES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows):
        r["required"] = MULTI_ANNOTATION_COUNT if (i % MULTI_ANNOTATION_EVERY == 0) else 1
    return rows


def load_tokens():
    with open(TOKENS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [
        {
            "token": r["Token"].strip().upper(),
            "category": r.get("Chapter_Category", ""),
            "type": r.get("Token_Type", ""),
            "description": r.get("Sign_Description", ""),
        }
        for r in rows
        if r.get("Token", "").strip()
    ]


SENTENCES = load_sentences()
SENTENCE_BY_ID = {s["id"]: s for s in SENTENCES}
TOKENS = load_tokens()


def load_associations(conn):
    extra = [r["name"] for r in conn.execute("SELECT name FROM associations ORDER BY name")]
    return DEFAULT_ASSOCIATIONS + [a for a in extra if a not in DEFAULT_ASSOCIATIONS]


def load_users(conn):
    return [dict(r) for r in conn.execute("SELECT id, name, association, created_at FROM users ORDER BY created_at")]


def load_annotations(conn):
    return [dict(r) for r in conn.execute("SELECT id, annotator_id FROM annotations")]


def progress_state(annotations):
    """Return per-sentence set of annotator ids, and overall counts."""
    by_sentence = {}
    for a in annotations:
        by_sentence.setdefault(a["id"], set()).add(a["annotator_id"])
    total_required = sum(s["required"] for s in SENTENCES)
    total_done = sum(min(len(by_sentence.get(s["id"], ())), s["required"]) for s in SENTENCES)
    return by_sentence, total_done, total_required


def next_sentence_for(annotator_id, annotations):
    by_sentence, _, _ = progress_state(annotations)
    # Prefer sentences with no annotations yet, then those still needing more annotators.
    candidates = []
    for s in SENTENCES:
        done = by_sentence.get(s["id"], set())
        if annotator_id in done or len(done) >= s["required"]:
            continue
        candidates.append((len(done), s))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def error(status, message):
    return JSONResponse({"error": message}, status_code=status)


class NewUser(BaseModel):
    name: str = ""
    association: str = ""


class Token(BaseModel):
    kind: str
    value: str = ""


class Submission(BaseModel):
    id: str
    annotator_id: str = ""
    tokens: list[Token] = []
    confidence: int | None = None
    non_manual_markers: str = ""
    notes: str = ""


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/meta")
def meta():
    with connect() as conn:
        associations = load_associations(conn)
    return {
        "associations": associations,
        "total_sentences": len(SENTENCES),
        "multi_count": sum(1 for s in SENTENCES if s["required"] > 1),
        "multi_required": MULTI_ANNOTATION_COUNT,
    }


@app.get("/api/tokens")
def tokens():
    return TOKENS


@app.get("/api/users")
def get_users():
    with connect() as conn:
        return load_users(conn)


@app.post("/api/users", status_code=201)
def create_user(body: NewUser):
    name = body.name.strip()
    association = body.association.strip()
    if not name:
        return error(400, "Name is required")
    if not association:
        return error(400, "Association is required")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if association not in DEFAULT_ASSOCIATIONS:
            conn.execute("INSERT OR IGNORE INTO associations (name) VALUES (?)", (association,))
        if conn.execute("SELECT 1 FROM users WHERE lower(name) = lower(?)", (name,)).fetchone():
            conn.execute("ROLLBACK")
            return error(409, "A user with that name already exists")
        slug = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "user"
        user_id = slug
        n = 2
        while conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
            user_id = f"{slug}_{n}"
            n += 1
        user = {
            "id": user_id,
            "name": name,
            "association": association,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        conn.execute(
            "INSERT INTO users (id, name, association, created_at) VALUES (:id, :name, :association, :created_at)",
            user,
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return JSONResponse(user, status_code=201)


@app.get("/api/next")
def next_sentence(annotator_id: str = Query("")):
    if not annotator_id:
        return error(400, "annotator_id required")
    with connect() as conn:
        annotations = load_annotations(conn)
    s = next_sentence_for(annotator_id, annotations)
    _, done, required = progress_state(annotations)
    mine = sum(1 for a in annotations if a["annotator_id"] == annotator_id)
    payload = {
        "progress": {"done": done, "required": required, "mine": mine},
        "all_complete": done >= required,
    }
    if s is None:
        payload["sentence"] = None
    else:
        payload["sentence"] = {k: s[k] for k in ("id", "english_sentence", "category", "sentence_type", "word_count", "length_band")}
        payload["sentence"]["required"] = s["required"]
    return payload


@app.post("/api/annotations", status_code=201)
def submit_annotation(body: Submission):
    s = SENTENCE_BY_ID.get(body.id)
    if s is None:
        return error(400, "Unknown sentence id")
    annotator_id = body.annotator_id.strip()
    if not annotator_id:
        return error(400, "annotator_id required")
    if not body.tokens:
        return error(400, "At least one token is required")
    if body.confidence not in (1, 2, 3):
        return error(400, "confidence must be 1-3")

    gloss_parts = []
    flagged = False
    for t in body.tokens:
        value = t.value.strip()
        if t.kind == "sign":
            gloss_parts.append(value.upper())
        elif t.kind == "fs":
            if not value:
                return error(400, "Fingerspelling text is required")
            gloss_parts.append(f'fs-"{value}"')
        elif t.kind == "sign_not_found":
            flagged = True
            gloss_parts.append(f"SIGN_NOT_FOUND({value.upper()})" if value else "SIGN_NOT_FOUND")
        else:
            return error(400, f"Unknown token kind: {t.kind}")

    conn = connect()
    try:
        user = conn.execute("SELECT association FROM users WHERE id = ?", (annotator_id,)).fetchone()
        if user is None:
            return error(400, "Unknown annotator")
        row = {
            "id": s["id"],
            "english_sentence": s["english_sentence"],
            "category": s["category"],
            "sentence_type": s["sentence_type"],
            "word_count": s["word_count"],
            "length_band": s["length_band"],
            "gsl_gloss": " ".join(gloss_parts),
            "annotator_id": annotator_id,
            "annotator_role": user["association"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "confidence": body.confidence,
            "non_manual_markers": body.non_manual_markers.strip(),
            "flagged_missing_sign": "yes" if flagged else "no",
            "notes": body.notes.strip(),
        }
        try:
            conn.execute(
                f"INSERT INTO annotations ({', '.join(ANNOTATION_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in ANNOTATION_COLUMNS)})",
                row,
            )
        except sqlite3.IntegrityError:
            return error(409, "You already annotated this sentence")
    finally:
        conn.close()
    return JSONResponse({"ok": True, "gsl_gloss": row["gsl_gloss"]}, status_code=201)


@app.get("/api/annotations.csv")
def download_annotations():
    with connect() as conn:
        rows = conn.execute(f"SELECT {', '.join(ANNOTATION_COLUMNS)} FROM annotations ORDER BY seq").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ANNOTATION_COLUMNS)
    w.writerows(tuple(r) for r in rows)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="annotations.csv"'},
    )


init_db()

app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
