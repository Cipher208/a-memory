"""Hook Loader - triggers decorator-based registration by importing hook modules."""


def load_all_hooks() -> None:
    """Import hook modules to trigger @hook_registry.mark decorators."""
