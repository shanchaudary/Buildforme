from __future__ import annotations

from pathlib import Path

path = Path(__file__).resolve().parent / "apply_stage7_packet7b.py"
text = path.read_text(encoding="utf-8")

old = '''text = replace_once(
    text,
    \'\'\'        "founder_override_blocking_findings": False,
        **(policy or {}),
    }
\'\'\',
    \'\'\'        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
        **(policy or {}),
    }
\'\'\',
    label="review policy automated flag",
)
text = replace_once(
    text,
    \'\'\'        "founder_override_blocking_findings": False,
    }
\'\'\',
    \'\'\'        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
    }
\'\'\',
    label="required policy values",
)
'''
new = '''text = replace_once(
    text,
    \'\'\'        "founder_override_blocking_findings": False,
    }
\'\'\',
    \'\'\'        "founder_override_blocking_findings": False,
        "automated_reviewer_execution_required": True,
    }
\'\'\',
    label="immutable automated review policy",
)
'''
if text.count(old) != 1:
    raise RuntimeError(f"Packet 7B policy patch block count={text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Packet 7B immutable policy patch corrected")
