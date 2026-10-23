# rate_limiter.py — модуль для ограничения частоты запросов пользователей.
#
# Защищает бота от спама и перегрузки Google API.

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List


class RateLimiter:
    """Ограничивает количество запросов от одного пользователя."""

    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        """
        Инициализирует лимитер.

        Параметры:
            max_requests    — максимальное количество запросов в окне
            window_seconds  — размер окна в секундах
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: Dict[int, List[datetime]] = defaultdict(list)

    def is_allowed(self, user_id: int) -> bool:
        """
        Проверяет, разрешён ли запрос от пользователя.

        Возвращает True если запрос разрешён, False если лимит превышен.
        """
        now = datetime.now()

        # Удаляем старые записи вне окна
        self.requests[user_id] = [
            t for t in self.requests[user_id]
            if now - t < timedelta(seconds=self.window_seconds)
        ]

        # Проверяем лимит
        if len(self.requests[user_id]) >= self.max_requests:
            return False

        # Регистрируем запрос
        self.requests[user_id].append(now)
        return True

    def reset(self, user_id: int) -> None:
        """Сбрасывает счётчик для пользователя."""
        if user_id in self.requests:
            del self.requests[user_id]


# Глобальный экземпляр лимитера
rate_limiter = RateLimiter(max_requests=10, window_seconds=60)
