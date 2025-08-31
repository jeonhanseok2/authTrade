# trader/broker.py
from abc import ABC, abstractmethod
class Broker(ABC):
    @abstractmethod
    def submit_market_order(self, symbol:str, qty:int, side:str)->dict: ...
    @abstractmethod
    def get_positions(self)->list[dict]: ...
Z
