"""Radar + SquadUp API.

Run:  uvicorn app.main:app --reload
Docs: http://localhost:8000/docs   Dashboard: http://localhost:8000/
"""
from __future__ import annotations

import json
import os
import random
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import radar, squad
from .db import connect, dumps, init_db, row_to_dict, rows_to_dicts
from .schemas import (
    CommunityCreate,
    EventCreate,
    OutreachUpdate,
    ParticipantUpdate,
    RadarRun,
    RegistrationCreate,
    SimulateRequest,
)
from .seed import SYNTHETIC_NAMES, seed_communities, seed_demo_event

BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "RadarSquadUpBot").lstrip("@")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    seed_communities()
    if os.getenv("SEED_DEMO_EVENT", "1") == "1":
        seed_demo_event()
    yield


app = FastAPI(
    title="Radar + SquadUp API",
    version="1.0.0",
    description="Radar brings people in. SquadUp makes each person bring more.",
    lifespan=lifespan,
)

_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,  # credentials + wildcard is invalid per the CORS spec
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def tracked_link(payload: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def get_event_or_404(conn: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    event = row_to_dict(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
    if not event:
        raise HTTPException(404, f"Event {event_id} not found")
    return event


def latest_event(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone())


def serialize_outreach(row: dict[str, Any]) -> dict[str, Any]:
    row["link"] = tracked_link(f"o_{row['ref_code']}")
    row["link_active"] = row["status"] == "approved"
    row["edited"] = bool(row.get("edited"))
    row["language_name"] = radar.LANGUAGE_NAMES.get(row["language"], row["language"])
    return row


def outreach_with_community(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT o.*, c.name AS community_name, c.platform, c.city AS community_city, c.size AS community_size,
                   c.kind AS community_kind, c.admin_name, c.languages AS community_languages
            FROM outreach o JOIN communities c ON c.id = o.community_id
            WHERE {where} ORDER BY o.score DESC, c.name""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["community_languages"] = _json_list(d.get("community_languages"))
        out.append(serialize_outreach(d))
    return out


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError):
        return []


def get_outreach_or_404(conn: sqlite3.Connection, outreach_id: int) -> dict[str, Any]:
    rows = outreach_with_community(conn, "o.id = ?", (outreach_id,))
    if not rows:
        raise HTTPException(404, f"Outreach draft {outreach_id} not found")
    return rows[0]


def resolve_payload(conn: sqlite3.Connection, payload: Optional[str]) -> dict[str, Any]:
    """Turn a Telegram /start payload into attribution: community link, referral, or direct."""
    payload = (payload or "").strip()
    kind, _, code = payload.partition("_")

    if kind == "o" and code:
        o = conn.execute(
            """SELECT o.id, o.event_id, o.status, c.name AS community_name
               FROM outreach o JOIN communities c ON c.id = o.community_id WHERE o.ref_code = ?""",
            (code,),
        ).fetchone()
        if o:
            event = get_event_or_404(conn, o["event_id"])
            if o["status"] == "approved":
                return {"kind": "community", "event": event, "outreach_id": o["id"],
                        "community_name": o["community_name"], "referrer": None, "payload": payload}
            # Link leaked before approval: allow sign-up, but do not attribute it.
            return {"kind": "direct", "event": event, "outreach_id": None,
                    "community_name": None, "referrer": None, "payload": payload}

    if kind == "r" and code:
        p = conn.execute(
            """SELECT p.id, p.name, p.event_id, p.team_id, p.status, t.number AS team_number
               FROM participants p LEFT JOIN teams t ON t.id = p.team_id WHERE p.invite_code = ?""",
            (code,),
        ).fetchone()
        if p:
            return {
                "kind": "referral",
                "event": get_event_or_404(conn, p["event_id"]),
                "outreach_id": None,
                "community_name": None,
                "referrer": {"id": p["id"], "first_name": p["name"].split()[0],
                             "team_number": p["team_number"], "active": p["status"] != "withdrawn"},
                "payload": payload,
            }

    if kind == "e" and code.isdigit():
        event = row_to_dict(conn.execute("SELECT * FROM events WHERE id = ?", (int(code),)).fetchone())
        if event:
            return {"kind": "direct", "event": event, "outreach_id": None,
                    "community_name": None, "referrer": None, "payload": payload}

    event = latest_event(conn)
    if not event:
        raise HTTPException(404, "No events are open for registration yet")
    return {"kind": "direct", "event": event, "outreach_id": None,
            "community_name": None, "referrer": None, "payload": payload}


def record_discovered_community(conn: sqlite3.Connection, college: str, event: dict[str, Any],
                                language: str) -> None:
    """SquadUp -> Radar feedback loop: unknown colleges/clubs join the directory."""
    name = college.strip()
    if len(name) < 3:
        return
    exists = conn.execute("SELECT 1 FROM communities WHERE lower(name) = lower(?)", (name,)).fetchone()
    if exists:
        return
    tags = [radar.normalise_tag(t) for t in event.get("tags", [])][:4]
    conn.execute(
        """INSERT INTO communities (name, kind, platform, city, tags, size, languages, admin_name, discovered_via)
           VALUES (?, 'discovered via SquadUp', 'other', ?, ?, 50, ?, '', 'squadup_referral')""",
        (name, event["city"], dumps(tags), dumps(sorted({language, "en"}))),
    )


def participant_view(conn: sqlite3.Connection, participant_id: int) -> dict[str, Any]:
    row = conn.execute(
        """SELECT p.*, e.name AS event_name, e.start_date, e.city AS event_city, t.number AS team_number
           FROM participants p JOIN events e ON e.id = p.event_id
           LEFT JOIN teams t ON t.id = p.team_id WHERE p.id = ?""",
        (participant_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Participant {participant_id} not found")
    data = dict(row)
    data["invite_link"] = tracked_link(f"r_{data['invite_code']}")
    data["team"] = squad.team_card(conn, data["team_id"]) if data["team_id"] else None
    data["is_synthetic"] = bool(data["is_synthetic"])
    return data


def register_participant(conn: sqlite3.Connection, body: RegistrationCreate,
                         synthetic: bool = False) -> tuple[dict[str, Any], bool]:
    """Core registration used by the API, the bot and the simulator. Returns (participant, created)."""
    attribution = resolve_payload(conn, body.ref_payload) if body.ref_payload or not body.event_id else None
    if body.event_id:
        event = get_event_or_404(conn, body.event_id)
        if attribution and attribution["event"]["id"] != event["id"]:
            attribution = None
    else:
        event = attribution["event"]  # type: ignore[index]

    if body.telegram_id is not None:
        existing = conn.execute(
            "SELECT id, status FROM participants WHERE event_id = ? AND telegram_id = ?",
            (event["id"], body.telegram_id),
        ).fetchone()
        if existing:
            if existing["status"] == "withdrawn":
                # Coming back after withdrawing: re-run matching with their (possibly new) role.
                team_id, _ = squad.assign_team(conn, event["id"], body.role, event["team_size"])
                conn.execute(
                    """UPDATE participants SET status = 'registered', role = ?, experience = ?, language = ?,
                       team_id = ? WHERE id = ?""",
                    (body.role, body.experience, body.language, team_id, existing["id"]),
                )
                return participant_view(conn, existing["id"]), True
            return participant_view(conn, existing["id"]), False

    outreach_id = attribution["outreach_id"] if attribution and attribution["kind"] == "community" else None
    referrer_id = (attribution["referrer"]["id"]
                   if attribution and attribution["kind"] == "referral" and attribution["referrer"]["active"]
                   else None)

    team_id, _reason = squad.assign_team(conn, event["id"], body.role, event["team_size"], referrer_id)
    cur = conn.execute(
        """INSERT INTO participants (event_id, name, telegram_id, language, role, experience,
                                     source_outreach_id, referrer_id, team_id, invite_code, college, is_synthetic)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event["id"], body.name.strip(), body.telegram_id, body.language, body.role, body.experience,
         outreach_id, referrer_id, team_id, squad.new_invite_code(), body.college.strip(), int(synthetic)),
    )
    if body.college:
        record_discovered_community(conn, body.college, event, body.language)
    return participant_view(conn, int(cur.lastrowid)), True


# --------------------------------------------------------------------------- #
# Health & config
# --------------------------------------------------------------------------- #
@app.get("/api/health", tags=["meta"])
def health() -> dict[str, Any]:
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "bot_username": BOT_USERNAME}


@app.get("/api/config", tags=["meta"])
def config() -> dict[str, Any]:
    return {
        "bot_username": BOT_USERNAME,
        "roles": [{"id": r, "label": squad.ROLE_LABELS[r]} for r in squad.ROLES],
        "languages": [{"id": k, "label": v} for k, v in radar.LANGUAGE_NAMES.items()],
        "themes": [{"id": k, "label": v} for k, v in radar.THEME_LABELS.items()],
    }


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
@app.post("/api/events", status_code=201, tags=["events"])
def create_event(body: EventCreate) -> dict[str, Any]:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO events (name, city, description, tags, start_date, venue, team_size)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (body.name.strip(), body.city.strip(), body.description.strip(), dumps(body.tags),
             body.start_date.isoformat() if body.start_date else None, body.venue.strip(), body.team_size),
        )
        event = get_event_or_404(conn, int(cur.lastrowid))
    event["profile"] = radar.build_profile(event)
    event["direct_link"] = tracked_link(f"e_{event['id']}")
    return event


@app.get("/api/events", tags=["events"])
def list_events() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = rows_to_dicts(conn.execute(
            """SELECT e.*,
                      (SELECT COUNT(*) FROM participants p WHERE p.event_id = e.id AND p.status != 'withdrawn')
                        AS registrations
               FROM events e ORDER BY e.id DESC"""
        ).fetchall())
    for e in rows:
        e["direct_link"] = tracked_link(f"e_{e['id']}")
    return rows


@app.get("/api/events/{event_id}", tags=["events"])
def get_event(event_id: int) -> dict[str, Any]:
    with connect() as conn:
        event = get_event_or_404(conn, event_id)
    event["profile"] = radar.build_profile(event)
    event["direct_link"] = tracked_link(f"e_{event['id']}")
    return event


@app.delete("/api/events/{event_id}", status_code=204, tags=["events"])
def delete_event(event_id: int) -> None:
    with connect() as conn:
        get_event_or_404(conn, event_id)
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


@app.get("/api/events/{event_id}/profile", tags=["radar"])
def event_profile(event_id: int) -> dict[str, Any]:
    with connect() as conn:
        return radar.build_profile(get_event_or_404(conn, event_id))


# --------------------------------------------------------------------------- #
# Communities directory
# --------------------------------------------------------------------------- #
@app.get("/api/communities", tags=["radar"])
def list_communities(city: Optional[str] = None) -> list[dict[str, Any]]:
    with connect() as conn:
        if city:
            rows = conn.execute("SELECT * FROM communities WHERE lower(city) = lower(?) ORDER BY name", (city,))
        else:
            rows = conn.execute("SELECT * FROM communities ORDER BY city, name")
        return rows_to_dicts(rows.fetchall())


@app.post("/api/communities", status_code=201, tags=["radar"])
def create_community(body: CommunityCreate) -> dict[str, Any]:
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO communities (name, kind, platform, city, tags, size, languages, admin_name, discovered_via)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'organizer')""",
                (body.name.strip(), body.kind.strip() or "club", body.platform, body.city.strip(),
                 dumps([radar.normalise_tag(t) for t in body.tags]), body.size, dumps(body.languages),
                 body.admin_name.strip()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"A community named '{body.name}' already exists")
        return row_to_dict(conn.execute("SELECT * FROM communities WHERE id = ?", (cur.lastrowid,)).fetchone())


# --------------------------------------------------------------------------- #
# Radar: rank + draft (nothing is ever sent automatically)
# --------------------------------------------------------------------------- #
@app.post("/api/events/{event_id}/radar/run", tags=["radar"])
def run_radar(event_id: int, body: Optional[RadarRun] = None) -> dict[str, Any]:
    limit = body.limit if body else 12
    with connect() as conn:
        event = get_event_or_404(conn, event_id)
        profile = radar.build_profile(event)
        communities = rows_to_dicts(conn.execute("SELECT * FROM communities").fetchall())
        ranked = radar.rank_communities(event, communities, limit=limit)
        ranked_ids = {r["community"]["id"] for r in ranked}

        # Drop stale, untouched drafts from earlier runs; keep anything a human edited or decided on.
        existing = conn.execute(
            "SELECT id, community_id, status, edited FROM outreach WHERE event_id = ?", (event_id,)
        ).fetchall()
        for row in existing:
            if row["community_id"] not in ranked_ids and row["status"] == "pending" and not row["edited"]:
                conn.execute("DELETE FROM outreach WHERE id = ?", (row["id"],))
        existing_by_cid = {r["community_id"]: r for r in existing}

        created = refreshed = 0
        for item in ranked:
            c = item["community"]
            prior = existing_by_cid.get(c["id"])
            if prior is None:
                ref_code = secrets.token_hex(4)
                lang = radar.pick_draft_language(profile, c)
                draft = radar.draft_message(event, c, item["overlaps"], lang, tracked_link(f"o_{ref_code}"))
                conn.execute(
                    """INSERT INTO outreach (event_id, community_id, score, overlaps, rationale, language, draft, ref_code)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, c["id"], item["score"], dumps(item["overlaps"]), item["rationale"], lang, draft, ref_code),
                )
                created += 1
            else:
                conn.execute(
                    "UPDATE outreach SET score = ?, overlaps = ?, rationale = ? WHERE id = ?",
                    (item["score"], dumps(item["overlaps"]), item["rationale"], prior["id"]),
                )
                if prior["status"] == "pending" and not prior["edited"]:
                    o = conn.execute("SELECT language, ref_code FROM outreach WHERE id = ?", (prior["id"],)).fetchone()
                    draft = radar.draft_message(event, c, item["overlaps"], o["language"],
                                                tracked_link(f"o_{o['ref_code']}"))
                    conn.execute("UPDATE outreach SET draft = ? WHERE id = ?", (draft, prior["id"]))
                refreshed += 1

        ranking = [
            {
                "rank": i + 1,
                "community": item["community"],
                "score": item["score"],
                "overlaps": item["overlaps"],
                "overlap_labels": [radar.THEME_LABELS.get(o, o) for o in item["overlaps"]],
                "is_local": item["is_local"],
                "size_factor": item["size_factor"],
                "rationale": item["rationale"],
            }
            for i, item in enumerate(ranked)
        ]
        outreach = outreach_with_community(conn, "o.event_id = ?", (event_id,))

    return {
        "event_id": event_id,
        "profile": profile,
        "formula": "Score = 22 × topic overlaps + 25 (if local) + size factor (≤15)",
        "scanned": len(communities),
        "ranking": ranking,
        "drafts_created": created,
        "drafts_refreshed": refreshed,
        "outreach": outreach,
    }


@app.get("/api/events/{event_id}/radar", tags=["radar"])
def get_ranking(event_id: int, limit: int = Query(12, ge=1, le=50)) -> dict[str, Any]:
    """Read-only ranking preview (does not create drafts)."""
    with connect() as conn:
        event = get_event_or_404(conn, event_id)
        communities = rows_to_dicts(conn.execute("SELECT * FROM communities").fetchall())
    ranked = radar.rank_communities(event, communities, limit=limit)
    return {
        "event_id": event_id,
        "profile": radar.build_profile(event),
        "scanned": len(communities),
        "ranking": [
            {"rank": i + 1, "community": r["community"], "score": r["score"], "overlaps": r["overlaps"],
             "overlap_labels": [radar.THEME_LABELS.get(o, o) for o in r["overlaps"]],
             "is_local": r["is_local"], "size_factor": r["size_factor"], "rationale": r["rationale"]}
            for i, r in enumerate(ranked)
        ],
    }


# --------------------------------------------------------------------------- #
# Outreach: human approval gate
# --------------------------------------------------------------------------- #
@app.get("/api/events/{event_id}/outreach", tags=["outreach"])
def list_outreach(event_id: int, status: Optional[str] = Query(None, pattern="^(pending|approved|rejected)$")
                  ) -> list[dict[str, Any]]:
    with connect() as conn:
        get_event_or_404(conn, event_id)
        if status:
            return outreach_with_community(conn, "o.event_id = ? AND o.status = ?", (event_id, status))
        return outreach_with_community(conn, "o.event_id = ?", (event_id,))


@app.patch("/api/outreach/{outreach_id}", tags=["outreach"])
def update_outreach(outreach_id: int, body: OutreachUpdate) -> dict[str, Any]:
    with connect() as conn:
        o = get_outreach_or_404(conn, outreach_id)
        if o["status"] == "approved":
            raise HTTPException(409, "This draft is already approved. Move it back to pending to edit it.")
        if body.language and body.draft is None:
            event = get_event_or_404(conn, o["event_id"])
            community = row_to_dict(conn.execute(
                "SELECT * FROM communities WHERE id = ?", (o["community_id"],)).fetchone())
            draft = radar.draft_message(event, community, o["overlaps"], body.language, o["link"])
            conn.execute("UPDATE outreach SET language = ?, draft = ?, edited = 0 WHERE id = ?",
                         (body.language, draft, outreach_id))
        elif body.draft is not None:
            conn.execute("UPDATE outreach SET draft = ?, edited = 1, language = COALESCE(?, language) WHERE id = ?",
                         (body.draft.strip(), body.language, outreach_id))
        return get_outreach_or_404(conn, outreach_id)


def _set_outreach_status(outreach_id: int, status: str) -> dict[str, Any]:
    with connect() as conn:
        get_outreach_or_404(conn, outreach_id)
        conn.execute(
            """UPDATE outreach SET status = ?,
                      approved_at = CASE WHEN ? = 'approved' THEN datetime('now') ELSE NULL END
               WHERE id = ?""",
            (status, status, outreach_id),
        )
        return get_outreach_or_404(conn, outreach_id)


@app.post("/api/outreach/{outreach_id}/approve", tags=["outreach"])
def approve_outreach(outreach_id: int) -> dict[str, Any]:
    return _set_outreach_status(outreach_id, "approved")


@app.post("/api/outreach/{outreach_id}/reject", tags=["outreach"])
def reject_outreach(outreach_id: int) -> dict[str, Any]:
    return _set_outreach_status(outreach_id, "rejected")


@app.post("/api/outreach/{outreach_id}/reset", tags=["outreach"])
def reset_outreach(outreach_id: int) -> dict[str, Any]:
    return _set_outreach_status(outreach_id, "pending")


# --------------------------------------------------------------------------- #
# Tracked links
# --------------------------------------------------------------------------- #
@app.get("/api/ref/resolve", tags=["squadup"])
def resolve(payload: Optional[str] = None) -> dict[str, Any]:
    with connect() as conn:
        return resolve_payload(conn, payload)


@app.post("/api/ref/click", tags=["squadup"])
def track_click(payload: str = Query(..., max_length=64)) -> dict[str, Any]:
    with connect() as conn:
        info = resolve_payload(conn, payload)
        if info["kind"] == "community":
            conn.execute("UPDATE outreach SET clicks = clicks + 1 WHERE id = ?", (info["outreach_id"],))
    return {"tracked": info["kind"] == "community", "kind": info["kind"], "event_id": info["event"]["id"]}


# --------------------------------------------------------------------------- #
# SquadUp: registration, teams, confirmation
# --------------------------------------------------------------------------- #
@app.post("/api/register", tags=["squadup"])
def register(body: RegistrationCreate) -> dict[str, Any]:
    with connect() as conn:
        participant, created = register_participant(conn, body)
    participant["created"] = created
    return participant


@app.get("/api/participants/lookup", tags=["squadup"])
def lookup_participant(telegram_id: int, event_id: Optional[int] = None) -> dict[str, Any]:
    with connect() as conn:
        if event_id:
            row = conn.execute(
                "SELECT id FROM participants WHERE telegram_id = ? AND event_id = ?", (telegram_id, event_id)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM participants WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (telegram_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Not registered")
        return participant_view(conn, row["id"])


@app.get("/api/participants/{participant_id}", tags=["squadup"])
def get_participant(participant_id: int) -> dict[str, Any]:
    with connect() as conn:
        return participant_view(conn, participant_id)


@app.patch("/api/participants/{participant_id}", tags=["squadup"])
def update_participant(participant_id: int, body: ParticipantUpdate) -> dict[str, Any]:
    with connect() as conn:
        p = participant_view(conn, participant_id)
        if body.language:
            conn.execute("UPDATE participants SET language = ? WHERE id = ?", (body.language, participant_id))
        if body.college is not None:
            conn.execute("UPDATE participants SET college = ? WHERE id = ?", (body.college.strip(), participant_id))
            if body.college.strip():
                event = get_event_or_404(conn, p["event_id"])
                record_discovered_community(conn, body.college, event, body.language or p["language"])
        return participant_view(conn, participant_id)


@app.post("/api/participants/{participant_id}/confirm", tags=["squadup"])
def confirm_participant(participant_id: int) -> dict[str, Any]:
    with connect() as conn:
        p = participant_view(conn, participant_id)
        if p["status"] == "withdrawn":
            raise HTTPException(409, "This registration was withdrawn. Register again to rejoin.")
        conn.execute("UPDATE participants SET status = 'confirmed' WHERE id = ?", (participant_id,))
        return participant_view(conn, participant_id)


@app.post("/api/participants/{participant_id}/withdraw", tags=["squadup"])
def withdraw_participant(participant_id: int) -> dict[str, Any]:
    """Frees the seat immediately so the balancer can hand it to the next person."""
    with connect() as conn:
        participant_view(conn, participant_id)
        conn.execute("UPDATE participants SET status = 'withdrawn', team_id = NULL WHERE id = ?", (participant_id,))
        return participant_view(conn, participant_id)


@app.get("/api/events/{event_id}/teams", tags=["squadup"])
def list_teams(event_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        get_event_or_404(conn, event_id)
        return squad.event_team_cards(conn, event_id)


@app.get("/api/teams/{team_id}/card", tags=["squadup"])
def get_team_card(team_id: int) -> dict[str, Any]:
    with connect() as conn:
        card = squad.team_card(conn, team_id)
    if not card:
        raise HTTPException(404, f"Team {team_id} not found")
    return card


@app.get("/api/reminders", tags=["squadup"])
def reminder_targets(event_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Everyone the bot should nudge: Telegram users who haven't withdrawn."""
    sql = """SELECT p.id AS participant_id, p.telegram_id, p.language, p.name, p.status, p.event_id,
                    e.name AS event_name, e.start_date, t.number AS team_number
             FROM participants p JOIN events e ON e.id = p.event_id LEFT JOIN teams t ON t.id = p.team_id
             WHERE p.telegram_id IS NOT NULL AND p.status != 'withdrawn'"""
    params: tuple = ()
    if event_id:
        sql += " AND p.event_id = ?"
        params = (event_id,)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@app.get("/api/events/{event_id}/stats", tags=["stats"])
def event_stats(event_id: int) -> dict[str, Any]:
    with connect() as conn:
        event = get_event_or_404(conn, event_id)
        o = conn.execute(
            """SELECT COUNT(*) AS drafts,
                      SUM(status = 'approved') AS approved,
                      SUM(status = 'pending') AS pending,
                      SUM(status = 'rejected') AS rejected,
                      COALESCE(SUM(clicks), 0) AS clicks
               FROM outreach WHERE event_id = ?""",
            (event_id,),
        ).fetchone()
        p = conn.execute(
            """SELECT SUM(status != 'withdrawn') AS registrations,
                      SUM(status = 'confirmed') AS confirmed,
                      SUM(status = 'withdrawn') AS withdrawn,
                      SUM(status != 'withdrawn' AND referrer_id IS NOT NULL) AS via_referral,
                      SUM(status != 'withdrawn' AND source_outreach_id IS NOT NULL) AS via_community,
                      SUM(is_synthetic) AS synthetic
               FROM participants WHERE event_id = ?""",
            (event_id,),
        ).fetchone()

        teams = squad.event_team_cards(conn, event_id)
        by_source = [
            dict(r) for r in conn.execute(
                """SELECT c.name AS label, c.platform, o.clicks,
                          (SELECT COUNT(*) FROM participants p WHERE p.source_outreach_id = o.id
                             AND p.status != 'withdrawn') AS registrations
                   FROM outreach o JOIN communities c ON c.id = o.community_id
                   WHERE o.event_id = ? AND o.status = 'approved'
                   ORDER BY registrations DESC, o.clicks DESC""",
                (event_id,),
            ).fetchall()
        ]
        registrations = p["registrations"] or 0
        via_referral = p["via_referral"] or 0
        direct = registrations - via_referral - (p["via_community"] or 0)
        if via_referral:
            by_source.append({"label": "Teammate invites (SquadUp loop)", "platform": "referral",
                              "clicks": None, "registrations": via_referral})
        if direct > 0:
            by_source.append({"label": "Direct / unattributed", "platform": "direct",
                              "clicks": None, "registrations": direct})

        roles = {r: 0 for r in squad.ROLES}
        for r in conn.execute(
            "SELECT role, COUNT(*) AS n FROM participants WHERE event_id = ? AND status != 'withdrawn' GROUP BY role",
            (event_id,),
        ).fetchall():
            roles[r["role"]] = r["n"]

        recent = [
            {**dict(r), "first_name": r["name"].split()[0]}
            for r in conn.execute(
                """SELECT p.id, p.name, p.role, p.status, p.created_at, t.number AS team_number,
                          c.name AS source, CASE WHEN p.referrer_id IS NOT NULL THEN 1 ELSE 0 END AS via_referral
                   FROM participants p LEFT JOIN teams t ON t.id = p.team_id
                   LEFT JOIN outreach o ON o.id = p.source_outreach_id
                   LEFT JOIN communities c ON c.id = o.community_id
                   WHERE p.event_id = ? ORDER BY p.id DESC LIMIT 8""",
                (event_id,),
            ).fetchall()
        ]
        discovered = conn.execute(
            "SELECT COUNT(*) FROM communities WHERE discovered_via = 'squadup_referral'"
        ).fetchone()[0]

    seeded = registrations - via_referral
    return {
        "event_id": event_id,
        "event_name": event["name"],
        "kpis": {
            "drafts": o["drafts"] or 0,
            "pending": o["pending"] or 0,
            "approved": o["approved"] or 0,
            "rejected": o["rejected"] or 0,
            "clicks": o["clicks"] or 0,
            "registrations": registrations,
            "confirmed": p["confirmed"] or 0,
            "withdrawn": p["withdrawn"] or 0,
            "via_referral": via_referral,
            "synthetic": p["synthetic"] or 0,
            "teams": len(teams),
            "full_teams": sum(1 for t in teams if t["is_full"]),
            "open_seats": sum(t["size"] - t["filled"] for t in teams),
            "viral_coefficient": round(via_referral / seeded, 2) if seeded else 0.0,
            "confirmation_rate": round((p["confirmed"] or 0) / registrations, 2) if registrations else 0.0,
            "discovered_communities": discovered,
        },
        "funnel": [
            {"stage": "Drafts written", "value": o["drafts"] or 0},
            {"stage": "Human-approved", "value": o["approved"] or 0},
            {"stage": "Link clicks", "value": o["clicks"] or 0},
            {"stage": "Registered", "value": registrations},
            {"stage": "Confirmed", "value": p["confirmed"] or 0},
        ],
        "by_source": by_source,
        "roles": roles,
        "recent": recent,
    }


# --------------------------------------------------------------------------- #
# Demo simulator — synthetic participants only, clearly flagged
# --------------------------------------------------------------------------- #
@app.post("/api/events/{event_id}/simulate", tags=["demo"])
def simulate(event_id: int, body: Optional[SimulateRequest] = None) -> dict[str, Any]:
    count = body.count if body else 12
    rng = random.Random()
    with connect() as conn:
        get_event_or_404(conn, event_id)
        approved = conn.execute(
            "SELECT id, ref_code, score FROM outreach WHERE event_id = ? AND status = 'approved'", (event_id,)
        ).fetchall()
        if not approved:
            raise HTTPException(400, "Approve at least one outreach draft first — simulated sign-ups arrive "
                                     "through approved community links.")
        created = []
        for _ in range(count):
            invite = None
            if rng.random() < 0.3:
                invite = conn.execute(
                    """SELECT invite_code FROM participants WHERE event_id = ? AND status != 'withdrawn'
                       ORDER BY RANDOM() LIMIT 1""",
                    (event_id,),
                ).fetchone()
            if invite:
                payload = f"r_{invite['invite_code']}"
            else:
                pick = rng.choices(approved, weights=[max(r["score"], 1) for r in approved])[0]
                conn.execute("UPDATE outreach SET clicks = clicks + ? WHERE id = ?", (rng.randint(1, 3), pick["id"]))
                payload = f"o_{pick['ref_code']}"
            role = rng.choices(squad.ROLES, weights=[40, 20, 20, 20])[0]
            name = f"{rng.choice(SYNTHETIC_NAMES)} {rng.choice('ABCDEGHKMNPRSTV')}."
            participant, _ = register_participant(
                conn,
                RegistrationCreate(event_id=event_id, name=name, role=role,
                                   experience=rng.choice(squad.EXPERIENCE_LEVELS),
                                   language=rng.choice(["en", "en", "hi"]), ref_payload=payload),
                synthetic=True,
            )
            if rng.random() < 0.45:
                conn.execute("UPDATE participants SET status = 'confirmed' WHERE id = ?", (participant["id"],))
            created.append(participant["id"])
    return {"created": len(created), "participant_ids": created, "synthetic": True}


# --------------------------------------------------------------------------- #
# Dashboard (also works when opened as a standalone file thanks to CORS)
# --------------------------------------------------------------------------- #
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
