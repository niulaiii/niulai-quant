"""因子体系。

加一个因子：写一个 Factor 子类，用 @register_factor 注册；放在 qfpy/factors/ 下，或者在 plugins/ 里放一个 .py 文件（自动加载）。
卖出因子设置 sides = ("sell",)，两边都能用设置 sides = ("buy", "sell")。
"""
from .base import FACTOR_TYPES, Factor, Outcome, ParamSpec, build_chain, build_factor, register_factor, factor_catalog  # noqa: F401
from . import builtin, expr_factor, signal_factor, sell, external, market_factors  # noqa: F401  (注册内置因子)
from .. import extras  # noqa: F401,E402  (第二批实验性因子)
