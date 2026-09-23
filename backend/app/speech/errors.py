"""Ошибка провайдера. Живёт в самом нижнем слое (speech), чтобы call и агентный слой
могли её использовать без импортов вверх; модуль call реэкспортирует тот же класс."""


class ProviderUnavailable(Exception):
    """Провайдер не настроен или упал. Ход не роняем: отдаём событие error с этим кодом."""

    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
