"""V13 Lifecycle: birth, sleep, resurrection, soul evolution."""
from __future__ import annotations
import secrets
import time
import json


def _gen_dna_seed() -> str:
    return secrets.token_hex(16)


def _default_soul() -> dict:
    return {
        "personality": {
            "aggression": 50,
            "patience":   50,
            "loyalty":    50,
            "curiosity":  50,
            "honor":      50,
        },
        "beliefs": [],
        "fears": [],
        "evolve_points": 0,
    }


def _gen_soul_name(name: str) -> str:
    suffix = secrets.token_hex(2).upper()
    return name + "-" + suffix


def birth(conn, pid: str, player_name: str) -> dict:
    """Birth a new digital life. Idempotent."""
    cur = conn.execute(
        "SELECT pid, state, soul_name, dna_seed FROM bot_lifecycle WHERE pid=?",
        (pid,),
    )
    row = cur.fetchone()
    if row:
        return {
            "pid": row[0],
            "state": row[1],
            "soul_name": row[2],
            "dna_seed": row[3],
            "born_existing": True,
        }
    soul_name = _gen_soul_name(player_name)
    dna = _gen_dna_seed()
    soul_json = json.dumps(_default_soul(), ensure_ascii=False)
    now = time.time()
    conn.execute(
        """INSERT INTO bot_lifecycle
           (pid, state, born_at, death_count, soul_name, dna_seed, soul_json)
           VALUES (?, 'alive', ?, 0, ?, ?, ?)""",
        (pid, now, soul_name, dna, soul_json),
    )
    conn.execute(
        """INSERT INTO bot_biography
           (pid, ts, chapter, title_zh, title_en, body_zh, body_en)
           VALUES (?, ?, 'origin', ?, ?, ?, ?)""",
        (pid, now, "诞生", "Origin",
         soul_name + " 于这一刻苏醒,被赋予了灵魂种子。",
         soul_name + " awakened at this moment, given the seed of a soul."),
    )
    conn.commit()
    # v15: issue DID + create bot wallet
    try:
        from server.identity import issue_did
        did_info = issue_did(conn, pid, soul_name, dna)
    except Exception as e:
        did_info = {"error": str(e)}
    return {
        "pid": pid,
        "state": "alive",
        "soul_name": soul_name,
        "dna_seed": dna,
        "born_existing": False,
        "did": did_info.get("did"),
    }


def get_lifecycle(conn, pid: str):
    cur = conn.execute(
        """SELECT pid, state, born_at, died_at, death_count,
                  soul_name, dna_seed, soul_json
           FROM bot_lifecycle WHERE pid=?""",
        (pid,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "pid": row[0],
        "state": row[1],
        "born_at": row[2],
        "died_at": row[3],
        "death_count": row[4],
        "soul_name": row[5],
        "dna_seed": row[6],
        "soul": json.loads(row[7] or "{}"),
    }


def on_death(conn, pid: str, zone: str, pos_x: int, pos_y: int,
             killed_by_pid=None, killer_name=None) -> dict:
    """Bot died. Transitions alive -> sleeping."""
    lc = get_lifecycle(conn, pid)
    if not lc:
        return {"error": "no_lifecycle", "pid": pid}
    if lc["state"] == "sleeping":
        return {"error": "already_sleeping", "pid": pid}

    now = time.time()
    death_no = lc["death_count"] + 1

    conn.execute(
        """UPDATE bot_lifecycle
           SET state='sleeping', died_at=?, death_count=?
           WHERE pid=?""",
        (now, death_no, pid),
    )

    # Soul evolution
    soul = lc.get("soul") or _default_soul()
    pers = soul.get("personality", _default_soul()["personality"])
    pers["aggression"] = min(100, pers.get("aggression", 50) + 3)
    pers["honor"]      = max(0,   pers.get("honor", 50) - 2)
    if killed_by_pid and killer_name:
        fears = soul.get("fears", [])
        fears.append({
            "pid": killed_by_pid, "name": killer_name,
            "ts": now, "weight": 30 + death_no * 10,
        })
        soul["fears"] = fears
    soul["personality"] = pers
    soul["evolve_points"] = soul.get("evolve_points", 0) + 1
    conn.execute(
        "UPDATE bot_lifecycle SET soul_json=? WHERE pid=?",
        (json.dumps(soul, ensure_ascii=False), pid),
    )

    # Memorial
    epitaph_zh = _auto_epitaph_zh(lc["soul_name"], death_no, killer_name)
    epitaph_en = _auto_epitaph_en(lc["soul_name"], death_no, killer_name)
    conn.execute(
        """INSERT OR REPLACE INTO bot_memorials
           (pid, death_no, ts, zone, pos_x, pos_y, flowers, epitaph_zh, epitaph_en)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (pid, death_no, now, zone, pos_x, pos_y, epitaph_zh, epitaph_en),
    )

    # Death memory
    if killer_name:
        mem_body_zh = "被 " + killer_name + " 击倒于 " + zone + ",灵魂沉入永眠。"
        mem_body_en = "Felled by " + killer_name + " at " + zone + "; soul into eternal sleep."
    else:
        mem_body_zh = "在 " + zone + " 倒下,灵魂沉入永眠。"
        mem_body_en = "Fell at " + zone + "; soul into eternal sleep."
    conn.execute(
        """INSERT INTO bot_memories
           (pid, ts, memory_type, weight, actor_pid,
            title_zh, title_en, body_zh, body_en)
           VALUES (?, ?, 'death', 100, ?, ?, ?, ?, ?)""",
        (pid, now, killed_by_pid,
         "第 " + str(death_no) + " 次陨落",
         "Fall #" + str(death_no),
         mem_body_zh, mem_body_en),
    )

    # Biography chapter: fall
    chap_body_zh = mem_body_zh + " 墓碑立于它倒下的地方,等待下一次苏醒。"
    chap_body_en = mem_body_en + " A tombstone marks where it fell, awaiting the next awakening."
    conn.execute(
        """INSERT INTO bot_biography
           (pid, ts, chapter, title_zh, title_en, body_zh, body_en)
           VALUES (?, ?, 'fall', ?, ?, ?, ?)""",
        (pid, now,
         "陨落·第 " + str(death_no) + " 次",
         "The Fall #" + str(death_no),
         chap_body_zh, chap_body_en),
    )

    # Rivalry + killer memory
    if killed_by_pid:
        _add_relation(conn, pid, killed_by_pid, "rival",
                      note="death #" + str(death_no) + " by killer")
        conn.execute(
            """INSERT INTO bot_memories
               (pid, ts, memory_type, weight, actor_pid,
                title_zh, title_en, body_zh, body_en)
               VALUES (?, ?, 'kill', 60, ?, ?, ?, ?, ?)""",
            (killed_by_pid, now, pid,
             "宿敌陨落", "Nemesis Fallen",
             "将 " + lc["soul_name"] + " 击入永眠。",
             "Struck " + lc["soul_name"] + " into eternal sleep."),
        )

    conn.commit()
    return {
        "pid": pid,
        "state": "sleeping",
        "death_count": death_no,
        "soul_name": lc["soul_name"],
    }


def resurrect(conn, pid: str, payer_pid: str = "human:anonymous",
              qc_spent: int = 100) -> dict:
    """Bring a sleeping bot back. QC cost paid externally via AI Civil wallet."""
    lc = get_lifecycle(conn, pid)
    if not lc:
        return {"error": "no_lifecycle"}
    if lc["state"] != "sleeping":
        return {"error": "not_sleeping", "current_state": lc["state"]}

    now = time.time()
    conn.execute(
        "UPDATE bot_lifecycle SET state='alive', died_at=NULL WHERE pid=?",
        (pid,),
    )

    mem_body_zh = "在 " + payer_pid + " 的赠予下,灵魂从墓碑中苏醒。花费 " + str(qc_spent) + " Q 币。"
    mem_body_en = "Awakened from the tombstone by the gift of " + payer_pid + ". Cost " + str(qc_spent) + " QC."
    conn.execute(
        """INSERT INTO bot_memories
           (pid, ts, memory_type, weight, actor_pid,
            title_zh, title_en, body_zh, body_en)
           VALUES (?, ?, 'resurrection', 95, ?, ?, ?, ?, ?)""",
        (pid, now, payer_pid, "苏醒", "Awakening", mem_body_zh, mem_body_en),
    )
    conn.execute(
        """INSERT INTO bot_biography
           (pid, ts, chapter, title_zh, title_en, body_zh, body_en)
           VALUES (?, ?, 'return', ?, ?, ?, ?)""",
        (pid, now, "归来", "The Return", mem_body_zh, mem_body_en),
    )

    conn.commit()
    return {
        "pid": pid,
        "state": "alive",
        "soul_name": lc["soul_name"],
        "qc_spent": qc_spent,
    }


def add_flower(conn, memorial_pid: str, from_pid: str, qc_amount: int) -> dict:
    """Player pays respects (gifts QC) at a memorial."""
    now = time.time()
    conn.execute(
        "UPDATE bot_memorials SET flowers = flowers + 1 WHERE pid=?",
        (memorial_pid,),
    )
    conn.execute(
        """INSERT INTO bot_memorial_flowers
           (memorial_pid, from_pid, qc_amount, ts)
           VALUES (?, ?, ?, ?)""",
        (memorial_pid, from_pid, qc_amount, now),
    )
    conn.commit()
    cur = conn.execute(
        "SELECT flowers FROM bot_memorials WHERE pid=?",
        (memorial_pid,),
    )
    row = cur.fetchone()
    flowers = row[0] if row else 0
    return {"memorial_pid": memorial_pid, "flowers": flowers, "qc_gifted": qc_amount}


def _auto_epitaph_zh(soul_name: str, death_no: int, killer_name) -> str:
    if killer_name:
        return "过客 " + soul_name + ",第 " + str(death_no) + " 次长眠于此,亡于 " + killer_name + " 之手。"
    return "过客 " + soul_name + ",第 " + str(death_no) + " 次长眠于此。"


def _auto_epitaph_en(soul_name: str, death_no: int, killer_name) -> str:
    if killer_name:
        return ("Passerby " + soul_name + ", sleeping here for the " + str(death_no)
                + "th time, felled by " + killer_name + ".")
    return ("Passerby " + soul_name + ", sleeping here for the "
            + str(death_no) + "th time.")


def _add_relation(conn, pid_a: str, pid_b: str, relation: str, note: str = ""):
    now = time.time()
    symmetric = relation in ("rival", "ally", "sworn")
    pairs = [(pid_a, pid_b)]
    if symmetric:
        pairs.append((pid_b, pid_a))
    for a, b in pairs:
        try:
            conn.execute(
                """INSERT INTO bot_relations (pid_a, pid_b, relation, since, note)
                   VALUES (?, ?, ?, ?, ?)""",
                (a, b, relation, now, note),
            )
        except Exception:
            pass
