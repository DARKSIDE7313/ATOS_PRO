class KillSwitchError(Exception):
    pass


class KillSwitch:
    def __init__(self):
        self._triggered = False

    def check(self):
        if self._triggered:
            raise KillSwitchError("Kill switch triggered")

    def trigger(self):
        self._triggered = True

    def reset(self):
        self._triggered = False
