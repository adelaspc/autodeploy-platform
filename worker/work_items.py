from worker.processing.command_processor import process_next_pending_command
from worker.processing.pipeline import process_next_pending_deployment


def process_next_work_item():
    command_result = process_next_pending_command()
    if command_result is not None:
        return command_result
    return process_next_pending_deployment()
