"""仓库数据模型 — GitRepoInfo / GitRepoCandidate。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GitRepoInfo:
    url: str


@dataclass
class GitRepoCandidate:
    name: str
    path: str
    is_git: bool
    git_path: str
