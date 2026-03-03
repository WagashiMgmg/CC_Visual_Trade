# Shared runtime state (in-process only, resets on restart)
from collections import deque
from datetime import datetime

cycle_running: bool = False

# Price history for emergency rapid-move detection: list of (datetime, price)
price_history: deque[tuple[datetime, float]] = deque(maxlen=120)

# Last emergency cycle timestamp (None = never triggered)
last_emergency_at: datetime | None = None
