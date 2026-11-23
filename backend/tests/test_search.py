"""Typo correction on a synthetic vocabulary, independent of any built database."""
import sqlite3

import pytest

import search


def _load(vocab: dict[str, int]) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE search_vocab (term TEXT, df INTEGER)")
    conn.executemany("INSERT INTO search_vocab VALUES (?,?)", vocab.items())
    search.load_vocab(conn)


@pytest.fixture(autouse=True)
def _restore_vocab():
    saved_vocab, saved_tri = dict(search._vocab), dict(search._trigrams)
    yield
    search._vocab.clear(); search._vocab.update(saved_vocab)
    search._trigrams.clear(); search._trigrams.update(saved_tri)


def test_transposition_beats_rare_deletion_even_past_the_candidate_cap():
    # "mlik" shares only the leading-"m" trigram with "milk". 200 other m-words
    # tie with it on overlap, which used to push "milk" past the 60-candidate cap
    # so the rare one-deletion neighbour "mik" won instead.
    # Fillers first, so an arbitrary tie order would cut "milk" at the cap.
    vocab = {f"m{i:03d}x": 5 for i in range(200)}
    vocab.update({"milk": 1000, "mik": 1})
    _load(vocab)
    assert search.correct("mlik") == "milk"


def test_common_word_survives_overlap_ties():
    # "xbcd" -> "abcd" is one substitution sharing two trigrams ("bcd", "cd ").
    # 200 rarer words tie with it on those two, and are inserted first, so an
    # arbitrary tie order would cut "abcd" at the cap; frequency must break it.
    vocab = {f"q{i:03d}bcd": 3 for i in range(200)}
    vocab["abcd"] = 900
    _load(vocab)
    assert search.correct("xbcd") == "abcd"


def test_known_common_word_is_left_alone():
    _load({"milk": 1000, "mild": 50})
    assert search.correct("milk") is None
