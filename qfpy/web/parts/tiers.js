/* ---------------- 分级股池、回测 ---------------- */
/* 分级股池已并进「选股结果」（第六轮），旧地址跳过去 */
function tiers(m){SUB.buy="results";return funnel(m)}
async function moveTier(d,c,t){await api("/api/tiers/move",{method:"POST",body:{date:d,code:c,target:t}});render()}

