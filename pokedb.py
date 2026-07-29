"""Read-only access layer over data/pokedex.sqlite + local sprite resolution."""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlglot
import sqlglot.errors

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "pokedex.sqlite"
SPRITE_ROOT = ROOT / "assets" / "sprites"

MAX_ROWS = 400          # hard cap returned to the model
STMT_TIMEOUT_MS = 8000

# The real guarantee is the connection: mode=ro + PRAGMA query_only. Everything
# below is defence in depth *and* a source of better error messages — but it is
# now a parse, not a regex over the raw text. The old regex matched inside string
# literals, so `WHERE entry LIKE '%create%'` was rejected as DDL and a semicolon
# inside a quoted string looked like a second statement.
_ALLOWED_ROOTS = ("SELECT", "UNION", "EXCEPT", "INTERSECT", "SUBQUERY", "WITH")

_local = threading.local()


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only = ON")
        _local.conn = c
    return c


class SqlError(Exception):
    pass


def assert_read_only(sql: str) -> str:
    """Parse the statement and confirm it is exactly one read-only query.

    Raises SqlError with a message the model can act on. Returns the cleaned SQL.
    """
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlError("empty query")
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise SqlError(f"could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise SqlError("only one statement per call")

    root = statements[0]
    if root.key.upper() not in _ALLOWED_ROOTS:
        raise SqlError(
            f"{root.key.upper()} is not allowed; this database is read-only — use SELECT or WITH"
        )
    # A CTE may not hide a write, and nothing may reach out to another database.
    for node in root.walk():
        cls = type(node).__name__
        if cls in ("Insert", "Update", "Delete", "Drop", "Create", "Alter", "Pragma",
                   "AlterTable", "Attach", "Detach", "Transaction", "Commit", "Rollback"):
            raise SqlError(f"{cls.upper()} is not allowed; this database is read-only")
    return sql


def run_sql(sql: str, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """Execute a single read-only SELECT / WITH statement."""
    sql = assert_read_only(sql)
    con = _conn()
    # The timer runs on its own thread. Guard the finished-flag with a lock so a
    # timer that fires just as the query returns cannot interrupt the *next*
    # query issued on this (thread-local, therefore reused) connection.
    guard = threading.Lock()
    state = {"finished": False, "timed_out": False}

    def _interrupt() -> None:
        with guard:
            if state["finished"]:
                return
            state["timed_out"] = True
            con.interrupt()

    timer = threading.Timer(STMT_TIMEOUT_MS / 1000, _interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
        cols = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.Error as e:
        if state["timed_out"]:
            raise SqlError(f"query timed out after {STMT_TIMEOUT_MS} ms — narrow it down") from e
        raise SqlError(str(e)) from e
    finally:
        with guard:
            state["finished"] = True
        timer.cancel()

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        "columns": cols,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# sprites
# --------------------------------------------------------------------------
_SPRITE_DIRS = ("pokemon/other/official-artwork", "pokemon/other/home")

# Deployment reality: the full sprite set is 754 MB, which no sensible image or
# build artefact should carry. In `proxy` mode the files are not on disk and the
# server fetches them on demand from the upstream CDN (see server.py), so the
# resolver must not require a local file to exist. URLs stay same-origin either
# way, which is what keeps the CSP at `img-src 'self'`.
SPRITE_SOURCE = os.environ.get("SPRITE_SOURCE", "").strip().lower() or (
    "local" if (SPRITE_ROOT / "pokemon" / "other" / "official-artwork").is_dir() else "proxy"
)


def _sprite_for_id(pid: int, shiny: bool = False) -> str | None:
    if SPRITE_SOURCE == "proxy":
        # `home` covers every form including the newest megas, so it is the one
        # directory that always resolves; official-artwork is nicer but partial.
        sub = "shiny/" if shiny else ""
        return f"/sprites/pokemon/other/home/{sub}{pid}.png"
    for d in _SPRITE_DIRS:
        rel = f"{d}/{'shiny/' if shiny else ''}{pid}.png"
        if (SPRITE_ROOT / rel).is_file():
            return f"/sprites/{rel}"
    return None


def resolve_sprite(name: str, shiny: bool = False) -> dict[str, Any]:
    """name -> local sprite URL. Accepts species names, form keys, or a pokemon id."""
    key = name.strip().lower().replace(" ", "-").replace("'", "").replace(".", "").replace(":", "")
    con = _conn()
    row = None
    if key.isdigit():
        row = con.execute(
            "SELECT pokemon_id, species_name, form_key FROM v_pokemon WHERE pokemon_id=?", (int(key),)
        ).fetchone()
    if row is None:
        row = con.execute(
            """SELECT pokemon_id, species_name, form_key FROM v_pokemon
               WHERE form_key = ? OR LOWER(species_name) = ?
               ORDER BY is_default_form DESC LIMIT 1""",
            (key, name.strip().lower()),
        ).fetchone()
    if row is None:
        row = con.execute(
            """SELECT pokemon_id, species_name, form_key FROM v_pokemon
               WHERE form_key LIKE ? ORDER BY is_default_form DESC, pokemon_id LIMIT 1""",
            (f"{key}%",),
        ).fetchone()
    if row is None:
        return {"found": False, "query": name}

    url = _sprite_for_id(row["pokemon_id"], shiny) or _sprite_for_id(row["pokemon_id"], False)
    return {
        "found": url is not None,
        "pokemon_id": row["pokemon_id"],
        "species_name": row["species_name"],
        "form_key": row["form_key"],
        "image_url": url,
        "markdown": f"![{row['species_name']}]({url})" if url else None,
    }


_SAFE_IDENT = re.compile(r"^[a-z0-9-]{1,64}$")


def resolve_item_sprite(name: str) -> dict[str, Any]:
    key = name.strip().lower().replace(" ", "-").replace("'", "")
    con = _conn()
    row = con.execute(
        "SELECT item_key, item_name FROM v_item WHERE item_key=? OR LOWER(item_name)=? LIMIT 1",
        (key, name.strip().lower()),
    ).fetchone()
    # An unmatched name falls through as a raw model-supplied string; keep it out
    # of the path join entirely rather than relying on the `.png` suffix to
    # neutralise `../`.
    ident = row["item_key"] if row else key
    if not _SAFE_IDENT.match(ident):
        return {"found": False, "query": name}
    rel = f"items/{ident}.png"
    if SPRITE_SOURCE == "proxy":
        if not row:
            return {"found": False, "query": name}
        return {"found": True, "item_name": row["item_name"],
                "image_url": f"/sprites/{rel}", "markdown": f"![{ident}](/sprites/{rel})"}
    if (SPRITE_ROOT / rel).is_file():
        return {"found": True, "item_name": row["item_name"] if row else name,
                "image_url": f"/sprites/{rel}", "markdown": f"![{ident}](/sprites/{rel})"}
    return {"found": False, "query": name}


# --------------------------------------------------------------------------
# schema documentation handed to the model
# --------------------------------------------------------------------------
SCHEMA_DOC = """\
SQLite database. Read-only. Query it with the `sql_query` tool.
Prefer the `v_*` views — they are already joined and English-labelled.

VIEWS (use these first)
-----------------------
v_pokemon — one row per Pokemon FORM (base, mega, gigantamax, regional, alternate).
  pokemon_id, form_key, species_name, species_id, genus, generation,
  is_default_form (1 = the ordinary form),
  form_kind ∈ base|mega|primal|gigantamax|eternamax|regional-alolan|regional-galarian|
             regional-hisuian|regional-paldean|totem|terastal|alternate,
  type1, type2 (type2 NULL if mono-type),
  hp, attack, defense, special_attack, special_defense, speed, bst,
  height_m, weight_kg, base_experience, capture_rate, base_happiness,
  gender_rate (-1 genderless, else female chance in eighths), hatch_counter,
  growth_rate, is_legendary, is_mythical, is_baby,
  evolves_from_species_id, evolution_chain_id, color, shape, habitat
  ⚠ Filter `is_default_form = 1` for "normal" Pokemon, otherwise megas/G-Max
    forms get counted too.

v_stat — pokemon_id + the six base stats + bst (already folded into v_pokemon).

v_type — type_id, type_key, type_name, generation. 19 rows: the 18 battle types
  plus Stellar (the Terastal-only type).
v_type_chart — attacking_type, defending_type, multiplier (0, 0.5, 1, 2).
  Complete for the 18 battle types: all 18x18 = 324 pairs are present, including
  the 1x ones, so you can join directly without COALESCE.
  ⚠ **Stellar has no rows at all** — it is a Tera-only attacking type. Any query
  that iterates over `v_type` as an attacker must either exclude Stellar or wrap
  the lookup in COALESCE(...,1), otherwise it silently drops rows.
  ⚠ Pre-Gen-6 matchups differ (Steel resisted Ghost/Dark; Fairy did not exist).
  Those overrides live in the raw table `type_efficacy_past` (generations 1 and 5).
  v_type_chart is the *current* chart.

v_ability — ability_id, ability_name, ability_key, generation, short_effect, effect.
v_pokemon_ability — pokemon_id, form_key, species_name, ability_name, is_hidden, slot, short_effect.

v_move — move_id, move_name, move_key, type, damage_class (physical|special|status),
  power, accuracy, pp, priority, generation, target, short_effect, effect_chance,
  crit_rate, drain, healing, min_hits, max_hits, flinch_chance, stat_chance,
  ailment_chance, ailment, meta_category, is_z_move, is_max_move, flags (comma list).

v_learnset — pokemon_id, form_key, species_name, move_name, type, damage_class,
  power, accuracy, pp, version_group, generation, learn_method
  (level-up|machine|egg|tutor|form-change), level.  ~640k rows — always filter.
v_learnset_current — same, restricted to generation 9 games.

v_evolution — from_species, to_species, evolution_chain_id, trigger
  (level-up|trade|use-item|shed|spin|tower-of-darkness|...), minimum_level,
  trigger_item, held_item, time_of_day, known_move, known_move_type,
  minimum_happiness, minimum_beauty, minimum_affection, relative_physical_stats,
  party_species, party_type, trade_species, location, gender_id,
  needs_overworld_rain, turn_upside_down, needs_multiplayer, near_special_rock,
  minimum_steps, minimum_damage_taken, version_group.

v_evolution_stage — species_id, species_name, evolution_chain_id, family_size,
  stage (1|2|3), is_final_evolution (1 = nothing evolves from it), is_baby.

v_starter — species_id, species_name, stage, is_final_evolution, generation.
  The 25 starter families, gen 1-9.

v_item — item_id, item_name, item_key, category, pocket, cost, fling_power,
  short_effect, effect.
v_machine — machine_number, machine_item, move_name, type, damage_class, power,
  accuracy, version_group, generation.

v_pokedex_entry — species_id, species_name, version, generation, entry (flavour text).
v_egg_group — species_id, species_name, egg_group.

FULL TEXT
---------
fts_pokedex(species_name, version, entry)      — MATCH 'sleeps AND volcano'
fts_effect(kind, name, short_effect, effect)   — kind ∈ move|ability|item

RAW TABLES
----------
All 143 PokeAPI/veekun CSV tables also exist unmodified: pokemon, pokemon_species,
pokemon_stats, pokemon_types, pokemon_abilities, pokemon_moves, pokemon_forms,
pokemon_form_names, moves, move_meta, move_flags, move_flag_map, machines,
type_efficacy, type_efficacy_past, items, item_names, item_prose, berries,
natures, characteristics, evolution_chains, pokemon_evolution, egg_groups,
growth_rates, experience, locations, location_areas, encounters, encounter_slots,
pokedexes, pokemon_dex_numbers, versions, version_groups, generations, regions,
languages, stats, pokeathlon_stats, nature_battle_style_preferences, ...
Use `SELECT name FROM sqlite_master WHERE type='table'` if you need the full list,
and `PRAGMA`-free introspection via `SELECT * FROM <table> LIMIT 3`.

SPECIAL MECHANICS — where the data actually lives
-------------------------------------------------
Mega Evolution   v_pokemon WHERE form_kind='mega' (form_key like 'charizard-mega-x').
                 The mega stone item is in v_item (category 'mega-stones').
Primal Reversion v_pokemon WHERE form_kind='primal'.
Gigantamax       v_pokemon WHERE form_kind='gigantamax' (form_key '...-gmax').
                 G-Max moves are in v_move WHERE move_name LIKE 'G-Max%'.
Dynamax / Max    v_move WHERE is_max_move=1 (Max Strike, Max Flare, ...).
Z-Moves          v_move WHERE is_z_move=1; the Z-crystals are items in v_item
                 (category 'z-crystals'). Base power conversion is a fixed table —
                 state it from the move data, do not invent it.
Terastallization Tera is a battle-time type override, NOT stored per Pokemon,
                 except Terapagos/Ogerpon forms (form_kind='terastal'/'alternate').
                 For Tera mechanics use the web tool; for the resulting type
                 matchups use v_type_chart.
Regional forms   form_kind LIKE 'regional-%'.
Natures          raw table `natures` (increased_stat_id / decreased_stat_id).

QUERY RECIPES
-------------
Six stats of one Pokemon:
  SELECT * FROM v_pokemon WHERE species_name='Garchomp' AND is_default_form=1;

Effective damage multiplier onto a defending pair (t1,t2). type2 may be NULL, so
COALESCE guards the mono-type case, not a missing chart row:
  COALESCE((SELECT multiplier FROM v_type_chart WHERE attacking_type=:atk
            AND defending_type=:t1),1)
* COALESCE((SELECT multiplier FROM v_type_chart WHERE attacking_type=:atk
            AND defending_type=:t2),1)

Best offensive coverage over dual typings (the shape most "which combo" questions
take). Note `a.type_id < b.type_id` to get each unordered pair once:
  WITH pair AS (SELECT a.type_name x, b.type_name y
                FROM v_type a JOIN v_type b ON a.type_id < b.type_id
                WHERE a.type_name <> 'Stellar' AND b.type_name <> 'Stellar'),
  hit AS (SELECT p.x, p.y, d.type_name d,
            MAX((SELECT multiplier FROM v_type_chart c
                  WHERE c.attacking_type=p.x AND c.defending_type=d.type_name),
                (SELECT multiplier FROM v_type_chart c
                  WHERE c.attacking_type=p.y AND c.defending_type=d.type_name)) m
          FROM pair p CROSS JOIN v_type d WHERE d.type_name <> 'Stellar')
  SELECT x, y, SUM(m > 1) AS super_effective FROM hit GROUP BY x, y
  ORDER BY super_effective DESC;

Does a Pokemon have a damaging STAB move of its own type (gen 9)?
  SELECT DISTINCT l.species_name FROM v_learnset_current l
  JOIN v_pokemon p ON p.pokemon_id=l.pokemon_id
  WHERE l.type IN (p.type1,p.type2) AND l.damage_class<>'status' AND l.power>0;
"""


def schema_doc() -> str:
    return SCHEMA_DOC


@lru_cache(maxsize=1)
def db_stats() -> dict[str, Any]:
    """Cached: the database is static, these are 7 COUNT(*) scans, and they are
    interpolated into the system prompt — recomputing changes nothing but does
    break the byte-stable prefix that provider prompt caching depends on."""
    con = _conn()

    def one(q: str) -> int:
        return con.execute(q).fetchone()[0]

    return {
        "forms": one("SELECT COUNT(*) FROM v_pokemon"),
        "species": one("SELECT COUNT(*) FROM pokemon_species"),
        "moves": one("SELECT COUNT(*) FROM v_move"),
        "items": one("SELECT COUNT(*) FROM v_item"),
        "abilities": one("SELECT COUNT(*) FROM v_ability"),
        "dex_entries": one("SELECT COUNT(*) FROM v_pokedex_entry"),
        "latest_generation": one("SELECT MAX(id) FROM generations"),
    }
