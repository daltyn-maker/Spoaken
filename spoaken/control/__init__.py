"""
control - Application Control Layer
====================================

Controller, command parser, and update management.
"""

from spoaken.control.controller import TranscriptionController
from spoaken.control.commands import CommandParser

__all__ = [
    "TranscriptionController",
    "CommandParser",
]
