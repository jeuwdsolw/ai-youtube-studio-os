"""Помилки агентів і адаптерів (окремо від пакета agents, щоб адаптери не створювали циклічних імпортів)."""


class RetryableError(RuntimeError):
    """Тимчасова помилка — задачу варто повторити."""


class BlockedError(RuntimeError):
    """Повторення не допоможе — потрібне рішення людини (ключ API, погодження, ліміт)."""


class LimitError(RetryableError):
    """Ліміт підписки/квоти: задачу відкласти до `until` (UTC), спробу не зараховувати."""

    def __init__(self, msg: str, until=None):
        super().__init__(msg)
        self.until = until
