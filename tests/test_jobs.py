"""Stdlib-only tests for job/command validation, GPU/Conda helpers and log tailing."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mcp"))

import job_tools
from remote_ws import commands
from remote_ws.sources import jobs as jobs_source
from remote_ws.validation import FileToolError, LoginError, ToolError

actual_t_jobs = job_tools.t_jobs
actual_t_tail_log = job_tools.t_tail_log


def context(project="demo", server="alpha"):
    return {
        "SERVER": server,
        "PROJECT": project,
        "CODE_ROOT": "/code",
        "DATA_ROOT": "/data",
        "CONDA_ROOT": "/conda",
        "CONDA_INIT": "",
        "CONFIGURED": True,
        "FORMAT_VERSION": "3",
    }


class CommandsTests(unittest.TestCase):
    def test_validate_arguments_combines_missing_and_invalid_fields(self):
        with self.assertRaises(ToolError) as raised:
            commands.validate_arguments(
                {"extra": 1}, {"command": str, "gpus": (str, int)}, ("command", "name"))
        message = str(raised.exception)
        self.assertIn("缺少必填字段: command, name", message)
        self.assertIn("不允许字段: extra", message)
        self.assertNotIn("允许字段及类型", message)
        self.assertNotIn("command（字符串）", message)
        self.assertNotIn("gpus（字符串或整数）", message)

    def test_validate_arguments_reports_types_and_allowed_fields(self):
        with self.assertRaises(ToolError) as raised:
            commands.validate_arguments(
                {"command": 1, "gpus": True},
                {"command": str, "gpus": (str, int)})
        message = str(raised.exception)
        self.assertIn("参数类型错误", message)
        self.assertIn("command 期望字符串，收到int", message)
        self.assertIn("gpus 期望字符串或整数，收到bool", message)
        self.assertNotIn("允许字段及类型", message)

    def test_normalize_gpus_accepts_non_negative_integers(self):
        self.assertEqual(commands.normalize_gpus(0), "0")
        self.assertEqual(commands.normalize_gpus(3), "3")
        self.assertEqual(commands.normalize_gpus("0,1"), "0,1")
        with self.assertRaises(ToolError):
            commands.normalize_gpus(-1)
        with self.assertRaises(ToolError):
            commands.normalize_gpus(True)

    def test_exec_normalizes_integer_gpus(self):
        calls = []

        def run(name, script, timeout, max_output_chars=None):
            calls.append((name, script, timeout, max_output_chars))
            return 0, "ok", "", 0.1

        with mock.patch.object(commands.config, "resolve_context", return_value=context()) as resolve, \
                mock.patch.object(commands, "default_server", return_value="other"):
            text, failed = commands.t_exec({"command": "echo hi", "gpus": 0}, run=run)
        self.assertFalse(failed)
        self.assertIn("exit_code: 0", text)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", calls[0][1])
        resolve.assert_called_once_with(None)

    def test_exec_rejects_negative_gpus_before_context_or_remote(self):
        run = mock.Mock()
        with mock.patch.object(commands.config, "resolve_context") as resolve:
            with self.assertRaises(ToolError):
                commands.t_exec({"command": "echo hi", "gpus": -1}, run=run)
        resolve.assert_not_called()
        run.assert_not_called()

    def test_exec_clamps_output_budget_with_note(self):
        calls = []

        def run(name, script, timeout, max_output_chars=None):
            calls.append(max_output_chars)
            return 0, "ok", "", 0.1

        with mock.patch.object(commands.config, "resolve_context", return_value=context()), \
                mock.patch.object(commands, "default_server", return_value="alpha"):
            text, failed = commands.t_exec(
                {"command": "echo hi", "max_output_chars": 30001}, run=run)
        self.assertFalse(failed)
        self.assertEqual(calls, [30000])
        self.assertIn("parameter_note: max_output_chars 超过 30000，已按 30000 执行。", text)

    def test_exec_output_budget_at_cap_has_no_note(self):
        calls = []

        def run(name, script, timeout, max_output_chars=None):
            calls.append(max_output_chars)
            return 0, "ok", "", 0.1

        with mock.patch.object(commands.config, "resolve_context", return_value=context()), \
                mock.patch.object(commands, "default_server", return_value="alpha"):
            text, failed = commands.t_exec(
                {"command": "echo hi", "max_output_chars": 30000}, run=run)
        self.assertFalse(failed)
        self.assertEqual(calls, [30000])
        self.assertNotIn("parameter_note", text)

    def test_exec_rejects_output_budget_below_native_minimum(self):
        run = mock.Mock()
        with self.assertRaises(FileToolError):
            commands.t_exec({"command": "echo hi", "max_output_chars": 255}, run=run)
        run.assert_not_called()

    def test_gpus_resolves_context_before_remote(self):
        run = mock.Mock()
        with mock.patch.object(commands.config, "resolve_context",
                               side_effect=LoginError("服务器未配置")) as resolve:
            with self.assertRaises(LoginError):
                commands.t_gpus({"server": "missing"}, run=run)
        resolve.assert_called_once_with("missing")
        run.assert_not_called()

    def test_gpus_with_no_free_card_does_not_suggest_zero(self):
        output = ("0, GPU-0, A100, 9000, 40000, 80\n"
                  "@@APPS\n"
                  "GPU-0\t123\t8000\talice 01:00\n")

        def run(name, script, timeout, max_output_chars=None):
            return 0, output, "", 0.1

        with mock.patch.object(commands.config, "resolve_context", return_value=context()):
            text = commands.t_gpus({}, run=run)
        self.assertIn("空闲 GPU: 无", text)
        self.assertNotIn('gpus="0"', text)

    def test_gpus_with_free_card_still_suggests_it(self):
        output = ("0, GPU-0, A100, 10, 40000, 0\n"
                  "@@APPS\n")

        def run(name, script, timeout, max_output_chars=None):
            return 0, output, "", 0.1

        with mock.patch.object(commands.config, "resolve_context", return_value=context()):
            text = commands.t_gpus({}, run=run)
        self.assertIn('gpus="0"', text)

    def test_conda_details_with_init_uses_conda_root_python_and_envs(self):
        calls = []

        def run(name, script, timeout, max_output_chars=None):
            calls.append((name, script))
            return 0, "listed", "", 0.1

        ctx = context()
        ctx["CONDA_INIT"] = "/conda/etc/profile.d/conda.sh"
        with mock.patch.object(commands.config, "resolve_context", return_value=ctx):
            text, failed = commands.t_conda_envs({"details": True}, run=run)
        self.assertFalse(failed)
        script = calls[0][1]
        self.assertIn("source /conda/etc/profile.d/conda.sh", script)
        self.assertIn("/conda/bin/python", script)
        self.assertIn("/conda/envs/*/", script)
        self.assertIn('"$HOME"/.conda/envs/*/', script)
        self.assertNotIn("v=$(python -c", script)


class JobControlTests(unittest.TestCase):
    def fake_run(self, calls):
        def run(name, script, timeout, max_output_chars=None):
            calls.append((name, script, timeout, max_output_chars))
            return 0, "pid=1 status=running log=/data/demo/logs/train.log", "", 0.1
        return run

    def test_start_defaults_cwd_to_project_code_root(self):
        calls = []
        fake_uuid = mock.Mock()
        fake_uuid.hex = "a" * 32
        with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                mock.patch.object(job_tools.uuid, "uuid4", return_value=fake_uuid):
            text, failed = job_tools.t_run_background(
                {"command": "python train.py", "name": "train", "project": "demo", "gpus": 0},
                run=self.fake_run(calls))
        self.assertFalse(failed)
        script = calls[0][1]
        self.assertIn("cd /code/demo", script)
        self.assertIn("mkdir -p /data/demo/logs", script)
        self.assertIn("CUDA_VISIBLE_DEVICES=0", script)
        self.assertIn("task_id=" + "a" * 32, text)

    def test_start_keeps_explicit_cwd(self):
        cases = [
            ("relative", "cd /code/relative"),
            ("~/project", "cd ~/project"),
            ("/absolute/project", "cd /absolute/project"),
            ("", "cd /code/demo"),
        ]
        for explicit, expected in cases:
            calls = []
            with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                    mock.patch.object(job_tools.uuid, "uuid4") as uuid4:
                uuid4.return_value.hex = "b" * 32
                job_tools.t_run_background(
                    {"command": "true", "name": "train", "project": "demo", "cwd": explicit},
                    run=self.fake_run(calls))
            self.assertIn(expected, calls[0][1], explicit)

    def test_start_rejects_negative_gpus_before_context_or_remote(self):
        run = mock.Mock()
        with mock.patch.object(job_tools.config, "resolve_context") as resolve:
            with self.assertRaises(ToolError):
                job_tools.t_run_background(
                    {"command": "true", "name": "train", "project": "demo", "gpus": -1},
                    run=run)
        resolve.assert_not_called()
        run.assert_not_called()

    def test_control_start_accepts_neutral_cross_branch_fields(self):
        captured = {}

        def start(payload, run=None):
            captured.update(payload)
            return "ok", False

        with mock.patch.object(job_tools, "t_run_background", side_effect=start):
            result = job_tools.t_job_control({
                "action": "start", "project": "demo", "command": "true", "name": "train",
                "job": "", "force": False, "gpus": 0})
        self.assertEqual(result, ("ok", False))
        self.assertEqual(captured["job"], "")
        self.assertFalse(captured["force"])
        self.assertEqual(captured["gpus"], "0")

    def test_control_start_rejects_negative_gpus(self):
        with mock.patch.object(job_tools, "t_run_background") as start:
            with self.assertRaises(ToolError):
                job_tools.t_job_control({
                    "action": "start", "project": "demo", "command": "true",
                    "name": "train", "gpus": -1})
        start.assert_not_called()

    def test_control_start_rejects_non_neutral_cross_branch_fields(self):
        for extra, label in (({"job": "a" * 32}, "job"), ({"force": True}, "force=true")):
            with mock.patch.object(job_tools, "t_run_background") as start:
                with self.assertRaises(ToolError) as raised:
                    job_tools.t_job_control({
                        "action": "start", "project": "demo", "command": "true",
                        "name": "train", **extra})
            start.assert_not_called()
            message = str(raised.exception)
            self.assertIn("action=start", message)
            self.assertIn(label, message)

    def test_control_start_requires_command_name_and_project(self):
        with mock.patch.object(job_tools, "t_run_background") as start:
            with self.assertRaises(ToolError) as raised:
                job_tools.t_job_control({"action": "start"})
        start.assert_not_called()
        message = str(raised.exception)
        self.assertIn("缺少必填字段", message)
        for key in ("command", "name", "project"):
            self.assertIn(key, message)

    def test_control_start_requires_explicit_project(self):
        with mock.patch.object(job_tools, "t_run_background") as start:
            with self.assertRaises(ToolError) as raised:
                job_tools.t_job_control({"action": "start", "command": "true", "name": "train"})
        start.assert_not_called()
        self.assertIn("缺少必填字段: project", str(raised.exception))

    def test_control_start_invalid_project_rejected_before_remote(self):
        with mock.patch.object(job_tools, "t_run_background") as start:
            with self.assertRaises(ToolError) as raised:
                job_tools.t_job_control({
                    "action": "start", "project": "bad/project", "command": "true",
                    "name": "train"})
        start.assert_not_called()
        self.assertIn("合法 project", str(raised.exception))

    def test_control_start_unknown_field_reports_allowed_fields(self):
        with mock.patch.object(job_tools, "t_run_background") as start:
            with self.assertRaises(ToolError) as raised:
                job_tools.t_job_control({
                    "action": "start", "project": "demo", "command": "true",
                    "name": "train", "bogus": 1})
        start.assert_not_called()
        message = str(raised.exception)
        self.assertIn("不允许字段: bogus", message)
        self.assertNotIn("允许字段及类型", message)

    def test_control_stop_accepts_neutral_cross_branch_fields(self):
        captured = {}

        def stop(payload):
            captured.update(payload)
            return "stopped"

        with mock.patch.object(job_tools, "t_stop_job", side_effect=stop):
            result = job_tools.t_job_control({
                "action": "stop", "job": "b" * 32, "command": "", "name": "",
                "cwd": "", "conda_env": "", "gpus": "", "confirmed": False,
                "force": True})
        self.assertEqual(result, "stopped")
        self.assertEqual(captured["command"], "")
        self.assertEqual(captured["gpus"], "")
        self.assertFalse(captured["confirmed"])
        self.assertTrue(captured["force"])

    def test_control_stop_rejects_non_neutral_cross_branch_fields(self):
        cases = [
            ({"command": "rm -rf x"}, "command"),
            ({"name": "train"}, "name"),
            ({"cwd": "/x"}, "cwd"),
            ({"conda_env": "env"}, "conda_env"),
            ({"gpus": 0}, "gpus=0"),
            ({"confirmed": True}, "confirmed=true"),
        ]
        for extra, label in cases:
            with mock.patch.object(job_tools, "t_stop_job") as stop:
                with self.assertRaises(ToolError) as raised:
                    job_tools.t_job_control({
                        "action": "stop", "job": "b" * 32, **extra})
            stop.assert_not_called()
            message = str(raised.exception)
            self.assertIn("action=stop", message)
            self.assertIn(label, message)

    def test_control_stop_keeps_empty_project_and_requires_full_id(self):
        captured = {}

        def stop(payload):
            captured.update(payload)
            return "stopped"

        with mock.patch.object(job_tools, "t_stop_job", side_effect=stop):
            result = job_tools.t_job_control({
                "action": "stop", "job": "d" * 32, "project": ""})
        self.assertEqual(result, "stopped")
        self.assertEqual(captured["project"], "")
        with mock.patch.object(job_tools, "t_stop_job") as stop:
            with self.assertRaises(ToolError):
                job_tools.t_job_control({"action": "stop", "job": "d" * 12, "project": ""})
        stop.assert_not_called()

    def test_control_stop_rejects_short_id(self):
        with mock.patch.object(job_tools, "t_stop_job") as stop:
            with self.assertRaises(ToolError) as raised:
                job_tools.t_job_control({"action": "stop", "job": "b" * 12})
        stop.assert_not_called()
        self.assertIn("完整32位", str(raised.exception))

    def test_tail_adds_full_identity_and_legacy_job_meta(self):
        record = {
            "log": "/data/alpha/logs/train-1.log",
            "pid": "123",
            "name": "train",
            "status": "running",
            "project": "alpha",
            "job_id": "c" * 32,
            "identity_version": 1,
        }
        seen = {}

        def request(server, operation, **params):
            seen.update(params)
            return {"text": "hello", "cursor": "cur", "more": False}

        with mock.patch.object(job_tools.config, "resolve_context",
                               return_value=context(project=None)), \
                mock.patch.object(job_tools, "find_job", return_value=record), \
                mock.patch.object(job_tools, "request", side_effect=request):
            value = json.loads(job_tools.t_tail_log({"job": "train"}))
        self.assertEqual(value["task_id"], "c" * 12)
        self.assertEqual(value["job_id"], "c" * 32)
        self.assertEqual(value["project"], "alpha")
        self.assertEqual(value["log"], record["log"])
        self.assertEqual(value["status"], "running")
        self.assertEqual(seen["job_meta"], record)


class ResourceQueryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.gpus_calls = []
        self.gpus_patch = mock.patch.object(commands, "t_gpus", side_effect=self.fake_gpus)
        self.gpus_patch.start()
        self.addCleanup(self.gpus_patch.stop)

    def fake_gpus(self, payload, run=None):
        self.gpus_calls.append(dict(payload))
        return "alpha GPU 状态:\n空闲 GPU: 无"

    def test_gpus_neutral_name_and_details_are_ignored_with_note(self):
        for extra in ({"name": ""}, {"details": False}, {"name": "", "details": False}):
            with self.subTest(extra=extra):
                self.gpus_calls.clear()
                text = commands.t_resource_query({"resource": "gpus", **extra})
                self.assertEqual(self.gpus_calls, [{}])
                self.assertIn("alpha GPU 状态", text)
                self.assertIn("parameter_note", text)
                self.assertIn("未发送远程", text)
                self.assertIn("resource='conda'", text)

    def test_gpus_normal_output_is_unchanged(self):
        text = commands.t_resource_query({"resource": "gpus"})
        self.assertEqual(text, "alpha GPU 状态:\n空闲 GPU: 无")
        self.assertNotIn("parameter_note", text)
        self.assertEqual(self.gpus_calls, [{}])

    def test_conda_empty_name_is_omitted(self):
        calls = []

        def query(payload, run=None):
            calls.append(dict(payload))
            return "envs"

        with mock.patch.object(commands, "t_conda_envs", side_effect=query):
            self.assertEqual(commands.t_resource_query({"resource": "conda", "name": ""}), "envs")
        self.assertEqual(calls, [{}])

    def test_conda_nonempty_name_is_preserved(self):
        calls = []

        def query(payload, run=None):
            calls.append(dict(payload))
            return "env"

        with mock.patch.object(commands, "t_conda_envs", side_effect=query):
            self.assertEqual(commands.t_resource_query({"resource": "conda", "name": "torch"}), "env")
        self.assertEqual(calls, [{"name": "torch"}])

    def test_gpus_non_neutral_name_or_details_points_to_conda(self):
        cases = [
            ({"name": "env"}, "name"),
            ({"details": True}, "details=true"),
            ({"name": "env", "details": True}, "details=true"),
        ]
        for extra, label in cases:
            with self.subTest(extra=extra):
                self.gpus_calls.clear()
                with self.assertRaises(ToolError) as raised:
                    commands.t_resource_query({"resource": "gpus", **extra})
                self.assertEqual(self.gpus_calls, [])
                message = str(raised.exception)
                self.assertIn("resource='conda'", message)
                self.assertIn(label, message)

    def test_gpus_wrong_types_are_rejected(self):
        for extra in ({"name": 0}, {"details": 0}, {"name": None}):
            with self.subTest(extra=extra):
                with self.assertRaises(ToolError):
                    commands.t_resource_query({"resource": "gpus", **extra})
        self.assertEqual(self.gpus_calls, [])


class JobQueryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.list_calls = []
        self.tail_calls = []
        self.list_patch = mock.patch.object(job_tools, "t_jobs", side_effect=self.fake_list)
        self.tail_patch = mock.patch.object(job_tools, "t_tail_log", side_effect=self.fake_tail)
        self.list_patch.start()
        self.tail_patch.start()
        self.addCleanup(self.list_patch.stop)
        self.addCleanup(self.tail_patch.stop)

    def fake_list(self, payload):
        self.list_calls.append(dict(payload))
        return "tasks"

    def fake_tail(self, payload):
        self.tail_calls.append(dict(payload))
        return json.dumps({"text": "log"}, ensure_ascii=False, separators=(",", ":"))

    def test_list_accepts_neutral_tail_fields_and_appends_note(self):
        text = job_tools.t_job_query({
            "action": "list", "job": "", "cursor": "", "query": "",
            "errors": False, "context_lines": 0, "lines": 0, "max_chars": 0})
        self.assertEqual(self.list_calls, [{}])
        self.assertIn("tasks\nparameter_note:", text)
        for label in ("job=''", "cursor=''", "query=''", "errors=false",
                      "context_lines=0", "lines=0", "max_chars=0"):
            self.assertIn(label, text)

    def test_list_normal_output_is_unchanged(self):
        self.assertEqual(job_tools.t_job_query({"action": "list"}), "tasks")
        self.assertEqual(self.list_calls, [{}])

    def test_list_non_neutral_tail_fields_are_rejected(self):
        cases = [
            ({"job": "train"}, "job"),
            ({"cursor": "token"}, "cursor"),
            ({"query": "error"}, "query"),
            ({"errors": True}, "errors"),
            ({"context_lines": 1}, "context_lines"),
            ({"lines": 1}, "lines"),
            ({"max_chars": 100}, "max_chars"),
        ]
        for extra, label in cases:
            with self.subTest(extra=extra):
                self.list_calls.clear()
                with self.assertRaises(ToolError) as raised:
                    job_tools.t_job_query({"action": "list", **extra})
                self.assertEqual(self.list_calls, [])
                message = str(raised.exception)
                self.assertIn("action=tail", message)
                self.assertIn(label, message)

    def test_list_native_limit_and_verbose_still_work(self):
        self.assertEqual(job_tools.t_job_query(
            {"action": "list", "limit": 5, "verbose": True}), "tasks")
        self.assertEqual(self.list_calls, [{"limit": 5, "verbose": True}])

    def test_list_over_budget_is_clamped_with_note(self):
        calls = []

        def list_jobs(name, limit, project, context):
            calls.append(limit)
            return []

        with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                mock.patch.object(job_tools, "list_jobs", side_effect=list_jobs):
            text = actual_t_jobs({"limit": 501})
        self.assertEqual(calls, [500])
        self.assertIn("parameter_note: limit 超过 500，已按 500 执行。", text)

    def test_list_at_cap_has_no_budget_note(self):
        with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                mock.patch.object(job_tools, "list_jobs", return_value=[]):
            text = actual_t_jobs({"limit": 500})
        self.assertNotIn("parameter_note", text)

    def test_list_empty_project_is_omitted(self):
        self.assertEqual(job_tools.t_job_query({"action": "list", "project": ""}), "tasks")
        self.assertEqual(self.list_calls, [{}])

    def test_tail_accepts_neutral_list_fields_and_adds_json_note(self):
        text = job_tools.t_job_query(
            {"action": "tail", "job": "train", "limit": 0, "verbose": False})
        self.assertEqual(self.tail_calls, [{"job": "train"}])
        value = json.loads(text)
        self.assertEqual(value["text"], "log")
        self.assertIn("limit=0", value["parameter_note"])
        self.assertIn("verbose=false", value["parameter_note"])
        self.assertIn("未改变 tail 操作", value["parameter_note"])

    def test_tail_normal_output_is_unchanged(self):
        expected = json.dumps({"text": "log"}, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(job_tools.t_job_query({"action": "tail", "job": "train"}), expected)
        self.assertEqual(self.tail_calls, [{"job": "train"}])

    def test_tail_over_budget_is_clamped_and_noted(self):
        record = {"log": "/data/demo/logs/train.log", "status": "running"}
        seen = {}

        def request(server, operation, **params):
            seen.update(params)
            return {"text": "log", "cursor": "next", "more": False}

        with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                mock.patch.object(job_tools, "find_job", return_value=record), \
                mock.patch.object(job_tools, "request", side_effect=request):
            value = json.loads(actual_t_tail_log({
                "job": "train", "lines": 2001, "max_chars": 12001, "context_lines": 11}))
        self.assertEqual((seen["lines"], seen["max_chars"], seen["context_lines"]),
                         (2000, 12000, 10))
        self.assertEqual(value["parameter_note"],
                         "lines 超过 2000，已按 2000 执行；max_chars 超过 12000，已按 12000 执行；context_lines 超过 10，已按 10 执行")

    def test_tail_at_budgets_has_no_note(self):
        record = {"log": "/data/demo/logs/train.log", "status": "running"}
        seen = {}

        def request(server, operation, **params):
            seen.update(params)
            return {"text": "log", "cursor": "next", "more": False}

        with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                mock.patch.object(job_tools, "find_job", return_value=record), \
                mock.patch.object(job_tools, "request", side_effect=request):
            value = json.loads(actual_t_tail_log({
                "job": "train", "lines": 2000, "max_chars": 12000, "context_lines": 10}))
        self.assertEqual((seen["lines"], seen["max_chars"], seen["context_lines"]),
                         (2000, 12000, 10))
        self.assertNotIn("parameter_note", value)

    def test_tail_empty_optional_values_are_omitted(self):
        record = {"log": "/data/demo/logs/train.log", "status": "running"}
        seen = {}

        def request(server, operation, **params):
            seen.update(params)
            return {"text": "log", "cursor": "next", "more": False}

        with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                mock.patch.object(job_tools, "find_job", return_value=record), \
                mock.patch.object(job_tools, "request", side_effect=request):
            value = json.loads(actual_t_tail_log({
                "job": "train", "project": "", "query": "", "cursor": ""}))
        self.assertEqual(value["text"], "log")
        self.assertIsNone(seen["cursor"])
        self.assertIsNone(seen["query"])

    def test_tail_non_neutral_list_fields_are_rejected(self):
        cases = [
            ({"limit": 1}, "limit"),
            ({"limit": -1}, "limit"),
            ({"verbose": True}, "verbose=true"),
        ]
        for extra, label in cases:
            with self.subTest(extra=extra):
                self.tail_calls.clear()
                with self.assertRaises(ToolError) as raised:
                    job_tools.t_job_query(
                        {"action": "tail", "job": "train", **extra})
                self.assertEqual(self.tail_calls, [])
                message = str(raised.exception)
                self.assertIn("action=list", message)
                self.assertIn(label, message)

    def test_tail_still_requires_job_with_neutral_fields(self):
        with self.assertRaises(ToolError) as raised:
            job_tools.t_job_query({"action": "tail", "limit": 0})
        self.assertEqual(self.tail_calls, [])
        self.assertIn("job 必填", str(raised.exception))


class ExecTimeoutTests(unittest.TestCase):
    def exec_result(self, timeout_sec=None, include=False):
        calls = []

        def run(name, script, timeout, max_output_chars=None):
            calls.append((name, script, timeout, max_output_chars))
            return 0, "ok", "", 0.1

        args = {"command": "echo hi"}
        if include:
            args["timeout_sec"] = timeout_sec
        with mock.patch.object(commands.config, "resolve_context", return_value=context()), \
                mock.patch.object(commands, "default_server", return_value="alpha"):
            text, failed = commands.t_exec(args, run=run)
        return text, failed, calls[0][2]

    def test_timeout_above_limit_is_clamped_with_note(self):
        text, failed, used = self.exec_result(1801, include=True)
        self.assertFalse(failed)
        self.assertEqual(used, 1800)
        self.assertTrue(text.startswith("exit_code: 0\nok"))
        self.assertIn("parameter_note", text)
        self.assertIn("1800", text)

    def test_timeout_at_limit_has_no_note(self):
        text, failed, used = self.exec_result(1800, include=True)
        self.assertFalse(failed)
        self.assertEqual(used, 1800)
        self.assertNotIn("parameter_note", text)

    def test_zero_and_missing_timeout_keep_default_120(self):
        for include, value in ((False, None), (True, 0)):
            with self.subTest(include=include, value=value):
                text, failed, used = self.exec_result(value, include=include)
                self.assertFalse(failed)
                self.assertEqual(used, 120)
                self.assertNotIn("parameter_note", text)

    def test_negative_timeout_is_clamped_to_one_with_note(self):
        text, failed, used = self.exec_result(-5, include=True)
        self.assertFalse(failed)
        self.assertEqual(used, 1)
        self.assertTrue(text.startswith("exit_code: 0\nok"))
        self.assertIn("parameter_note", text)
        self.assertIn("1 秒", text)


class LegacyVerboseJobsTests(unittest.TestCase):
    def test_verbose_list_defaults_for_missing_legacy_fields(self):
        legacy = {
            "name": "old",
            "status": "legacy/unverified",
            "pid": "123",
            "log": "/data/demo/logs/old.log",
            "project": "demo",
        }
        with mock.patch.object(job_tools.config, "resolve_context", return_value=context()), \
                mock.patch.object(job_tools, "list_jobs", return_value=[legacy]):
            text = job_tools.t_jobs({"verbose": True})
        self.assertIn("start=- last_write=- gpus=- env=-", text)
        self.assertIn("old [legacy/unverified]", text)
        self.assertIn("id=legacy", text)


class RemoteJobsSourceTests(unittest.TestCase):
    @staticmethod
    def namespace(root):
        marker = "\ntry:\n    a = json.load(sys.stdin)"
        source, separator, _ = jobs_source.REMOTE_SOURCE.partition(marker)
        if not separator:
            raise AssertionError("remote jobs source entry point not found")
        namespace = {"roots": [root]}
        exec(compile(source, "<remote-ws-jobs>", "exec"), namespace)
        return namespace

    @staticmethod
    def tail_args(path, **overrides):
        value = dict(log=path, lines=10, max_chars=2000, cursor=None,
                     source_server="alpha", job_meta=None, query=None,
                     errors=False, context_lines=2)
        value.update(overrides)
        return value

    def test_scoped_tail_id_uses_one_request_and_keeps_cursor_checks(self):
        import io
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            project = 'demo'
            logs = Path(root, project, 'logs')
            logs.mkdir(parents=True)
            jid = 'a' * 32
            path = logs / ('job-' + jid[:12] + '.log')
            path.write_text('# remote-ws job-v2 ' + json.dumps(dict(job_id=jid, pid='0', name='test')) + '\nstart\n')
            args = dict(action='tail', server='alpha', project=project, job=jid, lines=1)

            def request(server, payload, timeout, source):
                wire = mock.Mock(buffer=io.BytesIO())
                with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), mock.patch.object(sys, 'stdout', wire):
                    try:
                        exec(compile(source, '<remote>', 'exec'), {})
                    except SystemExit:
                        pass
                value = json.loads(wire.buffer.getvalue())
                if not value.pop('ok'):
                    raise FileToolError(value['error'])
                return value

            with mock.patch.object(job_tools.config, 'resolve_context', return_value=dict(context(), DATA_ROOT=root)), \
                    mock.patch.object(job_tools.file_tools, 'roots_config', return_value=[root]), \
                    mock.patch.object(job_tools.transport, 'remote_json', side_effect=request) as rpc:
                first = json.loads(job_tools.t_job_query(args))
                self.assertEqual(rpc.call_count, 1)
                self.assertEqual(first['text'], 'start\n')
                self.assertEqual((first['job_id'], first['task_id'], first['log'], first['project']),
                                 (jid, jid[:12], str(path), project))
                for i in range(10):
                    with path.open('a') as stream:
                        stream.write(str(i) + '\n')
                    args['cursor'] = first['cursor']
                    args['compact'] = i > 0
                    first = json.loads(job_tools.t_job_query(args))
                    self.assertEqual('log' in first, i == 0)
                    self.assertEqual('job_id' in first, i == 0)
                    self.assertIn('status', first)
                    self.assertEqual(first['text'], str(i) + '\n')
                self.assertEqual(rpc.call_count, 11)
                args['cursor'] = first['cursor']
                with self.assertRaisesRegex(FileToolError, '游标'):
                    job_tools.t_job_query(dict(args, query='changed'))
                path.rename(path.with_suffix('.old'))
                path.write_bytes(path.with_suffix('.old').read_bytes())
                with self.assertRaisesRegex(FileToolError, '游标'):
                    job_tools.t_job_query(args)
                with self.assertRaisesRegex(FileToolError, '找不到'):
                    job_tools.t_job_query(dict(args, job='b' * 32))

    def test_tail_replaces_invalid_utf8_and_marks_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "job.log")
            with open(path, "wb") as stream:
                stream.write(b"ok\n\xff\n")
            value = self.namespace(root)["tail_page"](self.tail_args(path))
        self.assertIn("\ufffd", value["text"])
        self.assertTrue(value.get("invalid_utf8"))
        self.assertNotIn("pending_line", value)

    def test_filtered_tail_replaces_invalid_utf8_and_marks_it(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "job.log")
            with open(path, "wb") as stream:
                stream.write(b"ok \xff\n")
            value = self.namespace(root)["tail_page"](
                self.tail_args(path, query="ok", context_lines=0))
        self.assertIn("\ufffd", value["text"])
        self.assertTrue(value.get("invalid_utf8"))

    def test_legacy_active_partial_line_is_pending(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "job.log")
            with open(path, "wb") as stream:
                stream.write(b"line1\npartial\xe2")
            meta = {"pid": str(os.getpid()), "uid": os.getuid(),
                    "identity_version": 1}
            value = self.namespace(root)["tail_page"](
                self.tail_args(path, job_meta=meta))
        self.assertEqual(value["text"], "line1\n")
        self.assertTrue(value.get("pending_line"))
        self.assertNotIn("\ufffd", value["text"])
        self.assertFalse(value.get("invalid_utf8", False))

    def test_legacy_finished_partial_line_is_returned_with_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "job.log")
            with open(path, "wb") as stream:
                stream.write(b"line1\npartial\xe2")
            meta = {"pid": "0", "uid": os.getuid(), "identity_version": 1}
            value = self.namespace(root)["tail_page"](
                self.tail_args(path, job_meta=meta))
        self.assertIn("partial\ufffd", value["text"])
        self.assertTrue(value.get("invalid_utf8"))
        self.assertNotIn("pending_line", value)

    def test_legacy_completed_with_live_pid_is_not_pending(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "job.log")
            with open(path, "wb") as stream:
                stream.write(b"line1\npartial\xe2")
            meta = {"pid": str(os.getpid()), "uid": os.getuid(),
                    "identity_version": 1, "status": "exit=0"}
            value = self.namespace(root)["tail_page"](
                self.tail_args(path, job_meta=meta))
        self.assertIn("partial\ufffd", value["text"])
        self.assertTrue(value.get("invalid_utf8"))
        self.assertNotIn("pending_line", value)

    def test_legacy_log_header_still_parses(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "job.log")
            header = (f"# remote-ws job pid={os.getpid()} name=old "
                      f"project=demo uid={os.getuid()}\n")
            with open(path, "wb") as stream:
                stream.write(header.encode("ascii"))
            record = self.namespace(root)["log_record"](path)
        self.assertEqual(record["identity_version"], 1)
        self.assertEqual(record["name"], "old")
        self.assertEqual(record["project"], "demo")
        self.assertEqual(record["status"], "legacy/unverified")


if __name__ == "__main__":
    unittest.main()