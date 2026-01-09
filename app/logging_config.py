import logging
import sys
import time


class ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        return time.localtime(time.time() + 5.5 * 3600)


def setup_logging():
    formatter = ISTFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        "%H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        force=True,
    )
