#!/usr/bin/env python3
"""
Build a single self-contained SQLite Pokedex from the PokeAPI/veekun CSV dataset.

  python build_db.py            # uses ./_src_pokeapi/data/v2/csv -> ./data/pokedex.sqlite

Every CSV becomes a raw table (typed by inference).  On top of that we create a
set of denormalised, English-labelled views that an LLM can query without having
to join eight tables to answer "what is Charizard's base Attack".
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CSV_DIR = Path(os.environ.get("POKEAPI_CSV", ROOT / "data" / "csv"))
DB_PATH = Path(os.environ.get("POKEDEX_DB", ROOT / "data" / "pokedex.sqlite"))

INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?\d+\.\d+$")

# CSVs that are dead weight for a battle/lore assistant (spin-off games, huge maps)
SKIP_PREFIXES = ("conquest_", "encounter_condition_value_map", "pal_park", "contest_combos")


def infer_types(rows: list[list[str]], ncols: int) -> list[str]:
    kinds = ["INTEGER"] * ncols
    for row in rows:
        for i in range(min(ncols, len(row))):
            v = row[i]
            if v == "":
                continue
            k = kinds[i]
            if k == "INTEGER":
                if INT_RE.match(v):
                    continue
                kinds[i] = "REAL" if FLOAT_RE.match(v) else "TEXT"
            elif k == "REAL":
                if INT_RE.match(v) or FLOAT_RE.match(v):
                    continue
                kinds[i] = "TEXT"
    return kinds


def load_csvs(con: sqlite3.Connection) -> None:
    files = sorted(p for p in CSV_DIR.glob("*.csv"))
    loaded = 0
    for path in files:
        name = path.stem
        if any(name.startswith(p) for p in SKIP_PREFIXES):
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                continue
            rows = [r for r in reader if r]
        cols = [re.sub(r"\W", "_", c) for c in header]
        kinds = infer_types(rows, len(cols))
        coldef = ", ".join(f'"{c}" {k}' for c, k in zip(cols, kinds, strict=True))
        con.execute(f'DROP TABLE IF EXISTS "{name}"')
        con.execute(f'CREATE TABLE "{name}" ({coldef})')
        ph = ",".join("?" * len(cols))
        clean = [
            [(None if v == "" else v) for v in (r + [""] * (len(cols) - len(r)))[: len(cols)]]
            for r in rows
        ]
        con.executemany(f'INSERT INTO "{name}" VALUES ({ph})', clean)
        loaded += 1
    con.commit()
    print(f"  loaded {loaded} tables")


INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_pkmn_species ON pokemon(species_id)",
    "CREATE INDEX IF NOT EXISTS ix_pkmn_ident ON pokemon(identifier)",
    "CREATE INDEX IF NOT EXISTS ix_stats_pk ON pokemon_stats(pokemon_id)",
    "CREATE INDEX IF NOT EXISTS ix_types_pk ON pokemon_types(pokemon_id)",
    "CREATE INDEX IF NOT EXISTS ix_abil_pk ON pokemon_abilities(pokemon_id)",
    "CREATE INDEX IF NOT EXISTS ix_pm_pk ON pokemon_moves(pokemon_id)",
    "CREATE INDEX IF NOT EXISTS ix_pm_move ON pokemon_moves(move_id)",
    "CREATE INDEX IF NOT EXISTS ix_pm_vg ON pokemon_moves(version_group_id)",
    "CREATE INDEX IF NOT EXISTS ix_evo_species ON pokemon_evolution(evolved_species_id)",
    "CREATE INDEX IF NOT EXISTS ix_species_chain ON pokemon_species(evolution_chain_id)",
    "CREATE INDEX IF NOT EXISTS ix_flavor_species ON pokemon_species_flavor_text(species_id)",
    "CREATE INDEX IF NOT EXISTS ix_te ON type_efficacy(damage_type_id, target_type_id)",
]

# ---------------------------------------------------------------------------
# Views.  EN = language_id 9.
# ---------------------------------------------------------------------------
VIEWS = r"""
DROP VIEW IF EXISTS v_type;
CREATE VIEW v_type AS
SELECT t.id AS type_id,
       t.identifier AS type_key,
       UPPER(SUBSTR(t.identifier,1,1)) || SUBSTR(t.identifier,2) AS type_name,
       t.generation_id
FROM types t
WHERE t.identifier NOT IN ('unknown','shadow');

DROP VIEW IF EXISTS v_type_chart;
CREATE VIEW v_type_chart AS
SELECT a.type_name AS attacking_type,
       d.type_name AS defending_type,
       te.damage_factor / 100.0 AS multiplier
FROM type_efficacy te
JOIN v_type a ON a.type_id = te.damage_type_id
JOIN v_type d ON d.type_id = te.target_type_id;

DROP VIEW IF EXISTS v_stat;
CREATE VIEW v_stat AS
SELECT p.id AS pokemon_id,
       MAX(CASE WHEN s.identifier='hp' THEN ps.base_stat END)              AS hp,
       MAX(CASE WHEN s.identifier='attack' THEN ps.base_stat END)          AS attack,
       MAX(CASE WHEN s.identifier='defense' THEN ps.base_stat END)         AS defense,
       MAX(CASE WHEN s.identifier='special-attack' THEN ps.base_stat END)  AS special_attack,
       MAX(CASE WHEN s.identifier='special-defense' THEN ps.base_stat END) AS special_defense,
       MAX(CASE WHEN s.identifier='speed' THEN ps.base_stat END)           AS speed,
       SUM(CASE WHEN s.identifier IN ('hp','attack','defense','special-attack',
                                      'special-defense','speed')
           THEN ps.base_stat ELSE 0 END)                                   AS bst
FROM pokemon p
JOIN pokemon_stats ps ON ps.pokemon_id = p.id
JOIN stats s ON s.id = ps.stat_id
GROUP BY p.id;

-- One row per playable Pokemon *form* (base, mega, gmax, regional, battle-only...).
DROP VIEW IF EXISTS v_pokemon;
CREATE VIEW v_pokemon AS
SELECT
  p.id                                   AS pokemon_id,
  p.identifier                           AS form_key,
  COALESCE(sn.name, sp.identifier)       AS species_name,
  sp.id                                  AS species_id,
  sn.genus                               AS genus,
  sp.generation_id                       AS generation,
  p.is_default                           AS is_default_form,
  CASE
    WHEN pf.is_mega = 1 OR p.identifier LIKE '%-mega'
      OR p.identifier LIKE '%-mega-x' OR p.identifier LIKE '%-mega-y' THEN 'mega'
    WHEN p.identifier LIKE '%-primal'  THEN 'primal'
    WHEN p.identifier LIKE '%-gmax'    THEN 'gigantamax'
    WHEN p.identifier LIKE '%-eternamax' THEN 'eternamax'
    WHEN p.identifier LIKE '%-alola'   THEN 'regional-alolan'
    WHEN p.identifier LIKE '%-galar'   THEN 'regional-galarian'
    WHEN p.identifier LIKE '%-hisui'   THEN 'regional-hisuian'
    WHEN p.identifier LIKE '%-paldea'  THEN 'regional-paldean'
    WHEN p.identifier LIKE '%-totem%'  THEN 'totem'
    WHEN p.identifier LIKE '%-terastal%' OR p.identifier LIKE '%-stellar%' THEN 'terastal'
    WHEN p.is_default = 1              THEN 'base'
    ELSE 'alternate'
  END                                    AS form_kind,
  t1.type_name                           AS type1,
  t2.type_name                           AS type2,
  st.hp, st.attack, st.defense, st.special_attack, st.special_defense, st.speed, st.bst,
  p.height / 10.0                        AS height_m,
  p.weight / 10.0                        AS weight_kg,
  p.base_experience,
  sp.capture_rate,
  sp.base_happiness,
  sp.gender_rate,                        -- -1 genderless, else female chance in eighths
  sp.hatch_counter,
  gr.identifier                          AS growth_rate,
  sp.is_legendary, sp.is_mythical, sp.is_baby,
  sp.evolves_from_species_id,
  sp.evolution_chain_id,
  cl.identifier                          AS color,
  sh.identifier                          AS shape,
  hb.identifier                          AS habitat
FROM pokemon p
JOIN pokemon_species sp ON sp.id = p.species_id
LEFT JOIN pokemon_species_names sn ON sn.pokemon_species_id = sp.id AND sn.local_language_id = 9
LEFT JOIN pokemon_forms pf ON pf.pokemon_id = p.id AND pf.is_default = 1
LEFT JOIN v_stat st ON st.pokemon_id = p.id
LEFT JOIN pokemon_types pt1 ON pt1.pokemon_id = p.id AND pt1.slot = 1
LEFT JOIN pokemon_types pt2 ON pt2.pokemon_id = p.id AND pt2.slot = 2
LEFT JOIN v_type t1 ON t1.type_id = pt1.type_id
LEFT JOIN v_type t2 ON t2.type_id = pt2.type_id
LEFT JOIN growth_rates gr ON gr.id = sp.growth_rate_id
LEFT JOIN pokemon_colors cl ON cl.id = sp.color_id
LEFT JOIN pokemon_shapes sh ON sh.id = sp.shape_id
LEFT JOIN pokemon_habitats hb ON hb.id = sp.habitat_id;

DROP VIEW IF EXISTS v_ability;
CREATE VIEW v_ability AS
SELECT a.id AS ability_id,
       COALESCE(an.name, a.identifier) AS ability_name,
       a.identifier AS ability_key,
       a.generation_id AS generation,
       ap.short_effect, ap.effect
FROM abilities a
LEFT JOIN ability_names an ON an.ability_id = a.id AND an.local_language_id = 9
LEFT JOIN ability_prose ap ON ap.ability_id = a.id AND ap.local_language_id = 9;

DROP VIEW IF EXISTS v_pokemon_ability;
CREATE VIEW v_pokemon_ability AS
SELECT pa.pokemon_id,
       vp.form_key, vp.species_name,
       va.ability_name, va.ability_key,
       pa.is_hidden, pa.slot,
       va.short_effect
FROM pokemon_abilities pa
JOIN v_pokemon vp ON vp.pokemon_id = pa.pokemon_id
JOIN v_ability va ON va.ability_id = pa.ability_id;

DROP VIEW IF EXISTS v_move;
CREATE VIEW v_move AS
SELECT m.id AS move_id,
       COALESCE(mn.name, m.identifier) AS move_name,
       m.identifier AS move_key,
       t.type_name AS type,
       dc.identifier AS damage_class,      -- physical / special / status
       m.power, m.accuracy, m.pp, m.priority,
       m.generation_id AS generation,
       mt.identifier AS target,
       mep.short_effect, m.effect_chance,
       mm.crit_rate, mm.drain, mm.healing, mm.min_hits, mm.max_hits,
       mm.flinch_chance, mm.stat_chance, mm.ailment_chance,
       ma.identifier AS ailment,
       mc.identifier AS meta_category,
       CASE WHEN m.id BETWEEN 622 AND 658 THEN 1 ELSE 0 END AS is_z_move,
       CASE WHEN m.id BETWEEN 757 AND 774 THEN 1 ELSE 0 END AS is_max_move,
       (SELECT GROUP_CONCAT(mf.identifier, ',')
          FROM move_flag_map mfm JOIN move_flags mf ON mf.id = mfm.move_flag_id
         WHERE mfm.move_id = m.id) AS flags
FROM moves m
LEFT JOIN move_names mn ON mn.move_id = m.id AND mn.local_language_id = 9
LEFT JOIN v_type t ON t.type_id = m.type_id
LEFT JOIN move_damage_classes dc ON dc.id = m.damage_class_id
LEFT JOIN move_targets mt ON mt.id = m.target_id
LEFT JOIN move_effect_prose mep ON mep.move_effect_id = m.effect_id AND mep.local_language_id = 9
LEFT JOIN move_meta mm ON mm.move_id = m.id
LEFT JOIN move_meta_ailments ma ON ma.id = mm.meta_ailment_id
LEFT JOIN move_meta_categories mc ON mc.id = mm.meta_category_id;

-- Learnset.  version_group tells you which game; method tells you how.
DROP VIEW IF EXISTS v_learnset;
CREATE VIEW v_learnset AS
SELECT pm.pokemon_id,
       vp.form_key, vp.species_name,
       vm.move_name, vm.move_key, vm.type, vm.damage_class, vm.power, vm.accuracy, vm.pp,
       vg.identifier AS version_group,
       vg.generation_id AS generation,
       mm.identifier AS learn_method,
       pm.level
FROM pokemon_moves pm
JOIN v_pokemon vp ON vp.pokemon_id = pm.pokemon_id
JOIN v_move vm ON vm.move_id = pm.move_id
JOIN version_groups vg ON vg.id = pm.version_group_id
JOIN pokemon_move_methods mm ON mm.id = pm.pokemon_move_method_id;

-- Latest-generation learnset only (much smaller; use this unless asked about old games)
DROP VIEW IF EXISTS v_learnset_current;
CREATE VIEW v_learnset_current AS
SELECT * FROM v_learnset
WHERE version_group IN (SELECT identifier FROM version_groups WHERE generation_id = 9);

DROP VIEW IF EXISTS v_evolution;
CREATE VIEW v_evolution AS
SELECT
  fromsn.name  AS from_species,
  pe.evolved_species_id,
  tosn.name    AS to_species,
  sp.evolution_chain_id,
  et.identifier AS trigger,
  pe.minimum_level,
  ti.identifier AS trigger_item,
  hi.identifier AS held_item,
  pe.time_of_day,
  km.identifier AS known_move,
  kt.identifier AS known_move_type,
  pe.minimum_happiness, pe.minimum_beauty, pe.minimum_affection,
  pe.relative_physical_stats,
  ps.identifier AS party_species,
  pt.identifier AS party_type,
  ts.identifier AS trade_species,
  loc.identifier AS location,
  pe.gender_id, pe.needs_overworld_rain, pe.turn_upside_down,
  pe.needs_multiplayer, pe.near_special_rock, pe.minimum_steps, pe.minimum_damage_taken,
  vg.identifier AS version_group
FROM pokemon_evolution pe
JOIN pokemon_species sp ON sp.id = pe.evolved_species_id
LEFT JOIN pokemon_species_names tosn   ON tosn.pokemon_species_id = sp.id AND tosn.local_language_id = 9
LEFT JOIN pokemon_species_names fromsn ON fromsn.pokemon_species_id = sp.evolves_from_species_id AND fromsn.local_language_id = 9
LEFT JOIN evolution_triggers et ON et.id = pe.evolution_trigger_id
LEFT JOIN items ti  ON ti.id = pe.trigger_item_id
LEFT JOIN items hi  ON hi.id = pe.held_item_id
LEFT JOIN moves km  ON km.id = pe.known_move_id
LEFT JOIN types kt  ON kt.id = pe.known_move_type_id
LEFT JOIN pokemon_species ps ON ps.id = pe.party_species_id
LEFT JOIN types pt  ON pt.id = pe.party_type_id
LEFT JOIN pokemon_species ts ON ts.id = pe.trade_species_id
LEFT JOIN locations loc ON loc.id = pe.location_id
LEFT JOIN version_groups vg ON vg.id = pe.version_group_id;

-- Position of a species inside its evolution family.
-- stage 1 = unevolved, 2 = first evolution, 3 = second evolution.
-- is_final_evolution = nothing evolves from it.
DROP VIEW IF EXISTS v_evolution_stage;
CREATE VIEW v_evolution_stage AS
SELECT sp.id AS species_id,
       COALESCE(sn.name, sp.identifier) AS species_name,
       sp.evolution_chain_id,
       (SELECT COUNT(*) FROM pokemon_species x WHERE x.evolution_chain_id = sp.evolution_chain_id) AS family_size,
       CASE WHEN sp.evolves_from_species_id IS NULL THEN 1
            WHEN (SELECT p2.evolves_from_species_id FROM pokemon_species p2 WHERE p2.id = sp.evolves_from_species_id) IS NULL THEN 2
            ELSE 3 END AS stage,
       CASE WHEN NOT EXISTS (SELECT 1 FROM pokemon_species c WHERE c.evolves_from_species_id = sp.id)
            THEN 1 ELSE 0 END AS is_final_evolution,
       sp.is_baby
FROM pokemon_species sp
LEFT JOIN pokemon_species_names sn ON sn.pokemon_species_id = sp.id AND sn.local_language_id = 9;

DROP VIEW IF EXISTS v_item;
CREATE VIEW v_item AS
SELECT i.id AS item_id,
       COALESCE(inm.name, i.identifier) AS item_name,
       i.identifier AS item_key,
       ic.identifier AS category,
       ip.identifier AS pocket,
       i.cost, i.fling_power,
       ipr.short_effect, ipr.effect
FROM items i
LEFT JOIN item_names inm ON inm.item_id = i.id AND inm.local_language_id = 9
LEFT JOIN item_categories ic ON ic.id = i.category_id
LEFT JOIN item_pockets ip ON ip.id = ic.pocket_id
LEFT JOIN item_prose ipr ON ipr.item_id = i.id AND ipr.local_language_id = 9;

DROP VIEW IF EXISTS v_pokedex_entry;
CREATE VIEW v_pokedex_entry AS
SELECT f.species_id,
       COALESCE(sn.name, sp.identifier) AS species_name,
       v.identifier AS version,
       vg.generation_id AS generation,
       REPLACE(REPLACE(f.flavor_text, CHAR(10), ' '), CHAR(12), ' ') AS entry
FROM pokemon_species_flavor_text f
JOIN pokemon_species sp ON sp.id = f.species_id
LEFT JOIN pokemon_species_names sn ON sn.pokemon_species_id = sp.id AND sn.local_language_id = 9
JOIN versions v ON v.id = f.version_id
JOIN version_groups vg ON vg.id = v.version_group_id
WHERE f.language_id = 9;

DROP VIEW IF EXISTS v_egg_group;
CREATE VIEW v_egg_group AS
SELECT peg.species_id,
       COALESCE(sn.name, sp.identifier) AS species_name,
       COALESCE(egn.name, eg.identifier) AS egg_group
FROM pokemon_egg_groups peg
JOIN pokemon_species sp ON sp.id = peg.species_id
LEFT JOIN pokemon_species_names sn ON sn.pokemon_species_id = sp.id AND sn.local_language_id = 9
JOIN egg_groups eg ON eg.id = peg.egg_group_id
LEFT JOIN egg_group_prose egn ON egn.egg_group_id = eg.id AND egn.local_language_id = 9;

-- Starters, tagged by generation, because "starter" is not a field in any dataset.
DROP VIEW IF EXISTS v_starter;
CREATE VIEW v_starter AS
SELECT es.species_id, es.species_name, es.stage, es.is_final_evolution,
       s.generation_id AS generation
FROM v_evolution_stage es
JOIN pokemon_species s ON s.id = es.species_id
WHERE es.evolution_chain_id IN (
  SELECT evolution_chain_id FROM pokemon_species WHERE id IN (
    1,4,7,        152,155,158,   252,255,258,   387,390,393,
    495,498,501,  650,653,656,   722,725,728,   810,813,816,
    906,909,912
  )
);

-- Machines (TM/TR/HM) per game.
DROP VIEW IF EXISTS v_machine;
CREATE VIEW v_machine AS
SELECT m.machine_number,
       COALESCE(inm.name, i.identifier) AS machine_item,
       vm.move_name, vm.type, vm.damage_class, vm.power, vm.accuracy,
       vg.identifier AS version_group,
       vg.generation_id AS generation
FROM machines m
JOIN v_move vm ON vm.move_id = m.move_id
JOIN version_groups vg ON vg.id = m.version_group_id
LEFT JOIN items i ON i.id = m.item_id
LEFT JOIN item_names inm ON inm.item_id = i.id AND inm.local_language_id = 9;
"""


def build_fts(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        DROP TABLE IF EXISTS fts_pokedex;
        CREATE VIRTUAL TABLE fts_pokedex USING fts5(species_name, version, entry);
        INSERT INTO fts_pokedex SELECT species_name, version, entry FROM v_pokedex_entry;

        DROP TABLE IF EXISTS fts_effect;
        CREATE VIRTUAL TABLE fts_effect USING fts5(kind, name, short_effect, effect);
        INSERT INTO fts_effect
            SELECT 'move', move_name, short_effect, '' FROM v_move
            UNION ALL SELECT 'ability', ability_name, short_effect, effect FROM v_ability
            UNION ALL SELECT 'item', item_name, short_effect, effect FROM v_item;
        """
    )
    con.commit()


def main() -> int:
    if not CSV_DIR.is_dir():
        print(f"CSV dir not found: {CSV_DIR}", file=sys.stderr)
        return 1
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    print(f"building {DB_PATH}")
    load_csvs(con)
    for stmt in INDEXES:
        con.execute(stmt)
    con.commit()
    print("  creating views")
    con.executescript(VIEWS)
    con.commit()
    print("  creating full-text indexes")
    build_fts(con)
    con.execute("ANALYZE")
    con.commit()

    n_forms = con.execute("SELECT COUNT(*) FROM v_pokemon").fetchone()[0]
    n_moves = con.execute("SELECT COUNT(*) FROM v_move").fetchone()[0]
    n_items = con.execute("SELECT COUNT(*) FROM v_item").fetchone()[0]
    n_entry = con.execute("SELECT COUNT(*) FROM v_pokedex_entry").fetchone()[0]
    n_mega = con.execute("SELECT COUNT(*) FROM v_pokemon WHERE form_kind='mega'").fetchone()[0]
    n_gmax = con.execute("SELECT COUNT(*) FROM v_pokemon WHERE form_kind='gigantamax'").fetchone()[0]
    con.close()
    size = DB_PATH.stat().st_size / 1e6
    print(
        f"done: {n_forms} forms ({n_mega} mega, {n_gmax} gigantamax), "
        f"{n_moves} moves, {n_items} items, {n_entry} dex entries, {size:.0f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
