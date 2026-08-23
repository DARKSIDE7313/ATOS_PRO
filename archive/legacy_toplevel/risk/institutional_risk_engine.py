class InstitutionalRiskEngine:
    def __init__(self, kill_switch, position_sizer):
        self.kill_switch = kill_switch
        self.sizer = position_sizer
        self.peak_equity = None
        self.daily_start_equity = None

    def check_risk(self, current_equity):
        if self.peak_equity is None:
            self.peak_equity = current_equity
        if self.daily_start_equity is None:
            self.daily_start_equity = current_equity

        self.peak_equity = max(self.peak_equity, current_equity)

        drawdown = (self.peak_equity - current_equity) / self.peak_equity
        if drawdown > 0.15:
            self.kill_switch.trigger()
            print(f"[RISK] 最大回撤 {drawdown:.1%} 触发 Kill Switch")
            return False

        daily_loss = (self.daily_start_equity - current_equity) / self.daily_start_equity
        if daily_loss > 0.02:
            print(f"[RISK] 单日亏损 {daily_loss:.1%} 超过 2%，今日停止")
            return False

        return True

    def approve_signal(self, equity, price, confidence, regime_multiplier):
        if not self.check_risk(equity):
            return 0
        base_qty = self.sizer.calculate_position(equity, price, confidence)
        return max(int(base_qty * regime_multiplier), 0)

    def reset_daily(self):
        self.daily_start_equity = None
