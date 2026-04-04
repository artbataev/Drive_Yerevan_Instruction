"""Local quiz API for road exam tickets."""
from __future__ import annotations

import base64
import json
import os
import random
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import AsyncOpenAI

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = ROOT / "questions.json"
PROGRESS_PATH = ROOT / "progress.json"
PROBLEMS_PATH = ROOT / "problems.json"
BALANCER_PATH = ROOT / "balancer.json"
STATIC_DIR = ROOT / "static"

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemini-2.5-pro")

EXAM_SIZE = 20
EXAM_PASS = 18

app = FastAPI(title="Road exam quiz")

_questions: list[dict] = []
_by_id: dict[str, dict] = {}
_by_lang: dict[str, list[dict]] = {}
_lang_index: dict[str, dict[str, int]] = {}
_available_langs: list[str] = []
_exam: dict[str, Any] | None = None


def load_questions() -> None:
    global _questions, _by_id, _by_lang, _lang_index, _available_langs
    if not QUESTIONS_PATH.is_file():
        raise RuntimeError(
            f"Missing {QUESTIONS_PATH.name}. Run: python extract_questions.py"
        )
    data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    _questions = data.get("questions", [])
    _by_id = {q["id"]: q for q in _questions}
    _by_lang = {}
    for q in _questions:
        _by_lang.setdefault(q.get("lang", "unknown"), []).append(q)
    _available_langs = sorted(_by_lang.keys())
    _lang_index = {}
    for lang, qs in _by_lang.items():
        _lang_index[lang] = {q["id"]: i for i, q in enumerate(qs)}


def _pool(lang: str | None = None) -> list[dict]:
    if lang and lang in _by_lang:
        return _by_lang[lang]
    return _questions


def _qindex(q: dict, lang: str | None = None) -> int:
    if lang and lang in _lang_index:
        return _lang_index[lang].get(q["id"], -1)
    return -1


# ---- Persistence helpers ----

def load_progress() -> dict:
    if not PROGRESS_PATH.is_file():
        return {}
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_progress(state: dict) -> None:
    _atomic_write(PROGRESS_PATH, state)


def load_problems() -> list[str]:
    if not PROBLEMS_PATH.is_file():
        return []
    try:
        return json.loads(PROBLEMS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_problems(ids: list[str]) -> None:
    _atomic_write(PROBLEMS_PATH, ids)


def load_balancer() -> list[str]:
    if not BALANCER_PATH.is_file():
        return []
    try:
        return json.loads(BALANCER_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_balancer(ids: list[str]) -> None:
    _atomic_write(BALANCER_PATH, ids)


def _atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@app.on_event("startup")
def startup() -> None:
    load_questions()


class AnswerBody(BaseModel):
    questionId: str
    choiceIndex: int


# ---- Payload helpers ----

def _question_payload(q: dict, lang: str | None = None) -> dict:
    return {
        "id": q["id"],
        "index": _qindex(q, lang),
        "text": q["text"],
        "options": q["options"],
        "image": q.get("image"),
        "source": q.get("source"),
        "page": q.get("page"),
        "lang": q.get("lang"),
    }


def _preview(q: dict) -> dict:
    return {
        "id": q["id"],
        "text": q["text"],
        "image": q.get("image"),
        "source": q.get("source"),
    }


# ---- Error tracking helpers ----

def _add_problem(qid: str) -> None:
    probs = load_problems()
    if qid not in probs:
        probs.append(qid)
        save_problems(probs)


def _add_to_balancer(qid: str) -> None:
    bal = load_balancer()
    bal.append(qid)
    save_balancer(bal)


def _record_answer(qid: str, choice: int, ok: bool) -> None:
    prog = load_progress()
    prev = prog.get(qid, {})
    prog[qid] = {
        "lastChoice": choice,
        "lastCorrect": ok,
        "everCorrect": bool(prev.get("everCorrect")) or ok,
        "everWrong": bool(prev.get("everWrong")) or (not ok),
        "attempts": int(prev.get("attempts", 0)) + 1,
    }
    save_progress(prog)
    if not ok:
        _add_problem(qid)
        _add_to_balancer(qid)


# ---- Languages ----

@app.get("/api/languages")
def get_languages() -> dict:
    return {"languages": _available_langs}


# ---- Practice mode ----

@app.get("/api/question")
def get_question(mode: str = "random", lang: str = Query("")) -> dict:
    pool = _pool(lang or None)
    if not pool:
        raise HTTPException(503, "No questions loaded")
    prog = load_progress()
    if mode == "sequential":
        for q in pool:
            st = prog.get(q["id"])
            if not st or not st.get("everCorrect"):
                return _question_payload(q, lang or None)
        return _question_payload(pool[0], lang or None)
    unanswered = [q for q in pool if not prog.get(q["id"], {}).get("everCorrect")]
    if not unanswered:
        unanswered = pool[:]
    return _question_payload(random.choice(unanswered), lang or None)


@app.get("/api/question/at/{index}")
def get_question_at(index: int, lang: str = Query("")) -> dict:
    pool = _pool(lang or None)
    if not pool:
        raise HTTPException(503, "No questions loaded")
    if index < 0 or index >= len(pool):
        raise HTTPException(
            404, f"Index {index} out of range (0..{len(pool) - 1})"
        )
    return _question_payload(pool[index], lang or None)


@app.get("/api/question/{qid}")
def get_one(qid: str) -> dict:
    q = _by_id.get(qid)
    if not q:
        raise HTTPException(404, "Unknown question")
    return _question_payload(q, q.get("lang"))


@app.post("/api/answer")
def post_answer(body: AnswerBody) -> dict:
    q = _by_id.get(body.questionId)
    if not q:
        raise HTTPException(404, "Unknown question")
    correct = q["correctIndex"]
    ok = body.choiceIndex == correct
    _record_answer(body.questionId, body.choiceIndex, ok)
    return {"correct": ok, "correctIndex": correct}


@app.get("/api/progress")
def get_progress(lang: str = Query("")) -> dict:
    pool = _pool(lang or None)
    pool_ids = {q["id"] for q in pool}
    prog = load_progress()
    scoped = {k: v for k, v in prog.items() if k in pool_ids}
    attempted = [v for v in scoped.values() if int(v.get("attempts", 0)) > 0]
    last_correct = sum(1 for v in attempted if v.get("lastCorrect"))
    last_wrong = len(attempted) - last_correct
    ever_ok = sum(1 for v in scoped.values() if v.get("everCorrect"))
    return {
        "totalQuestions": len(pool),
        "answeredAtLeastOnce": len(scoped),
        "lastCorrect": last_correct,
        "lastWrong": last_wrong,
        "everCorrect": ever_ok,
        "byQuestion": scoped,
    }


@app.get("/api/review")
def review_lists(lang: str = Query("")) -> dict:
    pool_ids = {q["id"] for q in _pool(lang or None)}
    prog = load_progress()
    last_correct: list[dict] = []
    last_wrong: list[dict] = []
    for qid, v in prog.items():
        if qid not in pool_ids:
            continue
        if int(v.get("attempts", 0)) < 1:
            continue
        q = _by_id.get(qid)
        if not q:
            continue
        p = _preview(q)
        if v.get("lastCorrect"):
            last_correct.append(p)
        else:
            last_wrong.append(p)
    return {"lastCorrect": last_correct, "lastWrong": last_wrong}


@app.post("/api/progress/reset")
def reset_progress() -> dict:
    save_progress({})
    return {"ok": True}


# ---- Exam mode ----

@app.post("/api/exam/start")
def exam_start(lang: str = Query("")) -> dict:
    global _exam
    pool = _pool(lang or None)
    chosen = random.sample(pool, min(EXAM_SIZE, len(pool)))
    _exam = {
        "ids": [q["id"] for q in chosen],
        "lang": lang or None,
        "index": 0,
        "correct": 0,
        "wrong": 0,
        "wrongIds": [],
        "finished": False,
    }
    return {
        "total": len(chosen),
        "passThreshold": EXAM_PASS,
        "question": _question_payload(chosen[0], lang or None),
        "index": 0,
    }


@app.get("/api/exam/status")
def exam_status() -> dict:
    if _exam is None:
        return {"active": False}
    return {
        "active": not _exam["finished"],
        "index": _exam["index"],
        "total": len(_exam["ids"]),
        "correct": _exam["correct"],
        "wrong": _exam["wrong"],
        "finished": _exam["finished"],
        "passed": _exam["correct"] >= EXAM_PASS if _exam["finished"] else None,
        "passThreshold": EXAM_PASS,
    }


class ExamAnswerBody(BaseModel):
    choiceIndex: int


@app.post("/api/exam/answer")
def exam_answer(body: ExamAnswerBody) -> dict:
    global _exam
    if _exam is None or _exam["finished"]:
        raise HTTPException(400, "No active exam")
    elang = _exam.get("lang")
    idx = _exam["index"]
    qid = _exam["ids"][idx]
    q = _by_id.get(qid)
    if not q:
        raise HTTPException(500, "Question not found")
    correct_idx = q["correctIndex"]
    ok = body.choiceIndex == correct_idx
    if ok:
        _exam["correct"] += 1
    else:
        _exam["wrong"] += 1
        _exam["wrongIds"].append(qid)
        _add_problem(qid)
        _add_to_balancer(qid)
    _exam["index"] = idx + 1
    total = len(_exam["ids"])
    finished = _exam["index"] >= total
    _exam["finished"] = finished
    next_q = None
    if not finished:
        nq = _by_id.get(_exam["ids"][_exam["index"]])
        if nq:
            next_q = _question_payload(nq, elang)
    passed = _exam["correct"] >= EXAM_PASS if finished else None
    return {
        "correct": ok,
        "correctIndex": correct_idx,
        "score": {"correct": _exam["correct"], "wrong": _exam["wrong"]},
        "index": _exam["index"],
        "total": total,
        "finished": finished,
        "passed": passed,
        "nextQuestion": next_q,
    }


@app.post("/api/exam/start-from-problems")
def exam_start_from_problems(lang: str = Query("")) -> dict:
    global _exam
    pool_ids = {q["id"] for q in _pool(lang or None)}
    probs = load_problems()
    prob_qs = [_by_id[qid] for qid in probs if qid in _by_id and qid in pool_ids]
    random.shuffle(prob_qs)
    chosen = prob_qs[:EXAM_SIZE]
    if len(chosen) < EXAM_SIZE:
        remaining = EXAM_SIZE - len(chosen)
        used = {q["id"] for q in chosen}
        extras = [q for q in _pool(lang or None) if q["id"] not in used]
        random.shuffle(extras)
        chosen.extend(extras[:remaining])
    _exam = {
        "ids": [q["id"] for q in chosen],
        "lang": lang or None,
        "index": 0,
        "correct": 0,
        "wrong": 0,
        "wrongIds": [],
        "finished": False,
    }
    return {
        "total": len(chosen),
        "passThreshold": EXAM_PASS,
        "question": _question_payload(chosen[0], lang or None),
        "index": 0,
    }


# ---- Problems (errors from everywhere) ----

@app.get("/api/problems")
def get_problems(lang: str = Query("")) -> dict:
    pool_ids = {q["id"] for q in _pool(lang or None)}
    probs = load_problems()
    items: list[dict] = []
    for qid in probs:
        if qid not in pool_ids:
            continue
        q = _by_id.get(qid)
        if q:
            items.append(_preview(q))
    return {"count": len(items), "problems": items}


@app.post("/api/problems/clear")
def clear_problems() -> dict:
    save_problems([])
    return {"ok": True}


@app.post("/api/problems/remove")
def remove_problem(body: dict) -> dict:
    qid = body.get("questionId", "")
    probs = load_problems()
    probs = [p for p in probs if p != qid]
    save_problems(probs)
    return {"ok": True}


# ---- Complicated (training from problems pool) ----

@app.get("/api/complicated/question")
def complicated_question(lang: str = Query("")) -> dict:
    pool_ids = {q["id"] for q in _pool(lang or None)}
    probs = load_problems()
    candidates = [_by_id[qid] for qid in probs if qid in _by_id and qid in pool_ids]
    if not candidates:
        raise HTTPException(404, "Нет сложных вопросов")
    return _question_payload(random.choice(candidates), lang or None)


# ---- Balancer (weighted repetition) ----

@app.get("/api/balancer/question")
def balancer_question(lang: str = Query("")) -> dict:
    pool_ids = {q["id"] for q in _pool(lang or None)}
    bal = load_balancer()
    filtered = [qid for qid in bal if qid in pool_ids and qid in _by_id]
    if not filtered:
        raise HTTPException(404, "Балансир пуст — ошибок ещё нет")
    qid = random.choice(filtered)
    return _question_payload(_by_id[qid], lang or None)


@app.get("/api/balancer/stats")
def balancer_stats(lang: str = Query("")) -> dict:
    pool_ids = {q["id"] for q in _pool(lang or None)}
    bal = load_balancer()
    filtered = [qid for qid in bal if qid in pool_ids]
    counts = Counter(filtered)
    items = []
    for qid, cnt in counts.most_common():
        q = _by_id.get(qid)
        if q:
            items.append(
                {"id": qid, "text": q["text"], "count": cnt, "image": q.get("image")}
            )
    return {"total": len(filtered), "unique": len(counts), "items": items}


@app.post("/api/balancer/answer")
def balancer_answer(body: AnswerBody) -> dict:
    q = _by_id.get(body.questionId)
    if not q:
        raise HTTPException(404, "Unknown question")
    correct = q["correctIndex"]
    ok = body.choiceIndex == correct
    prog = load_progress()
    prev = prog.get(body.questionId, {})
    prog[body.questionId] = {
        "lastChoice": body.choiceIndex,
        "lastCorrect": ok,
        "everCorrect": bool(prev.get("everCorrect")) or ok,
        "everWrong": bool(prev.get("everWrong")) or (not ok),
        "attempts": int(prev.get("attempts", 0)) + 1,
    }
    save_progress(prog)
    bal = load_balancer()
    if ok:
        try:
            bal.remove(body.questionId)
        except ValueError:
            pass
    else:
        bal.append(body.questionId)
        _add_problem(body.questionId)
    save_balancer(bal)
    return {"correct": ok, "correctIndex": correct}


# ---- Explain (NVIDIA Gemini API) ----

@app.post("/api/explain")
async def explain_question(body: dict) -> dict:
    qid = body.get("questionId", "")
    q = _by_id.get(qid)
    if not q:
        raise HTTPException(404, "Unknown question")
    if not LLM_API_KEY:
        raise HTTPException(500, "LLM_API_KEY not configured in .env")

    text = q["text"]
    options = q["options"]
    prompt = (
        "Это вопрос экзамена по ПДД Республики Армения. "
        "При ответе опирайся в первую очередь на армянскую редакцию ПДД, "
        "а не на российскую.\n\n"
        f"Вопрос:\n{text}\n\nВарианты ответов:\n"
    )
    for i, opt in enumerate(options, 1):
        prompt += f"{i}. {opt}\n"
    prompt += (
        f"\nПравильный ответ: {q['correctIndex'] + 1}. "
        f"{options[q['correctIndex']]}\n"
        "\nОбъясни, почему этот ответ правильный. Ответь на русском языке."
    )

    content: list[dict] = []
    image_file = q.get("image", "")
    image_path = ROOT / image_file if image_file else None
    if image_path and image_path.is_file():
        img_data = base64.b64encode(image_path.read_bytes()).decode()
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_data}"},
            }
        )
    content.append({"type": "text", "text": prompt})

    try:
        client = AsyncOpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
        )
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": content}],
            temperature=0.7,
        )
        return {"explanation": response.choices[0].message.content}
    except Exception as e:
        raise HTTPException(502, f"API error: {e}")


# ---- Original page image ----

@app.get("/api/page-image/{page_num}/{source:path}")
def page_image(source: str, page_num: int):
    if ".." in source:
        raise HTTPException(400, "Invalid source")
    pdf_path = ROOT / "pdfs" / source
    if not pdf_path.is_file():
        raise HTTPException(404, "PDF not found")

    cache_dir = ROOT / "media" / "pages"
    safe_name = source.replace("/", "_").replace("\\", "_")
    cache_name = f"{Path(safe_name).stem}_p{page_num}.png"
    cache_path = cache_dir / cache_name

    if not cache_path.is_file():
        import fitz

        doc = fitz.open(pdf_path)
        if page_num < 0 or page_num >= doc.page_count:
            doc.close()
            raise HTTPException(404, "Page out of range")
        page = doc.load_page(page_num)
        mat = fitz.Matrix(144 / 72, 144 / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        cache_dir.mkdir(parents=True, exist_ok=True)
        pix.save(str(cache_path))
        doc.close()

    return FileResponse(cache_path, media_type="image/png")


# ---- Static files (must be last) ----

app.mount("/media", StaticFiles(directory=str(ROOT / "media")), name="media")
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
