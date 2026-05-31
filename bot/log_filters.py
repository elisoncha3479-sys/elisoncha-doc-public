"""Logging filters."""

import logging
import re


class TokenMaskingFilter(logging.Filter):
    PATTERN = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            masked = self.PATTERN.sub("bot***", msg)
            if masked != msg:
                record.msg = masked
                record.args = ()
            if record.exc_text:
                record.exc_text = self.PATTERN.sub("bot***", record.exc_text)
        except Exception:
            pass
        return True


def install_token_masking_on_root() -> None:
    f = TokenMaskingFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(f)
