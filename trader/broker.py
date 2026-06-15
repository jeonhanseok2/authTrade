from abc import ABC, abstractmethod


class Broker(ABC):
    @abstractmethod
    def submit_market_order(self, symbol: str, qty: int, side: str) -> dict: ...

    @abstractmethod
    def list_positions(self) -> dict: ...

    @abstractmethod
    def get_account(self) -> dict: ...
