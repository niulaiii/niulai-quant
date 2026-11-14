"""数据提供方插件示例（单文件）——只是写法演示。kind（csv_pool）和内置数据源不重名。用 Python 实现「标准数据接口」的一部分能力。

注册数据源类型 csv_pool：从一个 CSV 文件读股池（能力 pool），实时行情委托给另一个已配置的数据源
（能力 quote，可选）。它展示了插件接口的全部要点：
  - 继承 DataSource，用 @register_source 注册，type_name 就是配置里的 type；
  - capabilities 声明提供哪些能力（名字和 HTTP 协议 /meta.capabilities 一致）；
  - 方法返回内部格式（个股代码用 6 位，单位同标准接口：元、股、百分数、亿元）；
  - config_fields 让网页「设置 → 数据源」显示填写提示；test_connection 可不写（基类按能力自动测）。

用法：
    [sources.mycsv]
    type = "csv_pool"
    path = "config/my_pool.csv"     # 第一列代码，第二列名称（可选），其余列是数值字段
    pool_name = "我的 CSV 股池"
    股池 key 写 "mycsv:all"
想把它对外提供成 HTTP 服务，可以用 qfpy.dataproto.make_provider_app(CsvPoolSource(...)) 包一层。
"""
import csv

from qfpy.datasources import DataSource, PoolResult, register_source
from qfpy.util import today_str

PLUGIN = {"name": "example_data_provider", "description": "数据提供方插件示例：从 CSV 读股池", "enabled": True}


@register_source
class CsvPoolSource(DataSource):
    type_name = "csv_pool"
    title = "示例插件：CSV 股池（写法演示）"
    description = "从本地 CSV 读一个股池；第一列代码、第二列名称、其余列为数值字段"
    capabilities = ("pool",)
    config_fields = [{"name": "path", "hint": "CSV 路径（相对项目根目录）"},
                     {"name": "pool_name", "hint": "股池显示名"}]

    def _rows(self):
        from pathlib import Path
        p = Path(str(self.cfg.get("path", "")))
        p = p if p.is_absolute() else self.root / p
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.reader(f) if r and r[0].strip()]
        if rows and not rows[0][0].strip().isdigit():
            header, rows = rows[0], rows[1:]
        else:
            header = ["code", "name"]
        return header, rows

    def list_pools(self):
        return [{"key": "all", "name": self.cfg.get("pool_name") or "CSV 股池", "desc": str(self.cfg.get("path", ""))}]

    def get_pool(self, key, date):
        header, rows = self._rows()
        stocks = []
        for r in rows:
            code = r[0].strip().zfill(6)
            fields = {}
            for h, v in zip(header[2:], r[2:]):
                try:
                    fields[h.strip()] = float(v)
                except ValueError:
                    pass
            stocks.append({"code": code, "name": (r[1].strip() if len(r) > 1 else "") or code, "fields": fields})
        return PoolResult(key=f"{self.name}:all", name=self.list_pools()[0]["name"], trading_date=date or today_str(), stocks=stocks)
