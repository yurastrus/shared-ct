# SPDX-License-Identifier: AGPL-3.0-only
"""The one rule that decides which species a series (observation) contains.

Until 2026-10 a verifier gave exactly one species per series, so "the species
of a series" was the single top-voted one (`ROW_NUMBER() ... rn = 1`). Since a
verifier may now name several species (a fox and a badger in the same frames),
a series can hold several ACCEPTED species:

    accepted = the top-ranked species (the old winner, always kept)
             + every other species named by MORE THAN HALF of the series' verifiers

Why the old winner stays unconditionally: 395 closed series on production
(2026-10-29) were closed without a strict majority (imports, moderator votes,
older rules). Requiring a majority for the winner too would silently drop them
from every statistic.

Why this changes nothing for single-species data: when every verifier names one
species, the votes of all species add up to the number of verifiers, so a
second species can never hold more than half of them. The accepted set is then
exactly {old winner}, whatever tie-break a page uses.

Votes are PEOPLE, not rows: identifications are copied onto every photo of the
series, and one person may now have several rows per photo (one per species).

Both the SQL fragment and the Python function below implement the same rule;
tests pin them to each other.
"""

# Rank order used when a caller gives none. species_id DESC is the last key
# so that a tie on votes and count always resolves the same way (a real
# species before the negative service categories), as the top-species chart
# already did. Without it ROW_NUMBER picked arbitrarily among the ~900 tied
# series on production (2026-10), and any change of query plan reshuffled
# about 500 of them. Pages with an established tie-break pass their own.
DEFAULT_RANK_ORDER = 'vote_count DESC, max_quantity DESC, species_id DESC'


def consensus_ctes(source_sql, rank_order=DEFAULT_RANK_ORDER):
    """CTE definitions ending in ``RankedConsensus``, without a leading WITH.

    ``source_sql`` is the FROM/JOIN/WHERE fragment that yields the
    identifications to count, with the aliases ``i`` (identifications) and
    ``p`` (photos), e.g.::

        FROM identifications i JOIN photos p ON i.photo_id = p.id

    ``RankedConsensus`` has one row per (series, species) with the columns
    observation_id, species_id, vote_count, max_quantity, voters, rn, accepted.
    Filter on ``accepted`` to get every species of a series; ``rn = 1`` is the
    single top-ranked one (the primary species).
    """
    return f"""
        ObservationVotes AS (
            -- one row per (series, verifier, species): rows per photo collapse here
            SELECT p.observation_id, i.user_id, i.species_id,
                   MAX(i.quantity) AS quantity
            {source_sql}
            GROUP BY p.observation_id, i.user_id, i.species_id
        ),
        ObservationConsensus AS (
            SELECT observation_id, species_id,
                   COUNT(*) AS vote_count,
                   MAX(quantity) AS max_quantity
            FROM ObservationVotes
            GROUP BY observation_id, species_id
        ),
        ObservationVoters AS (
            SELECT observation_id, COUNT(DISTINCT user_id) AS voters
            FROM ObservationVotes
            GROUP BY observation_id
        ),
        RankedConsensus AS (
            SELECT ranked.*,
                   (rn = 1 OR vote_count * 2 > voters) AS accepted
            FROM (
                SELECT oc.observation_id, oc.species_id, oc.vote_count,
                       oc.max_quantity, ov.voters,
                       ROW_NUMBER() OVER (PARTITION BY oc.observation_id
                                          ORDER BY {rank_order}) AS rn
                FROM ObservationConsensus oc
                JOIN ObservationVoters ov ON ov.observation_id = oc.observation_id
            ) ranked
        )"""


def tally_votes(votes):
    """{species_id: (voters, max_quantity)} and the number of distinct voters.

    ``votes`` is an iterable of (user_id, species_id, quantity) and may contain
    one tuple per photo; people are counted once per species and once overall.
    """
    per_species = {}
    users = set()
    for user_id, species_id, quantity in votes:
        users.add(user_id)
        entry = per_species.setdefault(species_id, [set(), 0])
        entry[0].add(user_id)
        entry[1] = max(entry[1], quantity or 0)
    return ({sp: (len(u), q) for sp, (u, q) in per_species.items()}, len(users))


def accepted_species(per_species, voters, primary):
    """Accepted species of a series given its tally and its primary species.

    ``primary`` is the caller's top-ranked species (its own tie-break). It is
    always accepted; others need more than half of ``voters``.
    """
    accepted = [primary] if primary is not None else []
    for sp, (count, _q) in per_species.items():
        if sp != primary and count * 2 > voters:
            accepted.append(sp)
    return accepted


def has_majority(per_species, voters):
    """True when the top species is named by more than half of the voters.

    This is the closing rule of a pending series (unchanged in meaning: before
    multi-species a "vote" was one person, and it still is).
    """
    if not per_species or not voters:
        return False
    top = max(count for count, _q in per_species.values())
    return top * 2 > voters


def is_multi_species(votes):
    """True when some verifier named more than one species in the series.

    ``votes`` are (user_id, species_id, ...) tuples, possibly one per photo.
    """
    per_user = {}
    for vote in votes:
        per_user.setdefault(vote[0], set()).add(vote[1])
    return any(len(named) > 1 for named in per_user.values())


UNIDENTIFIABLE_SPECIES_ID = -6   # "Неможливо визначити"
UNIDENTIFIABLE_MIN_VOTERS = 5    # owner's rule 2026-10-30: give someone else a chance


def can_close(per_species, voters, multi):
    """Closing rule of a pending series.

    One species per verifier (``multi`` False): the old rule, the top species
    needs more than half of the verifiers.

    Someone named several species (``multi`` True): additionally EVERY named
    species must be decided, named by more or by fewer than half of the
    verifiers. A species at exactly half keeps the series open for the next
    verifier, so a second animal noticed by one of two people is neither
    accepted nor dropped on a coin toss. (Owner's rule, 2026-10-30.)
    """
    if not has_majority(per_species, voters):
        return False
    # "Not identifiable" wins only with at least five people behind it: a
    # sixth verifier may still recognise the animal, and a series closed as
    # unidentifiable is soon cleaned away.
    top = max(per_species.items(), key=lambda kv: kv[1][0])[0]
    if top == UNIDENTIFIABLE_SPECIES_ID and voters < UNIDENTIFIABLE_MIN_VOTERS:
        return False
    if not multi:
        return True
    return all(count * 2 != voters for count, _q in per_species.values())
