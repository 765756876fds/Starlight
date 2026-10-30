from app.core.logger import Logger


def test_logger_accepts_standard_positional_format_arguments(tmp_path, capsys):
    logger = Logger(str(tmp_path))

    logger.info("role=%s; exiting", "secondary")

    assert "role=secondary; exiting" in capsys.readouterr().out
    logger.close()


def test_logger_format_mismatch_does_not_raise(tmp_path):
    logger = Logger(str(tmp_path))

    logger.warning("value=%s", "one", "extra")

    logger.close()
