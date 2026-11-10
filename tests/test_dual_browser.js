// Run after test_pc_browser.js against the isolated simulation server on 8782.
async page => {
  const assert=(value,message)=>{if(!value)throw new Error(message);};
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const state=async()=>(await page.request.get('http://127.0.0.1:8782/api/state')).json();
  const wait=async(predicate,message)=>{
    const end=Date.now()+15000;
    while(Date.now()<end){const s=await state();if(predicate(s))return s;await page.waitForTimeout(100);}
    throw new Error(message+': '+JSON.stringify((await state()).t));
  };
  const initial=await state();
  assert(initial.simulation&&!initial.connected,'requires disconnected simulation backend');
  const setup=await page.request.post('http://127.0.0.1:8782/api/command',{data:{action:'apply',config:{...initial.config,dual_pump:0,speed_mode:1,direction:0,direction2:0,thresholds:[1300,1560],speeds:[10000,6000,3000]}},headers:{'X-Control-Token':initial.token}});
  assert(setup.ok(),'dual test setup');
  await page.setViewportSize({width:1366,height:900});
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#backendStatus').textContent==='后台在线');
  assert(await page.locator('input[name="pumpMode"]').count()===0,'no mode selector');
  assert(await page.locator('#pump2Panel').isVisible(),'second pump always visible');
  await page.locator('#addThreshold').click();
  assert(await page.locator('#ruleRows .threshold').count()===3,'add threshold');
  for(const [id,value] of Object.entries({t1:'0.820',t2:'0.876',t3:'1.200',r0:'120',r1:'90',r2:'60',r3:'30'}))await page.locator('#'+id).fill(value);
  await page.locator('#direction2').selectOption('1');
  await page.locator('#saveRules').click();
  await wait(s=>JSON.stringify(s.config.thresholds)==='[820,876,1200]'&&s.config.speeds.length===4&&s.config.direction2===1&&s.t.saved,'three-decimal rules');
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#t3')?.value==='1.2');
  assert(await page.locator('#t2').inputValue()==='0.876','reload precision');
  await page.locator('#connectPump').click();
  await wait(s=>s.t.pumps.pump.valid,'pump1');
  await page.waitForFunction(()=>!document.querySelector('#manual').disabled);
  await page.locator('#manualRpm').fill('42');
  await page.locator('#manual').click();
  await wait(s=>s.t.state==='MANUAL'&&s.t.pumps.pump.rpm_x100===4200&&!s.channels.pump2,'pump1-only manual');
  assert(await page.locator('#connectPump2').isDisabled(),'stop before adding pump2');
  await page.locator('#stop').click();await wait(s=>s.t.state==='IDLE','pump1-only stop');
  await page.locator('#connectPump2').click();
  await wait(s=>s.t.pump_valid&&s.channels.pump2,'pump2');
  await page.locator('#manualRpm').fill('50');
  await page.locator('#manual').click();
  await wait(s=>s.t.state==='MANUAL'&&s.t.pumps.pump.rpm_x100===5000&&s.t.pumps.pump2.rpm_x100===5000,'dual manual');
  await page.waitForFunction(()=>document.querySelector('#rpm2').textContent==='50.00 rpm');
  assert(await page.locator('.remove-rule').first().isDisabled(),'cannot edit active rules');
  await page.locator('#manualRpm').fill('75.5');await page.locator('#manual').click();
  await wait(s=>s.t.pumps.pump.rpm_x100===7550&&s.t.pumps.pump2.rpm_x100===7550,'dual manual speed');
  await page.locator('#stop').click();await wait(s=>s.t.state==='IDLE','dual manual stop');
  await page.locator('#connectAdc').click();await wait(s=>s.t.voltage_valid,'ADC');
  await page.locator('#simulationPanel').evaluate(e=>e.open=true);
  const voltage=async value=>{await page.locator('#simValue').fill(String(value));await page.locator('#simApply').click();};
  await voltage(700);
  await page.waitForTimeout(2500);
  await page.locator('#auto').click();
  for(const [mv,stage,rpm] of [[700,0,12000],[850,1,9000],[1000,2,6000],[1300,3,3000]]){
    await voltage(mv);
    await wait(s=>s.t.state==='RUN'&&s.t.stage===stage&&s.t.pumps.pump.rpm_x100===rpm&&s.t.pumps.pump2.rpm_x100===rpm,'dual voltage stage '+stage);
  }
  await page.locator('#simulationPanel').evaluate(e=>e.open=false);
  await page.screenshot({path:'output/playwright/dual-desktop.png',fullPage:true});
  for(const width of [390,768]){
    await page.setViewportSize({width,height:844});
    await page.waitForTimeout(200);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'page overflow at '+width);
    assert(await page.evaluate(()=>![...document.querySelectorAll('button,input,select,h2,dd')].filter(e=>e.getClientRects().length).some(e=>e.scrollWidth>e.clientWidth+2)),'control overflow at '+width);
  }
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'output/playwright/dual-mobile.png',fullPage:true});
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#state').textContent==='电压联动中');
  assert((await state()).t.state==='RUN','reload retains group run');
  await page.locator('#disconnectAdc').click();await wait(s=>s.t.state==='HOLD','voltage hold');
  await page.locator('#connectAdc').click();await wait(s=>s.t.voltage_valid,'ADC reconnect');
  assert((await state()).t.state==='HOLD','no automatic resume');
  await page.locator('#resume').click();await wait(s=>s.t.state==='RUN','resume');
  await page.locator('#simulationPanel').evaluate(e=>e.open=true);
  await page.locator('#simPump2').uncheck();await page.locator('#simApply').click();
  await wait(s=>s.t.state==='FAULT'&&s.t.pumps.pump.valid&&!(s.t.pumps.pump.status&1)&&!s.t.pumps.pump2.valid,'peer stop on pump2 loss');
  assert(await page.locator('#reset').isDisabled(),'cannot reset while pump2 unknown');
  await page.locator('#simPump2').check();await page.locator('#simApply').click();
  await wait(s=>s.t.pump_valid&&!(s.t.status&1)&&!s.t.stop_required,'both stopped');
  await page.locator('#reset').click();await wait(s=>s.t.state==='IDLE','reset');
  await page.locator('#disconnectPump2').click();await page.locator('#disconnectPump').click();await page.locator('#disconnectAdc').click();
  await wait(s=>!s.connected,'disconnect');
  await page.locator('.remove-rule').last().click();
  assert(await page.locator('#ruleRows .threshold').count()===2,'remove threshold');
  await page.locator('.remove-rule').last().click();
  assert(await page.locator('#ruleRows .threshold').count()===1,'one threshold');
  assert(await page.locator('.remove-rule').first().isDisabled(),'minimum threshold count');
  await page.locator('#saveRules').click();
  await wait(s=>s.config.thresholds.length===1&&s.config.speeds.length===2&&s.t.saved,'save reduced stages');
  await page.locator('#connectPump2').click();
  await wait(s=>s.t.pumps.pump2.valid&&!s.channels.pump,'pump2-only connected');
  await page.locator('#connectAdc').click();await wait(s=>s.t.voltage_valid,'ADC with pump2 only');
  assert(await page.locator('#auto').isDisabled(),'automatic still requires both pumps');
  await page.locator('#manualRpm').fill('42.5');await page.locator('#manual').click();
  await wait(s=>s.t.state==='MANUAL'&&s.t.pumps.pump2.rpm_x100===4250&&!s.channels.pump,'pump2-only manual');
  await page.locator('#manualRpm').fill('65.25');await page.locator('#manual').click();
  await wait(s=>s.t.pumps.pump2.rpm_x100===6525,'pump2-only live speed');
  await page.locator('#stop').click();await wait(s=>s.t.state==='IDLE'&&!s.t.stop_required,'pump2-only stop');
  await page.locator('#disconnectPump2').click();await page.locator('#disconnectAdc').click();await wait(s=>!s.connected,'final disconnect');
  assert(errors.length===0,errors.join(','));
  return {result:'PASS',checks:['three-decimal thresholds','add/remove/save/reload rules','dual pump-only manual',
    'parallel RPM control','independent direction','four automatic stages','dual voltage HOLD/resume',
    'pump2 loss stops peer','fault recovery','390/768/1366 layout','no mode selector',
    'pump1-only manual','pump2-only manual/update/stop','automatic requires both'],errors};
}
