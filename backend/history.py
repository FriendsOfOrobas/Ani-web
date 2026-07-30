"""Historia ogladania i wznawianie — Stage 3 (docs.md D8).

Klucz to (uid, anilist_id), gdzie uid pochodzi z ciasteczka. Zadnych kont,
zadnej autoryzacji — docs.md §8.

Pozycje w czasie zapisujemy TYLKO gdy odtwarzacz ja raportuje
(`capabilities.position`, czyli dzis wylacznie mpv-ipc). Dla ani-cli i
android-intent zostaje sam numer odcinka — i to jest uczciwe, bo `dumpsys`
nie daje plynnej pozycji (docs.md D2). Wznawianie jest wtedy „od odcinka N",
nie „od 12:47".
"""

from db import now, transaction

# Ponizej tego progu nie ma sensu wznawiac — user dopiero wszedl.
MIN_RESUME_SECONDS = 30

# Tyle przed koncem uznajemy odcinek za dokonczony i proponujemy nastepny.
END_MARGIN_SECONDS = 90


class HistoryStore:
    def __init__(self, db_path):
        self.db_path = db_path

    def record(
        self,
        uid,
        anilist_id,
        episode,
        position=None,
        duration=None,
        title=None,
        cover=None,
        completed=None,
    ):
        """Zapisuje postep. Pola opcjonalne nie nadpisuja istniejacych wartosci.

        COALESCE na excluded.* jest tu celowo: `stop` z backendu bez pozycji nie
        moze wyzerowac pozycji zapisanej wczesniej przez backend, ktory ja zna.
        """
        with transaction(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO history (uid, anilist_id, episode, position, duration,
                                     title, cover, completed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(uid, anilist_id) DO UPDATE SET
                    episode    = excluded.episode,
                    position   = COALESCE(excluded.position, history.position),
                    duration   = COALESCE(excluded.duration, history.duration),
                    title      = COALESCE(excluded.title, history.title),
                    cover      = COALESCE(excluded.cover, history.cover),
                    completed  = COALESCE(excluded.completed, history.completed),
                    updated_at = excluded.updated_at
                """,
                (
                    uid,
                    int(anilist_id),
                    int(episode),
                    None if position is None else int(position),
                    None if duration is None else int(duration),
                    title,
                    cover,
                    None if completed is None else int(bool(completed)),
                    now(),
                ),
            )
        return self.entry(uid, anilist_id)

    def mark_completed(self, uid, anilist_id, episode, position=None, duration=None):
        """Odcinek dogral sie do konca.

        Wolane przez dozorce stanu, gdy odtwarzacz przeszedl z grania w koniec.
        Dziala takze dla backendow bez pozycji — one wiedza, ze sie skonczylo,
        tylko nie wiedza, na ktorej sekundzie.
        """
        return self.record(
            uid,
            anilist_id,
            episode,
            position=position,
            duration=duration,
            completed=True,
        )

    def clear_position(self, uid, anilist_id):
        """Nowy odcinek zaczyna sie od zera — stara pozycja nie moze zostac."""
        with transaction(self.db_path) as connection:
            connection.execute(
                "UPDATE history SET position = NULL, duration = NULL, completed = 0, "
                "updated_at = ? WHERE uid = ? AND anilist_id = ?",
                (now(), uid, int(anilist_id)),
            )

    def entry(self, uid, anilist_id):
        with transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT * FROM history WHERE uid = ? AND anilist_id = ?",
                (uid, int(anilist_id)),
            ).fetchone()
        return _as_dict(row)

    def recent(self, uid, limit=20):
        with transaction(self.db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM history WHERE uid = ? ORDER BY updated_at DESC LIMIT ?",
                (uid, int(limit)),
            ).fetchall()
        return [_as_dict(row) for row in rows]

    def delete(self, uid, anilist_id):
        with transaction(self.db_path) as connection:
            cursor = connection.execute(
                "DELETE FROM history WHERE uid = ? AND anilist_id = ?", (uid, int(anilist_id))
            )
        return cursor.rowcount > 0

    def resume(self, uid, anilist_id, total_episodes=0):
        """Od czego kontynuowac. Zawsze zwraca cos sensownego, tez dla nowego anime."""
        entry = self.entry(uid, anilist_id)
        if entry is None:
            return {"episode": 1, "position": 0, "reason": "nowe"}

        episode = entry["episode"]
        position = entry["position"] or 0
        duration = entry["duration"] or 0

        # Dwie drogi do "dokonczone": jawna flaga od dozorcy (dziala dla kazdego
        # backendu) albo pozycja blisko konca (tylko gdy znamy czas).
        finished = bool(entry["completed"]) or (
            duration and position >= duration - END_MARGIN_SECONDS
        )
        if finished:
            has_next = not total_episodes or episode < total_episodes
            if has_next:
                return {"episode": episode + 1, "position": 0, "reason": "nastepny odcinek"}
            return {"episode": episode, "position": 0, "reason": "koniec serii"}

        if position >= MIN_RESUME_SECONDS:
            return {"episode": episode, "position": position, "reason": "wznowienie"}

        return {"episode": episode, "position": 0, "reason": "od poczatku odcinka"}


def _as_dict(row):
    return dict(row) if row is not None else None
