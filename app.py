"""
Claude Usage Monitor — Flask web application.
"""
import threading
from flask import Flask, render_template, jsonify, request, abort

import database as db
import parser as session_parser
import optimizer
import watcher as watcher_mod

app = Flask(__name__)
_observer = None


def start_background_services():
    global _observer
    db.init_db()
    # Initial full parse
    count = session_parser.parse_all_sessions()
    print(f"[startup] Parsed {count} new messages from existing sessions")
    _observer = watcher_mod.start_watcher()


# ─── API routes ─────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_dashboard_stats())


@app.route("/api/sessions")
def api_sessions():
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    project = request.args.get("project")
    return jsonify(db.get_sessions(limit, offset, project))


@app.route("/api/sessions/<session_id>")
def api_session_detail(session_id):
    messages = db.get_session_messages(session_id)
    if not messages:
        abort(404)
    return jsonify(messages)


@app.route("/api/top-prompts")
def api_top_prompts():
    limit = int(request.args.get("limit", 20))
    return jsonify(db.get_top_token_messages(limit))


@app.route("/api/insights")
def api_insights():
    return jsonify(db.get_insights())


@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    """
    Body: { message_id: int, use_ai: bool }
    Returns optimization suggestions for a specific message.
    """
    data = request.get_json(force=True)
    message_id = data.get("message_id")
    use_ai = bool(data.get("use_ai", False))

    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()

    if not row:
        abort(404)

    row = dict(row)
    prompt = row.get("prompt_text", "")
    input_tokens = row.get("input_tokens", 0)

    suggestions = optimizer.analyze_prompt(prompt, input_tokens, use_ai=use_ai)

    # Persist suggestions
    for s in suggestions:
        db.insert_suggestion(
            message_id=message_id,
            session_id=row["session_id"],
            suggestion_type=s["type"],
            original_tokens=input_tokens,
            savings=s.get("estimated_savings", 0),
            suggestion=s["suggestion"],
        )

    return jsonify({
        "message_id": message_id,
        "input_tokens": input_tokens,
        "prompt_preview": prompt[:300],
        "suggestions": suggestions,
    })


@app.route("/api/rescan")
def api_rescan():
    """Manually trigger a full rescan of all session files."""
    count = session_parser.parse_all_sessions()
    return jsonify({"new_messages": count})


# ─── Page routes ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sessions")
def sessions_page():
    return render_template("sessions.html")


@app.route("/sessions/<session_id>")
def session_detail_page(session_id):
    return render_template("session_detail.html", session_id=session_id)


@app.route("/top-prompts")
def top_prompts_page():
    return render_template("top_prompts.html")


if __name__ == "__main__":
    t = threading.Thread(target=start_background_services, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=7846, debug=False)
