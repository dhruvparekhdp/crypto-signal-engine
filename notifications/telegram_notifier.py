import structlog
from telegram import Bot
from telegram.error import Forbidden, InvalidToken, TelegramError

from config.settings import settings

log = structlog.get_logger()


class TelegramNotifier:
    def __init__(self) -> None:
        token = settings.telegram_bot_token
        if token is None or not token.get_secret_value().strip():
            log.info("telegram_bot_token_not_set",
                     hint="Running with Telegram notifications disabled")
            self._bot = None
            return
        self._bot = Bot(token=token.get_secret_value())

    async def send_text(self, text: str, parse_mode: str | None = None) -> bool:
        """Send a message. Pass parse_mode=ParseMode.HTML for messages built with <b>/<i> tags —
        without it Telegram shows the tags literally instead of rendering them."""
        if self._bot is None or not settings.telegram_chat_id:
            return False
        try:
            await self._bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
                parse_mode=parse_mode,
            )
            return True
        except InvalidToken as e:
            log.error("telegram_invalid_token",
                      hint="Bot token is wrong — regenerate via @BotFather",
                      error=str(e))
        except Forbidden as e:
            log.error("telegram_forbidden",
                      hint="Chat ID wrong, or you haven't sent /start to your bot yet",
                      chat_id=settings.telegram_chat_id,
                      error=str(e))
        except TelegramError as e:
            log.error("telegram_text_send_failed", error=str(e), error_type=type(e).__name__,
                      chat_id=settings.telegram_chat_id)
        return False

    async def verify(self) -> bool:
        """Called at startup to validate credentials before anything else runs."""
        if self._bot is None:
            return False
        try:
            me = await self._bot.get_me()
            log.info("telegram_bot_verified", bot_username=me.username, bot_id=me.id)
            return True
        except InvalidToken:
            log.error("telegram_invalid_token",
                      hint="TELEGRAM_BOT_TOKEN is wrong — go to @BotFather and get a fresh token")
            return False
        except TelegramError as e:
            log.error("telegram_verify_failed", error=str(e))
            return False
