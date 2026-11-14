"""新建空白策略：不给内容时用「默认模板」当骨架，默认模板被删就回退内置模板。"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

_real_send = httpx.Client.send


@pytest.fixture(autouse=True)
def _allow_testclient(monkeypatch):
    monkeypatch.setattr(httpx.Client, "send", _real_send)


def _engine(make_engine):
    e = make_engine("paper")
    e.strategies.ensure()
    return e


def test_blank_new_uses_builtin_default(make_engine):
    e = _engine(make_engine)
    S = e.strategies
    assert S.default_template_id("buy") == "default"
    assert S.default_template_id("sell") == "classic"

    d = S.create("buy", "空白买入")
    assert d["from_template"] == "default" and d["content"]["layers"]
    assert d["content"].get("select")

    d2 = S.create("sell", "空白卖出")
    assert d2["from_template"] == "classic"
    assert [l["name"] for l in d2["content"]["layers"]] == ["止损", "止盈", "到期退出"]
    assert d2["content"]["exits"]["stop_loss_pct"] == 3.0


def test_blank_new_follows_default_template(make_engine):
    e = _engine(make_engine)
    S = e.strategies
    other = next(t for t in S.templates("buy") if t["id"] != S.default_template_id("buy"))
    S.set_default_template("buy", other["id"])
    d = S.create("buy", "跟随默认")
    assert d["from_template"] == other["id"]
    assert len(d["content"]["layers"]) == len(other["content"]["layers"])


def test_missing_default_template_falls_back(make_engine):
    e = _engine(make_engine)
    S = e.strategies
    f = e.data_dir / "templates" / "defaults.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"sell": "已删除的模板"}), encoding="utf-8")
    assert S.default_template_id("sell") == "classic"
    assert S.create("sell", "回退")["from_template"] == "classic"


def test_api_new_without_content(make_engine):
    from qfpy.web.server import create_app
    e = _engine(make_engine)
    c = TestClient(create_app(e))
    r = c.post("/api/strategies/buy", json={"name": "接口空白新建"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["name"] == "接口空白新建" and d["active"] is False and d["content"]["layers"]
    assert c.post("/api/strategies/buy", json={"name": ""}).status_code == 400
