import logging

from gene.logging import install_secret_filter


def test_upstream_header_and_token_logs_are_redacted(monkeypatch):
    secret = 'synthetic-test-credential'
    monkeypatch.setenv('GENE_AUDIO_PASSWORD', secret)
    monkeypatch.setenv('DISCORD_TOKEN', 'synthetic-discord-credential')
    original = logging.getLogRecordFactory()
    try:
        install_secret_filter()
        record = logging.getLogRecordFactory()('audio', logging.INFO, __file__, 1,
            'connecting with headers %s', ({'Authorization': secret},), None)
        assert secret not in record.getMessage()
        assert '[REDACTED]' in record.getMessage()
    finally:
        logging.setLogRecordFactory(original)

