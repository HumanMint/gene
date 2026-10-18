from cogs.numbers.scoring import score_votes


def test_votes_are_unique_and_self_votes_are_excluded():
    assert score_votes({1, 2, 3, 4, 5}, {6}, author_id=1) == 3


def test_both_votes_cancel_and_removal_changes_score():
    assert score_votes({1, 2}, {2}, author_id=99) == 1
    assert score_votes({1, 2}, set(), author_id=99) == 2
    assert score_votes({1}, {2}, author_id=99) == 0

