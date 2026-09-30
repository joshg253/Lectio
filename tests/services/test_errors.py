import logging

from services.errors import public_error


def test_public_error_hides_exception_text_and_logs_it(caplog):
    with caplog.at_level(logging.WARNING, logger="lectio.errors"):
        msg = public_error(RuntimeError("secret host db.internal"), "Syncing")
    assert msg == "Syncing failed — see the server log for details."
    assert "secret" not in msg
    record = caplog.records[-1]
    assert "Syncing failed" in record.getMessage()
    assert "secret host db.internal" in str(record.exc_info[1])
