/* ---------------- 配置 ---------------- */
const CFGTABS=[["sources","数据源"],["pools","股池列表与别名"],["schedule","时段与补录"],["live","下单程序"],["notifycfg","推送"],["api","对外 API"],["plugins","插件"],["home","首页"],["mode","运行模式"]];
let CFGX={};
async function config(m){const my=SEQ;META=cget("/api/meta")||await load("/api/meta");load("/api/meta").then(x=>META=x).catch(()=>{});const c=await load("/api/config");if(my!==SEQ)return;W=JSON.parse(JSON.stringify(c.config));RO=c.readonly;CFGX=c;drawCfg()}

