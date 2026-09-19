from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agent_message.core.state import StateStore
from agent_message.core.transfer import (
    SCRIPT_PATH,
    ensure_send_script,
    pending_requests,
    read_request,
    result_path,
    spool_dir,
)
from agent_message.orchestration.spool import SpoolWatcher

from tests.helpers import make_config


class TransferScriptInstallTests(unittest.TestCase):
    def test_installs_executable_script_that_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = ensure_send_script(root)
            assert installed is not None
            self.assertEqual(installed, root / SCRIPT_PATH)
            self.assertTrue(installed.stat().st_mode & 0o111)
            self.assertTrue(spool_dir(root).is_dir())
            self.assertEqual(spool_dir(root).stat().st_mode & 0o777, 0o777)
            first = installed.read_text(encoding="utf-8")

            again = ensure_send_script(root)
            assert again is not None
            self.assertEqual(again.read_text(encoding="utf-8"), first)

    def test_refreshes_stale_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / SCRIPT_PATH
            target.parent.mkdir(parents=True)
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            installed = ensure_send_script(root)
            assert installed is not None
            self.assertIn("send-to-feishu", installed.read_text(encoding="utf-8"))


@unittest.skipIf(shutil.which("sh") is None, "requires a POSIX shell")
class TransferScriptExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = ensure_send_script(self.root)
        assert self.script is not None

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(self.script), *args],
            cwd=str(cwd or self.root),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_writes_project_relative_request_without_waiting(self) -> None:
        (self.root / "reports").mkdir()
        (self.root / "reports" / "result.png").write_bytes(b"png")

        completed = self.run_script("reports/result.png", "0")

        self.assertEqual(completed.returncode, 0)
        requests = pending_requests(self.root)
        self.assertEqual(len(requests), 1)
        self.assertEqual(read_request(requests[0]), "reports/result.png")

    def test_resolves_paths_from_a_subdirectory_cwd(self) -> None:
        (self.root / "build").mkdir()
        (self.root / "build" / "log.txt").write_text("log", encoding="utf-8")

        completed = self.run_script("log.txt", "0", cwd=self.root / "build")

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(read_request(pending_requests(self.root)[0]), "build/log.txt")

    def test_rejects_files_outside_the_project(self) -> None:
        outside = Path(self.root.parent) / f"{self.root.name}-outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            completed = self.run_script(str(outside), "0")
            self.assertEqual(completed.returncode, 1)
            self.assertIn("项目目录内", completed.stderr)
            self.assertEqual(pending_requests(self.root), [])
        finally:
            outside.unlink()

    def test_rejects_missing_file_and_bad_usage(self) -> None:
        missing = self.run_script("nope.txt", "0")
        self.assertEqual(missing.returncode, 1)
        usage = self.run_script()
        self.assertEqual(usage.returncode, 2)
        self.assertIn("用法", usage.stderr)

    def test_waiting_call_returns_the_bridge_result(self) -> None:
        config = make_config(self.root, ("alpha",))
        project = config.projects["alpha"].path
        (project / "plot.png").write_bytes(b"png")
        script = ensure_send_script(project)
        assert script is not None
        state = StateStore(config)
        media: list[tuple[str, str, Path]] = []

        async def send_media(chat_id: str, kind: str, path: Path) -> None:
            media.append((chat_id, kind, path))

        try:
            state.create_task(
                project_alias="alpha",
                agent=config.projects["alpha"].default_agent,
                chat_id="chat-1",
                owner_open_id="ou-1",
                prompt="work",
            )
            state.claimed_run()
            process = subprocess.Popen(
                ["sh", str(script), "plot.png", "10"],
                cwd=str(project),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                for _ in range(200):
                    if pending_requests(project):
                        break
                    self.assertFalse(process.poll() is not None, "script exited before queueing")
                    time.sleep(0.05)
                else:
                    self.fail("script never queued a request")
                asyncio.run(SpoolWatcher(config, state, send_media).poll_once())
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        finally:
            state.close()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("已发送", stdout)
        self.assertEqual(media, [("chat-1", "image", project / "plot.png")])
        self.assertEqual(pending_requests(project), [])


class SpoolWatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_config(self.root, ("alpha",))
        self.state = StateStore(self.config)
        self.media: list[tuple[str, str, Path]] = []
        self.watcher = SpoolWatcher(self.config, self.state, self.send_media)

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    async def send_media(self, chat_id: str, kind: str, path: Path) -> None:
        self.media.append((chat_id, kind, path))

    def queue_request(self, project: str, relative: str, name: str = "1-1") -> Path:
        directory = spool_dir(self.config.projects[project].path)
        directory.mkdir(parents=True, exist_ok=True)
        request = directory / f"{name}.req"
        request.write_text(relative + "\n", encoding="utf-8")
        return request

    def start_run(self, project: str = "alpha", chat_id: str = "chat-1") -> None:
        self.state.create_task(
            project_alias=project,
            agent=self.config.projects[project].default_agent,
            chat_id=chat_id,
            owner_open_id="ou-1",
            prompt="work",
        )
        self.assertIsNotNone(self.state.claimed_run())

    def read_result(self, request: Path) -> str:
        path = result_path(request)
        self.assertTrue(path.is_file(), f"missing result for {request}")
        return path.read_text(encoding="utf-8")

    def test_delivers_request_to_the_projects_chat(self) -> None:
        project_path = self.config.projects["alpha"].path
        (project_path / "plot.png").write_bytes(b"png")
        self.start_run()
        request = self.queue_request("alpha", "plot.png")

        asyncio.run(self.watcher.poll_once())

        self.assertEqual(self.media, [("chat-1", "image", project_path / "plot.png")])
        self.assertTrue(self.read_result(request).startswith("ok\n"))
        self.assertFalse(request.exists())

    def test_rejects_paths_outside_the_project_and_missing_files(self) -> None:
        self.start_run()
        escape = self.queue_request("alpha", "../outside.txt", name="1-1")
        missing = self.queue_request("alpha", "nope.txt", name="2-2")

        asyncio.run(self.watcher.poll_once())

        self.assertEqual(self.media, [])
        self.assertTrue(self.read_result(escape).startswith("error\n"))
        self.assertTrue(self.read_result(missing).startswith("error\n"))

    def test_reports_missing_chat_context_and_send_failure(self) -> None:
        project_path = self.config.projects["alpha"].path
        (project_path / "report.pdf").write_bytes(b"pdf")
        idle = self.queue_request("alpha", "report.pdf", name="1-1")

        asyncio.run(self.watcher.poll_once())

        self.assertEqual(self.media, [])
        self.assertIn("会话", self.read_result(idle))

        self.start_run()
        failing = self.queue_request("alpha", "report.pdf", name="2-2")

        async def boom(_chat: str, _kind: str, _path: Path) -> None:
            raise RuntimeError("feishu unavailable")

        failing_watcher = SpoolWatcher(self.config, self.state, boom)
        asyncio.run(failing_watcher.poll_once())

        self.assertIn("feishu unavailable", self.read_result(failing))
        self.assertFalse(failing.exists())

    def test_request_is_consumed_before_sending(self) -> None:
        project_path = self.config.projects["alpha"].path
        (project_path / "plot.png").write_bytes(b"png")
        self.start_run()
        request = self.queue_request("alpha", "plot.png")

        seen: list[bool] = []

        async def observe(_chat: str, _kind: str, _path: Path) -> None:
            seen.append(request.exists())

        asyncio.run(SpoolWatcher(self.config, self.state, observe).poll_once())

        self.assertEqual(seen, [False])


if __name__ == "__main__":
    unittest.main()
