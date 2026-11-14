"""可插拔数据源。

加一个数据源 = 写一个 DataSource 子类并用 @register_source 注册（或放进 plugins/ 目录做成插件），
然后在 config/pipeline.toml 里加 [sources.<名字>] type = "<你的 type>"。
"""
from .base import SOURCE_TYPES, DataSource, NotSupported, PoolResult, register_source, build_sources, source_catalog  # noqa: F401
from . import gushi, watchlist, akshare_src, external_src, standard  # noqa: F401  (注册内置数据源)
