"""Jedna baza SQLite na cache metadanych (Stage 1) i historie (Stage 3).

Polaczenie otwieramy na operacje, nie trzymamy go w obiekcie. Wyglada
rozrzutnie, ale Flask chodzi z threaded=True (kazde SSE trzyma watek), a
sqlite3 domyslnie nie pozwala dzielic polaczenia miedzy watkami. Otwieranie
per-operacja jest tu najprostsze i bezpieczne — obciazenie to kilka zapytan na
klikniecie, nie ruch produkcyjny.
"""

import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS anime (
    anilist_id  INTEGER PRIMARY KEY,
    payload     TEXT    NOT NULL,
    fetched_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS search (
    query       TEXT    PRIMARY KEY,
    ids         TEXT    NOT NULL,
    fetched_at  REAL    NOT NULL
);

-- Leniwe mapowanie tytul -> zapytanie, ktore faktycznie zadzialalo w allanime
-- (docs.md D7). Zapisujemy dopiero po sukcesie, wiec to cache potwierdzen.
CREATE TABLE IF NOT EXISTS mapping (
    anilist_id      INTEGER PRIMARY KEY,
    provider_query  TEXT    NOT NULL,
    confirmed_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    uid         TEXT    NOT NULL,
    anilist_id  INTEGER NOT NULL,
    episode     INTEGER NOT NULL DEFAULT 1,
    position    INTEGER,
    duration    INTEGER,
    title       TEXT,
    cover       TEXT,
    -- Odcinek dograny do konca. Osobno od position, bo backendy bez pozycji
    -- (ani-cli, android-intent) i tak wiedza, ze odtwarzanie sie zakonczylo.
    -- NULLowalne CELOWO: NULL znaczy "nie ruszaj tej kolumny" w UPSERT-cie
    -- (COALESCE(excluded.completed, history.completed)). Z NOT NULL jawny NULL
    -- lecialby bledem, a bez NULL-a kazdy zapis postepu gasilby flage.
    completed   INTEGER DEFAULT 0,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (uid, anilist_id)
);

CREATE INDEX IF NOT EXISTS history_recent ON history (uid, updated_at DESC);
"""

# Kolumny dodane po tym, jak ktos mogl miec juz baze. SQLite nie ma
# "ADD COLUMN IF NOT EXISTS", wiec sprawdzamy sami.
MIGRATIONS = [
    ("history", "completed", "INTEGER DEFAULT 0"),
]


def connect(path):
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    # WAL: czytanie nie blokuje pisania, co przy SSE i pollingu ma znaczenie.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


@contextmanager
def transaction(path):
    """Polaczenie + commit/rollback + ZAMKNIECIE.

    Sam `with sqlite3.connect(...)` commituje, ale polaczenia nie zamyka —
    stad wyciek deskryptorow. Dlatego caly kod uzywa tego helpera, nie connect().
    """
    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_schema(path):
    with transaction(path) as connection:
        connection.executescript(SCHEMA)
        _apply_migrations(connection)


def _apply_migrations(connection):
    for table, column, definition in MIGRATIONS:
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def now():
    return time.time()
