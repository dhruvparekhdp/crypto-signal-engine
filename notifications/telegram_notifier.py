import structlog
from telegram import Bot
from telegram.error import Forbidden, InvalidToken, TelegramError

from config.settings import settings

log = structlog.get_logger()


class TelegramNotifier:
    """The bot is built from the CURRENT token on first use and rebuilt when the token changes: keys now live in
    the database (/keys) and load after this object is created, so reading the token here at start-up would leave
    alerts off for good once the token is no longer in .env."""

    def __init__(self) -> None:
        self._built_for: str | None = None
        self._bot_obj = None
        if self._bot is None:
            log.info("telegram_bot_token_not_set", hint="Telegram stays off until a token is saved on /keys")

    @property
    def _bot(self):
        token = settings.telegram_bot_token
        value = token.get_secret_value().strip() if token is not None else ""
        if not value:
            return None
        if value != self._built_for:
            self._bot_obj, self._built_for = Bot(token=value), value
        return self._bot_obj

    @_bot.setter
    def _bot(self, bot) -> None:                  # tests and callers that inject a bot
        self._bot_obj = bot
        token = settings.telegram_bot_token
        self._built_for = token.get_secret_value().strip() if token is not None else None

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
