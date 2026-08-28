"""
python -m backend.scripts.seed_anon_test_db

One-shot, idempotent seeder for the anon-mapping test database
(backend/src/identity/anon.py's `key_value` table) that
backend/tests/test_*_anon_boundary.py and friends run against.

This is the piece docs/plans/pii-boundary-test-suite.md §E calls out as
missing: today's anon_test Postgres (port 55433) has no in-repo seed script
at all -- every test file that needs it (see the `os.environ["ANON_DB_*"]`
block + `ALTER TABLE key_value ADD COLUMN IF NOT EXISTS date_perturbation`
pattern repeated at the top of each) has so far assumed a hand-seeded
database, which a CI runner can't reproduce. This script is that
reproducible seed, run once by .github/workflows/test.yml before pytest.

Deliberately NOT backend/scripts/dev_seed.py's anon-db seed: that one seeds
fake MRNs (9000001..9000006 -> anon ids 1001..1006) for exercising the
local docker-compose.dev.yml stack by hand. This script seeds the SPECIFIC
fixed real<->anon id pairs the test suite itself hardcodes throughout
(REAL_MRN = "500123" / ANON_MRN = "1001", REAL_MRN_2 = "500456" /
ANON_MRN_2 = "1002" -- see e.g. backend/tests/test_anon.py, and every
test_*_anon_boundary.py file) plus the one row test_anon.py's
test_lookup_real_ids_ignores_wrong_key_type needs (an id that exists, but
under a key_type_id other than 1, to prove the lookup query's own
`AND key_type_id = 1` filter actually excludes it). `date_perturbation` is
deliberately left NULL here for every row -- every test that needs a
non-zero perturbation seeds and resets it itself, per-test, via its own
`perturbation` fixture (see test_anon_date_shift.py's own header comment
for why: this script didn't exist yet when that convention was set, so
each test file stayed self-contained rather than assuming it).

Connects using the same ANON_DB_* environment variables
backend/src/identity/anon.py itself reads (see .env.example) -- run with
the test database's own values, e.g.:

    ANON_DB_HOST=localhost ANON_DB_PORT=55433 ANON_DB_NAME=anon_test \\
    ANON_DB_USER=postgres ANON_DB_PASS=test \\
    python -m backend.scripts.seed_anon_test_db
"""
import os

import psycopg2

# (anon_id, real_id) pairs, key_type_id=1 -- the ordinary id-mapping rows
# every test_*_anon_boundary.py file and test_anon.py itself hardcode.
ID_PAIRS = [
    (1001, 500123),
    (1002, 500456),
]

# test_anon.py's test_lookup_real_ids_ignores_wrong_key_type: this row
# exists (patient_id=9999) but under key_type_id=2, not 1 -- proving
# lookup_real_ids' `AND key_type_id = 1` filter actually excludes a row
# that would otherwise match on patient_id alone.
WRONG_KEY_TYPE_ROW = (9999, 999999, 2)


def seed(conn) -> None:
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS key_value (
                id SERIAL PRIMARY KEY,
                patient_id BIGINT NOT NULL,
                key_value BIGINT NOT NULL,
                key_type_id INT NOT NULL,
                date_perturbation INT
            )
            """
        )
        # Covers a database created by an older version of this script (or
        # by hand) before date_perturbation existed -- CREATE TABLE IF NOT
        # EXISTS above is a no-op against an already-existing table, so the
        # column would otherwise never get added.
        cur.execute("ALTER TABLE key_value ADD COLUMN IF NOT EXISTS date_perturbation INT")

        cur.execute("SELECT COUNT(*) FROM key_value WHERE key_type_id = 1 AND patient_id = ANY(%s)",
                     ([anon_id for anon_id, _ in ID_PAIRS],))
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO key_value (patient_id, key_value, key_type_id) VALUES (%s, %s, 1)",
                ID_PAIRS,
            )

        cur.execute(
            "SELECT COUNT(*) FROM key_value WHERE patient_id = %s AND key_type_id = %s",
            (WRONG_KEY_TYPE_ROW[0], WRONG_KEY_TYPE_ROW[2]),
        )
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO key_value (patient_id, key_value, key_type_id) VALUES (%s, %s, %s)",
                WRONG_KEY_TYPE_ROW,
            )


def main() -> None:
    conn = psycopg2.connect(
        host=os.environ["ANON_DB_HOST"],
        port=os.getenv("ANON_DB_PORT", "5432"),
        dbname=os.environ["ANON_DB_NAME"],
        user=os.environ["ANON_DB_USER"],
        password=os.environ["ANON_DB_PASS"],
    )
    try:
        seed(conn)
    finally:
        conn.close()
    print(f"Seeded anon-test DB: {', '.join(f'{a}->{r}' for a, r in ID_PAIRS)}, "
          f"plus wrong-key-type row {WRONG_KEY_TYPE_ROW[0]} (key_type_id={WRONG_KEY_TYPE_ROW[2]})")


if __name__ == "__main__":
    main()
