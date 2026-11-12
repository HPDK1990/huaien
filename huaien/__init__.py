"""怀恩 — 极简前台管家。"""

# ⚠️ 必须在任何第三方 import 之前清掉 PYTHONPATH 污染。
#    放在 __init__ 里 = 所有入口 (app / web / bridge / scripts) 自动覆盖。
from .runtime import clean_sys_path

clean_sys_path()

__version__ = "0.4.3"
