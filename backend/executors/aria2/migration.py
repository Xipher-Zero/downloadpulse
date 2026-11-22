"""Decode the persisted v1 executor identity without contacting the daemon."""
from transfers.models import ExecutionHandle


def legacy_handle(row, attempt_id, candidate=None, *, download_folder: str):
    """``download_folder``: the configured download folder, handed in by the migration
    that owns this upgrade -- this executor never reads application settings itself."""
    gid = str(row.get("download_id") or "").strip()
    if not gid:
        return None
    from executors.aria2.executor import execution_binding
    from executors.aria2.runtime import RPC_IDENTITY
    binding = execution_binding(download_folder, RPC_IDENTITY)
    # The v1 native job identity is already known: it binds as the native
    # identity; the durable correlation carries only target/redaction/binding.
    return ExecutionHandle("aria2", attempt_id, {"target": str(row.get("local_path") or ""), "binding": binding},
                           {"gid": gid})
