"""Expected browser and profile outcomes."""


class PauseRun(Exception):
    """A human must check the browser before any more follows."""


class RetryJob(Exception):
    """The browser could not determine a result; recheck later."""


class UnavailableJob(Exception):
    """This profile is gone or inaccessible at its current handle."""
