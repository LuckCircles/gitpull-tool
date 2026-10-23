"""仓库数据模型 — GitRepoCandidate。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GitRepoCandidate:
    name: str
    path: str
    is_git: bool
    git_path: str
