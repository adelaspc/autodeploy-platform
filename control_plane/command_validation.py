from __future__ import annotations

import shlex


MAX_COMMAND_LENGTH = 255
SHELL_CONTROL_PATTERNS = ("\x00", "\n", "\r", ";", "&&", "||", "|", "<", ">", "`", "$(")
SHELL_EXECUTABLES = {"sh", "bash", "dash", "zsh", "fish"}


class CommandValidationError(ValueError):
    pass


def parse_optional_command(value, field_name="command"):
    if value is None:
        return None
    if not isinstance(value, str):
        raise CommandValidationError(f"Invalid {field_name}. Expected a string")
    if not value.strip():
        raise CommandValidationError(f"Invalid {field_name}. Expected a non-empty string")
    if len(value) > MAX_COMMAND_LENGTH:
        raise CommandValidationError(f"Invalid {field_name}. Expected at most {MAX_COMMAND_LENGTH} characters")

    if any(pattern in value for pattern in SHELL_CONTROL_PATTERNS):
        raise CommandValidationError(
            f"Invalid {field_name}. Shell control characters and operators are not supported"
        )

    try:
        args = shlex.split(value)
    except ValueError as exc:
        raise CommandValidationError(f"Invalid {field_name}. Could not parse command arguments") from exc

    if not args:
        raise CommandValidationError(f"Invalid {field_name}. Expected a non-empty command")

    executable = args[0].rsplit("/", 1)[-1]
    if executable in SHELL_EXECUTABLES and len(args) > 1 and args[1] == "-c":
        raise CommandValidationError(f"Invalid {field_name}. Shell wrappers with -c are not supported")

    return args


def validate_optional_command(value, field_name):
    try:
        parse_optional_command(value, field_name)
    except CommandValidationError as exc:
        return str(exc)
    return None
