import csv
import json
import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SENTENCES_CSV = os.path.join(DATA, "sentences.csv")
TOKENS_CSV = os.path.join(DATA, "tokens.csv")
ANNOTATIONS_CSV = os.path.join(DATA, "annotations.csv")
USERS_JSON = os.path.join(DATA, "users.json")

# Every Nth sentence (by position) is annotated by 3 different annotators.
MULTI_ANNOTATION_EVERY = int(os.environ.get("GSL_MULTI_EVERY", "4"))  # 1000/4 = 250 sentences
MULTI_ANNOTATION_COUNT = 3

ASSOCIATIONS = [
    "Demonstration School for the Deaf Teacher",
    "Presbyterian College of Education",
]

ANNOTATION_COLUMNS = [
    "id", "english_sentence", "category", "sentence_type", "word_count", "length_band",
    "gsl_gloss", "annotator_id", "annotator_role", "timestamp", "confidence",
    "non_manual_markers", "flagged_missing_sign", "notes",
]

app = Flask(__name__, static_folder="static", static_url_path="/static")
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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/meta")
def meta():
    return jsonify({
        "associations": ASSOCIATIONS,
        "total_sentences": len(SENTENCES),
        "multi_count": sum(1 for s in SENTENCES if s["required"] > 1),
        "multi_required": MULTI_ANNOTATION_COUNT,
    })


@app.route("/api/tokens")
def tokens():
    return jsonify(TOKENS)


@app.route("/api/users", methods=["GET"])
def get_users():
    return jsonify(load_users())


@app.route("/api/users", methods=["POST"])
def create_user():
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    association = (body.get("association") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if association not in ASSOCIATIONS:
        return jsonify({"error": "Invalid association"}), 400
    with lock:
        users = load_users()
        if any(u["name"].lower() == name.lower() for u in users):
            return jsonify({"error": "A user with that name already exists"}), 409
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
    return jsonify(user), 201


@app.route("/api/next")
def next_sentence():
    annotator_id = request.args.get("annotator_id", "")
    if not annotator_id:
        return jsonify({"error": "annotator_id required"}), 400
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
    return jsonify(payload)


@app.route("/api/annotations", methods=["POST"])
def submit_annotation():
    body = request.get_json(force=True) or {}
    sentence_id = body.get("id")
    s = SENTENCE_BY_ID.get(sentence_id)
    if s is None:
        return jsonify({"error": "Unknown sentence id"}), 400
    annotator_id = (body.get("annotator_id") or "").strip()
    if not annotator_id:
        return jsonify({"error": "annotator_id required"}), 400
    tokens = body.get("tokens") or []
    if not isinstance(tokens, list) or not tokens:
        return jsonify({"error": "At least one token is required"}), 400
    try:
        confidence = int(body.get("confidence"))
    except (TypeError, ValueError):
        return jsonify({"error": "confidence must be 1-3"}), 400
    if confidence not in (1, 2, 3):
        return jsonify({"error": "confidence must be 1-3"}), 400

    gloss_parts = []
    flagged = False
    for t in tokens:
        kind = t.get("kind")
        value = (t.get("value") or "").strip()
        if kind == "sign":
            gloss_parts.append(value.upper())
        elif kind == "fs":
            if not value:
                return jsonify({"error": "Fingerspelling text is required"}), 400
            gloss_parts.append(f'fs-"{value}"')
        elif kind == "sign_not_found":
            flagged = True
            gloss_parts.append(f"SIGN_NOT_FOUND({value.upper()})" if value else "SIGN_NOT_FOUND")
        else:
            return jsonify({"error": f"Unknown token kind: {kind}"}), 400

    with lock:
        users = {u["id"]: u for u in load_users()}
        user = users.get(annotator_id)
        if user is None:
            return jsonify({"error": "Unknown annotator"}), 400
        annotations = load_annotations()
        if any(a["id"] == sentence_id and a["annotator_id"] == annotator_id for a in annotations):
            return jsonify({"error": "You already annotated this sentence"}), 409
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
            "confidence": confidence,
            "non_manual_markers": (body.get("non_manual_markers") or "").strip(),
            "flagged_missing_sign": "yes" if flagged else "no",
            "notes": (body.get("notes") or "").strip(),
        }
        append_annotation(row)
    return jsonify({"ok": True, "gsl_gloss": row["gsl_gloss"]}), 201


@app.route("/api/annotations.csv")
def download_annotations():
    if not os.path.exists(ANNOTATIONS_CSV):
        return ",".join(ANNOTATION_COLUMNS) + "\n", 200, {"Content-Type": "text/csv"}
    return send_from_directory(DATA, "annotations.csv", as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
