"""Storage backends the agent's file tools can be pointed at."""
from __future__ import annotations

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult


class ReadOnlyFilesystemBackend(FilesystemBackend):
    """Serves a local folder and refuses every change. Skills are code the agent runs, so it must never be able to alter them
    (this copy is shared by every session in the process)."""

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=f"{file_path} is read-only")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return EditResult(error=f"{file_path} is read-only")

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=f"{file_path} is read-only")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


def with_local_skills(sandbox, skills_path: str, local_dir) -> CompositeBackend:
    """Backend whose file tools read ``skills_path`` from a local copy of the skills (so the agent can list and read them without a
    sandbox existing yet) and everything else, including running commands, from the sandbox. The same skills are copied INTO the
    sandbox when it is created, so their scripts can run there."""
    return CompositeBackend(default=sandbox, routes={skills_path.rstrip("/") + "/": ReadOnlyFilesystemBackend(root_dir=local_dir, virtual_mode=True)})
