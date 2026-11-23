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


# ---------------------------------------------------------------------------
# Weighted closing rule (owner's decision 2026-11-23, biomon WORKLOG)
# ---------------------------------------------------------------------------
#
# Each person's vote for a species weighs  confirmed / (n + 1):
#   n          = how many times this person named this species in series where
#                the OTHER people had a clear majority;
#   confirmed  = how many of those the others' majority also named it.
# That is precision x n/(n+1): a record of 10 out of 10 weighs 10/11, a first
# vote on a species weighs nothing (the series is closed by others).
# DeepFaune adds  AI_VOTE_FACTOR x its score  to the species it named, but
# only when a person named that species too: it backs people, it never
# pushes against them (it has no kulan and says 'horse'; it misses animals
# and says 'empty').
# A series closes when the leader is ahead of the runner-up by at least the
# threshold. With 1.5 one person plus DeepFaune never closes (< 1 + 0.5), so
# two people are always needed, and weak or split votes need more.
# Checked on every finished series (2026-11-23): 96.4 % would close on the
# same species, 1 would change, 3.6 % would want more votes.

WEIGHTED_THRESHOLD_DEFAULT = 1.5
AI_VOTE_FACTOR_DEFAULT = 0.5


EXPERIENCE_K_DEFAULT = 1   # config VERIFIER_EXPERIENCE_K (biomon and myproject: 10)


def verifier_weight(confirmed, n, k=EXPERIENCE_K_DEFAULT):
    """precision x n/(n+k), which is simply confirmed / (n + k).

    k is how long a newcomer stays a newcomer: with k = 10 a record of 10 out
    of 10 weighs 0.5 (k = 1: 0.91), and weight 0.99 needs about 99 x k
    agreed votes on that species. Owner's choice 2026-11-23: k = 10, so two
    newcomers together never close a series.
    """
    return confirmed / (n + k) if n else 0.0


def grade_votes(series_votes):
    """Per (user, species): (n, confirmed) graded against the other people.

    ``series_votes``: iterable of vote lists, one per series, each an iterable
    of (user_id, species_id) (duplicates, e.g. one per photo, are ignored).
    A vote is graded only when the other people of the series have a unique
    top species by head count; a tie, or nobody else, says nothing about it.
    """
    n, confirmed = {}, {}
    for votes in series_votes:
        named = {}
        for user_id, species_id in votes:
            named.setdefault(user_id, set()).add(species_id)
        if len(named) < 2:
            continue
        for user_id, mine in named.items():
            counts = {}
            for other, theirs in named.items():
                if other == user_id:
                    continue
                for sp in theirs:
                    counts[sp] = counts.get(sp, 0) + 1
            ranked = sorted(counts.values(), reverse=True)
            if len(ranked) > 1 and ranked[0] == ranked[1]:
                continue
            truth = max(counts, key=counts.get)
            for sp in mine:
                key = (user_id, sp)
                n[key] = n.get(key, 0) + 1
                confirmed[key] = confirmed.get(key, 0) + (sp == truth)
    return {key: (n[key], confirmed[key]) for key in n}


AI_EMPTY = -1


def pick_series_ai(rows):
    """(species_id, score) of the AI's answer for a series, or None.

    ``rows``: (accuracy_rank, model_id, species_id, n_frames, best_score) per
    (model, species) of one series. Same order as ai_runner.SERIES_PICK_ORDER_SQL:
    the most accurate model (then the newer one); within it 'empty' only if
    no frame has an animal, else the species on most frames, then the best
    single-frame score, then the species id.
    """
    rows = list(rows)
    if not rows:
        return None
    best_model = max((r[0] or 0, r[1]) for r in rows)
    mine = [r for r in rows if ((r[0] or 0), r[1]) == best_model]

    def key(r):
        _rank, _model, sp, frames, score = r
        return (sp != AI_EMPTY, frames, score if score is not None else -1.0,
                sp is not None, sp or 0)

    _rank, _model, sp, _frames, score = max(mine, key=key)
    return sp, (score or 0.0)


def weighted_close(votes, weight_of, ai=None,
                   threshold=WEIGHTED_THRESHOLD_DEFAULT,
                   ai_factor=AI_VOTE_FACTOR_DEFAULT):
    """Species the series closes on under the weighted rule, or None.

    ``votes``: (user_id, species_id) pairs (one per person and species).
    ``weight_of(user_id, species_id)``: that person's weight on that species.
    ``ai``: (species_id, score) of the series' AI answer, or None.

    Guards kept from the majority rule, so every reader of the data (which
    ranks species by head count) sees the same winner:
      - the leader is named by more than half of the people;
      - 'not identifiable' still needs five people (can_close);
      - when someone named several species, every named species must be
        decided as before (can_close with multi=True).
    """
    pairs = {(u, s) for u, s in votes}
    per_species, voters = tally_votes((u, s, 0) for u, s in pairs)
    if not voters:
        return None
    points = {}
    for u, s in pairs:
        points[s] = points.get(s, 0.0) + weight_of(u, s)
    if ai is not None and ai[0] is not None and ai[0] in points:
        points[ai[0]] += ai_factor * (ai[1] or 0.0)
    ranked = sorted(points.items(), key=lambda kv: kv[1], reverse=True)
    leader, lead_points = ranked[0]
    lead = lead_points - (ranked[1][1] if len(ranked) > 1 else 0.0)
    if lead < threshold - 1e-9:
        return None
    if per_species[leader][0] * 2 <= voters:
        return None
    multi = any(n > 1 for n in _species_per_user(pairs).values())
    if leader == UNIDENTIFIABLE_SPECIES_ID or multi:
        if not can_close(per_species, voters, multi):
            return None
    return leader


def _species_per_user(pairs):
    out = {}
    for u, _s in pairs:
        out[u] = out.get(u, 0) + 1
    return out
