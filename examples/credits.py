"""Резерв и списание кредитов.

Почему не списывать сразу при запуске: цену генерации мы знаем заранее только
приблизительно. Провайдеры считают по факту — по длительности, разрешению и
реальному расходу, и он бывает меньше оценки. Списать больше, чем потрачено,
нечестно; списать после и обнаружить, что денег уже нет, — дыра.

Поэтому две ступени:

    оценка ──▶ РЕЗЕРВ ──▶ генерация ──▶ СПИСАНИЕ min(оценка, факт)
                  │                            │
                  └── деньги заняты ───────────┴── разница освобождается

Пока задача идёт, деньги удержаны: потратить их второй раз нельзя, но и списаны
они не были. При неудаче возвращать нечего — удержание просто снимается.

Пример самостоятельный. Гонки за кошелёк здесь показаны блокировкой строки в
базе; в примере она заменена комментарием, чтобы код запускался без СУБД.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class InsufficientCredits(Exception):
    """Не хватает свободных кредитов."""


@dataclass
class Wallet:
    """Кошелёк.

    `balance` — сколько всего.
    `reserved` — сколько удержано под идущие задачи.
    Доступно к трате: balance - reserved.
    """

    balance: int
    reserved: int = 0
    ledger: list[str] = field(default_factory=list)

    @property
    def available(self) -> int:
        return self.balance - self.reserved


def reserve(wallet: Wallet, amount: int, task_id: int) -> None:
    """Удержать кредиты под задачу."""
    if amount <= 0:
        raise ValueError("резерв должен быть положительным")
    # В боевом коде здесь блокировка строки кошелька:
    #   SELECT … FROM users WHERE id = :id FOR UPDATE
    # Без неё два одновременных запроса прочитают один и тот же остаток и
    # оба пройдут проверку — клиент уйдёт в минус.
    if wallet.available < amount:
        raise InsufficientCredits(f"доступно {wallet.available}, нужно {amount}")
    wallet.reserved += amount
    wallet.ledger.append(f"резерв {amount} по задаче {task_id}")


def commit(wallet: Wallet, quoted: int, metered: int, task_id: int) -> int:
    """Списать по факту, разницу вернуть. Возвращает списанное."""
    # Больше оценки не списываем никогда: клиенту показали цену, и она —
    # потолок. Если провайдер насчитал больше, это наш просчёт, не его.
    charged = min(quoted, metered)
    wallet.balance -= charged
    wallet.reserved -= quoted
    wallet.ledger.append(f"списано {charged} по задаче {task_id} (оценка {quoted}, факт {metered})")
    if charged < quoted:
        wallet.ledger.append(f"освобождено {quoted - charged}")
    return charged


def release(wallet: Wallet, amount: int, task_id: int, reason: str) -> None:
    """Снять удержание, ничего не списывая: задача не удалась."""
    wallet.reserved -= amount
    wallet.ledger.append(f"освобождено {amount} по задаче {task_id} — {reason}")


if __name__ == "__main__":
    w = Wallet(balance=100)

    reserve(w, 25, task_id=1)
    print(f"после резерва:  всего {w.balance}, занято {w.reserved}, свободно {w.available}")

    # Провайдер израсходовал меньше оценки — списываем по факту.
    commit(w, quoted=25, metered=18, task_id=1)
    print(f"после списания: всего {w.balance}, занято {w.reserved}, свободно {w.available}")

    reserve(w, 30, task_id=2)
    release(w, 30, task_id=2, reason="провайдер отклонил запрос")
    print(f"после отказа:   всего {w.balance}, занято {w.reserved}, свободно {w.available}")

    try:
        reserve(w, 1000, task_id=3)
    except InsufficientCredits as exc:
        print(f"перерасход не прошёл: {exc}")

    print("\nдвижения по счёту:")
    for line in w.ledger:
        print(" ", line)
