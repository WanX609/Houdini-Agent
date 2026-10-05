from __future__ import annotations

from typing import Any, Optional, Dict, List, Tuple

from houdini_agent.utils.safe_expr import _hscript_escape, set_expression_safe

try:
    import hou  # type: ignore
except Exception:
    hou = None  # type: ignore


class ParamOpsMixin:
    """Parameter get/set operations."""

    def set_parameter(self, node_path: str, param_name: str, value: Any) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """设置节点参数（设置前自动快照旧值，支持撤销）

        Returns:
            (success, message, undo_snapshot)
            undo_snapshot 包含 node_path, param_name, old_value, new_value
        """
        if hou is None:
            return False, "未检测到 Houdini API", None

        node = hou.node(node_path)
        if node is None:
            return False, f"未找到节点: {node_path}", None

        # 尝试获取参数
        parm = node.parm(param_name)
        if parm is None:
            # 尝试作为元组参数
            parm_tuple = node.parmTuple(param_name)
            if parm_tuple is None:
                # 列出相似参数名帮助 AI 纠正
                try:
                    all_parms = [p.name() for p in node.parms()]
                    hint_lower = param_name.lower()
                    similar = [p for p in all_parms if hint_lower in p.lower() or p.lower() in hint_lower][:8]
                    err = f"节点 {node_path} 不存在参数 '{param_name}'"
                    if similar:
                        err += f"\n相似参数: {', '.join(similar)}"
                    else:
                        # 列出前 15 个参数供参考
                        sample = all_parms[:15]
                        err += f"\n该节点可用参数(前15): {', '.join(sample)}"
                        if len(all_parms) > 15:
                            err += f" ... 共 {len(all_parms)} 个"
                except Exception:
                    err = f"未找到参数: {param_name}"
                return False, err, None

            if isinstance(value, (list, tuple)):
                try:
                    # 快照旧值（元组参数）
                    old_value = list(parm_tuple.eval())
                    parm_tuple.set(value)
                    new_value = list(parm_tuple.eval())
                    snapshot = {
                        "node_path": node_path,
                        "param_name": param_name,
                        "old_value": old_value,
                        "new_value": new_value,
                        "is_tuple": True,
                    }
                    return True, f"已设置 {node_path} {param_name}: {old_value} → {new_value}", snapshot
                except Exception as exc:
                    return False, f"设置失败: {exc}", None
            else:
                return False, f"参数 {param_name} 需要列表或元组值", None

        try:
            # 快照旧值（标量参数）
            try:
                old_expr = parm.expression()
                old_lang = str(parm.expressionLanguage())
                old_value = {"expr": old_expr, "lang": old_lang}
            except Exception:
                old_value = parm.eval()

            parm.set(value)
            actual_value = parm.eval()
            snapshot = {
                "node_path": node_path,
                "param_name": param_name,
                "old_value": old_value,
                "new_value": actual_value,
                "is_tuple": False,
            }
            return True, f"已设置 {node_path} {param_name}: {old_value} → {actual_value}", snapshot
        except Exception as exc:
            return False, f"设置失败: {exc}", None

    def set_parameter_expression(self, node_path: str, param_name: str,
                                 expression: str, language: str = "Hscript") -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """设置数值参数的 Hscript 表达式，返回可序列化的撤销快照。

        不覆盖无法用现有快照完整恢复的动画或 Python 表达式。
        """
        if hou is None:
            return False, "未检测到 Houdini API", None
        if not isinstance(language, str) or language.strip().lower() != "hscript":
            return False, "仅支持 Hscript 表达式；Python 表达式暂不支持", None
        if not isinstance(expression, str) or not expression.strip():
            return False, "expression 必须是非空字符串", None
        if _hscript_escape(expression) is None:
            return False, "表达式包含单引号，无法安全写入 Hscript 通道", None
        if not isinstance(node_path, str) or not node_path.startswith("/"):
            return False, "node_path 必须是绝对节点路径", None
        if not isinstance(param_name, str) or not param_name.strip() or "/" in param_name:
            return False, "param_name 必须是当前节点的单个参数名", None
        try:
            node = hou.node(node_path)
            if node is None:
                return False, f"未找到节点: {node_path}", None
            parm = node.parm(param_name)
            if parm is None:
                return False, f"未找到单个参数: {param_name}；元组参数请指定分量", None
            if parm.parmTemplate().type() not in (hou.parmTemplateType.Float, hou.parmTemplateType.Int):
                return False, "仅支持单个 Float/Int 数值参数的表达式", None
            keys = parm.keyframes()
            if len(keys) > 1:
                return False, "不覆盖多关键帧动画：现有撤销快照无法完整恢复它", None
            if keys:
                if abs(keys[0].time()) > 1e-9:
                    return False, "不覆盖非零时间的动画关键帧", None
                try:
                    old_expr = parm.expression()
                    old_lang = parm.expressionLanguage()
                except Exception:
                    return False, "不覆盖已有数值动画关键帧", None
                if old_lang != hou.exprLanguage.Hscript:
                    return False, "不覆盖 Python 表达式：暂不支持安全恢复", None
                if _hscript_escape(old_expr) is None:
                    return False, "旧表达式含单引号，无法保证安全撤销", None
                old_value = {"expr": old_expr, "lang": "Hscript"}
            else:
                old_value = parm.eval()

            new_value = {"expr": expression, "lang": "Hscript"}
            if old_value == new_value:
                return True, f"表达式未变化: {parm.path()}", None
            ok, message = set_expression_safe(parm, expression)
            if not ok:
                # hscript 错误可能发生在通道已创建后；尝试恢复原状态。
                try:
                    if keys:
                        restored, restore_message = set_expression_safe(parm, old_value["expr"])
                        if not restored:
                            raise RuntimeError(restore_message)
                    else:
                        parm.deleteAllKeyframes()
                        parm.set(old_value)
                except Exception as exc:
                    message += f"；恢复旧值失败: {exc}"
                return False, message, None
            snapshot = {
                "node_path": node.path(), "param_name": param_name,
                "old_value": old_value, "new_value": new_value,
                "is_tuple": False, "clear_keyframes": not bool(keys),
            }
            return True, message, snapshot
        except Exception as exc:
            return False, f"设置表达式失败: {exc}", None

    def batch_set_parameters(self, node_paths: List[str], param_name: str,
                             value: Any) -> Tuple[bool, str]:
        """批量设置参数"""
        if hou is None:
            return False, "未检测到 Houdini API"

        success = []
        failed = []

        for path in node_paths:
            node = hou.node(path)
            if not node:
                failed.append(f"{path}: 未找到")
                continue

            parm = node.parm(param_name)
            if not parm:
                parm_tuple = node.parmTuple(param_name)
                if parm_tuple and isinstance(value, (list, tuple)):
                    try:
                        parm_tuple.set(value)
                        success.append(node.name())
                    except Exception as e:
                        failed.append(f"{node.name()}: {e}")
                else:
                    failed.append(f"{node.name()}: 无参数 {param_name}")
                continue

            try:
                parm.set(value)
                success.append(node.name())
            except Exception as e:
                failed.append(f"{node.name()}: {e}")

        msg = f"修改成功: {len(success)} 个节点"
        if failed:
            msg += f"\n失败: {'; '.join(failed)}"

        return len(success) > 0, msg

    def find_nodes_by_param(self, param_name: str, value: Any = None,
                            network_path: Optional[str] = None,
                            recursive: bool = True) -> Tuple[bool, str]:
        """按参数值搜索节点"""
        if hou is None:
            return False, "未检测到 Houdini API"

        if network_path:
            network = hou.node(network_path)
            if not network:
                return False, f"未找到网络: {network_path}"
        else:
            network = self._current_network() or hou.node('/obj')

        results = []

        def search_in(parent):
            for node in parent.children():
                parm = node.parm(param_name)
                if parm:
                    parm_value = parm.eval()
                    if value is None or str(parm_value) == str(value):
                        results.append(f"- {node.path()}: {param_name}={parm_value}")
                if recursive and hasattr(node, 'children'):
                    search_in(node)

        search_in(network)

        if results:
            header = f"找到 {len(results)} 个节点包含参数 '{param_name}'"
            if value is not None:
                header += f" = {value}"
            return True, header + ":\n" + "\n".join(results[:50])

        return False, f"未找到包含参数 '{param_name}' 的节点"
