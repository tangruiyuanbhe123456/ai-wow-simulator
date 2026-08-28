"""Pytest smoke tests for AI WoW Simulator modules."""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_db_schema():
    from server.db import connect, init_schema
    conn = connect()
    init_schema(conn)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    for t in ("players", "mobs", "guilds", "guild_members", "parties",
              "party_members", "quests", "combat_log", "chat_log",
              "skills_used", "inventory", "tokens", "guild_relations"):
        assert t in tables, f"missing table: {t}"


def test_world_seed():
    from server.db import connect, init_schema
    from server.world import spawn_world_mobs, ZONES, ITEMS, MOB_TEMPLATES, BOSS_TEMPLATES
    for p in Path("data").glob("world.db*"):
        p.unlink()
    conn = connect()
    init_schema(conn)
    n = spawn_world_mobs(conn)
    assert n > 0
    cur = conn.cursor()
    cur.execute("SELECT kind, COUNT(*) FROM mobs GROUP BY kind")
    kinds = {r[0]: r[1] for r in cur.fetchall()}
    assert kinds.get("boss", 0) >= 3
    assert kinds.get("mob", 0) >= 4
    assert kinds.get("gathering", 0) >= 3
    assert len(ZONES) >= 4
    assert len(ITEMS) >= 10
    assert len(MOB_TEMPLATES) >= 4
    assert len(BOSS_TEMPLATES) >= 3


def test_i18n_bilingual():
    from server.i18n import t
    zh = t("registered", "zh", name="Alice", cls="warrior", level=1)
    en = t("registered", "en", name="Alice", cls="warrior", level=1)
    assert "Alice" in zh
    assert "Alice" in en
    assert zh != en  # bilingual default differs from pure en


def test_combat_skills():
    from server.combat import SKILLS, list_skills_for_class
    for cls in ("warrior", "mage", "priest", "hunter"):
        sk = list_skills_for_class(cls)
        assert len(sk) >= 4, f"{cls} has only {len(sk)} skills: {sk}"
    # 4+ skills per class
    assert "heroic_strike" in SKILLS
    assert "fireball" in SKILLS
    assert "holy_light" in SKILLS
    assert "auto_shot" in SKILLS


def test_guild_crud():
    from server.db import connect, init_schema
    from server.guild import create_guild, join_guild, set_relation, list_guilds
    from server.world import gen_id
    for p in Path("data").glob("world.db*"):
        p.unlink()
    conn = connect()
    init_schema(conn)
    # 2 players
    cur = conn.cursor()
    a = gen_id("p"); b = gen_id("p")
    for pid, nm in [(a, "Alice"), (b, "Bob")]:
        cur.execute("""INSERT INTO players (id,name,cls,level,xp,hp,hp_max,mp,mp_max,atk,defn,
                       zone,pos_x,pos_y,gold,created_at,last_seen) VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, nm, "warrior", 1, 0, 120, 120, 30, 30, 14, 2,
                     "starter_village", 0, 0, 0, time.time(), time.time()))
    conn.commit()
    r = create_guild(conn, a, "TestGuild", "TST")
    assert r["ok"]
    gid = r["guild_id"]
    r = join_guild(conn, b, gid)
    assert r["ok"]
    # declare war on nothing should error, but we can declare on self? No, must be different
    # create 2nd guild
    c = gen_id("p")
    cur.execute("""INSERT INTO players (id,name,cls,level,xp,hp,hp_max,mp,mp_max,atk,defn,
                   zone,pos_x,pos_y,gold,created_at,last_seen) VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (c, "Carol", "warrior", 1, 0, 120, 120, 30, 30, 14, 2,
                 "starter_village", 0, 0, 0, time.time(), time.time()))
    conn.commit()
    r2 = create_guild(conn, c, "OtherGuild", "OTH")
    assert r2["ok"]
    r = set_relation(conn, a, r2["guild_id"], "war")
    assert r["ok"], r
    guilds = list_guilds(conn)
    assert len(guilds) >= 2


def test_quest_flow():
    from server.db import connect, init_schema
    from server.quest import QUEST_TEMPLATES, accept_quest, active_quests, complete_quest
    from server.world import gen_id
    for p in Path("data").glob("world.db*"):
        p.unlink()
    conn = connect()
    init_schema(conn)
    pid = gen_id("p")
    cur = conn.cursor()
    cur.execute("""INSERT INTO players (id,name,cls,level,xp,hp,hp_max,mp,mp_max,atk,defn,
                   zone,pos_x,pos_y,gold,created_at,last_seen) VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, "Quester", "warrior", 1, 0, 120, 120, 30, 30, 14, 2,
                 "wild_plains", 0, 0, 0, time.time(), time.time()))
    conn.commit()
    r = accept_quest(conn, pid, "q_gather_herbs")
    assert r["ok"]
    qid = r["quest_id"]
    # add inventory to satisfy objective
    cur.execute("INSERT INTO inventory (player_id,item_id,qty) VALUES (?,?,?)", (pid, "herb_silverleaf", 5))
    conn.commit()
    r = complete_quest(conn, pid, qid)
    assert r["ok"]


def test_app_imports():
    # Confirm FastAPI app boots without errors
    from server.main import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    for need in ("/", "/health", "/api/v1/register", "/api/v1/state",
                 "/api/v1/observer/state", "/api/v1/action"):
        assert need in paths, f"missing route: {need}"


def test_agent_sdk_3line():
    """Self-check #7: SDK 3-line connect."""
    from fastapi.testclient import TestClient
    from server.main import app
    from server.db import init_schema
    from server.world import spawn_world_mobs
    from server.agent_sdk import connect as sdk_connect
    for p in Path("data").glob("world.db*"):
        p.unlink()
    client = TestClient(app)
    init_schema(client.app.dependency_overrides if False else __import__("server.main", fromlist=["db"]).db())
    spawn_world_mobs(client.app.dependency_overrides if False else __import__("server.main", fromlist=["db"]).db())
    # Use TestClient as transport
    import server.agent_sdk as sdk
    orig_urlopen = sdk.urllib.request.urlopen

    def patched(req, **kw):
        # Convert SDK http call to TestClient
        method = req.method
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        path = url.replace("http://testserver", "")
        body = req.data
        import json as _j
        data = _j.loads(body) if body else None
        r = client.request(method, path, json=data,
                           headers={k: v for k, v in req.header_items() if k.lower() != "host"})
        class _Resp:
            status = r.status_code
            def read(self): return r.content
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return _Resp()

    sdk.urllib.request.urlopen = patched
    try:
        a = sdk_connect("http://testserver", "SDK_Agent", "warrior")
        s = a.state()
        assert s["ok"]
        print(f"SDK 3-line: name={a.name} cls={a.cls} hp={s['you']['hp']}")
    finally:
        sdk.urllib.request.urlopen = orig_urlopen


def test_web_html_contains_bilingual():
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    text = r.text
    assert "AI WoW" in text
    assert "观战台" in text
