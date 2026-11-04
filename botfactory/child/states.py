from aiogram.fsm.state import State, StatesGroup


class Upload(StatesGroup):
    file = State()
    url = State()
    title = State()
    desc = State()
    category = State()
    access = State()
    price_irt = State()
    price_usd = State()
    storage = State()


class EditContent(StatesGroup):
    value = State()


class CategoryS(StatesGroup):
    name = State()


class SponsorS(StatesGroup):
    target = State()


class PlanS(StatesGroup):
    name = State()
    days = State()
    price_irt = State()
    price_usd = State()


class PaySet(StatesGroup):
    zarinpal = State()
    now_key = State()
    now_ipn = State()
    card = State()
    card_holder = State()
    note = State()


class SettingS(StatesGroup):
    welcome = State()
    channel = State()


class BroadcastS(StatesGroup):
    message = State()
    confirm = State()


class Receipt(StatesGroup):
    waiting = State()
