"""Re-export for the console script. Real implementation lives in capture.daemon."""

from screen_workflow.capture.daemon import main

__all__ = ["main"]
