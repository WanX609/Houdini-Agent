r"""Safe replacement for hou.Parm.setExpression().

Why this module exists
----------------------
Houdini 22.0.368 (windows-x86_64) segfaults inside
``HOMF_Parm::setExpression`` (libHOMF.dll, signal 11) when the call is made
from the interactive desktop process — e.g. from code executed by the agent's
``execute_python`` tool. Two field crashes on 2026-10-02/03 confirmed it:

    crash.modular_pipes_4k.fbx.WanX_31208_log.txt  (uptime 33862 s)
    crash.modular_pipes_4k.fbx.WanX_4196_log.txt   (uptime   415 s)

Both stack traces start with::

    Caught signal 11
    +0x33611e66 [HOMF_Parm::setExpression] D:\houdini_22\bin\libHOMF.dll

Headless hython does NOT reproduce the GUI crash. On multi-keyframe
channels it raises a clean ``hou.OperationFailed`` ("Parameter must have
exactly one keyframe"). Details:
``Doc/known_issues/2026-10-03-setexpression-segfault.md``.

The workaround uses the hscript channel commands that ``opscript`` itself
emits when it recreates expression channels (validated on 22.0.368)::

    chadd -t 0 0 /obj/node tx
    chkey -t 0 -v 0 -V 0 -m 0 -M 0 -a 0 -A 0 -F '<expr>' /obj/node/tx

``chadd -t 0 0`` creates a degenerate zero-length segment, so the channel has
exactly one key and the ``-F`` expression applies everywhere — byte-for-byte
the channel state that ``setExpression`` produces (same key count, same
``parm.expression()``, same ``parm.eval()``).
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional, Tuple

# `.setExpression(` on any receiver: hou.Parm, hou.ParmTuple, mocks, ...
SET_EXPRESSION_CALL_RE = re.compile(r"\.\s*setExpression\s*\(")

# Environment escape hatch for users on a fixed Houdini build who want the
# direct API back ("0", "false", "off", "no" disable the guard).
GUARD_ENV_VAR = "HOUDINI_AGENT_SETEXPRESSION_GUARD"

SET_EXPRESSION_GUIDANCE = (
    "hou.Parm.setExpression() segfaults the Houdini desktop process "
    "(signal 11 in HOMF_Parm::setExpression, Houdini 22.0.368) and crashed "
    "the app in the field. Do NOT call .setExpression() — instead use the "
    "dedicated tool set_parameter_expression(node_path='/obj/x', "
    "param_name='tx', expression='ch(\"../y/tx\")'). "
    "Use set_node_parameter for ordinary values. "
    "Python-language expressions are not supported by the expression tool. "
    "Set env HOUDINI_AGENT_SETEXPRESSION_GUARD=0 to disable this guard."
)


def guard_enabled() -> bool:
    """Whether the execute_python guard should block .setExpression() calls.

    Default on; opt out with HOUDINI_AGENT_SETEXPRESSION_GUARD=0 (for users
    on a Houdini build where the bug is fixed).
    """
    return os.environ.get(GUARD_ENV_VAR, "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def code_uses_set_expression(code: str) -> bool:
    """True if the code text contains any ``.setExpression(...)`` call."""
    return bool(SET_EXPRESSION_CALL_RE.search(code or ""))


def _hscript_escape(expr: str) -> Optional[str]:
    """Wrap an hscript expression in single quotes; None if not representable.

    hscript string literals use double quotes, so single-quoting the whole
    expression is safe unless the expression itself contains a single quote
    (no documented escape), which we refuse rather than mangle.
    """
    if "'" in (expr or ""):
        return None
    return "'%s'" % (expr or "")


def set_expression_safe(parm: Any, expression: str) -> Tuple[bool, str]:
    """Set a channel expression without hou.Parm.setExpression().

    Equivalent to ``parm.setExpression(expression, hou.exprLanguage.Hscript)``
    but implemented via hscript ``chadd``/``chkey`` so it cannot take the
    crash-on-GUI code path. Multi-keyframe channels are cleared first
    (``chrm``) — setExpression() rejected those anyway ("Parameter must have
    exactly one keyframe"), so this is strictly more capable.

    Only Hscript-language expressions are supported; there is no hscript
    command that sets a Python-language channel expression.

    Returns ``(True, message)`` or ``(False, error)``; never raises for
    bad input, and never calls parm.setExpression().
    """
    try:
        if parm is None:
            return False, "parm is None"
        path = parm.path()  # e.g. /obj/geo1/tx
        if not path or "/" not in path:
            return False, "cannot determine channel path from %r" % (path,)
        quoted = _hscript_escape(expression)
        if quoted is None:
            return False, (
                "expression contains a single quote, which hscript chkey "
                "cannot express: %r" % (expression,)
            )
        node_path, chan = path.rsplit("/", 1)

        import hou  # noqa: PLC0415 - only meaningful inside Houdini

        # Drop pre-existing animation so the channel ends with exactly one
        # key (setExpression's own precondition, enforced manually here).
        try:
            if len(parm.keyframes()) > 1:
                hou.hscript("chrm %s" % path)
        except Exception:
            pass

        out, err = hou.hscript("chadd -t 0 0 %s %s" % (node_path, chan))
        if err and "already" not in err.lower():
            # A channel normally already exists for standard parms; chadd
            # then reports nothing. Any other error is fatal.
            if not _is_benign_chadd_error(err):
                return False, "chadd failed: %s" % err.strip()
        out, err = hou.hscript(
            "chkey -t 0 -v 0 -V 0 -m 0 -M 0 -a 0 -A 0 -F %s %s" % (quoted, path)
        )
        if err:
            return False, "chkey failed: %s" % err.strip()
        return True, "expression set on %s" % path
    except ImportError:
        return False, "hou module unavailable (not inside Houdini)"
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool-level error
        return False, "%s: %s" % (type(exc).__name__, exc)


def _is_benign_chadd_error(err: str) -> bool:
    """chadd on an already-animated parm reports an error we can ignore."""
    low = (err or "").lower()
    return "exist" in low or "already" in low
