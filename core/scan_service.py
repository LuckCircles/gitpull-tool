"""Repository discovery service, independent from Qt."""

from __future__ import annotations

import os
from pathlib import Path

from models.repo import GitRepoCandidate


class ScanService:
    """Discover direct-child Git repositories in a base directory."""

    @staticmethod
    def scan_git_repos(base_path: str) -> list[GitRepoCandidate]:
        if not base_path or not base_path.strip():
            return []
        if ".." in Path(base_path).parts:
            return []

        base_abs = os.path.abspath(base_path)
        if not os.path.isdir(base_abs):
            return []

        candidates: list[GitRepoCandidate] = []
        base_prefix = base_abs.rstrip("\\\\/") + os.sep
        try:
            for entry_name in os.listdir(base_abs):
                child_path = os.path.abspath(os.path.join(base_abs, entry_name))
                if not child_path.startswith(base_prefix) or not os.path.isdir(child_path):
                    continue

                git_path = os.path.abspath(os.path.join(child_path, ".git"))
                if not git_path.startswith(child_path.rstrip("\\\\/") + os.sep):
                    continue
                if os.path.exists(git_path) and os.path.isdir(git_path):
                    candidates.append(
                        GitRepoCandidate(
                            name=entry_name,
                            path=child_path,
                            is_git=True,
                            git_path=git_path,
                        )
                    )
        except OSError:
            return []
        return candidates
