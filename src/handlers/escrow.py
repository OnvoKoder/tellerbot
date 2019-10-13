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
from config import SUPPORT_CHAT_ID
from decimal import Decimal
import functools
from time import time
from typing import Any, Mapping, Optional
import string

from bson.objectid import ObjectId
from bson.decimal128 import Decimal128

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from aiogram.dispatcher import FSMContext
from aiogram.utils import markdown

from src.handlers import tg, dp, private_handler, show_order, validate_money, start_keyboard
from src.database import database
from src.escrow import EscrowOffer, get_escrow_instance
from src.i18n import _
from src import states
from src.utils import normalize_money, MoneyValidationError


def escrow_callback_handler(*args, **kwargs):
    def decorator(handler):
        @dp.callback_query_handler(*args, **kwargs)
        async def wrapper(call: types.CallbackQuery):
            offer_id = call.data.split()[1]
            offer = await database.escrow.find_one({'_id': ObjectId(offer_id)})

            if not offer:
                await call.answer(_('Offer is not found.'))
                return

            return await handler(call, EscrowOffer(**offer))
        return wrapper
    return decorator


def escrow_message_handler(*args, id_filter: Optional[str] = None, stage: Optional[str] = None, **kwargs):
    def decorator(handler):
        @private_handler(*args, **kwargs)
        async def wrapper(message: types.Message, state: FSMContext):
            user_id = message.from_user.id
            if id_filter is not None:
                search_filter = {id_filter: user_id}
            else:
                search_filter = {'$or': [{'init.id': user_id}, {'counter.id': user_id}]}
            if stage is not None:
                search_filter['stage'] = stage
            offer = await database.escrow.find_one(search_filter)
            if not offer:
                await tg.send_message(message.chat.id, _('Offer is not found.'))
                return

            return await handler(message, state, EscrowOffer(**offer))
        return wrapper
    return decorator


@escrow_message_handler(id_filter='init.id', stage='creation', state=states.Escrow.sum)
async def set_escrow_sum(message: types.Message, state: FSMContext, offer: EscrowOffer):
    try:
        escrow_sum = await validate_money(message.text, message.chat.id)
    except MoneyValidationError as exception:
        await tg.send_message(message.chat.id, str(exception))
        return

    order = await database.orders.find_one({'_id': offer.order})
    order_sum = order.get(offer.sum_currency)
    if order_sum and escrow_sum > order_sum.to_decimal():
        await tg.send_message(
            message.chat.id,
            _("Send number not exceeding order's sum.")
        )
        return

    update_dict = {offer.sum_currency: Decimal128(escrow_sum)}
    new_currency = 'sell' if offer.sum_currency == 'sum_buy' else 'buy'
    update_dict[f'sum_{new_currency}'] = Decimal128(normalize_money(
        escrow_sum * order[f'price_{new_currency}'].to_decimal()
    ))
    escrow_currency = offer.type
    escrow_sum = update_dict[f'sum_{escrow_currency}']
    update_dict['sum_fee_up'] = Decimal128(normalize_money(
        escrow_sum.to_decimal() * Decimal('1.05')
    ))
    update_dict['sum_fee_down'] = Decimal128(normalize_money(
        escrow_sum.to_decimal() * Decimal('0.95')
    ))

    await offer.update_document(
        {'$set': update_dict, '$unset': {'sum_currency': True}}
    )
    answer = _('Do you agree to pay a fee of 5%?') + ' '
    if escrow_currency == 'buy':
        answer += _("(You'll pay {} {})")
        sum_fee_field = 'sum_fee_up'
    elif escrow_currency == 'sell':
        answer += _("(You'll get {} {})")
        sum_fee_field = 'sum_fee_down'
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            _('Yes'), callback_data='sell_accept_fee {} {}'.format(offer._id, sum_fee_field)
        ),
        InlineKeyboardButton(
            _('No'), callback_data='sell_decline_fee {} {}'.format(offer._id, sum_fee_field)
        )
    )
    answer = answer.format(update_dict[sum_fee_field], offer[escrow_currency])
    await tg.send_message(message.chat.id, answer, reply_markup=keyboard)
    await states.Escrow.sell_fee.set()


@escrow_callback_handler(lambda call: call.data.startswith('sell_accept_fee '), state=states.Escrow.sell_fee)
async def sell_pay_fee(call: types.CallbackQuery, offer: EscrowOffer):
    await call.answer()
    await tg.send_message(
        call.message.chat.id,
        _('Send your {} address.').format(offer.sell)
    )
    await states.Escrow.sell_address.set()


@escrow_callback_handler(lambda call: call.data.startswith('sell_decline_fee '), state=states.Escrow.sell_fee)
async def sell_decline_fee(call: types.CallbackQuery, offer: EscrowOffer):
    sum_fee_field = call.data.split()[2]
    await offer.update_document(
        {'$set': {sum_fee_field: offer['sum_' + offer.type]}}
    )
    await call.answer()
    await tg.send_message(
        call.message.chat.id,
        _('Send your {} address.').format(offer.sell)
    )
    await states.Escrow.sell_address.set()


@escrow_message_handler(id_filter='init.id', stage='creation', state=states.Escrow.sell_address)
async def set_sell_address(message: types.Message, state: FSMContext, offer: EscrowOffer):
    if len(message.text) > 35 or not all(ch in string.ascii_letters + string.digits for ch in message.text):
        await tg.send_message(message.chat.id, _('Address is invalid.'))
        return

    await database.escrow.update_one(
        {'_id': offer._id},
        {'$set': {
            'sell_address': message.text,
            'stage': 'pending'
        }}
    )
    order = await database.orders.find_one({'_id': offer.order})
    await show_order(
        order, offer.counter['id'], offer.counter['id'],
        show_id=True
    )
    locale = offer.counter['locale']
    buy_keyboard = InlineKeyboardMarkup()
    buy_keyboard.add(
        InlineKeyboardButton(
            _('Accept', locale=locale), callback_data='accept {}'.format(offer._id)
        ),
        InlineKeyboardButton(
            _('Decline', locale=locale), callback_data='decline {}'.format(offer._id)
        )
    )
    await tg.send_message(
        offer.counter['id'],
        _('You got an escrow offer to sell {} {} for {} {}.', locale=locale).format(
            offer.sum_sell, offer.sell,
            offer.sum_buy, offer.buy
        ),
        reply_markup=buy_keyboard
    )
    answer = _('Offer sent.')
    reply = await tg.send_message(message.from_user.id, answer)
    sell_keyboard = InlineKeyboardMarkup()
    sell_keyboard.add(InlineKeyboardButton(
        _('Cancel'), callback_data='escrow_cancel {}'.format(offer._id)
    ))
    partial_edit = functools.partial(
        tg.edit_message_reply_markup, message.chat.id, reply.message_id,
        reply_markup=sell_keyboard
    )
    asyncio.get_running_loop().call_later(60 * 60, partial_edit)
    await state.finish()


@escrow_callback_handler(lambda call: call.data.startswith('accept '))
async def accept_offer(call: types.CallbackQuery, offer: EscrowOffer):
    locale = offer.init['locale']
    sell_keyboard = InlineKeyboardMarkup()
    sell_keyboard.add(InlineKeyboardButton(
        _('Cancel', locale=locale), callback_data='escrow_cancel {}'.format(offer._id)
    ))
    await tg.send_message(
        offer.init['id'],
        _('Your escrow offer was accepted.', locale=locale) + ' ' +
        _("I'll notify you when transaction is complete.", locale=locale),
        reply_markup=sell_keyboard
    )
    await call.answer()

    await database.escrow.delete_many({
        'init.id': offer.init['id'],
        'stage': 'pending'
    })
    await database.escrow.update_one(
        {'_id': offer._id},
        {'$set': {
            'stage': 'active',
            'react_time': time()
        }}
    )
    answer = _('Do you agree to pay a fee of 5%?') + ' '
    escrow_currency = offer.type
    if escrow_currency == 'buy':
        answer += _("(You'll get {} {})")
        sum_fee_field = 'sum_fee_down'
    elif escrow_currency == 'sell':
        answer += _("(You'll pay {} {})")
        sum_fee_field = 'sum_fee_up'
    answer = answer.format(offer[sum_fee_field], offer[escrow_currency])
    buy_keyboard = InlineKeyboardMarkup()
    buy_keyboard.add(
        InlineKeyboardButton(
            _('Yes'), callback_data='buy_accept_fee {} {}'.format(offer._id, sum_fee_field)
        ),
        InlineKeyboardButton(
            _('No'), callback_data='buy_decline_fee {} {}'.format(offer._id, sum_fee_field)
        )
    )
    await tg.send_message(call.message.chat.id, answer, reply_markup=buy_keyboard)
    await states.Escrow.buy_fee.set()


@escrow_callback_handler(lambda call: call.data.startswith('decline '))
async def decline_offer(call: types.CallbackQuery, offer: EscrowOffer):
    offer.react_time = time()
    await offer.delete_document()
    await tg.send_message(
        offer.init['id'],
        _('Your escrow offer was declined.', locale=offer.init['locale'])
    )
    await call.answer()
    await tg.send_message(call.message.chat.id, _('Offer was declined.'))


@escrow_callback_handler(lambda call: call.data.startswith('buy_accept_fee '), state=states.Escrow.buy_fee)
async def buy_pay_fee(call: types.CallbackQuery, offer: EscrowOffer):
    await call.answer()
    await tg.send_message(
        call.message.chat.id,
        _('Send your {} address.').format(offer.buy)
    )
    await states.Escrow.buy_address.set()


@escrow_callback_handler(lambda call: call.data.startswith('buy_decline_fee '), state=states.Escrow.buy_fee)
async def buy_decline_fee(call: types.CallbackQuery, offer: EscrowOffer):
    sum_fee_field = call.data.split()[2]
    await offer.update_document(
        {'$set': {sum_fee_field: offer['sum_' + offer.type]}}
    )
    await call.answer()
    await tg.send_message(
        call.message.chat.id,
        _('Send your {} address.').format(offer.buy)
    )
    await states.Escrow.buy_address.set()


@escrow_message_handler(id_filter='counter.id', stage='active', state=states.Escrow.buy_address)
async def set_buy_address(message: types.Message, state: FSMContext, offer: EscrowOffer):
    if len(message.text) > 35 or not all(ch in string.ascii_letters + string.digits for ch in message.text):
        await tg.send_message(message.chat.id, _('Address is invalid.'))
        return

    escrow_currency = offer.type
    if escrow_currency == 'buy':
        escrow_id = offer.init['id']
        locale = offer.init['locale']
        memo_address = offer.sell_address
        send_reply = True
    elif escrow_currency == 'sell':
        escrow_id = offer.counter['id']
        locale = offer.counter['locale']
        memo_address = message.text
        send_reply = False

    memo = 'escrow for {} {} to {}'.format(
        offer.sum_buy, offer.buy, memo_address
    )
    await database.escrow.update_one(
        {'_id': offer._id},
        {'$set': {
            'buy_address': message.text,
            'memo': memo
        }}
    )
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            _('Sent', locale=locale), callback_data='escrow_sent {}'.format(offer._id)
        ),
        InlineKeyboardButton(
            _('Cancel', locale=locale), callback_data='escrow_cancel {}'.format(offer._id)
        )
    )
    escrow_address = markdown.bold(get_escrow_instance(offer[escrow_currency]).address)
    await state.finish()
    await tg.send_message(
        escrow_id,
        _('Send {} {} to address {}', locale=locale).format(
            offer.sum_fee_up, offer[escrow_currency], escrow_address
        ) + ' ' + _('with memo', locale=locale) + ':\n' + markdown.code(memo),
        reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
    )
    if send_reply:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton(
            _('Cancel'), callback_data='escrow_cancel {}'.format(offer._id)
        ))
        await tg.send_message(
            message.chat.id,
            _('Transfer information sent.') + ' ' +
            _("I'll notify you when transaction is complete."),
            reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
        )


@escrow_callback_handler(lambda call: call.data.startswith('escrow_cancel '))
async def cancel_offer(call: types.CallbackQuery, offer: EscrowOffer):
    if offer.stage == 'confirmed':
        await call.answer(_("You can't cancel escrow on this stage."))
        return

    sell_answer = _('Escrow was cancelled.', locale=offer.init['locale'])
    buy_answer = _('Escrow was cancelled.', locale=offer.counter['locale'])
    offer.cancel_time = time()
    await offer.delete_document()
    await call.answer()
    await tg.send_message(offer.init['id'], sell_answer, reply_markup=start_keyboard())
    await tg.send_message(offer.counter['id'], buy_answer, reply_markup=start_keyboard())
    sell_state = FSMContext(dp.storage, offer.init['id'], offer.init['id'])
    buy_state = FSMContext(dp.storage, offer.counter['id'], offer.counter['id'])
    await sell_state.finish()
    await buy_state.finish()


@escrow_callback_handler(lambda call: call.data.startswith('escrow_sent '))
async def escrow_sent_confirmation(call: types.CallbackQuery, offer: EscrowOffer):
    escrow_currency = offer.type

    if escrow_currency == 'buy':
        memo_address = offer.sell_address
        other_id = offer.counter['id']
        locale = offer.counter['locale']
        new_currency = 'sell'
    elif escrow_currency == 'sell':
        memo_address = offer.buy_address
        other_id = offer.init['id']
        locale = offer.init['locale']
        new_currency = 'buy'

    escrow_instance = get_escrow_instance(offer[escrow_currency])
    trx = await escrow_instance.get_transaction(
        offer.sum_fee_up.to_decimal(), offer[escrow_currency], offer.memo, offer.react_time
    )
    if trx:
        url = markdown.link(
            _('Transaction is confirmed.', locale=locale),
            escrow_instance.trx_url(trx['trx_id'])
        )
        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton(
                _('Sent', locale=locale), callback_data='tokens_sent {}'.format(offer._id)
            ),
            InlineKeyboardButton(
                _('Cancel', locale=locale), callback_data='tokens_cancel {}'.format(offer._id)
            )
        )
        await database.escrow.update_one(
            {'_id': offer._id},
            {'$set': {
                'return_address': trx['from'],
                'trx_id': trx['trx_id'],
                'stage': 'confirmed'
            }}
        )
        await tg.send_message(
            other_id,
            url + '\n' + _('Send {} {} to address {}', locale=locale).format(
                offer[f'sum_{new_currency}'], offer[new_currency], memo_address
            ) + '.',
            reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
        )
        await call.answer()
        await tg.send_message(
            call.message.chat.id,
            _('Transaction is confirmed.') + ' ' +
            _("I'll notify should you get {}.").format(offer[new_currency])
        )
    else:
        await call.answer(_("Transaction wasn't found."))


@escrow_callback_handler(lambda call: call.data.startswith('tokens_cancel '))
async def cancel_confirmed_offer(call: types.CallbackQuery, offer: EscrowOffer):
    escrow_currency = offer.type

    if escrow_currency == 'buy':
        return_user = offer.init
        cancel_user = offer.counter
    elif escrow_currency == 'sell':
        return_user = offer.counter
        cancel_user = offer.init

    escrow_instance = get_escrow_instance(offer[escrow_currency])
    trx_url = await escrow_instance.transfer(
        offer.return_address, offer.sum_fee_up.to_decimal(), offer[escrow_currency]
    )
    cancel_answer = _('Escrow was cancelled.', locale=cancel_user['locale'])
    return_answer = _('Escrow was cancelled.', locale=return_user['locale']) + ' ' + markdown.link(
        _('You got your {} {} back.', locale=return_user['locale']).format(
            offer.sum_fee_up, offer[escrow_currency]
        ), trx_url
    )
    await offer.delete_document()
    await call.answer()
    await tg.send_message(cancel_user['id'], cancel_answer, reply_markup=start_keyboard())
    await tg.send_message(return_user['id'], return_answer, reply_markup=start_keyboard())


@escrow_callback_handler(lambda call: call.data.startswith('tokens_sent '))
async def final_offer_confirmation(call: types.CallbackQuery, offer: EscrowOffer):
    escrow_currency = offer.type

    if escrow_currency == 'buy':
        confirm_user = offer.init
        other_user = offer.counter
        new_currency = 'sell'
    elif escrow_currency == 'sell':
        confirm_user = offer.counter
        other_user = offer.init
        new_currency = 'buy'

    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            _('Yes', locale=confirm_user['locale']),
            callback_data='escrow_complete {}'.format(offer._id)
        )
    )
    reply = await tg.send_message(
        confirm_user['id'],
        _('Did you get {}?', locale=confirm_user['locale']).format(offer[new_currency]),
        reply_markup=keyboard
    )
    keyboard.add(
        InlineKeyboardButton(
            _('No', locale=confirm_user['locale']),
            callback_data='escrow_validate {}'.format(offer._id)
        )
    )
    partial_edit = functools.partial(
        tg.edit_message_reply_markup, call.message.chat.id, reply.message_id,
        reply_markup=keyboard
    )
    asyncio.get_running_loop().call_later(60 * 10, partial_edit)
    await call.answer()
    await tg.send_message(
        other_user['id'],
        _("When your transfer is confirmed, I'll complete escrow.",
          locale=other_user['locale']),
        reply_markup=start_keyboard()
    )


@escrow_callback_handler(lambda call: call.data.startswith('escrow_complete '))
async def complete_offer(call: types.CallbackQuery, offer: EscrowOffer):
    escrow_currency = offer.type

    if escrow_currency == 'buy':
        recipient_user = offer.counter
        other_user = offer.init
    elif escrow_currency == 'sell':
        recipient_user = offer.init
        other_user = offer.counter

    escrow_instance = get_escrow_instance(offer[escrow_currency])
    trx_url = await escrow_instance.transfer(
        offer[f'{escrow_currency}_address'],
        offer.sum_fee_down.to_decimal(),
        offer[escrow_currency]
    )
    answer = _('Escrow is completed!', locale=other_user['locale'])
    recipient_answer = _('Escrow is completed!', locale=recipient_user['locale']) + ' ' + markdown.link(
        _('I sent you {} {}.', locale=recipient_user['locale']).format(
            offer.sum_fee_down, offer[escrow_currency]
        ), trx_url
    )
    await offer.delete_document()
    await tg.send_message(recipient_user['id'], recipient_answer, reply_markup=start_keyboard())
    await tg.send_message(other_user['id'], answer, reply_markup=start_keyboard())
    await call.answer()


@escrow_callback_handler(lambda call: call.data.startswith('escrow_validate '))
async def validate_offer(call: types.CallbackQuery, offer: EscrowOffer):
    escrow_currency = offer.type
    escrow_instance = get_escrow_instance(offer[escrow_currency])
    await tg.send_message(
        SUPPORT_CHAT_ID,
        'Unconfirmed escrow.\nTransaction: {}\nMemo: {}'.format(
            escrow_instance.trx_url(offer.trx_id),
            markdown.code(offer.memo),
        )
    )
    await offer.delete_document()
    await call.answer()
    await tg.send_message(
        call.message.chat.id,
        _("We'll manually validate your request and decide on the return."),
        reply_markup=start_keyboard()
    )
