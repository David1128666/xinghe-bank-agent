"use strict";

const $ = (selector) => document.querySelector(selector);
const money = (value) => Number(value || 0).toLocaleString("zh-CN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
let state = null;
let busy = false;
let scene = "assistant";
let toastTimer;
let recipientDialogToken = null;
const defaultProviders = [
  {id:"deepseek",name:"DeepSeek",base_url:"https://api.deepseek.com",model:"deepseek-chat"},
  {id:"qwen",name:"通义千问 · 阿里云百炼",base_url:"https://dashscope.aliyuncs.com/compatible-mode/v1",model:"qwen-plus"},
  {id:"openai",name:"ChatGPT · OpenAI",base_url:"https://api.openai.com/v1",model:"gpt-4o-mini"},
  {id:"custom",name:"其他 OpenAI 兼容服务",base_url:"",model:""},
];
function modelEnabled() {return Boolean(state?.llm?.enabled && state?.llm?.configured);}
function providerName(id) {return (state?.providers||defaultProviders).find(provider=>provider.id===id)?.name||"自定义服务";}
function requestId() {return crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random()}`;}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function append(parent, ...children) { children.filter(Boolean).forEach(child => parent.append(child)); return parent; }
function button(text, action, className = "secondary-button") {
  const node = el("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
function toast(text) {
  $("#toast").textContent = text; $("#toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("#toast").hidden = true, 5500);
}
const scenes = [
  ["assistant", "✧", "智能助手", "AI"], ["transfer", "↗", "转账与收款"],
  ["bills", "▤", "账单与分析"], ["investment", "⌁", "理财与持仓"],
  ["cards", "▱", "我的卡片"], ["subscriptions", "◈", "订阅与代扣"],
  ["stocks", "↗", "买股票"], ["plans", "▦", "计划与提醒"],
];
const chips = {
  assistant: [["本月消费分析", () => send("看2026-10账单")], ["转账", () => transferForm()], ["管理订阅", () => send("查看订阅列表")], ["一键买股票", () => changeScene("stocks")]],
  transfer: [["发起转账", () => transferForm()], ["收款人列表", () => recipientList()], ["收款请求", () => aaForm()], ["AA 请求列表", () => send("查看AA请求列表")], ["定时转账", () => send("查看定时转账")]],
  bills: [["2026 年 10 月账单", () => send("看2026-10账单")], ["选择期间", () => billForm()], ["查看流水", () => send("查看交易记录")], ["预算标签", () => budgetForm()]],
  investment: [["产品目录", () => send("查看理财产品")], ["样例风险测评", () => profileForm()], ["我的持仓", () => send("投资持仓")], ["比较产品", () => send("比较产品 P-BOND-SIM 和 P-EQUITY-SIM")]],
  cards: [["查看卡片", () => send("查看卡片")], ["申请虚拟卡", () => send("申请卡")], ["申请进度", () => send("查询申请进度")]],
  subscriptions: [["查看订阅", () => send("查看订阅列表")], ["价格变化", () => send("查看订阅列表")], ["查看提醒", () => send("查看提醒")]],
  stocks: [["虚拟股票行情", () => send("查看股票行情")], ["股票持仓", () => send("股票持仓")], ["演示安全码", () => enhanceAuth()]],
  plans: [["全部计划", () => send("查看已创建计划")], ["创建生日计划", () => birthdayForm()], ["创建提醒", () => reminderForm()], ["提醒列表", () => send("查看提醒")], ["组合计划", () => workflowForm()]],
};
const fieldLabels = {
  amount:"金额", fee:"费用", currency:"币种", execute_at:"执行时间", expected_arrival:"预计到账", reversible:"可撤销",
  payer_account_last4:"付款账户尾号", payee_name:"收款人", payee_account_last4:"收款账户尾号", environment:"环境",
  name:"名称", merchant:"商户", period:"期间", total_spend:"总支出", count:"数量", status:"状态", state:"状态",
  ledger_balance:"账面余额", available_balance:"可用余额", reserved_for_virtual_plans:"计划预留", account_last4:"账户尾号",
  transactions:"交易记录", transaction_id:"交易编号", id:"记录编号", date:"日期", category:"分类", reference:"凭证编号",
  by_category:"支出分类", by_merchant:"商户支出", monthly_trend:"月度趋势", anomalies_to_review:"需要核实的记录",
  savings_suggestions:"支出建议", recurring_expenses:"疑似周期支出", features:"观察到的特征", interpretation:"说明",
  recommendation_note:"匹配说明", profile_missing_fields:"待补充资料", suitable_product_codes:"匹配的产品", suitability_reasons:"匹配理由",
  region:"地区", goal:"目标", horizon:"期限", liquidity:"流动性", risk_tolerance:"风险承受能力", experience:"投资经验", suitability_restrictions:"适当性限制",
  product_code:"产品代码", product_name:"产品名称", risk_level:"风险等级", nav:"样例净值", fee_rate:"费率", lock_period:"锁定期", principal_risk:"风险说明", suggested_horizon:"建议持有期限",
  holding_value:"持仓金额", order_id:"订单编号", settled:"已结算", action:"操作类型", purpose:"用途", from:"来自", request_id:"请求编号",
  bank_debit:"银行代扣", merchant_contract:"商户合同", card_recurring_payment:"银行卡周期付款", next_charge:"下次扣款", renewal_reminder:"续费提醒", price_change_alert:"价格提醒",
  plan_id:"计划编号", plan_name:"计划名称", birthday_date:"纪念日", purchase_date:"计划采购日", reserve_amount:"预留预算", reserved_remaining:"剩余预留", total_spent:"累计支出", authorization_mode:"授权模式",
  flower_budget:"鲜花预算", cake_budget:"蛋糕预算", delivery_details:"配送点", notification_rule:"通知方式", max_amount:"金额上限", max_count:"次数上限", valid_until:"有效日期",
  schedule_id:"定时任务编号", next_run_date:"下次执行", execution_time:"执行时间", frequency:"频率", occurrences:"计划次数", executed_count:"已执行次数", remaining_occurrences:"剩余次数",
  budget_labels:"预算标签", label:"标签", month:"月份", reminders:"提醒", custom_reminders:"自建提醒", reminder_date:"提醒日期", reminder_text:"提醒内容",
  renewal_reminders:"续费提醒", plan_reminders:"计划提醒", remind_before:"提醒日期", delivery:"提醒位置", application_statuses:"申请进度", application_status:"申请状态", replacement_status:"补卡状态", card_last4:"卡片尾号", limit:"额度",
  disputes:"争议申请", reason:"原因", dispute_id:"争议编号", refund_status:"退款申请状态", events:"日历事件", title:"事件名称", message:"说明", authorization_required:"需要授权",
  orders:"订单明细", goods:"商品", charged:"已扣金额", spent_amount:"支出金额", unused_reserve_released:"已释放未用预留", step_id:"步骤编号", steps:"步骤", description:"步骤说明", notifications:"结果通知",
  merchant_allowlist:"允许商户", source_account_ref:"付款账户", reserve_active:"预算已生效", purchase_count:"已执行次数", spent_by_goods:"各商品累计消费", release_condition:"释放条件", preferences:"偏好",
  requested_limit:"申请额度", limit_request_status:"额度申请状态", quote_as_of:"数据时间", data_as_of:"数据时间", includes_pending:"包含待入账", matched:"匹配结果",
  payment_requests:"付款请求", scheduled_transfers:"定时转账", plans:"跨场景计划", controls:"交易开关", online:"线上交易", offline:"线下交易", domestic:"境内交易", international:"境外交易", card_not_present:"无卡交易",
  recipient:"收款人姓名或已确认的演示账户", recipient_query:"收款人姓名", "positive amount":"大于零的转账金额", execution_time:"执行时间", recurrence_end_condition:"周期转账的结束条件", stock_code:"虚构股票代码", shares:"买入股数",
};
const statuses = {DRAFT:"草稿 · 尚未执行",NOT_EXECUTED:"未执行",SUCCESS:"已完成", success:"成功", active:"已启用", stopped:"已停止", cancelled:"已取消", CANCELLED:"已取消", temporarily_locked:"临时锁定", permanent_loss:"已挂失", pending:"待处理", processing:"处理中", PROCESSING:"处理中", SCHEDULED:"已计划", scheduled:"已计划", AWAITING_CONFIRMATION:"等待确认", AWAITING_INFORMATION:"待补充信息", AWAITING_AUTHENTICATION:"待认证", UNDER_REVIEW:"等待审核", FAILED:"未完成", failed:"未完成", completed:"已完成", PARTIAL_SUCCESS:"部分完成", UNKNOWN:"待核实", declined:"已拒绝", settled:"已结算", once:"单次", monthly:"每月", weekly:"每周", yearly:"每年", range:"范围授权", continuous:"持续授权", checking:"活期账户", savings:"储蓄账户", subscribe:"申购", redeem:"赎回", now:"立即"};
const label = key => fieldLabels[key] || key;
function valueText(value) {
  if (value === null || value === undefined) return "暂无";
  if (typeof value === "boolean") return value ? "是" : "否";
  return statuses[value] || String(value);
}

function setupNavigation() {
  scenes.forEach(([key, icon, name, badge]) => {
    const node = button("", () => changeScene(key), "nav-item");
    node.dataset.scene = key;
    node.setAttribute("aria-label", name);
    append(node, el("span", "nav-icon", icon), el("span", "", name), badge ? el("span", "nav-count", badge) : null);
    $("#navigation").append(node);
  });
  [["♙","收款人列表",()=>recipientList()],["◷","我的定时任务",()=>send("查看定时转账")],["◈","预算与提醒",()=>send("查看预算标签")],["✧","体验组合计划",()=>workflowForm()]].forEach(([icon,text,fn])=>{
    const node=button("",fn,"service-button");append(node,el("span","service-icon",icon),el("span","",text),el("span","","›"));$("#service-list").append(node);
  });
  const tiles = [["↗","发起转账","小于 3000 元直接办理",()=>transferForm()],["▤","分析账单","让支出一目了然",()=>send("看2026-10账单")],["▱","管理卡片","临时锁定与解锁",()=>send("查看卡片")],["◈","订阅管理","续费与代扣安排",()=>send("查看订阅列表")]];
  tiles.forEach(([icon,title,desc,fn])=>{
    const tile=button("",fn,"action-tile");append(tile,el("span","tile-icon",icon),el("strong","",title),el("small","",desc));$("#action-grid").append(tile);
  });
  changeScene("assistant",false);
}
function changeScene(key, query=true) {
  scene=key;
  document.querySelectorAll(".nav-item").forEach(node=>{node.classList.toggle("active",node.dataset.scene===key);node.setAttribute("aria-current",node.dataset.scene===key?"page":"false");});
  $("#page-label").textContent=scenes.find(item=>item[0]===key)[2];
  $("#quick-chips").replaceChildren(...chips[key].map(([text,fn])=>button(text,fn,"chip")));
  if (!query || !state) return;
  const first={transfer:()=>transferForm(),bills:()=>send("看2026-10账单"),investment:()=>send("查看理财产品"),cards:()=>send("查看卡片"),subscriptions:()=>send("查看订阅列表"),stocks:()=>send("查看股票行情"),plans:()=>openPlanMenu()};
  if (first[key]) first[key]();
}
function setBusy(value, hint="正在处理，请稍候…") {
  busy=value;
  $("#reset-button").disabled=value || !state;
  $("#send-button").disabled=value || !state;
  $("#message-input").disabled=value || !state;
  $("#composer-hint").textContent=value?hint:modelEnabled()?"自然语言对话 · Enter 发送，Shift + Enter 换行":"本地演示 · 连接 API 可开启自然语言对话";
  $("#model-settings-button").disabled=value||!state;
  $("#model-connect-button").disabled=value||!state;
  document.querySelectorAll(".messages button,#current-operation button,#workflow-panel button").forEach(node=>node.disabled=value);
  document.querySelectorAll("#form-dialog button,#form-dialog input,#form-dialog select,#form-dialog textarea").forEach(node=>node.disabled=value);
  document.querySelectorAll("#model-dialog button,#model-dialog input,#model-dialog select").forEach(node=>node.disabled=value);
  document.querySelectorAll("[data-model-configured]").forEach(node=>node.disabled=value||!state?.llm?.configured);
}
async function loadState() {
  const response=await fetch("/api/state",{cache:"no-store"});
  if (!response.ok) throw new Error("连接未就绪，请确认启动窗口仍然打开。");
  state=await response.json();render();setBusy(false);
  if(state.messages.length>1) requestAnimationFrame(scrollToLatest);
}
function scrollToLatest(){const scroll=$("#chat-scroll"),latest=$("#messages").lastElementChild;if(!latest)return;if(!$("#current-operation").hidden||!$("#workflow-panel").hidden){scroll.scrollTop=scroll.scrollHeight;return;}if(latest.offsetHeight>scroll.clientHeight-24)scroll.scrollTop+=latest.getBoundingClientRect().top-scroll.getBoundingClientRect().top-12;else scroll.scrollTop=scroll.scrollHeight;}
async function send(message, options={}) {
  if (!state || busy) return;
  const action=options.reset?"reset":options.action||"message";
  if (action==="message" && !message?.trim()) return;
  if (action==="message" && state.pending_recipient){syncRecipientDialog();toast("请先核对或取消当前的新收款人。");return;}
  const usingModel=options.natural&&modelEnabled();
  setBusy(true,usingModel?"AI 正在理解需求并调用工具…":"正在办理虚拟业务，请稍候…");
  $("#processing-notice").hidden=false;
  $("#processing-text").textContent=usingModel?"正在与大模型对话，分析你的需求…":"正在处理，请稍候…";
  requestAnimationFrame(()=>{$("#chat-scroll").scrollTop=$("#chat-scroll").scrollHeight;});
  try {
    const body={message,action,source:options.natural?"chat":"control",request_id:requestId(),revision:state.revision,confirmation_token:state.confirmation_token};
    if(action==="confirm_recipient"||action==="cancel_recipient"){body.recipient_token=options.recipientToken;body.payload=options.payload||{};}
    const response=await fetch("/api/message",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify(body)});
    const data=await response.json();
    if (!response.ok) {if(response.status===409) await loadState();throw new Error(data.error||"未能完成操作，请刷新查询状态。");}
    state=data;$("#message-input").value="";render();
    requestAnimationFrame(scrollToLatest);
  } catch(error) {
    toast(error.message||"连接中断，请先刷新页面查看状态，不要重复提交。");
    try{await loadState();}catch{toast("本地服务已断开，请重新运行启动文件，再打开新地址。");}
  } finally {$("#processing-notice").hidden=true;setBusy(false);}
}

function render() {
  renderModelStatus();renderAuthStatus();
  $("#total-balance").textContent=state.accounts.length?money(state.accounts.reduce((sum,row)=>sum+Number(row.available_balance),0)):"需认证";
  $("#account-list").replaceChildren(...state.accounts.map((row,index)=>{
    const node=el("div","account-row");append(node,el("span","account-icon",index?"◈":"▱"),append(el("div"),el("strong","",row.name.replace("模拟","")),el("small","",`尾号 ${row.account_last4}`)),el("span","account-value",money(row.available_balance)));return node;
  }));
  $("#welcome").hidden=state.messages.length>1;
  $("#messages").replaceChildren(...state.messages.map((message,index)=>renderMessage(message,index)));
  renderPendingOperation();renderWorkflow();
  $("#reset-button").disabled=busy;
  syncRecipientDialog();
}
function renderAuthStatus() {
  const strip=$("#auth-strip"),info=state?.auth;if(!strip||!info)return;
  const enhanced=info.channel==="app";
  strip.hidden=enhanced;
  if(enhanced)return;
  $("#auth-strip-title").textContent="当前为即时通讯演示渠道（标准认证）";
  $("#auth-strip-text").textContent=`办理资金类操作前需要增强认证：演示安全码 ${info.code||"------"}（约 5 分钟有效，仅限本机会话）。`;
}
async function enhanceAuth() {
  if(!state||busy)return;
  const info=state.auth||{};
  if((info.channel||"app")==="app"){toast("当前已是增强认证渠道。");return;}
  const code=window.prompt(`请输入页面显示的 6 位演示安全码（当前：${info.code||"------"}）。\n这是本地演示用的模拟验证器，不是真实短信验证码。`,"");
  if(code===null)return;
  if(!/^[0-9]{6}$/.test(code.trim())){toast("请输入 6 位数字演示安全码。");return;}
  setBusy(true,"正在校验演示安全码…");
  $("#processing-notice").hidden=false;$("#processing-text").textContent="正在校验演示安全码…";
  try{
    const body={action:"authenticate",factor:code.trim(),message:"",request_id:requestId(),revision:state.revision,confirmation_token:state.confirmation_token};
    const response=await fetch("/api/message",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok){if(response.status===409)await loadState();throw new Error(data.error||"安全码未通过，请重试。");}
    state=data;render();toast("已通过演示安全码完成增强认证。");
  }catch(error){toast(error.message||"增强认证未完成。");}
  finally{$("#processing-notice").hidden=true;setBusy(false);}
}
function renderPendingOperation() {
  const parent=$("#current-operation"),task=state.task,last=state.messages.at(-1);
  const needsCard=task&&["DRAFT","AWAITING_INFORMATION","AWAITING_AUTHENTICATION","AWAITING_CONFIRMATION","UNDER_REVIEW","PROCESSING","UNKNOWN"].includes(task.state)&&last?.task?.task_id!==task.task_id&&!state.pending_recipient;
  parent.hidden=!needsCard;parent.replaceChildren();
  if(!needsCard)return;
  parent.append(el("p","current-operation-label","当前待处理业务"),renderMessage({role:"assistant",source:"tool",task,text:"这笔业务仍在等待处理，请以当前状态为准。"},state.messages.length-1));
}
function renderWorkflow() {
  const parent=$("#workflow-panel"),flow=state.workflow;
  parent.hidden=!flow;parent.replaceChildren();if(!flow)return;
  parent.append(el("div","response-title",`组合计划 · ${valueText(flow.state)}`));
  const opNames={public_info:"公开信息",balance:"查询余额",transaction_query:"查询流水",transfer:"模拟转账",card_management:"管理卡片",card_query:"查询卡片",bill_analysis:"分析账单",subscription:"订阅管理",subscription_query:"查询订阅",investment:"投资办理",investment_query:"投资查询",cross_scenario:"跨场景计划",cross_scenario_query:"查询计划",budget:"预算标签",reminder:"本地提醒",aa_request:"AA收款请求",aa_query:"查询AA请求",schedule_query:"查询定时任务",schedule_management:"管理定时任务",dispute:"交易争议",dispute_query:"查询争议",category_correction:"更正分类",preference_query:"查询偏好"};
  (flow.steps||[]).forEach((step,index)=>{const row=el("div","workflow-step");kv(row,`第 ${index+1} 步 · ${opNames[step.operation]||step.operation}`,valueText(step.state));if(step.summary)row.append(el("p","result-note",step.summary));if(step.payload)renderFriendly(row,step.payload);parent.append(row);});
  if(!["SUCCESS","CANCELLED","FAILED","PARTIAL_SUCCESS"].includes(flow.state)){
    parent.append(el("p","result-note",flow.state==="DRAFT"?"请核对各步骤的内容。继续后会按顺序办理，小于 3000 元的已核对收款人即时转账可直接完成。":"当前步骤完成后，可以继续后续步骤；取消将停止尚未执行的步骤。"));
    const actions=el("div","button-row");append(actions,button(flow.state==="DRAFT"?"核对并继续组合计划":"继续组合计划",()=>send("继续组合计划"),"primary-button"),button("取消组合计划",()=>send("取消组合计划")));parent.append(actions);
  }
}
function renderModelStatus() {
  const enabled=modelEnabled(),settings=state.llm||{};
  $("#model-mode-strip").classList.toggle("connected",enabled);
  $("#model-mode-title").textContent=enabled?`API 模式 · ${providerName(settings.provider)}`:"本地演示模式 · 未连接大模型";
  $("#model-mode-description").textContent=enabled?`${settings.model} · 直接描述需求，办理结果以业务工具卡片为准。`:"当前按演示指令运行。连接大模型后，可自由对话、追问和安排业务。";
  $("#model-button-label").textContent=enabled?"模型设置":"连接大模型";
  $("#model-connect-button").textContent=enabled?"管理 ↗":"去连接 ↗";
  $("#message-input").placeholder=enabled?"用自己的话描述需求，例如：帮我看看最近的钱花在哪了？":"例如：查余额；连接大模型后，也可以自由描述需求";
}
async function modelRequest(action,payload={}) {
  setBusy(true,action==="test"?"正在测试大模型连接…":"正在更新模型设置…");
  try {
    const response=await fetch("/api/model",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify({action,payload,revision:state.revision,request_id:requestId()})});
    const data=await response.json();
    if(!response.ok){if(response.status===409)await loadState();throw new Error(data.error||"未能更新模型设置，请稍后重试。");}
    state=data;render();return data;
  } finally {setBusy(false);}
}
function closeModelDialog() {
  if(busy)return;
  const password=$("#model-api-key");if(password)password.value="";
  $("#model-dialog").close();
}
function modelSettings() {
  if(!state||busy)return;
  const body=$("#model-dialog-body");body.replaceChildren();
  body.append(el("p","dialog-intro","选择服务商，填写 API 密钥即可启用自然语言 Agent。也可以填写其他 OpenAI 兼容接口。"));
  const privacy=el("div","model-privacy-note");append(privacy,el("strong","","本地虚拟银行 + 大模型对话"),el("p","","对话和必要的虚拟业务摘要会发送到所选服务商；密钥仅本次运行有效，不会写入项目文件。关闭服务后需要重新填写。"));body.append(privacy);
  const providers=state.providers?.length?state.providers:defaultProviders,current=state.llm||{};
  const selected=current.provider||providers[0].id,preset=providers.find(item=>item.id===selected)||providers[0];
  const form=el("form","model-settings-form");form.autocomplete="off";
  const providerField=field("provider","API 服务商","select",selected,providers.map(item=>[item.id,item.name]));
  const baseField=field("base_url","API 地址（Base URL）","url",current.base_url||preset.base_url,[],"填写服务商提供的 OpenAI 兼容地址，例如 https://api.deepseek.com。地域或套餐不同，地址也可能不同。");
  const modelField=field("model","模型名称","text",current.model||preset.model,[],"示例名称可修改，请填写你的 API 账号有权限调用、支持工具调用的文本模型。");
  const keyField=field("api_key","API 密钥","password","",[],"密钥不会回显。保留当前服务商和地址时，留空可继续使用已保存的会话密钥。");
  const providerInput=providerField.querySelector("select"),baseInput=baseField.querySelector("input"),modelInput=modelField.querySelector("input"),keyInput=keyField.querySelector("input");
  providerInput.id="model-provider";baseInput.id="model-base-url";modelInput.id="model-name";keyInput.id="model-api-key";
  baseInput.maxLength=2048;modelInput.maxLength=200;keyInput.maxLength=4096;keyInput.autocomplete="off";keyInput.spellcheck=false;
  const status=el("p","model-test-status");status.setAttribute("role","status");
  const error=el("p","form-error");error.setAttribute("role","alert");
  const normalized=value=>String(value||"").trim().replace(/\/+$/,"");
  const unchangedEndpoint=()=>providerInput.value===current.provider&&normalized(baseInput.value)===normalized(current.base_url);
  const dirty=()=>!state.llm?.configured||providerInput.value!==state.llm.provider||normalized(baseInput.value)!==normalized(state.llm.base_url)||modelInput.value.trim()!==state.llm.model||Boolean(keyInput.value.trim());
  function edited(){status.textContent="";status.classList.remove("success");error.textContent="";keyInput.required=!(state.llm?.configured&&unchangedEndpoint());}
  providerInput.addEventListener("change",()=>{const item=providers.find(provider=>provider.id===providerInput.value);baseInput.value=item?.base_url||"";modelInput.value=item?.model||"";keyInput.value="";edited();});
  [baseInput,modelInput,keyInput].forEach(input=>input.addEventListener("input",edited));
  const footer=el("div","form-footer model-settings-footer"),test=button("测试连接",async()=>{
    if(busy)return;
    if(dirty()){error.textContent="请先保存当前设置，再测试连接。";return;}
    error.textContent="";status.classList.remove("success");status.textContent="正在发送一条简单的连接测试消息，请稍候…";
    try {const data=await modelRequest("test");status.textContent=data.model_test||"连接测试成功，可以开始对话。";status.classList.add("success");}
    catch(problem){status.textContent="";error.textContent=problem.message||"测试失败，请检查服务地址、密钥和模型名称。";}
  });
  test.dataset.modelConfigured="true";
  const submit=el("button","primary-button","保存并启用");submit.type="submit";
  append(footer,button("关闭",closeModelDialog),test,submit);
  const disconnect=button("断开 API，回到本地演示",async()=>{
    if(busy)return;
    try{await modelRequest("disconnect");keyInput.value="";modelSettings();toast("已清除本轮 API 密钥，切回本地演示。");}
    catch(problem){error.textContent=problem.message||"未能断开连接，请重试。";}
  },"text-button model-disconnect");disconnect.dataset.modelConfigured="true";
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(busy)return;error.textContent="";status.textContent="";status.classList.remove("success");
    try {
      await modelRequest("configure",{provider:providerInput.value,base_url:baseInput.value.trim(),model:modelInput.value.trim(),api_key:keyInput.value.trim()});
      keyInput.value="";Object.assign(current,state.llm);keyInput.required=false;keyInput.placeholder="已保存本轮会话密钥，留空即可";
      status.textContent="已启用 API 模式。可以测试连接，或关闭窗口开始对话。";status.classList.add("success");
    } catch(problem){error.textContent=problem.message||"未能保存，请检查填写内容。";}
  });
  append(form,providerField,baseField,modelField,keyField,status,error,footer,disconnect);body.append(form);
  if(current.configured){keyInput.placeholder="已保存本轮会话密钥，留空即可";status.textContent=`当前使用 ${providerName(current.provider)} · ${current.model}`;}
  edited();if(current.configured)status.textContent=`当前使用 ${providerName(current.provider)} · ${current.model}`;
  setBusy(false);if(!$("#model-dialog").open)$("#model-dialog").showModal();
}
function renderMessage(message,index) {
  const container=el("article",`message ${message.role}`);
  if(message.role==="assistant") container.append(el("span","message-avatar","✦"));
  const content=el("div","message-content");const bubble=el("div","bubble");
  const latest=index===state.messages.length-1;
  if(message.role==="user") bubble.append(el("div","reply-text",message.text));
  else if(!message.task) bubble.append(el("div","reply-text",message.text));
  else {
    if(message.source==="model"&&message.text.startsWith("【AI 解读】\n")){
      const ending=message.text.indexOf("\n\n【系统办理结果】\n");
      if(ending>0){const explanation=el("div","model-explanation");append(explanation,el("div","model-explanation-title","AI 解读"),el("div","reply-text",message.text.slice("【AI 解读】\n".length,ending)));bubble.append(explanation);}
    }
    const task=message.task;
    if(task.state==="AWAITING_CONFIRMATION") renderPreview(bubble,task,latest);
    else if(task.state==="AWAITING_INFORMATION") {
      bubble.append(el("div","response-title","我们还需要核对一点信息"));
      if(task.recipient_choices.length){bubble.append(el("p","","有同名收款人，请核对账户尾号。小于 3000 元的转账补齐信息后将直接办理："));task.recipient_choices.forEach((choice,i)=>{if(latest)bubble.append(button(`${choice.name} · 尾号 ${choice.account_last4} →`,()=>send(String(i+1)),"choice-button"));else bubble.append(el("p","",`${i+1}. ${choice.name} · ${choice.account_last4}`));});}
      else{bubble.append(el("p","",`请补充：${task.missing_information.map(label).join("、")}。在下方输入即可。`));if(latest&&task.operation==="investment")bubble.append(button("取消本次并填写风险资料",async()=>{await send("取消");profileForm();},"secondary-button"));}
      if(latest)bubble.append(button("取消当前操作",()=>send("取消"),"secondary-button"));
    } else if(task.state==="SUCCESS" && task.result && typeof task.result==="object") {
      const data=task.result.data||task.result;
      const chip=el("span","status-chip",task.result.data?"✓ 查询完成":"✓ 模拟办理成功");bubble.append(chip);
      renderData(bubble,data,task,latest);
      if(task.transaction_reference&&!task.result.data)bubble.append(el("div","result-note",`凭证 ${task.transaction_reference}`));
    } else {
      const style=task.state==="FAILED"?"error":task.state==="UNDER_REVIEW"||task.state==="UNKNOWN"?"warn":"pending";
      bubble.append(el("span",`status-chip ${style}`,valueText(task.state)));
      bubble.append(el("div","reply-text",message.text.replace("【纯虚拟模拟】","")));
      if(latest){const actions=el("div","button-row");
        if(task.state==="AWAITING_AUTHENTICATION") actions.append(button("输入演示安全码完成增强认证",()=>enhanceAuth(),"primary-button"));
        if(["PROCESSING","UNKNOWN"].includes(task.state)) actions.append(button("刷新当前状态",()=>send("查状态")));
        if(task.state==="PROCESSING"&&task.operation==="investment"&&task.transaction_reference?.startsWith("SIM-INV-"))actions.append(button("手动模拟结算",()=>send(`模拟结算投资${task.transaction_reference}`),"primary-button"));
        if(task.state==="UNDER_REVIEW")actions.append(button("模拟审核拒绝",()=>send("模拟审核拒绝")));
        if(task.state==="SCHEDULED")actions.append(button("查看计划",()=>send("查看已创建计划")));
        if(task.state==="PARTIAL_SUCCESS")renderFriendly(bubble,task.result||{});
        bubble.append(actions);
      }
    }
    const toolMarker="【系统办理结果】\n",toolOffset=message.source==="model"?message.text.indexOf(toolMarker):-1;
    if(toolOffset>=0){const record=el("details","model-tools-details");append(record,el("summary","detail-toggle","查看完整工具记录"),el("pre","",message.text.slice(toolOffset+toolMarker.length)));bubble.append(record);}
    if(task.result?.data_as_of)bubble.append(el("div","result-note",`数据时间 ${new Date(task.result.data_as_of).toLocaleString("zh-CN")}`));
  }
  content.append(bubble);
  if(message.role==="assistant")content.append(el("div","message-meta",`${message.task?"业务工具结果":message.source==="model"?"AI 对话":"星河助手"}${latest?" · 当前回复":""}`));
  container.append(content);return container;
}
function renderPreview(parent,task,current) {
  parent.append(el("span","status-chip pending",current?"◷ 等待你的确认":"操作摘要 · 历史记录"));
  const preview=task.preview||{};
  parent.append(el("div","preview-summary",preview.summary));
  const fields=el("div","preview-fields");
  ["amount","fee","payee_name","payer_account_last4","execute_at","expected_arrival"].forEach(key=>{if(preview[key]!==null&&preview[key]!==undefined)append(fields,append(el("div","preview-field"),el("small","",label(key)),el("strong","",key==="amount"||key==="fee"?`¥ ${money(preview[key])}`:valueText(preview[key]))));});parent.append(fields);
  if(preview.details) {const details=el("details","preview-details");details.open=current;details.append(el("summary","","操作范围与详细说明"));renderFriendly(details,preview.details);parent.append(details);}
  parent.append(el("p","result-note",current?"核对无误后点击确认。点击取消将结束本次待办理操作。":"请以对话最下方的当前状态为准。"));
  if(current){const row=el("div","button-row");append(row,button(task.risk_level==="R4"?"确认提交模拟审核":task.confirmation_phrase,()=>send(task.confirmation_phrase),"primary-button"),button("取消",()=>send("取消")));parent.append(row);}
}
function kv(parent,key,value) {append(parent,append(el("div","key-value"),el("span","",label(key)),el("span","",valueText(value))));}
function details(parent,title,data) {const node=el("details","details-body");node.append(el("summary","detail-toggle",title));renderFriendly(node,data);parent.append(node);}
function renderFriendly(parent,data,depth=0) {
  if(depth>5){parent.append(el("span","",JSON.stringify(data)));return;}
  const node=el("div","friendly-data");
  if(Array.isArray(data)){if(!data.length)node.append(el("p","empty-note","暂无记录"));data.forEach((row,i)=>{const item=el("div","nested");if(row&&typeof row==="object")renderFriendly(item,row,depth+1);else item.textContent=valueText(row);node.append(item);});}
  else if(data&&typeof data==="object")Object.entries(data).filter(([key])=>!["environment","idempotency_key","category_labels_are_estimates","category_correction_examples","anomaly_options","quote_as_of","historical_performance_note"].includes(key)).forEach(([key,value])=>{
    if(value&&typeof value==="object"){node.append(el("h3","",label(key)));renderFriendly(node,value,depth+1);}else kv(node,key,value);
  });else node.append(el("p","",valueText(data)));
  parent.append(node);
}
function renderData(parent,data,task,current) {
  if(task.operation==="transfer"&&!task.result?.data&&task.preview) {
    const preview=task.preview;
    parent.append(el("div","response-title","模拟转账已完成"));
    parent.append(el("div","bill-total",`¥ ${money(preview.amount)}`));
    kv(parent,"收款人",`${preview.payee_name} · 尾号 ${preview.payee_account_last4}`);kv(parent,"付款账户",`尾号 ${preview.payer_account_last4}`);kv(parent,"转账费用",`¥ ${money(preview.fee)}`);
    if(task.confirmation_policy==="virtual_transfer_below_3000")parent.append(el("p","result-note","已按本轮演示规则直接办理：金额小于 3000 元。"));
    if(current){const row=el("div","button-row");append(row,button("再转一笔",()=>transferForm()),button("收款人列表",()=>recipientList()));parent.append(row);}
  } else if(data.accounts) {
    parent.append(el("div","response-title","你的账户余额"));const grid=el("div","result-grid");data.accounts.forEach(row=>{const item=el("div","result-item");append(item,el("h3","",`${row.name.replace("模拟","")} · ${row.account_last4}`),el("div","big-number",`¥ ${money(row.available_balance)}`),el("small","","可用余额"));if(Number(row.reserved_for_virtual_plans)>0)kv(item,"reserved_for_virtual_plans",`¥ ${money(row.reserved_for_virtual_plans)}`);grid.append(item);});parent.append(grid);
  } else if(data.by_category) {
    parent.append(el("div","response-title",`${data.period} 支出分析`));parent.append(el("div","bill-total",`¥ ${money(data.total_spend)}`));
    Object.entries(data.by_category).forEach(([name,value])=>{const row=el("div","bar-row");const track=el("div","bar-track"),bar=el("span");bar.style.width=`${Math.max(1,Number(value)/Math.max(1,Number(data.total_spend))*100)}%`;append(track,bar);append(row,el("span","",name),track,el("span","",money(value)));parent.append(row);});
    if(data.anomalies_to_review?.length){parent.append(el("p","result-note",`有 ${data.anomalies_to_review.length} 笔记录需要核实，提示不代表已确认欺诈。`));details(parent,"查看待核实记录",data.anomalies_to_review);}
    if(data.savings_suggestions?.length)parent.append(el("p","result-note",data.savings_suggestions.join(" ")));
    details(parent,"查看完整账单与趋势",data);
  } else if(data.stocks) {
    parent.append(el("div","response-title","一键购买虚拟股票"));
    parent.append(el("p","result-note","固定样例价 · 每手 100 股。点击买入即完成虚拟成交，金额与费用如下。"));
    const list=el("div","stock-list");
    data.stocks.forEach(stock=>{
      const card=el("div","stock-card"),head=el("div","stock-heading");
      append(head,append(el("div"),el("h3","",stock.stock_name),el("small","",`${stock.stock_code} · 虚构股票`)),append(el("div","stock-price"),el("strong","",`¥ ${money(stock.price)}`),el("small","","固定样例价 / 股")));card.append(head);
      kv(card,"买入股数",`${stock.lot_size} 股（1 手）`);kv(card,"股票金额",`¥ ${money(stock.lot_amount)}`);kv(card,"交易费用",`¥ ${money(stock.lot_fee)}`);
      const bottom=el("div","stock-bottom");append(bottom,append(el("div","stock-total"),el("small","","活期合计扣款"),el("strong","",`¥ ${money(stock.lot_total)}`)));
      if(current)bottom.append(button(`买入 ${stock.stock_code} · ${stock.lot_size} 股（需确认）`,()=>send(`买入 ${stock.stock_code} ${stock.lot_size}股`),"primary-button"));
      card.append(bottom);list.append(card);
    });parent.append(list);
    if(current)parent.append(button("查看股票持仓",()=>send("股票持仓")));
  } else if(data.stock_holdings) {
    parent.append(el("div","response-title","你的虚拟股票持仓"));
    if(!data.stock_holdings.length)parent.append(el("p","empty-note","暂无股票持仓。查看虚拟行情后，可以一键买入一手样例股票。"));
    data.stock_holdings.forEach(stock=>{const card=el("div","record");append(card,el("div","response-title",`${stock.stock_name} · ${stock.stock_code}`));kv(card,"持有股数",`${stock.shares} 股`);kv(card,"含费平均成本",`¥ ${money(stock.average_cost)} / 股`);kv(card,"累计成本",`¥ ${money(stock.cost_basis)}`);kv(card,"样例市值",`¥ ${money(stock.market_value)}`);kv(card,"样例浮动盈亏",`¥ ${money(stock.unrealized_pnl)}`);parent.append(card);});
    if(current)parent.append(button("继续查看虚拟股票",()=>send("查看股票行情")));
  } else if(data.stock_order) {
    const order=data.stock_order;
    parent.append(el("div","response-title","虚拟股票已成交"));
    append(parent,el("p","",`${order.stock_name}（${order.stock_code}） · 买入 ${order.shares} 股`));
    kv(parent,"成交样例价",`¥ ${money(order.price)} / 股`);kv(parent,"股票金额",`¥ ${money(order.amount)}`);kv(parent,"交易费用",`¥ ${money(order.fee)}`);kv(parent,"活期合计扣款",`¥ ${money(order.total)}`);kv(parent,"当前持有股数",`${order.holding_shares} 股`);kv(parent,"订单编号",order.order_id);
    if(current){const row=el("div","button-row");append(row,button("查看股票持仓",()=>send("股票持仓")),button("返回虚拟行情",()=>send("查看股票行情")));parent.append(row);}
  } else if(data.cards) {
    parent.append(el("div","response-title","你的虚拟卡片"));const list=el("div","row-list");
    data.cards.forEach(row=>{const item=el("div","record");append(item,append(el("div","record-heading"),el("strong","",`星河卡 · ${row.card_last4}`),el("span","",valueText(row.status))));kv(item,"limit",`¥ ${money(row.limit)}`);details(item,"交易开关",row.controls);
      if(current){const actions=el("div","button-row");
        if(row.status==="active"||row.status==="temporarily_locked")append(actions,button(row.status==="active"?"临时锁卡":"解锁卡片",()=>send(`${row.status==="active"?"临时锁卡":"解锁"}尾号${row.card_last4}`)),button("交易开关",()=>cardControlForm(row)),button("调整额度",()=>amountForm("申请调整额度","新额度",n=>`调整尾号${row.card_last4}额度到${n}元`,5000)),button("挂失",()=>send(`挂失尾号${row.card_last4}`),"secondary-button danger-button"));
        actions.append(button("补卡申请",()=>send(`补卡尾号${row.card_last4}`)));item.append(actions);}
      list.append(item);});parent.append(list);
  } else if(data.subscriptions) {
    parent.append(el("div","response-title","订阅与扣款安排"));parent.append(el("p","result-note",`商户订阅月费合计 ¥ ${money(data.active_monthly_total)} · 停止代扣不代表商户合同已取消。`));
    const list=el("div","row-list");data.subscriptions.forEach(row=>{const item=el("div","record"),name=row.merchant.replace(/（.*）/,"");append(item,append(el("div","record-heading"),el("strong","",name),el("span","",`¥ ${money(row.amount)} / 月`)));kv(item,"bank_debit",row.bank_debit);kv(item,"merchant_contract",row.merchant_contract);kv(item,"card_recurring_payment",row.card_recurring_payment);kv(item,"next_charge",row.next_charge);
      if(current){const actions=el("div","button-row");append(actions,button(row.bank_debit==="active"?"停止代扣":"恢复代扣",()=>send(`${row.bank_debit==="active"?"停止代扣":"恢复代扣"}${name}`)),button("合同与其他操作",()=>subscriptionMenu(name)),button(row.renewal_reminder?"关闭续费提醒":"开启续费提醒",()=>send(`${row.renewal_reminder?"关闭":"开启"}${name}续费提醒`)));item.append(actions);}list.append(item);});parent.append(list);details(parent,"周期扣款与价格变化",{recurring_expenses:data.recurring_detections,"价格变化":data.price_change_alerts});
  } else if(data.products) {
    parent.append(el("div","response-title","虚构理财产品"));parent.append(el("p","result-note",data.recommendation_note));
    const grid=el("div","result-grid");data.products.forEach(row=>{const item=el("div","result-item");append(item,el("h3","",row.name),el("small","",row.product_code));kv(item,"risk_level",row.risk_level);kv(item,"nav",row.nav);kv(item,"fee_rate",`${Number(row.fee_rate)*100}%`);item.append(el("p","result-note",row.principal_risk));if(current)item.append(button("模拟申购",()=>amountForm("模拟申购","申购金额",n=>`申购${row.product_code} ${n}元`,100)));grid.append(item);});parent.append(grid);
    if(current)parent.append(button(data.profile_missing_fields?.length?"填写样例风险测评":"重新填写风险资料",()=>profileForm()));
    if(data.suitable_product_codes?.length)parent.append(el("p","result-note",`本轮资料匹配：${data.suitable_product_codes.join("、")}`));
  } else if(data.holdings) {
    parent.append(el("div","response-title","你的虚构持仓"));if(!data.holdings.length)parent.append(el("p","empty-note","目前还没有已结算持仓。申购提交后，可手动模拟结算。"));
    data.holdings.forEach(row=>{const item=el("div","record");append(item,el("h3","response-title",row.product_name));kv(item,"holding_value",`¥ ${money(row.holding_value)}`);if(current)item.append(button("模拟赎回",()=>amountForm("模拟赎回","赎回金额",n=>`赎回${row.product_code} ${n}元`,50)));parent.append(item);});
  } else if(data.order) {
    renderFriendly(parent,data.order);if(current&&!data.order.settled)parent.append(button("手动模拟结算",()=>send(`模拟结算投资${data.order.order_id}`),"primary-button"));
  } else if(data.payment_requests) {
    parent.append(el("div","response-title","AA 付款请求"));if(!data.payment_requests.length)parent.append(el("p","empty-note","暂无付款请求。"));
    data.payment_requests.forEach(row=>{const item=el("div","record");append(item,append(el("div","record-heading"),el("strong","",`${row.from} · ${row.purpose}`),el("span","",`¥ ${money(row.amount)}`)));kv(item,"status",row.status);if(current&&row.status==="pending")item.append(button("核对并支付",()=>send(`支付AA请求${row.request_id}`)));parent.append(item);});
  } else if(data.plans||data.scheduled_transfers) {
    parent.append(el("div","response-title","计划与提醒"));
    (data.plans||[]).forEach(row=>{const item=el("div","record");append(item,append(el("div","record-heading"),el("strong","",row.plan_name),el("span","",valueText(row.status))));kv(item,"plan_id",row.plan_id);kv(item,"purchase_date",row.purchase_date);kv(item,"reserved_remaining",row.reserved_remaining);details(item,"计划范围与步骤",row);
      if(current&&row.status==="scheduled"){const actions=el("div","button-row");if(!row.reserve_active)actions.append(button("模拟激活预算",()=>send(`模拟预留计划${row.plan_id}`)));append(actions,button("模拟到期采购",()=>purchaseForm(row)),button("取消计划",()=>send(`取消计划${row.plan_id}`)));item.append(actions);}parent.append(item);});
    (data.scheduled_transfers||[]).forEach(row=>{const item=el("div","record");append(item,append(el("div","record-heading"),el("strong","",`定时转账 · ¥ ${money(row.amount)}`),el("span","",valueText(row.status))));renderFriendly(item,row);if(current&&row.status==="scheduled"){const actions=el("div","button-row");append(actions,button("模拟到期运行",()=>send(`模拟运行计划${row.schedule_id}`)),button("取消定时任务",()=>send(`取消定时任务${row.schedule_id}`)));item.append(actions);}parent.append(item);});
    if(!(data.plans||[]).length&&!(data.scheduled_transfers||[]).length)parent.append(el("p","empty-note","还没有登记计划。可以在“计划与提醒”里创建生日采购计划、定时转账或本地提醒。"));
    ["custom_reminders","renewal_reminders","plan_reminders"].forEach(key=>{if(data[key]?.length)details(parent,label(key),data[key]);});
  } else if(data.events) {
    parent.append(el("p","",data.message));if(data.authorization_required&&current)parent.append(button("授权读取虚构日历",()=>send("授权读取模拟日历"),"primary-button"));renderFriendly(parent,data.events);
  } else if(data.transactions) {
    parent.append(el("div","response-title","交易明细"));const wrap=el("div","data-table-wrap"),table=el("table","data-table"),head=el("tr");["日期","商户","金额","分类"].forEach(text=>head.append(el("th","",text)));table.append(head);
    data.transactions.forEach(row=>{const tr=el("tr");[row.date,row.merchant,`¥ ${money(row.amount)}`,row.category].forEach(text=>tr.append(el("td","",text)));table.append(tr);});append(wrap,table);parent.append(wrap);if(current){const actions=el("div","button-row");append(actions,button("更正消费分类",()=>categoryForm()),button("提交交易争议",()=>disputeForm()));parent.append(actions);}
  } else {
    renderFriendly(parent,data);
  }
}

function dialog(title,intro) {$("#dialog-title").textContent=title;$("#dialog-body").replaceChildren();if(intro)$("#dialog-body").append(el("p","dialog-intro",intro));if(!$("#form-dialog").open)$("#form-dialog").showModal();return $("#dialog-body");}
function closeDialog(){
  if(recipientDialogToken&&state?.pending_recipient){if(!busy)send("",{action:"cancel_recipient",recipientToken:recipientDialogToken});return;}
  $("#form-dialog").close();
}
function syncRecipientDialog(){
  const pending=state?.pending_recipient;
  if(!pending){if(recipientDialogToken){recipientDialogToken=null;$("#form-dialog").close();}return;}
  if(recipientDialogToken===pending.token&&$("#form-dialog").open)return;
  recipientDialogToken=pending.token;
  const immediate=["now","立即"].includes(pending.execution_time);
  const direct=Number(pending.amount)>0&&Number(pending.amount)<3000&&immediate;
  const intro=direct?"首次向这位虚构收款人转账，请核对姓名和模拟账户。确认后会保存收款人并办理这笔小额转账。":"首次向这位虚构收款人转账，请先核对姓名和模拟账户。保存后继续核对转账摘要，确认后办理。";
  const body=dialog("首次收款人身份核对",intro),form=el("form","recipient-confirm-form");
  const nameField=field("name","虚构收款人姓名","text",pending.name),last4Field=field("account_last4","虚构收款账户尾号","text",pending.account_last4,[],"仅填写 4 位演示数字，不需要真实银行卡信息。");
  const nameInput=nameField.querySelector("input");nameInput.maxLength=16;
  const last4Input=last4Field.querySelector("input");last4Input.pattern="[0-9]{4}";last4Input.maxLength=4;last4Input.inputMode="numeric";
  append(form,nameField,last4Field);
  const identity=el("div","recipient-transfer-summary");kv(identity,"模拟收款银行",pending.bank_name||"星河模拟银行");kv(identity,"本次转账金额",`¥ ${money(pending.amount)}`);kv(identity,"付款账户",`尾号 ${pending.payer_account_last4||"1234"}`);kv(identity,"执行时间",valueText(pending.execution_time));if(pending.purpose)kv(identity,"用途",pending.purpose);form.append(identity);
  form.append(el("p","form-note","身份核对仅用于这轮虚拟演示。已保存的收款人在本轮后续转账时可以直接选择；重新开始演示或重启服务后恢复初始列表。"));
  const error=el("div","form-error"),row=el("div","form-footer"),submit=el("button","primary-button",direct?"确认身份并付款":"确认身份并继续");submit.type="submit";
  append(row,button("取消本次转账",closeDialog),submit);append(form,error,row);body.append(form);
  form.addEventListener("submit",event=>{event.preventDefault();if(busy)return;const name=nameInput.value.trim(),accountLast4=last4Input.value.trim();if(!/^[\u4e00-\u9fff·]{2,16}$/.test(name)){error.textContent="请填写 2 至 16 个中文字符的虚构收款人姓名。";return;}if(!/^\d{4}$/.test(accountLast4)){error.textContent="请填写 4 位数字的虚构账户尾号。";return;}error.textContent="";send("",{action:"confirm_recipient",recipientToken:pending.token,payload:{name,account_last4:accountLast4}});});
}
function recipientList(){
  if(!state||busy)return;if(state.pending_recipient){syncRecipientDialog();return;}
  const body=dialog("收款人列表","这里保存本轮演示中的虚构收款人。新收款人首次办理时先核对身份；遇到同名收款人，办理时还会让你选择账户尾号。"),list=el("div","recipient-list");
  const rows=state.recipients||[];
  if(!rows.length)list.append(el("p","empty-note","暂时没有收款人，可以在转账表单里输入新的虚构姓名。"));
  rows.forEach(person=>{const item=el("div","recipient-item"),head=el("div","record-heading");append(head,el("strong","",person.name),el("span",person.identity_confirmed?"recipient-verified":"recipient-unverified",person.identity_confirmed?"身份已核对":"首次需核对"));append(item,head,el("p","",`${person.bank_name||"星河模拟银行"} · 尾号 ${person.account_last4}`),button(`向${person.name}转账`,()=>{closeDialog();transferForm(person.name);}));list.append(item);});
  body.append(list);const row=el("div","form-footer");append(row,button("关闭",closeDialog),button("向新收款人转账",()=>{closeDialog();transferForm("");},"primary-button"));body.append(row);
}
function field(name,title,type="text",value="",options=[],hint="") {
  const wrap=el("label","form-field");wrap.append(el("span","",title));let input;
  if(type==="select"){input=el("select");options.forEach(option=>{const item=el("option","",Array.isArray(option)?option[1]:option);item.value=Array.isArray(option)?option[0]:option;input.append(item);});}
  else if(type==="textarea")input=el("textarea");else{input=el("input");input.type=type;}
  input.name=name;input.setAttribute("aria-label",title);input.value=value;input.required=true;if(type==="number"){input.min="0.01";input.max="100000000";input.step="0.01";}if(type==="text")input.maxLength=160;
  wrap.append(input);if(hint)wrap.append(el("small","",hint));return wrap;
}
function formDialog(title,intro,fields,build,submitLabel="生成办理预览") {
  if(!state||busy)return;
  if(state.pending_recipient){syncRecipientDialog();toast("请先核对或取消当前的新收款人。");return;}
  if(["AWAITING_CONFIRMATION","AWAITING_INFORMATION"].includes(state.task?.state)){toast("请先完成或取消对话最下方的待办理操作。");return;}
  const body=dialog(title,intro),form=el("form");fields.forEach(item=>form.append(item));
  const error=el("div","form-error"),footer=el("div","form-footer"),submit=el("button","primary-button",submitLabel);submit.type="submit";
  append(footer,button("返回",closeDialog),submit);append(form,error,footer);body.append(form);
  form.addEventListener("submit",event=>{event.preventDefault();try{const values=Object.fromEntries(new FormData(form));const message=build(values);closeDialog();send(message);}catch(problem){error.textContent=problem.message;}});
}
function safeText(text) {const result=String(text).trim();if(!result||/[\r\n；;]/.test(result))throw new Error("请填写单行内容，不要包含分号。");return result;}
function amount(value) {if(!/^\d+(\.\d{1,2})?$/.test(String(value))||Number(value)<=0)throw new Error("请输入大于 0 且最多两位小数的金额。");return value;}
function dateAfter(days=1){const value=new Date(`${state.today}T12:00:00`);value.setDate(value.getDate()+days);return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`;}
function transferForm(recipientName="小明") {
  const person=field("recipient","收款人姓名","text",recipientName,[],"可输入新的中文虚构姓名；首次出现会弹出身份核对窗口。");person.querySelector("input").setAttribute("list","transfer-recipients");
  const choices=el("datalist");choices.id="transfer-recipients";[...new Set((state?.recipients||[]).map(row=>row.name))].forEach(name=>{const option=el("option");option.value=name;choices.append(option);});person.append(choices);
  formDialog("安排一笔模拟转账","新收款人首次先核对身份。已核对收款人的小于 3000 元转账直接办理；3000 元及以上先确认摘要。默认从活期立即转出。",[
    field("account","付款账户","select","活期",[["活期","活期账户 · 尾号 1234"],["储蓄","储蓄账户 · 尾号 5678"]]),
    person,
    field("amount","转账金额（元）","number","300"),field("purpose","用途","text","午餐"),
    field("time","执行方式","select","now",[["now","现在转账"],["tomorrow","明天登记执行"],["monthly","每月转账，共 3 次"]]),
  ],values=>{const when=values.time==="now"?"现在":values.time==="tomorrow"?dateAfter():`每月定时转账${dateAfter()}`;return `${when}从${values.account}转账${amount(values.amount)}元给${safeText(values.recipient)}${values.time==="monthly"?"每月执行3次":""}用途${safeText(values.purpose)}`;},"提交转账");
}
function amountForm(title,labelText,build,value){formDialog(title,"下一步先核对金额、影响和费用。",[field("amount",`${labelText}（元）`,"number",String(value))],values=>build(amount(values.amount)));}
function billForm(){formDialog("选择账单期间","样例主要分布在 2026 年 8 至 10 月，也支持自定义期间。",[field("start","开始日期","date","2026-10-01"),field("end","结束日期","date","2026-10-31")],v=>{if(v.start>v.end)throw new Error("开始日期不能晚于结束日期。");return `分析${v.start}到${v.end}账单`;},"查询账单");}
function profileForm(){formDialog("填写样例风险测评","这组资料只用于虚构产品匹配。请按自己的演示意图检查每一项。",[
  field("region","地区","text","虚构区"),field("goal","投资目标","text","稳健长期配置"),field("horizon","期限","select","3年",["3年","1年","6个月"]),field("liquidity","流动性需求","select","中",["中","高","低"]),field("risk","风险承受能力","select","R2",[["R1","R1 较低"],["R2","R2 中低"],["R3","R3 中等"],["R4","R4 较高"]]),field("experience","投资经验","select","新手",["新手","有经验"]),field("amount","可投资金额（元）","number","5000"),field("limits","适当性限制","text","无")
],v=>`风险测评 地区：${safeText(v.region)}，目标：${safeText(v.goal)}，期限：${v.horizon}，流动性：${v.liquidity}，${v.risk}，经验：${v.experience}，金额：${amount(v.amount)}元，适当性限制：${safeText(v.limits)}`,"查看匹配结果");}
function aaForm(){formDialog("创建 AA 收款请求","只向指定虚构收款人发送请求，不会扣取对方的资金。",[field("person","向谁收款","select","小明",["小明","张伟"]),field("amount","分摊金额（元）","number","50"),field("purpose","用途","text","聚餐分摊")],v=>`AA收款${amount(v.amount)}元给${v.person}用途${safeText(v.purpose)}`);}
function cardControlForm(card){formDialog("设置卡片交易开关",`虚构卡片尾号 ${card.card_last4}，每次选择一项进行变更。`,[field("control","交易类型","select","线上交易",["线上交易","线下交易","境内交易","境外交易","无卡交易"]),field("enabled","开关","select","关闭",["关闭","开启"])],v=>`${v.enabled}${v.control}尾号${card.card_last4}`);}
function subscriptionMenu(name){const body=dialog(`${name} · 其他操作`,"每一种操作的处理对象不同，请在下一步摘要里核对。停止代扣不会取消商户合同，退款申请也不等于已退款。");const list=el("div","standalone-actions");[["取消商户订阅合同",`取消商户合同${name}`],["停止银行卡周期付款",`停止银行卡周期付款${name}`],["单独提交退款申请",`退款申请${name}`],["开启价格变化提醒",`开启${name}价格变化提醒`],["关闭价格变化提醒",`关闭${name}价格变化提醒`]].forEach(([title,cmd])=>list.append(button(title,()=>{closeDialog();send(cmd);})));body.append(list);}
function birthdayForm(){formDialog("创建生日采购计划","选定虚构纪念日，确认预算、商品和商户范围。程序不会读取真实日历或寄送商品。",[
 field("date","纪念日","date",dateAfter(16)),field("reserve","从活期预留预算（元）","number","1000"),field("flower","鲜花预算（元）","number","350"),field("cake","蛋糕预算（元）","number","200"),field("limit","每次采购上限（元）","number","600"),field("mode","授权方式","select","范围授权",[["一次性授权","每次采购前单独确认"],["范围授权","范围内最多执行 1 次"],["持续授权","范围内最多执行 2 次"]]),field("delivery","虚构配送点","text","A")
],v=>{if(v.date<=state.today)throw new Error("请选择未来的纪念日。");if(Number(v.flower)+Number(v.cake)>Number(v.reserve))throw new Error("商品预算合计不能超过预留预算。");return `创建生日计划${v.date}活期预留${amount(v.reserve)}元鲜花${amount(v.flower)}元蛋糕${amount(v.cake)}元${v.mode}最大金额${amount(v.limit)}元${v.mode==="持续授权"?"最多执行2次":""}虚拟花店虚拟蛋糕店鲜花蛋糕虚拟配送点${safeText(v.delivery)}`;});}
function purchaseForm(plan){formDialog("手动模拟到期采购","按已确认的商户、商品、金额和次数范围办理。范围授权内的订单可能直接执行；越界会被阻止。",[field("flower","鲜花订单（元）","number","300"),field("cake","蛋糕订单（元）","number","200")],v=>`模拟到期执行计划${plan.plan_id}鲜花${amount(v.flower)}元@虚拟花店蛋糕${amount(v.cake)}元@虚拟蛋糕店`,"按授权模拟办理");}
function reminderForm(){formDialog("添加本地提醒","提醒只在本地列表显示，不发送微信、短信或邮件。",[field("date","提醒日期","date",dateAfter()),field("text","提醒内容","text","核对月度账单")],v=>`创建提醒${v.date}内容${safeText(v.text)}`);}
function budgetForm(){formDialog("设置预算标签","这是分类标签，不会冻结或扣取资金。",[field("name","标签名称","text","交通"),field("amount","预算金额（元）","number","1000")],v=>`设置预算标签${safeText(v.name)}${amount(v.amount)}元`);}
function categoryForm(){formDialog("更正消费分类","请填写样例交易编号。可以在交易明细中核对。",[field("id","交易编号","text","SIM-TX-105"),field("category","新分类","text","交通")],v=>{if(!/^SIM-TX-\d+$/.test(v.id))throw new Error("请填写完整的 SIM-TX- 编号。");return `把${v.id}分类改为${safeText(v.category)}`;});}
function disputeForm(){formDialog("提交交易争议","这里只登记模拟争议申请，不自动退款或拒付。",[field("id","交易编号","text","SIM-TX-105"),field("reason","争议原因","text","不认识这笔交易")],v=>{if(!/^SIM-TX-\d+$/.test(v.id))throw new Error("请填写完整的 SIM-TX- 编号。");return `提交争议${v.id}原因${safeText(v.reason)}`;});}
function workflowForm(){formDialog("创建组合计划","多个步骤按依赖执行，重要操作仍会逐项展示摘要、逐项确认。",[field("steps","组合步骤","textarea","查余额；现在从活期转账25元给小明用途午餐；临时锁卡尾号9012")],v=>`组合计划：${v.steps}`);}
function openPlanMenu(){
  const body=dialog("计划与提醒","定时转账、生日采购、本地提醒和组合计划都在这里统一查看与创建。");
  const groups=[
    ["查看计划与提醒",[["全部计划",()=>send("查看已创建计划")],["提醒列表",()=>send("查看提醒")]]],
    ["新建安排",[["创建生日采购计划",()=>birthdayForm()],["创建本地提醒",()=>reminderForm()],["创建组合计划",()=>workflowForm()]]],
    ["预算标签",[["设置预算标签",()=>budgetForm()],["查看预算标签",()=>send("查看预算标签")]]],
  ];
  groups.forEach(([title,items])=>{
    body.append(el("p","plan-menu-label",title));
    const list=el("div","standalone-actions");
    items.forEach(([label,action])=>list.append(button(label,()=>{closeDialog();action();})));
    body.append(list);
  });
}
function guide(){const body=dialog("第一次使用，从这里开始","可以用大模型自由对话，也可以直接点击功能按钮。所有业务均使用虚拟数据。");[
 ["1","连接大模型，再自然对话","右上角“连接大模型”支持 DeepSeek、千问、OpenAI 和其他兼容服务。保存 API 设置后，可以问“最近哪里花得比较多”，不必记固定指令。",()=>modelSettings(),"打开模型设置"],
 ["2","先查询余额","右侧显示账户概览，点击按钮也能在对话里查看。",()=>send("查余额"),"查询余额"],
 ["3","试一笔模拟转账","直接输入“给张三转账 2888 元”，首次先核对并保存虚构收款人。以后小于 3000 元直接办理，3000 元及以上核对摘要后确认。",()=>transferForm(),"打开转账表单"],
 ["4","体验股票与计划提醒","左侧“一键买股票”可按显示的样例价格与费用买入 100 股；“计划与提醒”里统一创建生日采购计划、本地提醒和组合计划。",null,null],
 ["5","核对业务工具结果","AI 可以解释和分析，办理结果以对话里的业务工具卡片为准。遇到待确认操作，先确认或取消再开始下一件事。",null,null],
 ["6","重新体验或结束","“新一轮演示”可重置本轮业务数据。关闭启动窗口会停止本地服务并清除 API 密钥；网页刷新会保留当前会话。",null,null]
].forEach(([num,title,text,action,caption])=>{const row=el("div","guide-step"),content=el("div");append(content,el("h3","",title),el("p","",text));if(action)content.append(button(caption,()=>{closeDialog();action();}));append(row,el("span","",num),content);body.append(row);});}
$("#chat-form").addEventListener("submit",event=>{event.preventDefault();send($("#message-input").value.trim(),{natural:true});});
$("#message-input").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();send(event.target.value.trim(),{natural:true});}});
$("#model-settings-button").addEventListener("click",modelSettings);$("#model-connect-button").addEventListener("click",modelSettings);
$("#close-model-dialog").addEventListener("click",closeModelDialog);
$("#model-dialog").addEventListener("cancel",event=>{event.preventDefault();closeModelDialog();});
$("#model-dialog").addEventListener("close",()=>{const password=$("#model-api-key");if(password)password.value="";});
$("#model-dialog").addEventListener("click",event=>{if(event.target===$("#model-dialog")){const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeModelDialog();}});
$("#close-dialog").addEventListener("click",closeDialog);
$("#form-dialog").addEventListener("cancel",event=>{if(recipientDialogToken&&state?.pending_recipient){event.preventDefault();closeDialog();}});
$("#form-dialog").addEventListener("click",event=>{if(event.target===$("#form-dialog")){const rect=event.target.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeDialog();}});
$("#help-button").addEventListener("click",guide);$("#guide-button").addEventListener("click",guide);
$("#auth-button").addEventListener("click",enhanceAuth);
$("#reset-button").addEventListener("click",()=>{const body=dialog("开始新一轮演示","当前虚构业务数据、计划与对话会重置，余额恢复样例初始值。本地审计日志会保留。"),row=el("div","form-footer");append(row,button("继续当前演示",closeDialog),button("确认重新开始",()=>{closeDialog();send("",{reset:true});},"primary-button"));body.append(row);});
setupNavigation();setBusy(true);
loadState().catch(error=>{toast(error.message);$("#messages").append(el("p","loading","未能连接本地服务。请重新运行启动文件，并使用启动窗口中显示的新地址。"));});
