(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const baudRates=[4800,9600,14400,19200,38400,56000,57600,115200];
  const ruleFactors={full_mv:1000,raw_full:1};
  const channelsSpec=[['pump','Pump',['pump_port','pump','baud']],['pump2','Pump2',['pump2_port','pump2','pump2_baud']],['adc','Adc',['adc_port','adc','adc_baud']]];
  const states={DISCONNECTED:'未连接水泵',IDLE:'待机',STARTING:'启动中',RUN:'电压联动中',MANUAL:'手动运行',HOLD:'保持转速',STOPPING:'停止确认中',FAULT:'故障'};
  const faultText={VOLTAGE:'电压无效，保持最后转速。采集恢复后可点击“恢复联动”。',PUMP_COMM:'水泵通信异常，停止指令可能无法送达，请现场确认。',LEAK:'水泵报告漏液，请检查后清除故障。',STALL:'水泵报告堵转，请检查后清除故障。',UNEXPECTED_STOP:'水泵意外停止，请检查后清除故障。',DIRECTION_CHANGED:'水泵方向改变，请停泵检查。',EXTERNAL_RUN:'水泵已经在运行，尚未接管。请核对后停止，再清除故障。',SYSTEM_PAUSE:'电脑或后台曾暂停，请停泵检查后重新启动。',CONTROL_ERROR:'后台控制异常，请现场检查。'};
  faultText.PUMP_MISMATCH='两台泵的运行状态不一致，已向两台请求停止，请现场确认。';
  const errors={SELECT_PORTS:'请选择该设备的串口。',SAME_PORT:'两台设备不能使用同一个串口。',STOP_AND_WAIT:'请先停止水泵并等待停止确认。',DISCONNECT_FIRST:'请先断开对应设备再修改通信参数。',RPM_RANGE:'转速应在0.1～350rpm之间。',THRESHOLD:'阈值1必须小于阈值2，且两个阈值都在采集量程内。',HYSTERESIS:'阈值间隔过小，请至少间隔0.021V。',ADC_NOT_CONNECTED:'请先连接电压采集模块。',PUMP_NOT_CONNECTED:'请先连接水泵。',VOLTAGE_INVALID:'没有有效电压，请检查采集模块、接线和量程。',FAULT_NOT_CLEARED:'尚未确认停泵，或水泵报警未解除。',CANNOT_RESUME:'电压或泵状态尚未恢复，请检查设备。',MANUAL_ONLY:'当前不是手动运行状态。',LOCAL_SESSION_REQUIRED:'后台已重启，请刷新页面。'};
  Object.assign(errors,{THRESHOLD:'阈值须递增，且在0V与量程上限之间；最多32个。',RULE_LIST:'请为每个电压区间填写转速。',PUMPS_REQUIRED:'电压自动联动需要两台泵都连接正常。'});
  let data=null,token='',serverId='',lastContact=0,online=false,busy=false,dirty=true;
  const tell=(message,error=false)=>{$('notice').hidden=false;$('notice').textContent=message;$('notice').className=error?'error':'';};
  function option(id,value,label=value){const select=$(id);if(![...select.options].some(o=>o.value===String(value)))select.add(new Option(label,String(value)));}
  async function api(path,body){
    const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),6000);
    try{
      const response=await fetch(path,{cache:'no-store',signal:abort.signal,...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify(body)})});
      const result=await response.json();
      if(!response.ok)throw new Error(errors[result.error]||result.detail||result.error);
      return result;
    }catch(error){if(error.name==='AbortError')throw new Error('后台响应超时，执行结果未知，请核对状态。');throw error;}
    finally{clearTimeout(timer);}
  }
  function fill(config){
    for(const [key,factor] of Object.entries(ruleFactors))$(key).value=config[key]/factor;
    for(const key of [...channelsSpec.flatMap(([, ,keys])=>keys),'direction','direction2']){if($(key).tagName==='SELECT')option(key,config[key]);$(key).value=config[key];}
    drawRules((config.thresholds.length?config.thresholds:[config.t1,config.t2]).map(x=>x/1000),
      (config.thresholds.length?config.speeds:[config.r0,config.r1,config.r2]).map(x=>x/100));
    dirty=!config.speed_mode;
  }
  function draftRules(){
    const count=document.querySelectorAll('#ruleRows .threshold').length;
    return {thresholds:Array.from({length:count},(_,i)=>$('t'+(i+1)).value),speeds:Array.from({length:count+1},(_,i)=>$('r'+i).value)};
  }
  function drawRules(thresholds,speeds){
    $('ruleRows').replaceChildren();
    const count=thresholds.length;
    for(let i=0;i<=count;i++){
      const row=document.createElement('tr');
      const range=document.createElement('td');
      range.textContent=i===0?'低于阈值1':i===count?'达到阈值'+i:`阈值${i}至阈值${i+1}`;
      row.append(range);
      const thresholdCell=document.createElement('td');
      if(i===0)thresholdCell.textContent='—';
      else{const input=document.createElement('input');Object.assign(input,{id:'t'+i,type:'number',min:'0.001',step:'0.001',required:true,value:thresholds[i-1],className:'threshold'});input.setAttribute('aria-label','阈值'+i);thresholdCell.append(input);}
      row.append(thresholdCell);
      const speedCell=document.createElement('td'),speed=document.createElement('input');
      Object.assign(speed,{id:'r'+i,type:'number',min:'0.1',max:'350',step:'0.01',required:true,value:speeds[i]});speed.setAttribute('aria-label','第'+(i+1)+'档转速');speedCell.append(speed);row.append(speedCell);
      const removeCell=document.createElement('td');
      if(i>0){const button=document.createElement('button');Object.assign(button,{type:'button',className:'remove-rule',textContent:'×',title:'删除阈值'+i});button.setAttribute('aria-label',button.title);button.dataset.index=i-1;
        button.onclick=()=>{const draft=draftRules();draft.thresholds.splice(i-1,1);draft.speeds.splice(i,1);drawRules(draft.thresholds,draft.speeds);dirty=true;render();};removeCell.append(button);}
      row.append(removeCell);$('ruleRows').append(row);
    }
    $('thresholdCount').textContent=count+'个阈值 · '+(count+1)+'档转速';
  }
  async function scan(){
    const ports=(await api('/api/ports')).ports;
    for(const id of ['pump_port','pump2_port','adc_port']){const value=$(id).value;$(id).replaceChildren(new Option('请选择',''));for(const p of ports)option(id,p.device,p.device+' · '+p.description);if(value)option(id,value);$(id).value=value;}
    if(data?.simulation){for(const [id,port] of [['pump_port','SIM-PUMP'],['pump2_port','SIM-PUMP2'],['adc_port','SIM-BZY']])if(!$(id).value)$(id).value=port;}
  }
  async function refresh(){
    const next=await api('/api/state?since=2147483647');
    const changed=serverId&&serverId!==next.server_id;
    data=next;token=next.token;lastContact=Date.now();online=true;
    if(!serverId||changed){fill(data.config);if(changed)tell('后台已重启，请核对设备状态。',true);}
    serverId=next.server_id;
    render();
  }
  function render(){
    const ok=online&&Date.now()-lastContact<2500,t=data?.t,channels=data?.channels||{};
    const hasPump=!!(channels.pump||channels.pump2);
    const stopped=ok&&hasPump&&t.pump_valid&&!(t.status&13)&&t.state==='IDLE'&&!t.stop_required;
    const editable=ok&&['IDLE','DISCONNECTED'].includes(t.state)&&!busy;
    $('backendStatus').textContent=ok?'后台在线':'后台离线';
    $('simulationBanner').hidden=$('simulationPanel').hidden=!data?.simulation;
    for(const [channel,name,keys] of channelsSpec){
      const connected=ok&&channels[channel],connecting=data?.connecting?.includes(channel);
      const valid=channel==='adc'?t?.voltage_valid:t?.pumps[channel]?.valid;
      const status=t?.pumps[channel]?.status||0;
      $(channel+'Status').textContent=!ok?'状态未知':connecting?'连接中':connected?(valid?(channel!=='adc'?(status&12?'报警':status&1?'运行中':'待机'):'已连接'):'已连接 · 数据无效'):'未连接';
      $(channel+'Status').title=channel==='adc'?'':t?.pumps[channel]?.error||'';
      const connectable=ok&&!busy&&(['IDLE','DISCONNECTED','FAULT'].includes(t.state)||(t.state==='HOLD'&&t.fault==='EXTERNAL_RUN'));
      $('connect'+name).disabled=!ok||connected||connecting||busy||(channel!=='adc'&&!connectable);
      $('disconnect'+name).disabled=!connected||busy||(channel!=='adc'&&(!['IDLE','FAULT'].includes(t.state)||!valid||!!(status&1)||t.stop_required));
      for(const id of keys)$(id).disabled=!ok||connected||connecting||busy;
    }
    $('voltage').textContent=ok&&channels.adc&&t.voltage_valid?(t.mv/1000).toFixed(3)+' V':ok&&channels.adc?'无效':'—';
    for(const [key,id] of [['pump','rpm'],['pump2','rpm2']]){const p=t?.pumps[key];$(id).textContent=ok&&channels[key]&&p?.valid&&p.rpm_known?(p.rpm_x100/100).toFixed(2)+' rpm':'—';}
    $('state').textContent=ok?(states[t.state]||t.state):'状态未知';
    $('manual').textContent=t?.state==='MANUAL'?'更新转速':'启动水泵';
    $('manual').disabled=busy||!(stopped||(ok&&hasPump&&t.pump_valid&&t.state==='MANUAL'));
    $('manual').title=hasPump?'控制当前已连接的水泵':'请先连接至少一台水泵';
    $('stop').disabled=!ok||!(channels.pump||channels.pump2);
    $('reset').disabled=busy||!ok||t.state!=='FAULT'||!t.pump_valid||!!(t.status&13);
    $('direction').disabled=$('direction2').disabled=!editable;
    $('manualRpm').disabled=!ok||busy;
    $('auto').disabled=busy||dirty||!stopped||!channels.pump||!channels.pump2||!channels.adc||!t.voltage_valid;
    $('auto').title='需要正极泵、负极泵和BZY均连接正常';
    $('resume').disabled=busy||!ok||t.state!=='HOLD'||t.fault!=='VOLTAGE'||!t.voltage_valid||!t.pump_valid;
    $('saveRules').disabled=!editable;
    $('scan').disabled=!ok||busy;
    for(const key of Object.keys(ruleFactors))$(key).disabled=!editable;
    for(const input of document.querySelectorAll('#ruleRows input'))input.disabled=!editable;
    const count=document.querySelectorAll('#ruleRows .threshold').length;
    for(const button of document.querySelectorAll('.remove-rule'))button.disabled=!editable||count<=1;
    $('addThreshold').disabled=!editable||count>=32;
    $('ruleStatus').textContent=dirty?'规则未保存':data?.t.saved?'规则已保存':'规则已应用';
    if(ok&&t.fault!=='NONE')tell(faultText[t.fault]||t.fault,true);
  }
  async function run(fn){if(busy)return;busy=true;render();try{await fn();}catch(error){tell(error.message,true);}finally{busy=false;render();}}
  async function command(action,extra={}){await api('/api/command',{action,...extra});await refresh();}
  for(const [channel,name,keys] of channelsSpec){
    $('connect'+name).onclick=()=>run(async()=>{
      const settings=Object.fromEntries(keys.map(k=>[k,k.endsWith('_port')?$(k).value:Number($(k).value)]));
      await command('connect_channel',{channel,settings});tell(channel==='adc'?'采集端口已连接。':'水泵端口已连接。');
    });
    $('disconnect'+name).onclick=()=>run(async()=>{await command('disconnect_channel',{channel});tell(channel==='adc'?'电压采集已断开。':'水泵已断开。');});
  }
  $('manual').onclick=()=>run(async()=>{
    const rpm=Number($('manualRpm').value);
    if(!Number.isFinite(rpm)||rpm<0.1||rpm>350)throw new Error(errors.RPM_RANGE);
    const updating=data.t.state==='MANUAL';
    await command(updating?'set_speed':'manual',{rpm_x100:Math.round(rpm*100),direction:Number($('direction').value),direction2:Number($('direction2').value)});
    tell(updating?'转速修改请求已发送。':'启动请求已发送，等待运行确认。');
  });
  $('stop').onclick=async()=>{try{await command('stop');tell('停止请求已发送，等待停止确认。');}catch(error){tell(error.message,true);}};
  $('reset').onclick=()=>run(async()=>{await command('reset');tell('故障已清除。');});
  $('rules').onsubmit=e=>{e.preventDefault();run(async()=>{
    const config={...data.config,speed_mode:1,direction:Number($('direction').value),direction2:Number($('direction2').value)};
    for(const [key,factor] of Object.entries(ruleFactors)){
      const number=Number($(key).value)*factor;
      if($(key).value===''||!Number.isFinite(number)||Math.abs(number-Math.round(number))>0.00001)throw new Error('请核对输入数值。');
      config[key]=Math.round(number);
    }
    const draft=draftRules();
    const scaled=(values,factor)=>values.map(value=>{const number=Number(value)*factor;if(value===''||!Number.isFinite(number)||Math.abs(number-Math.round(number))>0.00001)throw new Error('电压最多三位小数，转速最多两位小数。');return Math.round(number);});
    config.thresholds=scaled(draft.thresholds,1000);config.speeds=scaled(draft.speeds,100);
    config.vmin=0;config.vmax=config.full_mv;
    await command('apply',{config});await command('save');dirty=false;render();tell('电压与转速规则已保存。');
  });};
  $('auto').onclick=()=>run(async()=>{await command('start');tell('电压联动已启动，等待水泵运行确认。');});
  $('resume').onclick=()=>run(async()=>{await command('resume');tell('电压联动已恢复。');});
  $('scan').onclick=()=>run(scan);
  $('simApply').onclick=()=>run(async()=>{await api('/api/sim',{voltage_mv:Number($('simValue').value),adc_online:$('simAdc').checked,pump_online:$('simPump').checked,pump2_online:$('simPump2').checked});tell('模拟输入已更新。');});
  $('addThreshold').onclick=()=>{const draft=draftRules(),last=Number(draft.thresholds.at(-1));const next=Math.round((last+0.1)*1000)/1000;if(!Number.isFinite(next)||next>=Number($('full_mv').value)){tell('请先调整末尾阈值，为新阈值留出量程。',true);return;}draft.thresholds.push(next);draft.speeds.push(draft.speeds.at(-1));drawRules(draft.thresholds,draft.speeds);dirty=true;render();};
  $('rules').addEventListener('input',()=>{dirty=true;render();});
  $('direction').addEventListener('change',()=>{dirty=true;render();});
  $('direction2').addEventListener('change',()=>{dirty=true;render();});
  for(const id of ['baud','pump2_baud','adc_baud'])for(const baud of baudRates)option(id,baud);
  async function poll(){try{const initial=!serverId;await refresh();if(initial)await scan();}catch(error){online=false;render();tell('后台连接失败：'+error.message,true);}setTimeout(poll,500);}
  poll();
})();
