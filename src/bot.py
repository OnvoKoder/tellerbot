# Copyright (C) 2019  alfred richardsn
#
# This file is part of TellerBot.
#
# TellerBot is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with TellerBot.  If not, see <https://www.gnu.org/licenses/>.
import asyncio
import logging
import typing

from aiogram import Bot
from aiogram import types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import Dispatcher

from src import config
from src.database import storage
from src.i18n import i18n


tg = Bot(token=config.TOKEN, loop=asyncio.get_event_loop())
dp = Dispatcher(tg, storage=storage)
dp.middleware.setup(i18n)

logging.basicConfig(
    filename=config.LOG_FILENAME, filemode='a', level=config.LOGGER_LEVEL
)
dp.middleware.setup(LoggingMiddleware())


def private_handler(*args, **kwargs):
    """Register handler only for private message."""

    def decorator(handler: typing.Callable):
        dp.register_message_handler(
            handler,
            lambda message: message.chat.type == types.ChatType.PRIVATE,  # noqa: E721
            *args,
            **kwargs
        )
        return handler

    return decorator


state_handlers = {}


def state_handler(state):
    """Associate ``state`` with decorated handler."""

    def decorator(handler):
        state_handlers[state.state] = handler
        return handler

    return decorator
