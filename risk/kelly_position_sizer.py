class KellyPositionSizer:
    def __init__(self, win_rate=0.55, win_loss_ratio=2.0,
                 kelly_fraction=0.5, max_position_pct=0.05):
        self.win_rate = win_rate
        self.win_loss_ratio = win_loss_ratio
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct

    def calculate_position(self, equity, price, confidence=1.0):
        full_kelly = (self.win_rate * self.win_loss_ratio - (1 - self.win_rate)) / self.win_loss_ratio
        full_kelly = max(0, full_kelly)
        adjusted_kelly = full_kelly * self.kelly_fraction * confidence
        position_pct = min(adjusted_kelly, self.max_position_pct)
        dollar_amount = equity * position_pct
        return max(int(dollar_amount / price), 0)

    def update_performance(self, win_rate, win_loss_ratio):
        self.win_rate = win_rate
        self.win_loss_ratio = win_loss_ratio
