"""HumanMint/fri#335: a learning pass asks, right before its model request, which of the messages it holds Gene knows
were withdrawn, as its finish would refuse them. A withdrawal only Gene knows about (a fence of a message withdrawn by
its ID, or one its author's exchange was withdrawn for) then never reaches the model, not just the derived memory."""
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_protocol import contract
from tests.test_api import headers
from tests.test_fri_learning import learning  # noqa: F401 (the fixture)

DELETED, EDITED, TAKEN_BACK, KEPT, BEFORE = '9001', '9002', '9003', '9004', '9005'


def ask(client, batch, messages):
    return client.post(f'/agent/learning/{batch}/withdrawn', json={'messages': messages}, headers=headers('fri'))


def held(*pairs):
    return [{'id': message, 'user_id': user} for message, user in pairs]


def test_a_pass_hears_of_every_withdrawal_its_finish_would_refuse(learning):  # noqa: F811
    clock, store, settings, scheduler = learning
    client = TestClient(create_app(settings))
    store.withdraw_saved('123', [BEFORE], deleted=False)  # An edit before the batch: no fence, nothing to refuse.
    claim = scheduler.claim()
    assert claim
    store.withdraw_saved('123', [DELETED], deleted=True)  # By ID alone: only Gene knows.
    store.withdraw_saved('123', [EDITED], deleted=False)  # An edit while the batch reads leaves a fence too.
    with store.connection() as db:  # Member 11's exchange with it withdrawn: their own message, taken back.
        db.execute('INSERT INTO chat_turn_withdrawals VALUES (?,?,?,?)', ('123', '11', TAKEN_BACK, clock[0]))
    pairs = [(DELETED, '11'), (EDITED, '14'), (TAKEN_BACK, '11'), (KEPT, '11'), (BEFORE, '14')]
    response = ask(client, claim['id'], held(*pairs))
    assert response.status_code == 200, response.text
    assert response.json() == {'withdrawn': [DELETED, EDITED, TAKEN_BACK]}
    # The same records at the finish: it keeps exactly those the pass was told it may send.
    assert ask(client, claim['id'], held((TAKEN_BACK, '14'), (KEPT, '14'))).json() == {'withdrawn': []}


def test_only_the_pass_holding_the_claim_may_ask(learning):  # noqa: F811
    clock, store, settings, scheduler = learning
    client = TestClient(create_app(settings))
    assert ask(client, 'none', held((KEPT, '11'))).status_code == 400  # No claim at all.
    claim = scheduler.claim()
    assert ask(client, claim['id']+'x', held((KEPT, '11'))).status_code == 400
    clock[0] += 3600  # Past the hour its finish is still taken in.
    assert ask(client, claim['id'], held((KEPT, '11'))).status_code == 400
    assert ask(client, claim['id'], held(*[(str(9100+n), '11') for n in range(41)])).status_code == 422  # As finish's.
    assert client.post(f"/agent/learning/{claim['id']}/withdrawn", json={'messages': held((KEPT, '11'))}).status_code == 401


def test_the_controller_says_it_answers():
    assert 'learning-withdrawals' in contract()['capabilities']
