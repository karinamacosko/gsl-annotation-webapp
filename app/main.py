import csv
import json
import os
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
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
ANNOTATIONS_CSV = os.path.join(WRITE_DIR, "annotations.csv")
USERS_JSON = os.path.join(WRITE_DIR, "users.json")
ASSOCIATIONS_JSON = os.path.join(WRITE_DIR, "associations.json")

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
lock = threading.Lock()


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


def load_associations():
    extra = []
    if os.path.exists(ASSOCIATIONS_JSON):
        with open(ASSOCIATIONS_JSON, encoding="utf-8") as f:
            extra = json.load(f)
    return DEFAULT_ASSOCIATIONS + [a for a in extra if a not in DEFAULT_ASSOCIATIONS]


def save_association(name):
    assocs = load_associations()
    if name not in assocs:
        extra = [a for a in assocs if a not in DEFAULT_ASSOCIATIONS] + [name]
        with open(ASSOCIATIONS_JSON, "w", encoding="utf-8") as f:
            json.dump(extra, f, indent=2)


def load_users():
    if not os.path.exists(USERS_JSON):
        return []
    with open(USERS_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_users(users):
    with open(USERS_JSON, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


def load_annotations():
    if not os.path.exists(ANNOTATIONS_CSV):
        return []
    with open(ANNOTATIONS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_annotation(row):
    new_file = not os.path.exists(ANNOTATIONS_CSV)
    with open(ANNOTATIONS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ANNOTATION_COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)


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
    return {
        "associations": load_associations(),
        "total_sentences": len(SENTENCES),
        "multi_count": sum(1 for s in SENTENCES if s["required"] > 1),
        "multi_required": MULTI_ANNOTATION_COUNT,
    }


@app.get("/api/tokens")
def tokens():
    return TOKENS


@app.get("/api/users")
def get_users():
    return load_users()


@app.post("/api/users", status_code=201)
def create_user(body: NewUser):
    name = body.name.strip()
    association = body.association.strip()
    if not name:
        return error(400, "Name is required")
    if not association:
        return error(400, "Association is required")
    with lock:
        save_association(association)
        users = load_users()
        if any(u["name"].lower() == name.lower() for u in users):
            return error(409, "A user with that name already exists")
        slug = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
        user_id = slug
        n = 2
        while any(u["id"] == user_id for u in users):
            user_id = f"{slug}_{n}"
            n += 1
        user = {
            "id": user_id,
            "name": name,
            "association": association,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        users.append(user)
        save_users(users)
    return JSONResponse(user, status_code=201)


@app.get("/api/next")
def next_sentence(annotator_id: str = Query("")):
    if not annotator_id:
        return error(400, "annotator_id required")
    with lock:
        annotations = load_annotations()
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

    with lock:
        users = {u["id"]: u for u in load_users()}
        user = users.get(annotator_id)
        if user is None:
            return error(400, "Unknown annotator")
        annotations = load_annotations()
        if any(a["id"] == body.id and a["annotator_id"] == annotator_id for a in annotations):
            return error(409, "You already annotated this sentence")
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
        append_annotation(row)
    return JSONResponse({"ok": True, "gsl_gloss": row["gsl_gloss"]}, status_code=201)


@app.get("/api/annotations.csv")
def download_annotations():
    if not os.path.exists(ANNOTATIONS_CSV):
        return PlainTextResponse(",".join(ANNOTATION_COLUMNS) + "\n", media_type="text/csv")
    return FileResponse(ANNOTATIONS_CSV, media_type="text/csv", filename="annotations.csv")


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
