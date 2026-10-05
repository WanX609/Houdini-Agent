# -*- coding: utf-8 -*-
"""专用表达式工具：分派、输入拒绝、快照、回滚、注册和确认模式。"""
import ast
import json
import inspect
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def expression_api(monkeypatch):
    hou = types.ModuleType("hou")
    hou.exprLanguage = types.SimpleNamespace(Hscript="Hscript", Python="Python")
    hou.parmTemplateType = types.SimpleNamespace(Float="Float", Int="Int", String="String")
    hou.hscript = Mock(return_value=("", ""))
    p = Mock()
    p.path.return_value = "/obj/geo1/tx"
    p.keyframes.return_value = ()
    p.parmTemplate.return_value.type.return_value = hou.parmTemplateType.Float
    p.eval.return_value = 7.0
    p.setExpression.side_effect = AssertionError("unsafe API must never be called")
    node = Mock()
    node.path.return_value = "/obj/geo1"
    node.parm.return_value = p
    hou.node = Mock(return_value=node)
    monkeypatch.setitem(sys.modules, "hou", hou)

    from houdini_agent.utils.mcp import client
    from houdini_agent.utils.mcp.tools import param_ops
    from houdini_agent.utils import hooks

    monkeypatch.setattr(client, "hou", hou)
    monkeypatch.setattr(param_ops, "hou", hou)
    monkeypatch.setattr(hooks, "get_hook_manager", lambda: Mock())
    return client.HoudiniMCP(), hou, node, p


def args(**updates):
    result = {"node_path": "/obj/geo1", "param_name": "tx", "expression": 'ch("../ref/tx") + 1'}
    result.update(updates)
    return result


def existing_expression(p, expression="$F", language="Hscript", time=0):
    key = Mock()
    key.time.return_value = time
    p.keyframes.return_value = (key,)
    p.expression.return_value = expression
    p.expressionLanguage.return_value = language


@pytest.mark.parametrize("parm_type", ["Float", "Int"])
def test_expression_tool_dispatches_and_snapshots_plain_value(expression_api, parm_type):
    api, hou, node, p = expression_api
    p.parmTemplate.return_value.type.return_value = parm_type
    result = api.execute_tool("set_parameter_expression", args())
    assert result["success"] is True
    snapshot = result["_undo_snapshot"]
    assert snapshot == {
        "node_path": "/obj/geo1", "param_name": "tx", "old_value": 7.0,
        "new_value": {"expr": 'ch("../ref/tx") + 1', "lang": "Hscript"},
        "is_tuple": False, "clear_keyframes": True,
    }
    json.dumps(snapshot)  # bridge 快照必须可序列化
    assert [call.args[0] for call in hou.hscript.call_args_list] == [
        "chadd -t 0 0 /obj/geo1 tx",
        'chkey -t 0 -v 0 -V 0 -m 0 -M 0 -a 0 -A 0 -F \'ch("../ref/tx") + 1\' /obj/geo1/tx',
    ]
    p.setExpression.assert_not_called()


def test_expression_tool_snapshots_existing_hscript_expression(expression_api):
    api, hou, node, p = expression_api
    existing_expression(p)
    result = api.execute_tool("set_parameter_expression", args())
    assert result["success"] is True
    assert result["_undo_snapshot"]["old_value"] == {"expr": "$F", "lang": "Hscript"}
    assert result["_undo_snapshot"]["clear_keyframes"] is False
    p.setExpression.assert_not_called()


def test_unchanged_expression_does_not_mutate_or_create_checkpoint(expression_api):
    api, hou, node, p = expression_api
    existing_expression(p, args()["expression"])
    result = api.execute_tool("set_parameter_expression", args())
    assert result["success"] is True
    assert "_undo_snapshot" not in result
    hou.hscript.assert_not_called()


@pytest.mark.parametrize("missing", ["node_path", "param_name", "expression"])
def test_required_arguments_are_reported(expression_api, missing):
    api, hou, node, p = expression_api
    values = args()
    values.pop(missing)
    result = api.execute_tool("set_parameter_expression", values)
    assert result["success"] is False
    assert missing in result["error"]
    assert "set_parameter_expression(" in result["error"]
    hou.hscript.assert_not_called()


@pytest.mark.parametrize("updates, error", [
    ({"language": "Python"}, "Python"),
    ({"language": None}, "Hscript"),
    ({"expression": ""}, "非空字符串"),
    ({"expression": " "}, "非空字符串"),
    ({"expression": None}, "非空字符串"),
    ({"expression": 1}, "非空字符串"),
    ({"expression": "ch('../ref/tx')"}, "单引号"),
    ({"node_path": "geo1"}, "绝对节点路径"),
    ({"param_name": None}, "单个参数名"),
    ({"param_name": ""}, "单个参数名"),
    ({"param_name": "../other/tx"}, "单个参数名"),
    ({"param_name": "/obj/other/tx"}, "单个参数名"),
])
def test_invalid_input_is_rejected_before_mutation(expression_api, updates, error):
    api, hou, node, p = expression_api
    result = api.execute_tool("set_parameter_expression", args(**updates))
    assert result["success"] is False
    assert error in result["error"]
    hou.hscript.assert_not_called()
    p.set.assert_not_called()
    p.setExpression.assert_not_called()


@pytest.mark.parametrize("state, error", [
    ("missing_node", "未找到节点"),
    ("missing_parm", "未找到单个参数"),
    ("string_parm", "Float/Int"),
    ("multiple_keys", "多关键帧"),
    ("nonzero_time", "非零时间"),
    ("value_key", "数值动画"),
    ("python_expression", "Python"),
    ("quoted_old_expression", "旧表达式含单引号"),
])
def test_unsupported_channel_state_is_preserved(expression_api, state, error):
    api, hou, node, p = expression_api
    if state == "missing_node":
        hou.node.return_value = None
    elif state == "missing_parm":
        node.parm.return_value = None
    elif state == "string_parm":
        p.parmTemplate.return_value.type.return_value = "String"
    elif state == "multiple_keys":
        p.keyframes.return_value = (object(), object())
    else:
        existing_expression(p)
        if state == "nonzero_time":
            p.keyframes.return_value[0].time.return_value = 1
        elif state == "value_key":
            p.expression.side_effect = RuntimeError("no expression")
        elif state == "python_expression":
            p.expressionLanguage.return_value = "Python"
        elif state == "quoted_old_expression":
            p.expression.return_value = "ch('../ref/tx')"
    result = api.execute_tool("set_parameter_expression", args())
    assert result["success"] is False
    assert error in result["error"]
    assert "_undo_snapshot" not in result
    hou.hscript.assert_not_called()
    p.deleteAllKeyframes.assert_not_called()
    p.setExpression.assert_not_called()


def test_hscript_error_rolls_back_unanimated_value(expression_api):
    api, hou, node, p = expression_api
    hou.hscript.side_effect = [("", ""), ("", "Invalid expression")]
    result = api.execute_tool("set_parameter_expression", args())
    assert result["success"] is False
    assert "chkey failed: Invalid expression" in result["error"]
    assert "_undo_snapshot" not in result
    p.deleteAllKeyframes.assert_called_once()
    p.set.assert_called_once_with(7.0)
    p.setExpression.assert_not_called()


def test_hscript_error_rolls_back_existing_hscript(expression_api):
    api, hou, node, p = expression_api
    existing_expression(p)
    hou.hscript.side_effect = [("", ""), ("", "Invalid expression"), ("", ""), ("", "")]
    result = api.execute_tool("set_parameter_expression", args())
    assert result["success"] is False
    assert "chkey failed" in result["error"]
    assert "-F '$F' /obj/geo1/tx" in hou.hscript.call_args.args[0]
    p.setExpression.assert_not_called()


def test_rollback_error_is_visible(expression_api):
    api, hou, node, p = expression_api
    hou.hscript.side_effect = [("", ""), ("", "Invalid expression")]
    p.deleteAllKeyframes.side_effect = RuntimeError("locked parameter")
    result = api.execute_tool("set_parameter_expression", args())
    assert result["success"] is False
    assert "恢复旧值失败: locked parameter" in result["error"]
    p.setExpression.assert_not_called()


def test_bridge_undo_removes_expression_channel_before_restoring_value(expression_api, monkeypatch):
    api, hou, node, p = expression_api
    snapshot = api.execute_tool("set_parameter_expression", args())["_undo_snapshot"]
    from houdini_agent.bridge import server
    monkeypatch.setattr(server, "_main_thread", lambda fn: fn())
    result = server._undo_node_op({"op": "modify", "snapshot": snapshot})
    assert result["success"] is True
    p.deleteAllKeyframes.assert_called_once()
    p.set.assert_called_once_with(7.0)
    p.setExpression.assert_not_called()


def test_tool_schema_and_read_only_mode_gates():
    from houdini_agent.utils.ai_client import HOUDINI_TOOLS
    from houdini_agent.utils.tool_registry import ToolRegistry
    definitions = [tool for tool in HOUDINI_TOOLS if tool["function"]["name"] == "set_parameter_expression"]
    assert len(definitions) == 1
    schema = definitions[0]["function"]["parameters"]
    assert schema["required"] == ["node_path", "param_name", "expression"]
    assert schema["properties"]["language"]["enum"] == ["Hscript"]
    registry = ToolRegistry()
    registry.register_core_tools(HOUDINI_TOOLS)
    assert registry.is_tool_allowed_in_mode("set_parameter_expression", "agent")
    assert registry.is_tool_allowed_in_mode("set_parameter_expression", "plan_executing")
    assert not registry.is_tool_allowed_in_mode("set_parameter_expression", "ask")
    assert not registry.is_tool_allowed_in_mode("set_parameter_expression", "plan_planning")


def test_external_mcp_tool_uses_the_same_safe_implementation(expression_api, monkeypatch):
    api, hou, node, p = expression_api
    from houdini_agent.utils.mcp import server
    registered = {}
    def register(fn=None, **options):
        if fn is None:
            return lambda target: register(target, **options)
        registered[options.get("name", fn.__name__)] = fn
        return fn
    monkeypatch.setattr(server, "mcp", types.SimpleNamespace(tool=register))
    monkeypatch.setattr(server, "hou", hou)
    server._setup_fastmcp_tools()
    assert list(inspect.signature(registered["set_parameter_expression"]).parameters) == [
        "node_path", "param_name", "expression", "language",
    ]
    result = registered["set_parameter_expression"](**args())
    assert result["status"] == "success"
    assert result["_undo_snapshot"]["old_value"] == 7.0
    assert hou.hscript.call_count == 2
    p.setExpression.assert_not_called()
    monkeypatch.delenv("HOUDINI_AGENT_SETEXPRESSION_GUARD", raising=False)
    hou.node.reset_mock()
    rejected = registered["execute_python_code"]('hou.node("/obj/geo1").parm("tx").setExpression("$F")')
    assert rejected["status"] == "error"
    assert "set_parameter_expression" in rejected["message"]
    hou.node.assert_not_called()
    allowed = registered["execute_python_code"]('"set_expression" in globals()')
    assert allowed["status"] == "success"
    assert allowed["data"]["result"] == "False"


@pytest.mark.parametrize("path, constant", [
    ("core/agent_runner.py", "_CONFIRM_TOOLS"),
    ("ui/plan_mixin.py", "_SELF_TRACKING_TOOLS"),
    ("ui/run_mixin.py", "_MUTATING_TOOLS"),
    ("ui/ai_tab.py", "_COOK_TRIGGERING_TOOLS"),
    ("ui_qml/controller.py", "CONFIRM_TOOLS"),
    ("ui_qml/controller.py", "_MUTATING"),
    ("ui_qml/controller.py", "_COOK_TRIGGERING"),
])
def test_ui_mutation_metadata_includes_expression_tool(path, constant):
    # Qt 不可用时读取声明，确保新工具不会绕过确认或场景修改保护。
    root = Path(__file__).resolve().parents[1] / "houdini_agent"
    tree = ast.parse((root / path).read_text(encoding="utf-8"))
    for entry in ast.walk(tree):
        if isinstance(entry, ast.Assign) and any(isinstance(t, ast.Name) and t.id == constant for t in entry.targets):
            value = entry.value.args[0] if isinstance(entry.value, ast.Call) else entry.value
            if isinstance(value, ast.Set):
                assert "set_parameter_expression" in ast.literal_eval(value)
                return
    pytest.fail("tool metadata not found: %s %s" % (path, constant))
