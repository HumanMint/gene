"""Separate a definite controller refusal from an uncertain transport outcome."""

from redbot.core import commands


class ControllerRejected(commands.UserFeedbackCheckFailure):
    """A controller 4xx response confirms this submission was rejected."""
