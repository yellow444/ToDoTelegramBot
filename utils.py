from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def separate_callback_data(data: str) -> list[str]:
    """Separate the callback data into its parts."""

    return data.split(";")


def task_markup(done: bool = False) -> InlineKeyboardMarkup:
    """Return inline keyboard markup for task messages.

    When ``done`` is ``True``, the first button allows to mark task as undone.
    """

    first_button = (
        InlineKeyboardButton("Выполнено", callback_data="undone")
        if done
        else InlineKeyboardButton("✔️ Выполнить", callback_data="done")
    )
    keyboard = [
        [
            first_button,
            InlineKeyboardButton("📅 Напомнить", callback_data="date"),
            InlineKeyboardButton("❌ Удалить", callback_data="del"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

