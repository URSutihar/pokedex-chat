"""The read-only SQL layer and sprite resolution."""
from __future__ import annotations

import pytest

import pokedb


# --------------------------------------------------------------------------
# The guard must block writes without rejecting legitimate queries. The old
# regex failed the second half: it matched keywords inside string literals.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT species_name FROM v_pokemon LIMIT 1",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "SELECT 1 UNION SELECT 2",
        # keyword inside a string literal — must NOT trip the guard
        "SELECT 'we create things' AS note",
        "SELECT entry FROM v_pokedex_entry WHERE entry LIKE '%update%' LIMIT 1",
        # semicolon inside a string literal is not a second statement
        "SELECT 'a;b' AS x",
        "SELECT species_name AS update_count FROM v_pokemon LIMIT 1",
    ],
)
def test_allows_legitimate_reads(sql):
    assert pokedb.assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE pokemon",
        "DELETE FROM pokemon",
        "INSERT INTO pokemon VALUES (1)",
        "UPDATE pokemon SET id = 2",
        "CREATE TABLE t (a int)",
        "ALTER TABLE pokemon RENAME TO p",
        "PRAGMA table_info(pokemon)",
        "ATTACH DATABASE '/etc/passwd' AS leak",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE pokemon",
        "WITH t AS (SELECT 1) DELETE FROM pokemon",
    ],
)
def test_blocks_writes_and_multi_statements(sql):
    with pytest.raises(pokedb.SqlError):
        pokedb.assert_read_only(sql)


def test_empty_query_rejected():
    with pytest.raises(pokedb.SqlError):
        pokedb.assert_read_only("   ")


def test_row_cap_and_truncation_flag():
    res = pokedb.run_sql("SELECT species_name FROM v_pokemon", max_rows=5)
    assert res["row_count"] == 5
    assert res["truncated"] is True


def test_connection_is_read_only_even_if_the_guard_is_bypassed():
    """Defence in depth: mode=ro is the real guarantee, not the parser."""
    import sqlite3

    con = pokedb._conn()
    with pytest.raises(sqlite3.OperationalError):
        con.execute("CREATE TABLE should_not_exist (a int)")


# --------------------------------------------------------------------------
# Known-good facts. These double as a smoke test that the DB built correctly.
# --------------------------------------------------------------------------
def test_known_base_stats():
    res = pokedb.run_sql(
        "SELECT attack, special_attack FROM v_pokemon "
        "WHERE species_name = 'Blaziken' AND is_default_form = 1"
    )
    assert res["rows"] == [[120, 110]]


def test_type_chart_is_complete_for_the_18_battle_types():
    """The schema doc promises the model a dense 18x18 chart. Hold it to that."""
    assert pokedb.run_sql("SELECT COUNT(*) FROM v_type_chart")["rows"] == [[324]]
    res = pokedb.run_sql(
        "SELECT multiplier FROM v_type_chart "
        "WHERE attacking_type = 'Water' AND defending_type = 'Fire'"
    )
    assert res["rows"] == [[2.0]]
    # 1x pairs are present, not implied by absence
    res = pokedb.run_sql(
        "SELECT multiplier FROM v_type_chart "
        "WHERE attacking_type = 'Normal' AND defending_type = 'Water'"
    )
    assert res["rows"] == [[1.0]]


def test_stellar_is_the_documented_gap():
    """The one attacking type with no rows — the doc warns about it explicitly."""
    res = pokedb.run_sql(
        "SELECT type_name FROM v_type WHERE type_name NOT IN "
        "(SELECT DISTINCT attacking_type FROM v_type_chart)"
    )
    assert res["rows"] == [["Stellar"]]
    assert "Stellar has no rows" in pokedb.schema_doc()


def test_schema_doc_matches_reality_on_row_count():
    assert "324" in pokedb.schema_doc()


def test_form_kinds_present():
    res = pokedb.run_sql(
        "SELECT form_kind, COUNT(*) FROM v_pokemon "
        "WHERE form_kind IN ('mega','gigantamax','primal') GROUP BY form_kind ORDER BY 1"
    )
    kinds = {r[0]: r[1] for r in res["rows"]}
    assert kinds["mega"] > 40 and kinds["gigantamax"] > 20 and kinds["primal"] >= 2


# --------------------------------------------------------------------------
# Sprites
# --------------------------------------------------------------------------
def test_resolve_sprite_by_name_and_form():
    assert pokedb.resolve_sprite("Charizard")["found"]
    r = pokedb.resolve_sprite("charizard-mega-y")
    assert r["found"] and r["form_key"] == "charizard-mega-y"
    assert r["image_url"].startswith("/sprites/")


def test_resolve_sprite_unknown_is_not_found():
    assert pokedb.resolve_sprite("definitely-not-a-pokemon-xyz")["found"] is False


@pytest.mark.parametrize(
    "name",
    ["../../../etc/passwd", "..%2f..%2fetc", "a/../../b", "....//....//x", "life orb/../../.."],
)
def test_item_sprite_rejects_traversal(name):
    """The identifier reaches a path join, so it is allowlisted, not merely escaped."""
    res = pokedb.resolve_item_sprite(name)
    assert res["found"] is False
    assert "image_url" not in res


def test_item_sprite_happy_path():
    res = pokedb.resolve_item_sprite("Life Orb")
    assert res["found"] and res["image_url"] == "/sprites/items/life-orb.png"


def test_db_stats_is_memoised():
    """It feeds the system prompt; an unstable prompt never hits a provider cache."""
    a, b = pokedb.db_stats(), pokedb.db_stats()
    assert a is b
