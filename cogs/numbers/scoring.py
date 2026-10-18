def score_votes(up: set[int], down: set[int], author_id: int) -> int:
    """One vote per person. Both reactions cancel out; self-votes do not count."""
    excluded = {author_id}
    return len(up - down - excluded) - len(down - up - excluded)

