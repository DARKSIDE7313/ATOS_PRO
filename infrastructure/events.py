from dataclasses import dataclass
from datetime import datetime


@dataclass
class MarketEvent:
    ticker: str
    close: float
    volume: int
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


@dataclass
class SignalEvent:
    ticker: str
    side: str  # BUY or SELL
    confidence: float
    price: float
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()
