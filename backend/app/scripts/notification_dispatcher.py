"""Persistent notification outbox worker with bounded Telegram retries."""

import time

from app.logger import configure_logging, get_logger
from app.utils import config_watch, notifications
from app.utils.postgres import get_db_cm

logger = get_logger()


def dispatch_available(limit: int = 20) -> int:
    processed = 0
    for _ in range(limit):
        with get_db_cm() as db:
            if not notifications.deliver_pending_once(db):
                break
            processed += 1
    return processed


def enqueue_reports() -> int:
    with get_db_cm() as db:
        return notifications.enqueue_scheduled_reports(db)


def poll_commands(offsets: dict[str, int]) -> int:
    with get_db_cm() as db:
        return notifications.poll_telegram_commands_once(db, offsets)


def main() -> None:
    configure_logging()
    logger.info("Notification dispatcher started")
    # Exits (status 0) when a restart-class setting changes, so Docker restarts the sidecar with the new config.
    watcher = config_watch.RestartWatcher(log=logger.info)
    telegram_offsets: dict[str, int] = {}
    last_command_poll = 0.0
    while True:
        watcher.exit_if_changed()
        try:
            enqueue_reports()
            processed = dispatch_available()
        except Exception:  # noqa: BLE001
            logger.exception("Notification dispatch cycle failed")
            processed = 0
        now = time.monotonic()
        if now - last_command_poll >= 2.0:
            try:
                poll_commands(telegram_offsets)
            except Exception:  # noqa: BLE001
                # Telegram request URLs contain the bot token. Do not log an
                # exception traceback that could expose it through HTTP internals.
                logger.warning("Telegram command poll failed")
            last_command_poll = now
        time.sleep(0.5 if processed else 2.0)


if __name__ == "__main__":
    main()
