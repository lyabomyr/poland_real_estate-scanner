"""GitHub Actions workflow dispatch helpers used by Telegram bot commands."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

import requests


@dataclass
class DispatchResult:
    workflow_run_id: Optional[int]
    html_url: Optional[str]


class GitHubWorkflowDispatcher:
    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        workflow_id: str,
        ref: str,
        token: str,
    ):
        self.owner = owner
        self.repo = repo
        self.workflow_id = workflow_id
        self.ref = ref
        self.token = token

    @classmethod
    def from_env(
        cls,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> "GitHubWorkflowDispatcher | None":
        env_map = os.environ if env is None else env
        owner = (env_map.get("GITHUB_REPOSITORY_OWNER") or "").strip()
        repo = (env_map.get("GITHUB_REPOSITORY_NAME") or "").strip()
        workflow_id = (env_map.get("GITHUB_SCAN_WORKFLOW_FILE") or "scan.yml").strip()
        ref = (env_map.get("GITHUB_SCAN_WORKFLOW_REF") or "main").strip()
        token = (env_map.get("GITHUB_WORKFLOW_TOKEN") or "").strip()
        if not (owner and repo and workflow_id and ref and token):
            return None
        return cls(
            owner=owner,
            repo=repo,
            workflow_id=workflow_id,
            ref=ref,
            token=token,
        )

    def dispatch_scan(
        self,
        *,
        trigger_chat_id: str,
        trigger_chat_title: Optional[str],
        trigger_user_id: Optional[int],
        trigger_user_name: Optional[str],
        command: str = "scan",
    ) -> DispatchResult:
        url = (
            "https://api.github.com/repos/"
            f"{self.owner}/{self.repo}/actions/workflows/{self.workflow_id}/dispatches"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload = {
            "ref": self.ref,
            "inputs": {
                "trigger_command": command,
                "trigger_chat_id": str(trigger_chat_id),
                "trigger_chat_title": (trigger_chat_title or "")[:120],
                "trigger_user_id": str(trigger_user_id or ""),
                "trigger_user_name": (trigger_user_name or "")[:120],
            },
        }
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json() if response.content else {}
        return DispatchResult(
            workflow_run_id=data.get("workflow_run_id"),
            html_url=data.get("html_url"),
        )
