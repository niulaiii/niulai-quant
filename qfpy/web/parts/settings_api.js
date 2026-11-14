/* ---------- 设置：对外 API、运行模式、首页 ---------- */
function cfgApi(b){b.innerHTML=`<div class="card"><h3>对外 API（/api/v1）</h3>${kv([["启用",inp("api.enabled","bool")],["token",inp("api.token","text","已遮蔽，不改就保持原值；环境变量 "+(W.api.token_env||"QF_API_TOKEN")+" 优先；都没配时 /api/v1 一律 401")],["允许通过 API 改策略",inp("api.allow_config_write","bool","关闭后外部只能读和推送数据")]])}
   <div class="small mute">API 里没有下单、撤单、改开关或风控的接口。文档：<a href="/api/v1/openapi.json" target="_blank">/api/v1/openapi.json</a> · docs/API.md；第三方行情/股池的标准接口：<a href="/api/dataproto/openapi.json" target="_blank">/api/dataproto/openapi.json</a> · docs/DATA_PROVIDER.md。</div></div>`}
function cfgMode(b){const cur=CFGX.mode,md=W.execution.mode;
  const opt=(k,n,d)=>`<label class="modeopt ${md===k?"on":""}"><input type="radio" name="xmode" value="${k}" ${md===k?"checked":""} onchange="set('execution.mode','${k}');drawCfg()"> <b>${n}</b><div class="small mute">${d}</div></label>`;
  b.innerHTML=`<div class="card"><h3>运行模式 <span class="r small mute">当前生效：${esc({paper:"模拟盘",dry_run:"演练",live:"实盘"}[cur]||cur)}</span></h3><div class="modes">${opt("paper","模拟盘","本地模拟成交，有收益曲线和胜率，不碰真钱")}${opt("dry_run","演练","完整走一遍，委托只生成不发出，不改本地记录")}${opt("live","实盘","通过下单程序发真实委托")}</div>
   <div class="small mute">保存后立即生效，切换时记日志、推送钉钉。自动发委托另外由顶栏的「自动交易」开关控制；单笔买入上限、每日买入金额等在「风控」里改。</div></div>
   <div class="card"><h3>只能手改文件的项</h3><div class="small mute">网页监听地址和数据目录只能改 ${esc(CFGX.path||"config/pipeline.toml")} 并重启（默认只监听 127.0.0.1）。</div><pre class="small">${esc(JSON.stringify({web:W.web,data_dir:W.data_dir},null,2))}</pre></div>`}
function cfgHome(b){b.innerHTML=`<div class="card"><h3>首页（大盘行情 + 股池走势）</h3>${kv([["指数",inp("home.indices","list","如 000001.SH,399001.SZ,399006.SZ,000688.SH,000300.SH")],["股池走势回看交易日",inp("home.pool_days")],["交易时段自动刷新间隔（秒）",inp("home.refresh_secs",null,"交易日 9:15–11:30、13:00–15:00 按这个间隔在后台刷新首页行情，默认 15 秒；其余时间和休市日每 5 分钟一次。页面切到后台时暂停")]])}
   <div class="small mute">行情按「数据源与行情 → 实时行情顺序」取。股池走势包含勾选的股池和牛来时段里用到的所有股池。</div></div>`}
/* 保存：校验 → 切实盘时弹二次确认框 → 写回 */
/* 保存：校验 → 写回（切实盘不再设门槛） */
async function saveCfg(){const v=await api("/api/config/validate",{method:"POST",body:body()});
  $("#cfgmsg").innerHTML=v.ok?'<div class="ok-box small">校验通过</div>':`<div class="warn small">${v.errors.map(esc).join("<br>")}</div>`;if(!v.ok)return;
  const r=await api("/api/config",{method:"PUT",body:body()});PNAMES=null;SRCST=null;toast("已保存并生效"+(r.changes&&r.changes.length?`（${r.changes.length} 项）`:""));if(TAB==="config")await config($("#main"));else if(EMB)await embCfg($("#main"),EMB.panels,EMB.secs);refreshHeader()}
