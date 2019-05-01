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


from datetime import datetime
from decimal import Decimal
from time import time
from typing import Any, Mapping

from bson.decimal128 import Decimal128
from bson.objectid import ObjectId

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import any_state

from . import tg, dp, private_handler, show_order, show_orders, validate_money
from ..database import database, STATE_KEY
from ..i18n import _
from ..states import field_editing
from ..utils import normalize_money, MoneyValidationError


@dp.callback_query_handler(lambda call: call.data.startswith('orders '), state=any_state)
async def orders_button(call: types.CallbackQuery):
    query = {
        '$or': [
            {'expiration_time': {'$exists': False}},
            {'expiration_time': {'$gt': time()}}
        ]
    }
    args = call.data.split()
    start = max(0, int(args[1]))
    invert = bool(int(args[2]))
    await show_orders(call, query, start, 'orders', invert, user_id=call.from_user.id)


@dp.callback_query_handler(lambda call: call.data.startswith('my_orders '), state=any_state)
async def my_orders_button(call: types.CallbackQuery):
    query = {'user_id': call.from_user.id}
    args = call.data.split()
    start = max(0, int(args[1]))
    invert = bool(int(args[2]))
    await show_orders(call, query, start, 'my_orders', invert)


def order_handler(handler):
    async def decorator(call: types.CallbackQuery):
        order_id = call.data.split()[1]
        order = await database.orders.find_one({'_id': ObjectId(order_id)})

        if not order:
            await call.answer(_('Order is not found.'))
            return

        return await handler(call, order)
    return decorator


@dp.callback_query_handler(lambda call: call.data.startswith('get_order '), state=any_state)
@order_handler
async def get_order_button(call: types.CallbackQuery, order: Mapping[str, Any]):
    await call.answer()
    await show_order(order, call.message.chat.id, call.from_user.id, show_id=True)


@private_handler(commands=['id'])
@private_handler(regexp='ID: [a-f0-9]{24}')
async def get_order_command(message: types.Message):
    try:
        order_id = message.text.split()[1]
    except IndexError:
        await tg.send_message(message.chat.id, _("Send order's ID as an argument."))
        return

    order = await database.orders.find_one({'_id': ObjectId(order_id)})
    if not order:
        await tg.send_message(message.chat.id, _('Order is not found.'))
        return
    await show_order(order, message.chat.id, message.from_user.id)


@dp.callback_query_handler(lambda call: call.data.startswith(('invert ', 'revert ')), state=any_state)
@order_handler
async def invert_button(call: types.CallbackQuery, order: Mapping[str, Any]):
    args = call.data.split()

    invert = args[0] == 'invert'
    location_message_id = int(args[2])
    edit = bool(int(args[3]))
    show_id = call.message.text.startswith('ID')

    await call.answer()
    await show_order(
        order, call.message.chat.id, call.from_user.id,
        message_id=call.message.message_id,
        location_message_id=location_message_id, show_id=show_id,
        invert=invert, edit=edit
    )


@dp.callback_query_handler(lambda call: call.data.startswith('edit '), state=any_state)
@order_handler
async def edit_button(call: types.CallbackQuery, order: Mapping[str, Any]):
    args = call.data.split()
    field = args[2]

    if field == 'sum_buy':
        answer = _('Send new amount of buying.')
    elif field == 'sum_sell':
        answer = _('Send new amount of selling.')
    elif field == 'price':
        answer = _('Send new price.')
    elif field == 'payment_system':
        answer = _('Send new payment system.')
    elif field == 'duration':
        answer = _('Send new duration.')
    elif field == 'comments':
        answer = _('Send new comments.')
    else:
        answer = None

    await call.answer()
    if answer:
        result = await tg.send_message(call.message.chat.id, answer)
        await database.users.update_one(
            {'id': call.from_user.id},
            {'$set': {
                'edit.order_message_id': call.message.message_id,
                'edit.message_id': result.message_id,
                'edit.order_id': order['_id'],
                'edit.field': field,
                'edit.location_message_id': int(args[3]),
                'edit.invert': bool(int(args[4])),
                'edit.show_id': call.message.text.startswith('ID')
            }}
        )
        await field_editing.set()


@private_handler(state=field_editing)
async def edit_field(message: types.Message, state: FSMContext):
    user = await database.users.find_one({'id': message.from_user.id})
    edit = user['edit']
    field = edit['field']
    invert = edit['invert']
    update_dict = {}
    set_dict = {}
    error = None

    if message.text == '-':
        update_dict['$unset'] = {field: True}
        if field == 'duration':
            update_dict['$unset']['expiration_time'] = True
            update_dict['$unset']['notify'] = True

    elif field == 'sum_buy':
        try:
            transaction_sum = await validate_money(message.text, message.chat.id)
        except MoneyValidationError as exception:
            error = str(exception)
        else:
            order = await database.orders.find_one({'_id': edit['order_id']})
            set_dict['sum_buy'] = Decimal128(transaction_sum)
            if order['price_sell']:
                set_dict['sum_sell'] = Decimal128(normalize_money(
                    transaction_sum * order['price_sell'].to_decimal()
                ))

    elif field == 'sum_sell':
        try:
            transaction_sum = await validate_money(message.text, message.chat.id)
        except MoneyValidationError as exception:
            error = str(exception)
        else:
            order = await database.orders.find_one({'_id': edit['order_id']})
            set_dict['sum_sell'] = Decimal128(transaction_sum)
            if order['price_buy']:
                set_dict['sum_buy'] = Decimal128(normalize_money(
                    transaction_sum * order['price_buy'].to_decimal()
                ))

    elif field == 'price':
        try:
            price = await validate_money(message.text, message.chat.id)
        except MoneyValidationError as exception:
            error = str(exception)
        else:
            order = await database.orders.find_one({'_id': edit['order_id']})

            if invert:
                price_sell = normalize_money(Decimal(1) / price)
                set_dict['price_buy'] = Decimal128(price)
                set_dict['price_sell'] = Decimal128(price_sell)

                if order['sum_currency'] == 'buy':
                    set_dict['sum_sell'] = Decimal128(normalize_money(
                        order['sum_buy'].to_decimal() * price_sell
                    ))
                elif 'sum_sell' in order:
                    set_dict['sum_buy'] = Decimal128(normalize_money(
                        order['sum_sell'].to_decimal() * price
                    ))
            else:
                price_buy = normalize_money(Decimal(1) / price)
                set_dict['price_buy'] = Decimal128(price_buy)
                set_dict['price_sell'] = Decimal128(price)

                if order['sum_currency'] == 'sell':
                    set_dict['sum_buy'] = Decimal128(normalize_money(
                        order['sum_sell'].to_decimal() * price_buy
                    ))
                elif 'sum_buy' in order:
                    set_dict['sum_sell'] = Decimal128(normalize_money(
                        order['sum_buy'].to_decimal() * price
                    ))

    elif field == 'payment_system':
        payment_system = message.text.replace('\n', ' ')
        if len(payment_system) > 150:
            await tg.send_message(
                message.chat.id,
                _('This value should contain less than 150 characters '
                  '(you sent {} characters).').format(len(payment_system))
            )
            return
        set_dict['payment_system'] = payment_system

    elif field == 'duration':
        try:
            duration = int(message.text)
            if duration <= 0:
                raise ValueError
        except ValueError:
            error = _('Send natural number.')
        else:
            order = await database.orders.find_one({'_id': edit['order_id']})
            set_dict['duration'] = duration
            expiration_time = order['start_time'] + duration * 24 * 60 * 60
            set_dict['expiration_time'] = expiration_time
            set_dict['notify'] = expiration_time > time()

    elif field == 'comments':
        comments = message.text
        if len(comments) > 150:
            await tg.send_message(
                message.chat.id,
                _('This value should contain less than 150 characters '
                  '(you sent {} characters).').format(len(comments))
            )
            return
        set_dict['comments'] = comments

    if set_dict:
        update_dict['$set'] = set_dict

    if update_dict:
        result = await database.orders.update_one(
            {'_id': edit['order_id']},
            update_dict
        )
        if result.modified_count:
            order = await database.orders.find_one({'_id': edit['order_id']})
            await show_order(
                order, message.chat.id, message.from_user.id,
                message_id=edit['order_message_id'],
                location_message_id=edit['location_message_id'],
                show_id=edit['show_id'], invert=edit['invert'], edit=True
            )
        await database.users.update_one(
            {'id': message.from_user.id},
            {'$unset': {'edit': True, STATE_KEY: True}}
        )
        await tg.delete_message(message.chat.id, message.message_id)
        await tg.delete_message(message.chat.id, edit['message_id'])
    elif error:
        await tg.delete_message(message.chat.id, message.message_id)
        await tg.edit_message_text(error, message.chat.id, edit['message_id'])


@dp.callback_query_handler(lambda call: call.data.startswith('delete '), state=any_state)
async def delete_button(call: types.CallbackQuery):
    order_id = call.data.split()[1]
    order = await database.orders.find_one_and_delete({
        '_id': ObjectId(order_id), 'user_id': call.from_user.id,
    })
    if not order:
        await call.answer(_("Couldn't delete order."))
        return

    args = call.data.split()
    location_message_id = int(args[2])
    show_id = call.message.text.startswith('ID')

    keyboard = InlineKeyboardMarkup(row_width=6)
    keyboard.row(InlineKeyboardButton(
        _('Restore'), callback_data='restore {} {} {}'.format(
            order['_id'], location_message_id, int(show_id)
        )
    ))
    keyboard.row(InlineKeyboardButton(
        _('Hide'), callback_data='hide {}'.format(location_message_id)
    ))

    order['date'] = datetime.utcnow()
    await database.trash.insert_one(order)
    await tg.edit_message_text(
        _('Order is deleted. You can restore it in 30 minutes.'),
        call.message.chat.id, call.message.message_id,
        reply_markup=keyboard
    )


@dp.callback_query_handler(lambda call: call.data.startswith('restore '), state=any_state)
async def restore_button(call: types.CallbackQuery):
    order_id = call.data.split()[1]
    order = await database.trash.find_one_and_delete({
        '_id': ObjectId(order_id), 'user_id': call.from_user.id,
    })
    if not order:
        await call.answer(_('Order is not found.'))
        return

    args = call.data.split()
    location_message_id = int(args[2])
    show_id = bool(int(args[3]))

    await database.orders.insert_one(order)
    await show_order(
        order, call.message.chat.id, call.from_user.id,
        message_id=call.message.message_id,
        location_message_id=location_message_id, show_id=show_id
    )


@dp.callback_query_handler(lambda call: call.data.startswith('hide '), state=any_state)
async def hide_button(call: types.CallbackQuery):
    await tg.delete_message(call.message.chat.id, call.message.message_id)
    location_message_id = call.data.split()[1]
    if location_message_id != '-1':
        await tg.delete_message(call.message.chat.id, location_message_id)
