# -*- coding: utf-8 -*-
"""setExpression GUI 段错误防护：完全离线，禁止调用真实 hou API。"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from houdini_agent.utils import safe_expr


@pytest.mark.parametrize("code, expected", [
    ('parm.setExpression("$F")', True),
    ('obj . setExpression ("$F")', True),
    ('obj.\n setExpression\t("$F")', True),
    ('set_expression(parm, "$F")', False),
    ('myExpression("$F")', False),
    ('parm.setExpressionLanguage(lang)', False),
    ('parm.setExpression', False),
    ('# parm.setExpression("$F")', True),
    ('text = "parm.setExpression("', True),
    ('', False),
    (None, False),
])
def test_code_uses_set_expression(code, expected):
    assert safe_expr.code_uses_set_expression(code) is expected


def test_guard_enabled_by_default(monkeypatch):
    monkeypatch.delenv(safe_expr.GUARD_ENV_VAR, raising=False)
    assert safe_expr.guard_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", " FALSE ", " Off "])
def test_guard_opt_out(monkeypatch, value):
    monkeypatch.setenv(safe_expr.GUARD_ENV_VAR, value)
    assert safe_expr.guard_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "anything"])
def test_guard_other_values_enable(monkeypatch, value):
    monkeypatch.setenv(safe_expr.GUARD_ENV_VAR, value)
    assert safe_expr.guard_enabled() is True


@pytest.mark.parametrize("expression, expected", [
    ('ch("../ref/tx") + 1', '\'ch("../ref/tx") + 1\''),
    ('$F * 2', "'$F * 2'"),
    ('', "''"),
    ("ch('../ref/tx')", None),
])
def test_hscript_escape(expression, expected):
    assert safe_expr._hscript_escape(expression) == expected


@pytest.fixture
def fake_hou(monkeypatch):
    hou = types.ModuleType("hou")
    hou.hscript = Mock(return_value=("", ""))
    hou.exprLanguage = types.SimpleNamespace(Hscript="Hscript", Python="Python")
    monkeypatch.setitem(sys.modules, "hou", hou)
    return hou


@pytest.fixture
def parm():
    p = Mock()
    p.path.return_value = "/obj/geo1/tx"
    p.keyframes.return_value = ()
    p.setExpression.side_effect = AssertionError("unsafe API must never be called")
    return p


def test_none_parm_returns_error(fake_hou):
    ok, message = safe_expr.set_expression_safe(None, "$F")
    assert ok is False
    assert "parm is None" in message
    fake_hou.hscript.assert_not_called()


def test_single_quote_rejected_before_channel_mutation(fake_hou, parm):
    parm.keyframes.return_value = (object(), object())
    ok, message = safe_expr.set_expression_safe(parm, "ch('../ref/tx')")
    assert ok is False
    assert "single quote" in message
    fake_hou.hscript.assert_not_called()
    parm.setExpression.assert_not_called()


@pytest.mark.parametrize("key_count", [0, 1, 2, 3])
def test_exact_channel_commands(fake_hou, parm, key_count):
    parm.keyframes.return_value = tuple(object() for _ in range(key_count))
    ok, message = safe_expr.set_expression_safe(parm, 'ch("../ref/tx") + 1')
    assert ok is True
    assert "/obj/geo1/tx" in message
    expected = (["chrm /obj/geo1/tx"] if key_count > 1 else []) + [
        "chadd -t 0 0 /obj/geo1 tx",
        'chkey -t 0 -v 0 -V 0 -m 0 -M 0 -a 0 -A 0 -F \'ch("../ref/tx") + 1\' /obj/geo1/tx',
    ]
    assert [call.args[0] for call in fake_hou.hscript.call_args_list] == expected
    parm.setExpression.assert_not_called()


@pytest.mark.parametrize("error", ["Channel already exists", "Channel exists"])
def test_existing_channel_is_benign(fake_hou, parm, error):
    fake_hou.hscript.side_effect = [("", error), ("", "")]
    assert safe_expr.set_expression_safe(parm, "$F")[0] is True
    assert fake_hou.hscript.call_count == 2
    parm.setExpression.assert_not_called()


def test_chadd_error_stops_before_chkey(fake_hou, parm):
    fake_hou.hscript.return_value = ("", "Invalid node")
    ok, message = safe_expr.set_expression_safe(parm, "$F")
    assert ok is False
    assert message == "chadd failed: Invalid node"
    assert fake_hou.hscript.call_count == 1
    parm.setExpression.assert_not_called()


def test_chkey_error_has_no_unsafe_fallback(fake_hou, parm):
    fake_hou.hscript.side_effect = [("", ""), ("", "Invalid expression")]
    ok, message = safe_expr.set_expression_safe(parm, "$F")
    assert ok is False
    assert message == "chkey failed: Invalid expression"
    parm.setExpression.assert_not_called()


def test_hscript_exception_returns_error(fake_hou, parm):
    fake_hou.hscript.side_effect = RuntimeError("hscript failed")
    assert safe_expr.set_expression_safe(parm, "$F") == (False, "RuntimeError: hscript failed")
    parm.setExpression.assert_not_called()


def test_hou_unavailable_returns_error(monkeypatch, parm):
    monkeypatch.setitem(sys.modules, "hou", None)
    assert safe_expr.set_expression_safe(parm, "$F") == (
        False, "hou module unavailable (not inside Houdini)",
    )
    parm.setExpression.assert_not_called()


@pytest.fixture
def executor(fake_hou):
    # 单独加载 mixin，避免 mcp 包初始化拉入 HTTP/Qt 服务依赖。
    path = Path(__file__).resolve().parents[1] / "houdini_agent/utils/mcp/tools/exec_ops.py"
    spec = importlib.util.spec_from_file_location("_safe_expr_exec_ops_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    obj = module.ExecOpsMixin()
    obj._stop_event = None
    return obj


def test_execute_python_guard_blocks_before_any_code_runs(executor, fake_hou, monkeypatch):
    monkeypatch.delenv(safe_expr.GUARD_ENV_VAR, raising=False)
    fake_hou.record = Mock()
    ok, result = executor.execute_python('hou.record(); hou.parm.setExpression("$F")')
    assert ok is False
    assert result["error"] == safe_expr.SET_EXPRESSION_GUIDANCE
    fake_hou.record.assert_not_called()
    fake_hou.hscript.assert_not_called()


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_execute_python_guard_can_be_disabled(executor, fake_hou, monkeypatch, value):
    monkeypatch.setenv(safe_expr.GUARD_ENV_VAR, value)
    fake_hou.parm = Mock()
    ok, result = executor.execute_python('hou.parm.setExpression("$F")')
    assert ok is True, result
    fake_hou.parm.setExpression.assert_called_once_with("$F")


@pytest.mark.parametrize("code", [
    "'set_expression' in globals()",
    "print('set_expression' in globals())",
])
def test_execute_python_does_not_inject_expression_helper(executor, fake_hou, monkeypatch, code):
    monkeypatch.delenv(safe_expr.GUARD_ENV_VAR, raising=False)
    ok, result = executor.execute_python(code)
    assert ok is True, result
    assert result["return_value"] == "False" or result["output"].strip() == "False"
    fake_hou.hscript.assert_not_called()


@pytest.mark.parametrize("language, error", [
    ("Hscript", ""),
    ("Hscript", "Invalid expression"),
    ("Python", ""),
])
def test_bridge_undo_routes_language_and_reports_safe_failure(fake_hou, parm, monkeypatch, language, error):
    from houdini_agent.bridge import server

    monkeypatch.setattr(server, "_main_thread", lambda fn: fn())
    log = Mock()
    monkeypatch.setattr(server, "_log", log)
    fake_hou.node = Mock(return_value=types.SimpleNamespace(parm=lambda name: parm))
    fake_hou.hscript.side_effect = [("", ""), ("", error)]
    if language == "Python":
        # 仅此手动撤销分支保留原 API；调用仍是 mock。
        parm.setExpression.side_effect = None

    result = server._undo_node_op({
        "op": "modify",
        "snapshot": {
            "node_path": "/obj/geo1", "param_name": "tx",
            "old_value": {"expr": "$F", "lang": language},
        },
    })
    assert result["success"] is (not bool(error))
    if language == "Python":
        parm.setExpression.assert_called_once_with("$F", fake_hou.exprLanguage.Python)
        fake_hou.hscript.assert_not_called()
    else:
        parm.setExpression.assert_not_called()
        assert fake_hou.hscript.call_count == 2
    if error:
        assert result["error"] == "chkey failed: Invalid expression"
        log.assert_called_once()
    else:
        log.assert_not_called()


def test_guard_guidance_points_to_dedicated_tool():
    assert "set_parameter_expression(" in safe_expr.SET_EXPRESSION_GUIDANCE
    assert "set_node_parameter" in safe_expr.SET_EXPRESSION_GUIDANCE
    assert "injected helper" not in safe_expr.SET_EXPRESSION_GUIDANCE
